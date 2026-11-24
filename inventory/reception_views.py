"""Recepción de mercancía con escáner (perfil Recepción, inventario, administración y dirección)."""
from datetime import date
from django.contrib import messages
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import IntegrityError, OperationalError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST
from . import recepcion as R, services as S
from .forms import TokenForm
from .models import Invoice, ReceivingSession, Batch
from .views import roles, locations


def _wants_json(request):
    return request.headers.get('X-Requested-With') == 'fetch'


def _date(value):
    try:
        return date.fromisoformat((value or '').strip())
    except ValueError:
        return None


@roles(*S.RECEIVERS)
def reception_home(request):
    today = timezone.localdate()
    pending = list(R.receivable_invoices())
    open_ids = dict(ReceivingSession.objects.filter(status='open').values_list('invoice_id', 'pk'))
    for inv in pending:
        inv.pending_units = inv.expected_units - inv.received_units
        inv.session_id = open_ids.get(inv.pk)
    waiting = Invoice.objects.filter(review=Invoice.Review.PENDING, reception_required=True).select_related('supplier').order_by('issue_date')[:8]
    done = ReceivingSession.objects.filter(status='confirmed').select_related('invoice__supplier', 'closed_by', 'receipt').order_by('-closed_at')[:10]
    return render(request, 'reception/home.html', {
        'title': 'Recibir mercancía', 'pending': pending, 'waiting': waiting, 'done': done, 'today': today,
        'k_pending': len(pending), 'k_units': sum(i.pending_units for i in pending),
        'k_today': ReceivingSession.objects.filter(status='confirmed', closed_at__date=today).count()})


@roles(*S.RECEIVERS)
@require_POST
def reception_find(request):
    invoice, info, message = R.find_invoice(request.POST.get('codigo', ''))
    if invoice is None:
        messages.error(request, message)
        return redirect('reception')
    if invoice.review != Invoice.Review.VALIDATED:
        messages.error(request, f'La factura {invoice.number} de {invoice.supplier.name} está registrada pero compras aún no la valida. '
                                'Avise a inventario o contabilidad para validarla.' if invoice.review == Invoice.Review.PENDING else 'La factura está anulada.')
        return redirect('reception')
    if invoice.received_units >= invoice.expected_units or not invoice.reception_required:
        messages.info(request, f'La mercancía de la factura {invoice.number} ya se recibió completa.')
        return redirect('reception')
    return redirect('reception_invoice', pk=invoice.pk)


def _session_context(session, request, result=None):
    data = R.summary(session)
    return {'session': session, 'invoice': session.invoice, 's': data, 'result': result, 'locations': locations(),
            'today': timezone.localdate(), 'token_form': TokenForm(), 'title': f'Recibir {session.invoice.number}'}


@roles(*S.RECEIVERS)
def reception_invoice(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related('supplier'), pk=pk)
    try:
        session = R.open_session(request.user, invoice)
    except ValidationError as e:
        messages.error(request, ' '.join(e.messages))
        return redirect('reception')
    return render(request, 'reception/session.html', _session_context(session, request))


def _respond(request, session, result):
    """JSON para el escáner (sin recargar) o redirección con mensaje si el navegador no ejecuta JavaScript."""
    session.refresh_from_db()
    ctx = _session_context(session, request, result)
    if _wants_json(request):
        return JsonResponse({'kind': result.get('kind'), 'ok': result.get('ok', False), 'warn': result.get('warn', False), 'message': result.get('message', ''),
                             'tally': render_to_string('reception/_tally.html', ctx, request=request),
                             'prompt': render_to_string('reception/_prompt.html', ctx, request=request),
                             'progress': render_to_string('reception/_progress.html', ctx, request=request)})
    level = messages.success if result.get('ok') else messages.warning if result.get('kind') in ('unknown', 'needs_details', 'duplicate', 'invoice') else messages.error
    level(request, result.get('message', ''))
    if result.get('kind') in ('unknown', 'needs_details'):
        return render(request, 'reception/session.html', ctx)
    return redirect('reception_invoice', pk=session.invoice_id)


def _guard(request, pk, action):
    session = get_object_or_404(ReceivingSession.objects.select_related('invoice__supplier'), pk=pk)
    try:
        result = action(session)
    except ValidationError as e:
        result = {'kind': 'error', 'ok': False, 'message': ' '.join(e.messages)}
    except (IntegrityError, OperationalError):
        result = {'kind': 'error', 'ok': False, 'message': 'La recepción cambió mientras se guardaba. Vuelva a escanear la caja.'}
    except PermissionDenied:
        raise
    return _respond(request, session, result)


@roles(*S.RECEIVERS)
@require_POST
def reception_scan(request, pk):
    p = request.POST
    return _guard(request, pk, lambda s: R.scan(request.user, s.pk, p.get('codigo', ''), p.get('cajas') or 1, p.get('lote', ''), _date(p.get('vence'))))


@roles(*S.RECEIVERS)
@require_POST
def reception_link(request, pk):
    p = request.POST

    def act(s):
        R.link_code(request.user, s.pk, p.get('gtin', ''), p.get('producto'), p.get('unidades') or 1)
        return R.scan(request.user, s.pk, p.get('codigo', ''), p.get('cajas') or 1)
    return _guard(request, pk, act)


@roles(*S.RECEIVERS)
@require_POST
def reception_manual(request, pk):
    p = request.POST
    return _guard(request, pk, lambda s: R.manual(request.user, s.pk, p.get('linea'), p.get('lote', ''), _date(p.get('vence')), p.get('cantidad') or 0,
                                                  p.get('gtin', ''), p.get('serial', '')))


@roles(*S.RECEIVERS)
@require_POST
def reception_remove(request, pk, scan_pk):
    def act(s):
        R.remove_scan(request.user, s.pk, scan_pk)
        return {'kind': 'removed', 'ok': True, 'message': 'Registro quitado de la recepción.'}
    return _guard(request, pk, act)


@roles(*S.RECEIVERS)
@require_POST
def reception_confirm(request, pk):
    session = get_object_or_404(ReceivingSession.objects.select_related('invoice__supplier'), pk=pk)
    form = TokenForm(request.POST)
    received_on = _date(request.POST.get('fecha')) or timezone.localdate()
    if not form.is_valid():
        messages.error(request, 'La solicitud venció. Vuelva a intentarlo.')
        return redirect('reception_invoice', pk=session.invoice_id)
    try:
        R.confirm(request.user, session.pk, received_on, request.POST.get('ubicacion', ''), request.POST.get('nota', ''), form.cleaned_data['token'])
    except ValidationError as e:
        messages.error(request, ' '.join(e.messages))
        return redirect('reception_invoice', pk=session.invoice_id)
    except (IntegrityError, OperationalError):
        messages.error(request, 'La recepción cambió mientras se guardaba. Revise y vuelva a confirmar.')
        return redirect('reception_invoice', pk=session.invoice_id)
    data = R.summary(session)
    messages.success(request, f'Recepción registrada: {data["counted"]} unidades de la factura {session.invoice.number} ({session.invoice.supplier.name}).')
    return redirect('reception')


@roles(*S.RECEIVERS)
@require_POST
def reception_discard(request, pk):
    session = get_object_or_404(ReceivingSession, pk=pk)
    try:
        R.discard(request.user, session.pk)
    except ValidationError as e:
        messages.error(request, ' '.join(e.messages))
        return redirect('reception_invoice', pk=session.invoice_id)
    messages.info(request, 'Recepción descartada. No se registró ninguna entrada.')
    return redirect('reception')
