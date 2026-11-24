import csv
import shutil
import time
import uuid
from datetime import timedelta, date
from decimal import Decimal
from functools import wraps
from pathlib import Path
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError, OperationalError
from django.db.models import F, Q, Count
from django.http import HttpResponse, FileResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.http import require_POST
from .models import (User, Product, Batch, SupplyRequest, Reservation, Supplier, SupplierCode, Invoice, InvoiceFile,
                     Movement, Audit, AlertRead, Settings, LoginAttempt, with_payable_balance)
from . import forms, services as S
from .einvoice import read_upload

DEFAULT_LOCATION = 'Bodega principal'


def roles(*allowed):
    def decorate(fn):
        @login_required
        @wraps(fn)
        def wrapped(request, *args, **kwargs):
            S.require(request.user, allowed)
            return fn(request, *args, **kwargs)
        return wrapped
    return decorate


def locations():
    return list(Batch.objects.order_by().values_list('location', flat=True).distinct()) or [DEFAULT_LOCATION]


def run_form(request, form, action, title, success, extra=None, template='form.html'):
    if request.method == 'POST' and form.is_valid():
        try:
            result = action(form.cleaned_data)
        except ValidationError as err:
            form.add_error(None, err)
        except IntegrityError:
            form.add_error(None, 'El registro ya existe o cambió durante la operación. Revise antes de repetir.')
        except OperationalError:
            form.add_error(None, 'La base de datos está ocupada. Espere un momento y vuelva a intentar; no se guardó la operación.')
        else:
            messages.success(request, (extra or {}).get('done', 'Operación guardada.'))
            return redirect(success(result) if callable(success) else success)
    return render(request, template, {'form': form, 'title': title, 'locations': locations(), **(extra or {})})


# ------------------------------------------------------------------ acceso

class SignIn(LoginView):
    template_name = 'login.html'
    authentication_form = forms.SignInForm
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        now = timezone.now()
        cutoff = now - timedelta(minutes=15)
        keys = [('account:' + request.POST.get('username', '').strip().casefold(), 5), ('ip:' + request.META.get('REMOTE_ADDR', 'unknown'), 30)]
        # Solo se guardan resúmenes HMAC, nunca usuarios intentados ni contraseñas.
        for raw, limit in keys:
            key = salted_hmac('login-throttle', raw).hexdigest()
            if LoginAttempt.objects.filter(key=key, created_at__gt=cutoff).count() >= limit:
                response = render(request, 'login.html', {'form': self.get_form(), 'limited': True}, status=429)
                response['Retry-After'] = '900'
                return response
        LoginAttempt.objects.filter(created_at__lt=cutoff).delete()
        for raw, _ in keys:
            LoginAttempt.objects.create(key=salted_hmac('login-throttle', raw).hexdigest())
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        self.request.session['user_version'] = self.request.user.session_version
        LoginAttempt.objects.filter(key=salted_hmac('login-throttle', 'account:' + form.cleaned_data['username'].strip().casefold()).hexdigest()).delete()
        S.event(self.request.user, 'Inicio de sesión')
        return response


def forgot_password(request):
    return render(request, 'forgot.html')


@login_required
@require_POST
def sign_out(request):
    S.event(request.user, 'Cierre de sesión')
    logout(request)
    return redirect('login')


class ChangePassword(PasswordChangeView):
    template_name = 'password_change.html'
    form_class = forms.ChangePasswordForm
    success_url = reverse_lazy('home')

    def form_valid(self, form):
        response = super().form_valid(form)
        u = self.request.user
        u.must_change_password = False
        u.session_version += 1
        u.save(update_fields=['must_change_password', 'session_version'])
        self.request.session['user_version'] = u.session_version
        S.event(u, 'Contraseña cambiada')
        messages.success(self.request, 'Contraseña actualizada. Las demás sesiones abiertas se cerraron.')
        return response


# ------------------------------------------------------------------ inicio

def health(request):
    """Comprobación local: la aplicación responde y la base de datos está disponible."""
    from django.db import connection
    try:
        connection.ensure_connection()
    except Exception:
        return HttpResponse('base de datos no disponible', status=503, content_type='text/plain; charset=utf-8')
    return HttpResponse('ok', content_type='text/plain; charset=utf-8')


def home(request):
    """Resumen: una sola plantilla con bloques según el perfil. Los cálculos viven en `feria.resumen`.

    Sin sesión, la raíz lleva a la presentación pública del proyecto (desde allí se entra a la demostración)."""
    if not request.user.is_authenticated:
        return redirect('landing')
    from feria import resumen
    return render(request, 'home.html', resumen.context(request))


# --------------------------------------------------------------- inventario

@roles(*S.MANAGERS)
def stock(request):
    q, category = request.GET.get('q', '').strip(), _category(request)
    batches = Batch.objects.select_related('product').order_by('product__name', 'expiry')
    if request.GET.get('ver') != 'todos':
        batches = batches.filter(physical__gt=0)
        if q:
            batches = batches.filter(Q(product__name__icontains=q) | Q(product__code__icontains=q) | Q(lot__icontains=q) | Q(location__icontains=q))
    if category:
        batches = batches.filter(product__category=category)
    return render(request, 'stock.html', {'batches': batches, 'q': q, 'category': category, 'categories': Product.Category.choices,
                                          'expiry_days': Settings.current().expiry_days})


@roles(*S.MANAGERS)
def batch_trace(request, pk):
    batch = get_object_or_404(Batch.objects.select_related('product'), pk=pk)
    return render(request, 'trace.html', {
        'batch': batch,
        'movements': batch.movements.select_related('actor', 'invoice_line__invoice__supplier', 'reservation__request__requester').order_by('created_at'),
        'reservations': Reservation.objects.filter(batch=batch).select_related('created_by', 'request__requester').order_by('-created_at')})


def _category(request):
    """Categoría elegida en el filtro (`?categoria=`), o '' si no es válida."""
    value = request.GET.get('categoria', '')
    return value if value in Product.Category.values else ''


def _movement_filter(request):
    qs = Movement.objects.select_related('batch__product', 'actor', 'invoice_line__invoice__supplier').order_by('-created_at')
    f = {k: request.GET.get(k, '').strip() for k in ('producto', 'tipo', 'desde', 'hasta')}
    if f['producto']:
        qs = qs.filter(batch__product_id=f['producto'])
    if f['tipo']:
        qs = qs.filter(kind=f['tipo'])
    for key, lookup in (('desde', 'created_at__date__gte'), ('hasta', 'created_at__date__lte')):
        try:
            if f[key]:
                qs = qs.filter(**{lookup: date.fromisoformat(f[key])})
        except ValueError:
            f[key] = ''
    return qs, f


@roles(*S.MANAGERS)
def movements(request):
    qs, f = _movement_filter(request)
    page = Paginator(qs, 50).get_page(request.GET.get('pagina'))
    query = request.GET.copy()
    query.pop('pagina', None)
    return render(request, 'movements.html', {'page': page, 'filters': f, 'products': Product.objects.all(), 'kinds': Movement.Kind.choices, 'query': query.urlencode()})


def csv_response(filename, header, rows):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.write('﻿')
    writer = csv.writer(response, delimiter=';')
    writer.writerow(header)
    for row in rows:
        writer.writerow(['' if v is None else str(v).replace('.', ',') if isinstance(v, Decimal) else v for v in row])
    return response


@roles(*S.MANAGERS)
def movements_csv(request):
    qs, _ = _movement_filter(request)
    S.event(request.user, 'Exportación de movimientos')
    rows = ((timezone.localtime(m.created_at).strftime('%Y-%m-%d %H:%M'), m.get_kind_display(), m.batch.product.code, m.batch.product.name, m.batch.lot,
             m.batch.expiry.isoformat(), m.batch.location, m.quantity, m.reference, m.get_loss_reason_display() if m.loss_reason else '',
             m.invoice_line.invoice.supplier.name if m.invoice_line else '', m.note, m.actor.username) for m in qs.iterator())
    return csv_response(f'movimientos_{timezone.localdate():%Y%m%d}.csv',
                        ['Fecha', 'Tipo', 'Código', 'Producto', 'Lote', 'Vencimiento', 'Ubicación', 'Cantidad', 'Documento', 'Causa baja', 'Proveedor', 'Observación', 'Usuario'], rows)


@roles(*S.MANAGERS)
def adjust_in(request):
    form = forms.AdjustInForm(request.POST or None, initial={'location': locations()[0]})
    return run_form(request, form, lambda d: S.adjust_in(request.user, d['product'], d['lot'], d['expiry'], d['location'], d['quantity'], d['kind'], d['note'], d['token']),
                    'Entrada por saldo inicial o ajuste', 'stock', {'intro': 'Use el saldo inicial para cargar las existencias contadas al iniciar. Las compras se registran desde la factura del proveedor.', 'done': 'Entrada registrada.', 'back': 'stock', 'submit': 'Registrar entrada'})


@roles(*S.MANAGERS)
def adjust_out(request):
    form = forms.AdjustOutForm(request.POST or None, initial={'batch': request.GET.get('lote'), 'kind': request.GET.get('tipo', 'writeoff')})
    return run_form(request, form, lambda d: S.adjust_out(request.user, d['batch'].pk, d['quantity'], d['kind'], d['loss_reason'], d['note'], d['token']),
                    'Baja o ajuste negativo', 'stock', {'intro': 'Las bajas registran pérdidas por vencimiento, almacenamiento inadecuado, daño o derrame. Quedan en la trazabilidad del lote.', 'done': 'Baja o ajuste registrado en la trazabilidad del lote.', 'back': 'stock', 'submit': 'Registrar'})


@roles(*S.MANAGERS)
def exit_stock(request):
    form = forms.ExitForm(request.POST or None, initial={'batch': request.GET.get('lote')})
    return run_form(request, form, lambda d: S.exit_stock(request.user, d['batch'].pk, d['quantity'], d['reference'], d['note'], d['token']),
                    'Registrar salida', 'stock', {'intro': 'Ventas o despachos que no provienen de una solicitud interna.', 'done': 'Salida registrada.', 'back': 'stock', 'submit': 'Registrar salida'})


@roles(*S.MANAGERS)
def reserve(request):
    form = forms.ReserveForm(request.POST or None, initial={'batch': request.GET.get('lote')})
    return run_form(request, form, lambda d: S.reserve(request.user, d['batch'].pk, d['quantity'], d['note'], d['token']),
                    'Crear reserva', 'reservations', {'intro': 'La reserva aparta unidades sin descontar el físico hasta confirmar la entrega.', 'done': 'Reserva creada.', 'back': 'reservations', 'submit': 'Crear reserva'})


# ---------------------------------------------------------------- productos

@roles(*S.DOCUMENTS)
def products(request):
    category = _category(request)
    rows = S.stock_position() if request.user.role in S.MANAGERS else None
    catalog = Product.objects.all()
    if category:
        catalog = catalog.filter(category=category)
        rows = [p for p in rows if p.category == category] if rows is not None else None
    return render(request, 'products.html', {'rows': rows, 'products': catalog, 'category': category, 'categories': Product.Category.choices})


@roles(*S.MANAGERS)
def product_form(request, pk=None):
    product = get_object_or_404(Product, pk=pk) if pk else None
    form = forms.ProductForm(request.POST or None, instance=product)

    def save(d):
        with transaction.atomic():
            S.operation(request.user, d['token'], 'product')
            p = form.save()
            S.event(request.user, 'Producto actualizado' if product else 'Producto creado', p.pk, p.name)

    return run_form(request, form, save, f'Editar {product.name}' if product else 'Nuevo producto', 'products', {'done': 'Producto guardado.', 'back': 'products'})


@roles(*S.MANAGERS)
def product_codes(request, pk):
    """Códigos de barras de la referencia (se registran en la recepción); inventario puede quitar uno errado."""
    from .models import ProductBarcode
    product = get_object_or_404(Product, pk=pk)
    if request.method == 'POST':
        form = forms.TokenForm(request.POST)
        code = ProductBarcode.objects.filter(pk=request.POST.get('codigo'), product=product).first()
        if form.is_valid() and code:
            with transaction.atomic():
                S.operation(request.user, form.cleaned_data['token'], 'barcode_remove')
                S.event(request.user, 'Código de barras retirado', product.pk, f'{code.gtin} · {product.name}')
                code.delete()
            messages.success(request, 'Código retirado. La próxima vez que se escanee se preguntará de nuevo a qué referencia corresponde.')
        return redirect('product_codes', pk=pk)
    return render(request, 'product_codes.html', {'product': product, 'codes': product.barcodes.select_related('created_by'), 'title': f'Códigos de {product.name}'})


# ------------------------------------------------------- solicitudes y reservas

@roles(*S.REQUESTERS)
def new_request(request):
    form = forms.RequestForm(request.POST or None, initial={'product': request.GET.get('product')})
    return run_form(request, form, lambda d: S.request_stock(request.user, d['product'], d['quantity'], d['purpose'], d['token'], d['needed_by']),
                    'Solicitar productos', 'requests', {'intro': 'Inventario revisa la disponibilidad, reserva el lote y confirma la entrega.', 'done': 'Solicitud enviada a inventario.', 'back': 'requests', 'submit': 'Enviar solicitud'})


@login_required
def request_list(request):
    qs = S.visible_requests(request.user)
    state = request.GET.get('estado', '')
    if state in dict(SupplyRequest.Status.choices):
        qs = qs.filter(status=state)
    return render(request, 'requests.html', {'requests': qs, 'state': state, 'states': SupplyRequest.Status.choices})


@roles(*S.MANAGERS)
def approve_request(request, pk):
    obj = get_object_or_404(SupplyRequest.objects.select_related('product', 'requester'), pk=pk)
    if obj.status != 'pending':
        messages.info(request, 'La solicitud ya fue atendida.')
        return redirect('requests')
    form = forms.ApproveForm(request.POST or None, supply_request=obj, suggested=S.suggested_batch(obj.product, obj.quantity))
    return run_form(request, form, lambda d: S.approve(request.user, pk, d['batch'].pk, d['token']), 'Aprobar y reservar', 'requests',
                    {'summary': obj, 'done': 'Solicitud aprobada y unidades reservadas.', 'back': 'requests'}, template='approve.html')


@login_required
def resolve_request(request, pk, action):
    obj = get_object_or_404(S.visible_requests(request.user), pk=pk)
    if action == 'reject':
        S.require(request.user, S.MANAGERS)
        form = forms.ReasonForm(request.POST or None)
    elif action == 'cancel':
        S.require(request.user, S.REQUESTERS)
        form = forms.TokenForm(request.POST or None)
    else:
        raise Http404
    return run_form(request, form, lambda d: S.resolve_request(request.user, obj.pk, action, d.get('reason', ''), d['token']),
                    'Rechazar solicitud' if action == 'reject' else 'Cancelar solicitud', 'requests',
                    {'intro': f'{obj.product.name} · {obj.product.qty(obj.quantity)} · {obj.purpose}', 'done': 'Solicitud actualizada.', 'back': 'requests',
                     'danger': True, 'submit': 'Rechazar' if action == 'reject' else 'Cancelar solicitud'})


@roles(*S.MANAGERS)
def reservations(request):
    show = request.GET.get('ver', 'activas')
    qs = Reservation.objects.select_related('batch__product', 'request__requester', 'created_by').order_by('-created_at')
    if show == 'activas':
        qs = qs.filter(status='active')
    return render(request, 'reservations.html', {'reservations': qs, 'show': show})


@roles(*S.MANAGERS)
def finish_reservation(request, pk, action):
    obj = get_object_or_404(Reservation.objects.select_related('batch__product', 'request__requester'), pk=pk)
    if action not in ('dispatch', 'release'):
        raise Http404
    return run_form(request, forms.TokenForm(request.POST or None), lambda d: S.finish_reservation(request.user, pk, action, d['token']),
                    'Confirmar entrega' if action == 'dispatch' else 'Liberar reserva', 'reservations',
                    {'intro': f'{obj.batch.product.qty(obj.quantity)} de {obj.batch.product.name} · lote {obj.batch.lot} · {obj.purpose}'
                              + (f' · {obj.request.requester.display_name}' if obj.request_id else ''),
                     'note': 'La entrega descuenta el físico del lote.' if action == 'dispatch' else 'Las unidades vuelven a quedar disponibles sin movimiento de salida.',
                     'done': 'Entrega confirmada.' if action == 'dispatch' else 'Reserva liberada.', 'back': 'reservations',
                     'submit': 'Confirmar entrega' if action == 'dispatch' else 'Liberar'})


# ---------------------------------------------------------------- facturas

def _invoice_queryset():
    return Invoice.objects.select_related('supplier').prefetch_related('lines')


@roles(*S.DOCUMENTS)
def invoices(request):
    tab, q = request.GET.get('vista', 'todas'), request.GET.get('q', '').strip()
    qs = _invoice_queryset().order_by('-issue_date', '-pk')
    counts = {
        'revisar': Invoice.objects.filter(review=Invoice.Review.PENDING).count(),
        'recibir': Invoice.objects.filter(review=Invoice.Review.VALIDATED, lines__received__lt=F('lines__quantity')).distinct().count(),
        'pagar': with_payable_balance(Invoice.objects.filter(review=Invoice.Review.VALIDATED)).filter(balance_due__gt=0).count(),
    }
    if tab == 'revisar':
        qs = qs.filter(review=Invoice.Review.PENDING)
    elif tab == 'recibir':
        qs = qs.filter(review=Invoice.Review.VALIDATED, lines__received__lt=F('lines__quantity')).distinct()
    elif tab == 'pagar':
        qs = qs.filter(pk__in=with_payable_balance(Invoice.objects.filter(review=Invoice.Review.VALIDATED)).filter(balance_due__gt=0).values('pk'))
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(supplier__name__icontains=q) | Q(supplier__nit__icontains=q) | Q(cufe__icontains=q))
    return render(request, 'invoices.html', {'invoices': qs, 'tab': tab, 'q': q, 'counts': counts})


@roles(*S.DOCUMENTS)
def invoices_csv(request):
    S.event(request.user, 'Exportación de facturas')
    rows = []
    for inv in _invoice_queryset().order_by('issue_date'):
        for l in inv.lines.all():
            rows.append((inv.supplier.name, inv.supplier.nit_display, inv.number, inv.issue_date, inv.due_date, inv.ordered_on, inv.order_number,
                         inv.get_review_display(), inv.reception_label, inv.payment_label, l.product.code, l.product.name, l.quantity, l.received,
                         l.unit_price, l.discount, l.tax_rate, l.line_total, inv.total, getattr(inv.last_payment, 'paid_on', ''),
                         inv.discount_taken or '', inv.balance))
    return csv_response(f'facturas_{timezone.localdate():%Y%m%d}.csv',
                        ['Proveedor', 'NIT', 'Factura', 'Emisión', 'Vencimiento', 'Fecha pedido', 'Orden de compra', 'Revisión', 'Recepción', 'Pago', 'Código',
                         'Producto', 'Cantidad', 'Recibido', 'Valor unitario', 'Descuento línea', 'IVA %', 'Total línea', 'Total factura', 'Último pago', 'Descuento pronto pago', 'Saldo'], rows)


IMPORT_DIR = 'importaciones'


def _import_dir(token):
    return Path(settings.PRIVATE_DIR) / IMPORT_DIR / token


def _clean_imports():
    root = Path(settings.PRIVATE_DIR) / IMPORT_DIR
    if root.exists():
        for d in root.iterdir():
            if d.is_dir() and time.time() - d.stat().st_mtime > 86400:
                shutil.rmtree(d, ignore_errors=True)


def _guess_product(supplier, line):
    code = line.get('supplier_code')
    if code:
        match = SupplierCode.objects.filter(supplier=supplier, code=code).first()
        if match:
            return match.product_id
    text = line.get('description', '').lower()
    for p in Product.objects.filter(active=True):
        if p.name.lower() in text or (p.code.lower() in text):
            return p.pk
    return None


@roles(*S.DOCUMENTS)
def invoice_import(request):
    form = forms.ImportForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        upload = form.cleaned_data['file']
        try:
            if upload.size > S.MAX_FILE:
                raise ValidationError('El archivo supera 20 MB.')
            data, files = read_upload(upload.name, upload.read())
            if data.get('currency') and data['currency'] != 'COP':
                raise ValidationError('Por ahora el sistema registra facturas en pesos colombianos (COP).')
            if data.get('source') == 'pdf':
                # Un PDF todavía no revisado no debe crear proveedores.
                supplier = None
                if data.get('supplier_nit'):
                    try:
                        nit = S.normalize_nit(data['supplier_nit'])
                    except ValidationError:
                        data.setdefault('warnings', []).append('Revise el NIT del proveedor; no se pudo validar su lectura.')
                    else:
                        supplier = Supplier.objects.filter(nit=nit).first()
            else:
                supplier = S.supplier_from_import(request.user, data['supplier_nit'], data['supplier_name'])
        except ValidationError as err:
            form.add_error(None, err)
        else:
            _clean_imports()
            token = uuid.uuid4().hex
            folder = _import_dir(token)
            folder.mkdir(parents=True)
            names = []
            for i, (name, raw) in enumerate(files):
                stored = f'{i:02d}_' + ''.join(c for c in name if c.isalnum() or c in '._-')[-80:]
                (folder / stored).write_bytes(raw)
                names.append([stored, name])
            data['supplier_id'] = supplier.pk if supplier else None
            for line in data['lines']:
                line['product'] = _guess_product(supplier, line)
            request.session['import:' + token] = {'data': data, 'files': names}
            S.event(request.user, 'Factura PDF leída' if data.get('source') == 'pdf' else 'Factura electrónica leída',
                    supplier.pk if supplier else '', f'{data.get("number", "")} · {data.get("supplier_name", "")}')
            return redirect(reverse('invoice_new') + '?importacion=' + token)
    return render(request, 'invoice_import.html', {'form': form})


@roles(*S.DOCUMENTS)
def invoice_import_file(request, token):
    """El soporte temporal solo es accesible desde la sesión que lo cargó."""
    imported = request.session.get('import:' + token.hex)
    if not imported:
        raise Http404
    for stored, original in imported['files']:
        if original.lower().endswith('.pdf'):
            path = _import_dir(token.hex) / stored
            if not path.is_file():
                raise Http404
            return FileResponse(path.open('rb'), content_type='application/pdf', filename=original)
    raise Http404


def _import_initial(data):
    header = {k: data.get(k) for k in ('number', 'cufe', 'issue_date', 'due_date', 'order_number', 'ordered_on', 'global_discount', 'retention')}
    header['supplier'] = data['supplier_id']
    if data.get('source') == 'pdf':
        header.update(supplier_name=data.get('supplier_name', ''), supplier_nit=data.get('supplier_nit', ''),
                      pdf_total=data.get('payable', ''), pdf_currency=data.get('currency', ''))
    lines = [{k: l.get(k) for k in ('product', 'supplier_code', 'description', 'quantity', 'unit_price', 'discount', 'tax_rate', 'expected_lot', 'expected_expiry')}
             for l in data['lines']]
    for l in lines:
        value = l['tax_rate']
        l['tax_rate'] = str(int(Decimal(value))) if value not in (None, '') and Decimal(value) in (0, 5, 19) else ''
    return header, lines


@roles(*S.DOCUMENTS)
def invoice_form(request, pk=None):
    invoice = get_object_or_404(_invoice_queryset(), pk=pk) if pk else None
    if invoice and invoice.review != Invoice.Review.PENDING:
        messages.info(request, 'La factura ya fue validada; los cambios se registran con nota crédito o anulación.')
        return redirect('invoice', pk=invoice.pk)
    token = request.GET.get('importacion', '')
    imported = request.session.get('import:' + token) if token else None
    if token:
        try:
            valid_token = uuid.UUID(hex=token).hex == token
        except ValueError:
            valid_token = False
        if not valid_token or not imported or invoice or any(not (_import_dir(token) / stored).is_file() for stored, _ in imported['files']):
            messages.error(request, 'La lectura venció o el soporte ya no está disponible. Cargue nuevamente la factura.')
            return redirect('invoice_import')
    is_pdf = bool(imported and imported['data'].get('source') == 'pdf')
    header_initial, line_initial = {}, []
    if imported:
        header_initial, line_initial = _import_initial(imported['data'])
    elif invoice:
        header_initial = {f: getattr(invoice, f) for f in ('number', 'issue_date', 'due_date', 'ordered_on', 'order_number', 'cufe', 'global_discount', 'retention', 'notes')}
        header_initial['supplier'] = invoice.supplier_id
        line_initial = [{'product': l.product_id, 'supplier_code': l.supplier_code, 'description': l.description, 'quantity': l.quantity, 'unit_price': l.unit_price,
                         'discount': l.discount, 'tax_rate': str(int(l.tax_rate)), 'expected_lot': l.expected_lot, 'expected_expiry': l.expected_expiry} for l in invoice.lines.all()]
    else:
        header_initial = {'supplier': request.GET.get('proveedor'), 'issue_date': timezone.localdate()}
    payment_terms, payment_terms_text = None, ''
    if is_pdf or (invoice and invoice.source == 'pdf'):
        if is_pdf:
            source_data = imported['data']
            term_initial = {'payment_days': source_data.get('payment_days', ''), 'discount_base': source_data.get('discount_base', '')}
            tiers = [[t['days'], t['percent']] for t in source_data.get('discount_tiers', [])]
            payment_terms_text = source_data.get('payment_terms_text', '')
        else:
            source_terms = invoice.document_terms or {}
            term_initial = {'payment_days': source_terms.get('payment_days', (invoice.due_date - invoice.issue_date).days),
                            'discount_base': source_terms.get('base', '')}
            tiers = source_terms.get('tiers', [])
            payment_terms_text = source_terms.get('printed_text', '')
        for i, (days, percent) in enumerate(tiers[:4], 1):
            term_initial.update({f'days_{i}': days, f'percent_{i}': percent})
        payment_terms = forms.InvoicePaymentTermsForm(request.POST or None, prefix='pago', initial=term_initial)
    Form = forms.PDFInvoiceForm if is_pdf else forms.InvoiceForm
    form = Form(request.POST or None, request.FILES or None, initial=header_initial)
    LineSet = forms.invoice_line_formset(extra=0 if line_initial else 1)
    lines = LineSet(request.POST or None, initial=line_initial or None, prefix='lineas')
    if request.method == 'POST' and form.is_valid() and lines.is_valid() and (payment_terms is None or payment_terms.is_valid()):
        files = forms.uploads(form.cleaned_data['files'])
        if imported:
            folder = _import_dir(token)
            files = [(original, (folder / stored).read_bytes()) for stored, original in imported['files']] + files
        try:
            if payment_terms:
                terms = payment_terms.cleaned_data['document_terms']
                terms['printed_text'] = payment_terms_text
                form.cleaned_data['document_terms'] = terms
            if is_pdf:
                subtotal, tax = Decimal(0), Decimal(0)
                for line in lines.lines():
                    net = S.money(line['quantity'] * line['unit_price'] - line['discount'])
                    subtotal += net
                    tax += S.money(net * line['tax_rate'] / 100)
                total = subtotal + tax - form.cleaned_data['global_discount'] - form.cleaned_data['retention']
                if total != form.cleaned_data['pdf_total']:
                    raise ValidationError('El total calculado no coincide con el total del PDF que confirmó. Revise cantidades, valores, descuentos, IVA y retenciones antes de guardar.')
            with transaction.atomic():
                if is_pdf and not form.cleaned_data.get('supplier'):
                    form.cleaned_data['supplier'] = S.supplier_from_import(request.user, form.cleaned_data['supplier_nit'], form.cleaned_data['supplier_name'])
                if invoice:
                    saved = S.update_invoice(request.user, invoice.pk, form.cleaned_data, lines.lines(), files, form.cleaned_data['token'])
                else:
                    source = imported['data'].get('source', 'xml') if imported else 'manual'
                    saved = S.create_invoice(request.user, form.cleaned_data, lines.lines(), files, form.cleaned_data['token'], source=source)
        except ValidationError as err:
            form.add_error(None, err)
        except (IntegrityError, OperationalError):
            form.add_error(None, 'No se guardó la factura. Revise los datos y vuelva a intentar.')
        else:
            if imported:
                request.session.pop('import:' + token, None)
                shutil.rmtree(_import_dir(token), ignore_errors=True)
            messages.success(request, f'Factura {saved.number} guardada. Revísela contra el soporte y valídela para continuar con la recepción.')
            return redirect('invoice', pk=saved.pk)
    return render(request, 'invoice_form.html', {
        'form': form, 'lines': lines, 'invoice': invoice, 'imported': imported, 'is_pdf': is_pdf, 'import_token': uuid.UUID(token) if token else '',
        'payment_terms': payment_terms, 'payment_terms_text': payment_terms_text, 'products': Product.objects.filter(active=True),
        'title': f'Corregir factura {invoice.number}' if invoice else 'Revisar factura importada' if imported else 'Registrar factura de proveedor'})


@roles(*S.DOCUMENTS)
def invoice_detail(request, pk):
    invoice = get_object_or_404(_invoice_queryset().select_related('created_by', 'validated_by'), pk=pk)
    receipts = invoice.receipts.select_related('actor').prefetch_related('movements__batch').order_by('received_on', 'pk')
    history = Audit.objects.filter(target=str(invoice.pk), event__in=['Factura registrada', 'Factura corregida', 'Factura validada', 'Factura anulada',
                                                                      'Recepción confirmada', 'Pago registrado', 'Soporte adjuntado']).select_related('actor').order_by('created_at')
    from .payables import settlement
    offer = settlement(invoice, timezone.localdate()) if invoice.review == Invoice.Review.VALIDATED and not invoice.paid else None
    allocations = invoice.allocations.select_related('payment').order_by('payment__paid_on', 'pk')
    return render(request, 'invoice_detail.html', {'invoice': invoice, 'receipts': receipts, 'history': history, 'offer': offer, 'allocations': allocations,
                                                   'credits': invoice.credit_applications.select_related('credit_note'), 'adjustments': invoice.adjustments.all(),
                                                   'attach_form': forms.AttachForm(), 'token_form': forms.TokenForm(), 'can_receive': request.user.role in S.MANAGERS,
                                                   'can_pay': request.user.role in S.FINANCE})


@roles(*S.DOCUMENTS)
@require_POST
def invoice_validate(request, pk):
    form = forms.TokenForm(request.POST)
    if form.is_valid():
        try:
            S.validate_invoice(request.user, pk, form.cleaned_data['token'])
        except ValidationError as err:
            messages.error(request, ' '.join(err.messages))
        else:
            messages.success(request, 'Factura validada. Quedó lista para recepción y en cuentas por pagar.')
    else:
        messages.error(request, 'No se pudo validar la factura. Recargue la página y vuelva a intentar.')
    return redirect('invoice', pk=pk)


@roles(*S.DOCUMENTS)
def invoice_void(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    return run_form(request, forms.ReasonForm(request.POST or None), lambda d: S.void_invoice(request.user, pk, d['reason'], d['token']),
                    f'Anular factura {invoice.number}', reverse('invoice', args=[pk]),
                    {'intro': 'La anulación conserva el registro y sus soportes. Solo es posible sin recepciones ni pagos.', 'done': 'Factura anulada.',
                     'back_url': reverse('invoice', args=[pk]), 'danger': True, 'submit': 'Anular factura'})


@roles(*S.DOCUMENTS)
@require_POST
def invoice_attach(request, pk):
    form = forms.AttachForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            S.attach_files(request.user, pk, forms.uploads(form.cleaned_data['files']), form.cleaned_data['token'])
        except ValidationError as err:
            messages.error(request, ' '.join(err.messages))
        else:
            messages.success(request, 'Soporte adjuntado.')
    else:
        messages.error(request, 'Seleccione al menos un archivo.')
    return redirect('invoice', pk=pk)


@roles(*S.DOCUMENTS)
def invoice_file(request, pk):
    f = get_object_or_404(InvoiceFile, pk=pk)
    inline = f.content_type in ('application/pdf', 'image/png', 'image/jpeg')
    response = FileResponse(f.file.open('rb'), as_attachment=not inline, filename=f.name, content_type=f.content_type)
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@roles(*S.MANAGERS)
def invoice_receive(request, pk):
    invoice = get_object_or_404(_invoice_queryset(), pk=pk)
    if invoice.review != Invoice.Review.VALIDATED:
        messages.error(request, 'Valide la factura antes de recibir la mercancía.' if invoice.review == Invoice.Review.PENDING else 'La factura está anulada.')
        return redirect('invoice', pk=pk)
    pending = [l for l in invoice.lines.all() if l.pending]
    if not pending:
        messages.info(request, 'La mercancía de esta factura ya se recibió completa.')
        return redirect('invoice', pk=pk)
    choices = [(l.pk, f'{l.product.name} · faltan {l.pending}') for l in pending]
    place = locations()[0]
    initial = [{'line': l.pk, 'quantity': l.pending, 'lot': l.expected_lot, 'expiry': l.expected_expiry, 'location': place} for l in pending]
    form = forms.ReceiptForm(request.POST or None)
    rows = forms.ReceiptRowFormSet(request.POST or None, initial=initial, prefix='lotes', form_kwargs={'line_choices': choices})
    if request.method == 'POST' and form.is_valid() and rows.is_valid():
        try:
            S.receive_invoice(request.user, pk, rows.lines(), form.cleaned_data['received_on'], form.cleaned_data['note'], form.cleaned_data['token'])
        except ValidationError as err:
            form.add_error(None, err)
        except (IntegrityError, OperationalError):
            form.add_error(None, 'No se confirmó la recepción. Revise los datos y vuelva a intentar.')
        else:
            messages.success(request, 'Recepción confirmada. Las unidades ya están en inventario.')
            return redirect('invoice', pk=pk)
    return render(request, 'invoice_receive.html', {'invoice': invoice, 'pending': pending, 'form': form, 'rows': rows, 'locations': locations()})


@roles(*S.FINANCE)
def invoice_pay(request, pk):
    invoice = get_object_or_404(_invoice_queryset(), pk=pk)
    if invoice.review != Invoice.Review.VALIDATED or invoice.paid:
        messages.info(request, 'Esta factura no tiene pago pendiente.')
        return redirect('invoice', pk=pk)
    from .payables import settlement
    today = timezone.localdate()
    offer = settlement(invoice, today)
    schedule = [{'days': d, 'percent': p, 'until': invoice.issue_date + timedelta(days=d), 'amount': S.money(offer['base'] * p / 100),
                 'open': (invoice.issue_date + timedelta(days=d)) >= today} for d, p in offer['tiers']]
    return run_form(request, forms.PaymentForm(request.POST or None),
                    lambda d: S.pay_invoice(request.user, pk, d['paid_on'], d['method'], d['reference'], d['token']),
                    f'Registrar pago · {invoice.number}', reverse('invoice', args=[pk]),
                    {'invoice': invoice, 'offer': offer, 'schedule': schedule, 'done': 'Pago registrado.'}, template='invoice_pay.html')


# -------------------------------------------------------------- proveedores

@roles(*S.DOCUMENTS)
def suppliers(request):
    rows = Supplier.objects.annotate(n=Count('invoices', filter=~Q(invoices__review='void'))).prefetch_related('tiers')
    return render(request, 'suppliers.html', {'suppliers': rows})


@roles(*S.DOCUMENTS)
def supplier_form(request, pk=None):
    supplier = get_object_or_404(Supplier, pk=pk) if pk else None
    if supplier:
        S.require(request.user, S.FINANCE)
    initial = ({f: getattr(supplier, f) for f in ('name', 'email', 'phone', 'payment_days', 'discount_base', 'active')} | {'nit': supplier.nit_display}) if supplier else {}
    form = forms.SupplierForm(request.POST or None, initial=initial)
    tiers = forms.TierFormSet(request.POST or None, prefix='tramos', initial=[{'days': t.days, 'percent': t.percent} for t in supplier.tiers.order_by('days')] if supplier else None)
    if request.method == 'POST' and form.is_valid() and tiers.is_valid():
        data = [t.cleaned_data for t in tiers if t.cleaned_data.get('days')]
        try:
            saved = S.save_supplier(request.user, supplier, form.cleaned_data, data, form.cleaned_data['token'])
        except ValidationError as err:
            form.add_error(None, err)
        except IntegrityError:
            form.add_error(None, 'Ya existe un proveedor con este NIT.')
        else:
            messages.success(request, f'Proveedor {saved.name} guardado.')
            return redirect('suppliers')
    return render(request, 'supplier_form.html', {'form': form, 'tiers': tiers, 'supplier': supplier,
                                                  'title': f'Condiciones de {supplier.name}' if supplier else 'Nuevo proveedor'})


# ------------------------------------------------------------------ alertas

@login_required
def alert_list(request):
    from notificaciones.notifier import recent
    items = S.alerts(request.user)
    return render(request, 'alerts.html', {'alerts': items, 'unread': sum(1 for a in items if not a['read']), 'notices': recent(request.user)})


@login_required
@require_POST
def read_alert(request):
    items = S.alerts(request.user)
    keys = [a['key'] for a in items] if request.POST.get('key') == '__todas__' else [request.POST.get('key')]
    for item in items:
        if item['key'] in keys:
            AlertRead.objects.update_or_create(user=request.user, key=item['key'], defaults={'revision': item['revision']})
    return redirect('alerts')


@roles(*S.ADMINS, 'inventory', 'finance')
def settings_view(request):
    cfg = Settings.current()
    form = forms.SettingsForm(request.POST or None, initial={'expiry_days': cfg.expiry_days, 'discount_alert_days': cfg.discount_alert_days, 'due_alert_days': cfg.due_alert_days})

    def save(d):
        with transaction.atomic():
            S.operation(request.user, d['token'], 'settings')
            Settings.objects.filter(pk=cfg.pk).update(expiry_days=d['expiry_days'], discount_alert_days=d['discount_alert_days'], due_alert_days=d['due_alert_days'])
            S.event(request.user, 'Configuración de alertas actualizada')

    return run_form(request, form, save, 'Configuración de alertas', 'alerts',
                    {'intro': 'Los mínimos por referencia se definen en cada producto.', 'done': 'Configuración guardada.', 'back': 'alerts'})


# ----------------------------------------------------------- administración

def _manageable(actor, target):
    """La dirección administra todas las cuentas; la administración, las que no son de dirección ni de administración."""
    return actor.role == 'owner' or target.role not in S.ADMINS


@roles(*S.ADMINS)
def users(request):
    accounts = list(User.objects.order_by('-is_active', 'role', 'username'))
    for u in accounts:
        u.manageable = u.pk != request.user.pk and _manageable(request.user, u)
    return render(request, 'users.html', {'users': accounts})


@roles(*S.ADMINS)
def new_user(request):
    allowed = [r for r in User.Role.values if request.user.role == 'owner' or r not in S.ADMINS]
    form = forms.AccountForm(request.POST or None, allowed_roles=allowed)

    def save(d):
        with transaction.atomic():
            S.operation(request.user, d['token'], 'account')
            u = form.save(commit=False)
            u.must_change_password = True
            u.save()
            S.event(request.user, 'Cuenta creada', u.pk, u.role)

    return run_form(request, form, save, 'Crear cuenta', 'users',
                    {'intro': 'Entregue la contraseña inicial en persona. El sistema exige cambiarla en el primer ingreso.', 'done': 'Cuenta creada.', 'back': 'users', 'submit': 'Crear cuenta'})


@roles(*S.ADMINS)
def toggle_user(request, pk, action):
    u = get_object_or_404(User, pk=pk)
    if u.pk == request.user.pk or action not in ('deactivate', 'activate'):
        return HttpResponse('Operación no permitida sobre esta cuenta.', status=400)
    if not _manageable(request.user, u):
        raise PermissionDenied('Solo la dirección administra las cuentas de dirección y de administración.')

    def save(d):
        with transaction.atomic():
            S.operation(request.user, d['token'], action)
            User.objects.filter(pk=u.pk).update(is_active=action == 'activate', session_version=F('session_version') + 1)
            S.event(request.user, 'Cuenta desactivada' if action == 'deactivate' else 'Cuenta reactivada', u.pk)

    return run_form(request, forms.TokenForm(request.POST or None), save, ('Desactivar' if action == 'deactivate' else 'Reactivar') + f' cuenta de {u.display_name}', 'users',
                    {'intro': 'Se bloquea el acceso y se cierran sus sesiones. Su historial se conserva.' if action == 'deactivate' else 'La persona podrá ingresar con su contraseña actual.',
                     'done': 'Cuenta actualizada.', 'back': 'users', 'danger': action == 'deactivate', 'submit': 'Desactivar' if action == 'deactivate' else 'Reactivar'})


@roles(*S.ADMINS)
def reset_user_password(request, pk):
    u = get_object_or_404(User, pk=pk, is_active=True)
    if u.pk == request.user.pk:
        return redirect('password_change')
    if not _manageable(request.user, u):
        raise PermissionDenied('Solo la dirección administra las cuentas de dirección y de administración.')
    form = forms.ResetAccountForm(u, request.POST or None)

    def save(d):
        with transaction.atomic():
            S.operation(request.user, d['token'], 'reset_password')
            locked = User.objects.select_for_update().get(pk=u.pk)
            locked.set_password(d['new_password1'])
            locked.must_change_password = True
            locked.session_version += 1
            locked.save(update_fields=['password', 'must_change_password', 'session_version'])
            S.event(request.user, 'Contraseña temporal restablecida', u.pk)

    return run_form(request, form, save, f'Restablecer contraseña de {u.display_name}', 'users',
                    {'intro': 'Verifique la identidad de la persona y entregue la clave temporal en persona. Se cerrarán sus sesiones y deberá cambiarla al ingresar.',
                     'done': 'Contraseña temporal asignada.', 'back': 'users'})


@roles(*S.ADMINS)
def audit(request):
    page = Paginator(Audit.objects.select_related('actor').order_by('-created_at'), 50).get_page(request.GET.get('pagina'))
    return render(request, 'audit.html', {'page': page})
