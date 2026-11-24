"""Pantallas de descuentos de venta y permisos de la dirección."""
import uuid
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from inventory import services as S
from inventory.models import User
from inventory.views import roles
from . import discounts as D
from .models import Sale, SaleDiscount, DiscountRequest


def _token(request):
    try:
        return uuid.UUID(request.POST.get('token', ''))
    except ValueError:
        raise ValidationError('La solicitud venció. Vuelva a intentarlo.')


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _run(request, action, fallback, success=None):
    try:
        result = action(_token(request))
    except ValidationError as e:
        messages.error(request, ' '.join(e.messages))
        return redirect(fallback)
    except (IntegrityError, OperationalError):
        messages.error(request, 'La operación cambió mientras se guardaba. Revise y vuelva a intentar.')
        return redirect(fallback)
    if success:
        messages.success(request, success(result) if callable(success) else success)
    return redirect(fallback)


@roles(*S.ADMINS)
def discounts(request):
    month = D.month_start()
    ctx = {'title': 'Descuentos y permisos', 'month': month, 'token': uuid.uuid4()}
    if request.user.role == 'owner':
        admins = User.objects.filter(role='admin', is_active=True).order_by('first_name', 'username')
        ctx.update({
            'owner_view': True,
            'quotas': [D.quota_status(u, month) for u in admins],
            'pending': DiscountRequest.objects.filter(status=DiscountRequest.Status.PENDING).select_related('requested_by', 'sale__party').order_by('created_at'),
            'decided': DiscountRequest.objects.exclude(status=DiscountRequest.Status.PENDING).select_related('requested_by', 'sale__party', 'decided_by')[:15],
            'granted': SaleDiscount.objects.filter(month=month).exclude(status=SaleDiscount.Status.REPLACED)
                                           .select_related('sale__party', 'applied_by', 'approved_by')[:40],
        })
    else:
        ctx.update({
            'quota': D.quota_status(request.user, month),
            'mine': DiscountRequest.objects.filter(requested_by=request.user).select_related('sale__party', 'decided_by')[:20],
            'granted': SaleDiscount.objects.filter(month=month, applied_by=request.user).exclude(status=SaleDiscount.Status.REPLACED)
                                           .select_related('sale__party', 'approved_by')[:40],
        })
    return render(request, 'cartera/discounts.html', ctx)


@roles(*S.ADMINS)
def sale_discount(request, pk):
    sale = get_object_or_404(Sale.objects.select_related('party', 'price_list'), pk=pk)
    if sale.status != Sale.Status.DRAFT:
        messages.info(request, 'Los descuentos se conceden sobre ventas en borrador, antes de enviarlas al sistema contable.')
        return redirect('sale', pk=pk)
    lines = list(sale.lines.select_related('product'))
    for line in lines:
        line.gross = line.quantity * line.unit_price
    error, values, percent, reason = None, {}, '', ''
    if request.method == 'POST':
        values = {str(l.pk): request.POST.get(f'linea-{l.pk}', '') for l in lines}
        percent = request.POST.get('porcentaje', '').strip()
        reason = request.POST.get('motivo', '').strip()
        try:
            outcome, obj = D.grant_discount(request.user, sale.pk, values, percent or None, reason, _token(request))
        except ValidationError as e:
            error = ' '.join(e.messages)
        except (IntegrityError, OperationalError):
            error = 'La venta cambió mientras se guardaba. Revise y vuelva a intentar.'
        else:
            if outcome == 'pending':
                messages.warning(request, f'No le quedan permisos este mes: el descuento de {S.cop(obj.amount)} quedó en espera de la confirmación de la dirección. '
                                          'La venta no se puede enviar al sistema contable hasta que decida.')
            elif request.user.role == 'owner':
                messages.success(request, f'Descuento de {S.cop(obj.amount)} concedido.')
            else:
                q = D.quota_status(request.user)
                used = 'usó 1 permiso' if obj.consumed else 'se ajustó con el mismo permiso'
                messages.success(request, f'Descuento de {S.cop(obj.amount)} concedido: {used}. Le quedan {q["available"]} de {q["total"]} este mes.')
            return redirect('sale', pk=pk)
    else:
        current = D.active_discount(sale)
        if current:
            values = {str(l['line']): l['discount'] for l in current.lines}
            reason = current.reason
    for line in lines:
        line.proposed = values.get(str(line.pk), '') or ''
    return render(request, 'cartera/sale_discount.html', {
        'sale': sale, 'lines': lines, 'gross_total': sum((l.gross for l in lines), 0), 'error': error, 'percent': percent, 'reason': reason, 'token': uuid.uuid4(),
        'quota': D.quota_status(request.user) if request.user.role == 'admin' else None, 'current': D.active_discount(sale),
        'already': SaleDiscount.objects.filter(sale=sale, applied_by=request.user, month=D.month_start(), consumed=True).exists(),
        'title': f'Descuento · {sale.label}'})


@roles(*S.ADMINS)
@require_POST
def sale_discount_remove(request, pk):
    return _run(request, lambda t: D.remove_discount(request.user, pk, t), reverse('sale', args=[pk]), 'Descuento retirado: la venta vuelve al precio de lista.')


@roles(*S.ADMINS)
@require_POST
def discount_withdraw(request, pk):
    req = get_object_or_404(DiscountRequest, pk=pk)
    back = reverse('sale', args=[req.sale_id]) if req.sale_id and request.POST.get('volver') == 'venta' else reverse('discounts')
    return _run(request, lambda t: D.withdraw_request(request.user, pk, t), back, 'Solicitud retirada.')


@roles(*S.OWNER)
@require_POST
def discount_decide(request, pk):
    approve = request.POST.get('decision') == 'aprobar'
    quantity = _int(request.POST.get('cantidad')) if request.POST.get('cantidad') else None
    note = request.POST.get('nota', '')
    return _run(request, lambda t: D.decide_request(request.user, pk, approve, note, t, quantity=quantity), reverse('discounts'),
                'Solicitud aprobada.' if approve else 'Solicitud rechazada.')


@roles(*S.OWNER)
@require_POST
def discount_quota(request, user_pk):
    quota = _int(request.POST.get('cupo'))
    return _run(request, lambda t: D.set_quota(request.user, user_pk, quota, t), reverse('discounts'),
                lambda u: f'Cupo de {u.display_name}: {u.discount_quota} permisos por mes.')


@roles(*S.OWNER)
@require_POST
def discount_extra(request, user_pk):
    quantity = _int(request.POST.get('cantidad'))
    return _run(request, lambda t: D.grant_extra(request.user, user_pk, quantity, request.POST.get('motivo', ''), t), reverse('discounts'),
                lambda g: f'{g.quantity} permisos adicionales para {g.user.display_name} este mes.')


@roles('admin')
@require_POST
def discount_permits(request):
    quantity = _int(request.POST.get('cantidad'))
    return _run(request, lambda t: D.request_permits(request.user, quantity, request.POST.get('motivo', ''), t), reverse('discounts'),
                'Solicitud enviada a la dirección.')
