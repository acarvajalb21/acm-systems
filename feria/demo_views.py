"""Vistas del escenario de demostración: cambio rápido de perfil y documentos de prueba."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from inventory import services as S
from . import documentos, perfiles
from .models import DemoState

CONTENT_TYPES = {'.pdf': 'application/pdf', '.xml': 'application/xml', '.csv': 'text/csv; charset=utf-8', '.html': 'text/html; charset=utf-8',
                 '.txt': 'text/plain; charset=utf-8'}
KIND_LABELS = {'factura_pdf': 'Factura de proveedor en PDF', 'factura_xml': 'Factura de proveedor en XML', 'extracto': 'Extracto bancario',
               'codigos': 'Códigos para la recepción'}


@require_POST
def demo_profile(request):
    """Inicia sesión como la cuenta de demostración del perfil elegido.

    Solo si el cambio rápido está activo (404 si no), la petición viene de este mismo equipo (403 si no) y la cuenta
    es una de las cuentas de demostración activas registradas al generar el escenario (404 si no). No cambia ningún
    permiso: desde ese momento la sesión es la de esa cuenta y cada vista verifica su rol como siempre.
    """
    if not settings.FERIA.get('profile_switch'):
        raise Http404
    if not perfiles.is_local(request):
        raise PermissionDenied('El cambio rápido de perfil solo funciona desde el equipo de la demostración.')
    user = perfiles.account_for(request.POST.get('perfil', ''))
    if user is None:
        raise Http404
    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    request.session['user_version'] = user.session_version
    S.event(user, 'Ingreso con perfil de demostración', f'user-{user.pk}', user.get_role_display())
    messages.info(request, f'Ahora está como {user.display_name} · {user.get_role_display()}.')
    target = request.POST.get('next', '')
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}) or target.startswith('/ingresar'):
        target = ''
    return redirect(target or 'home')


def _files():
    state = DemoState.current()
    folder = documentos.folder()
    out = []
    for f in (state.cases or {}).get('documentos', {}).get('files', []):
        path = folder / f['name']
        if path.is_file():
            out.append({**f, 'kind_label': KIND_LABELS.get(f['kind'], 'Documento'), 'size_kb': max(1, round(path.stat().st_size / 1024)), 'path': path})
    return state, out


@login_required
def demo_documents(request):
    """Documentos de prueba del escenario para descargarlos y cargarlos en la pantalla que corresponde."""
    state, files = _files()
    codes = (state.cases or {}).get('recepcion', {})
    targets = {'factura_pdf': ('invoice_import', 'Compras → Facturas → Importar'), 'factura_xml': ('invoice_import', 'Compras → Facturas → Importar'),
               'extracto': ('bank_import', 'Finanzas → Banco → Importar extracto'), 'codigos': ('reception', 'Operaciones → Recepción')}
    for f in files:
        f['target'], f['target_label'] = targets.get(f['kind'], ('home', 'Resumen'))
    return render(request, 'feria/documentos.html', {
        'title': 'Documentos de demostración', 'files': files, 'state': state, 'codes': codes.get('codes_plain', []),
        'reception_url': codes.get('url', ''), 'reception_invoice': codes.get('invoice_number', '')})


@login_required
def demo_document(request, name):
    """Entrega un documento de la lista del escenario (nunca una ruta arbitraria)."""
    _, files = _files()
    doc = next((f for f in files if f['name'] == name), None)
    if doc is None:
        raise Http404
    return FileResponse(doc['path'].open('rb'), as_attachment=True, filename=doc['name'],
                        content_type=CONTENT_TYPES.get(doc['path'].suffix.lower(), 'application/octet-stream'))
