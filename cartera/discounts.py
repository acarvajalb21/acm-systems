"""Descuentos de venta con permisos de la dirección.

Reglas:
- Las ventas se registran a precio de lista; nadie de facturación o contabilidad digita descuentos.
- Solo la administración o la dirección conceden un descuento, sobre una venta en borrador.
- La dirección asigna a cada cuenta de administración N permisos por mes. Un permiso = una venta con descuento en el mes;
  ajustar el descuento de la misma venta en el mismo mes no gasta otro permiso.
- Sin permisos disponibles, el descuento queda en espera de la confirmación de la dirección y la venta no se puede
  enviar al sistema contable hasta que decida. La administración también puede pedir más permisos para el mes.
- La dirección concede descuentos sin consumir permisos.
"""
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone
from inventory.models import User, money
from inventory.services import ADMINS, OWNER, require, event, operation, cop
from .models import Sale, SaleDiscount, DiscountGrant, DiscountRequest

ZERO = Decimal('0.00')


def month_start(day=None):
    day = day or timezone.localdate()
    return day.replace(day=1)


def quota_status(user, month=None):
    """Permisos del mes de una cuenta de administración: base, adicionales, usados y disponibles."""
    month = month or month_start()
    extra = DiscountGrant.objects.filter(user=user, month=month).aggregate(s=Sum('quantity'))['s'] or 0
    used = SaleDiscount.objects.filter(applied_by=user, month=month, consumed=True).count()
    granted = SaleDiscount.objects.filter(applied_by=user, month=month).exclude(status=SaleDiscount.Status.REPLACED)
    total = user.discount_quota + extra
    return {'user': user, 'month': month, 'base': user.discount_quota, 'extra': extra, 'total': total, 'used': used,
            'available': max(0, total - used), 'over': max(0, used - total),
            'amount': granted.aggregate(s=Sum('amount'))['s'] or ZERO,
            'pending': DiscountRequest.objects.filter(requested_by=user, status=DiscountRequest.Status.PENDING).count(),
            'percent_used': min(100, round(used * 100 / total)) if total else (100 if used else 0)}


def active_discount(sale):
    return sale.discounts.filter(status=SaleDiscount.Status.ACTIVE).select_related('applied_by', 'approved_by').first()


def pending_request(sale):
    return sale.discount_requests.filter(status=DiscountRequest.Status.PENDING).select_related('requested_by').first()


def _parse(value):
    if value in (None, ''):
        return ZERO
    try:
        amount = money(Decimal(str(value).strip()))
    except (InvalidOperation, ValueError):
        raise ValidationError('Revise los valores del descuento: use números sin puntos de miles.')
    if amount < 0:
        raise ValidationError('El descuento no puede ser negativo.')
    return amount


def proposal(sale, values=None, percent=None):
    """Descuento por línea a partir de valores en pesos o de un porcentaje común (sin superar el valor de la línea)."""
    lines, total, gross = [], ZERO, ZERO
    for line in sale.lines.select_related('product'):
        line_gross = money(line.quantity * line.unit_price)
        if percent not in (None, ''):
            try:
                p = Decimal(str(percent).replace(',', '.'))
            except InvalidOperation:
                raise ValidationError('El porcentaje no es válido.')
            if p < 0 or p > 100:
                raise ValidationError('El porcentaje debe estar entre 0 y 100.')
            amount = money(line_gross * p / 100)
        else:
            amount = _parse((values or {}).get(str(line.pk)))
        if amount > line_gross:
            raise ValidationError(f'El descuento de {line.product.name} supera el valor de la línea ({cop(line_gross)}).')
        lines.append({'line': line.pk, 'product': line.product.name, 'discount': str(amount), 'gross': str(line_gross)})
        total += amount
        gross += line_gross
    return lines, money(total), money(gross)


def _write(sale, lines):
    by_line = {int(l['line']): Decimal(l['discount']) for l in lines}
    current = list(sale.lines.all())
    if set(by_line) != {l.pk for l in current}:
        raise ValidationError('La venta cambió después de proponer el descuento. Vuelva a calcularlo.')
    for line in current:
        line.discount = by_line[line.pk]
        line.save(update_fields=['discount'])
    current = list(sale.lines.all())
    sale.subtotal = money(sum((l.base for l in current), ZERO))
    sale.tax = money(sum((l.tax_value for l in current), ZERO))
    sale.total = money(sale.subtotal + sale.tax)
    if sale.total <= 0:
        raise ValidationError('El total de la venta debe ser mayor que cero.')
    sale.save(update_fields=['subtotal', 'tax', 'total', 'updated_at'])


def _end_active(sale, status, note):
    SaleDiscount.objects.filter(sale=sale, status=SaleDiscount.Status.ACTIVE).update(status=status, ended_at=timezone.now(), end_note=note[:300])


def _apply(sale, lines, amount, gross, applied_by, reason, consumed, approved_by=None, req=None):
    _end_active(sale, SaleDiscount.Status.REPLACED, 'Reemplazado por un nuevo descuento')
    _write(sale, lines)
    return SaleDiscount.objects.create(sale=sale, applied_by=applied_by, approved_by=approved_by, request=req, month=month_start(),
                                       consumed=consumed, lines=lines, amount=amount, gross=gross, reason=reason)


def _draft(sale_id):
    sale = Sale.objects.select_for_update().select_related('party').get(pk=sale_id)
    if sale.status != Sale.Status.DRAFT:
        raise ValidationError('Los descuentos se conceden sobre ventas en borrador, antes de enviarlas al sistema contable.')
    return sale


@transaction.atomic
def grant_discount(user, sale_id, values, percent, reason, token):
    """Concede o deja en espera un descuento. Devuelve ('applied' | 'pending', objeto)."""
    require(user, ADMINS)
    operation(user, token, 'discount')
    sale = _draft(sale_id)
    reason = (reason or '').strip()
    if not reason:
        raise ValidationError('Escriba el motivo del descuento.')
    lines, amount, gross = proposal(sale, values, percent)
    if amount <= 0:
        raise ValidationError('Indique un descuento mayor que cero o use «Quitar descuento».')
    if pending_request(sale):
        raise ValidationError('Esta venta ya tiene un descuento en espera de la dirección. Retírelo antes de proponer otro.')
    detail = f'{sale.party.name} · {cop(amount)} sobre {cop(gross)}'
    if user.role == 'owner':
        d = _apply(sale, lines, amount, gross, user, reason, consumed=False, approved_by=user)
        event(user, 'Descuento concedido por la dirección', f'sale-{sale.pk}', detail)
        return 'applied', d
    month = month_start()
    already = SaleDiscount.objects.filter(sale=sale, applied_by=user, month=month, consumed=True).exists()
    # Bloquea las filas de la cuenta para que dos descuentos simultáneos no gasten el mismo permiso.
    User.objects.select_for_update().get(pk=user.pk)
    if already or quota_status(user, month)['available'] > 0:
        d = _apply(sale, lines, amount, gross, user, reason, consumed=not already)
        event(user, 'Descuento concedido con permiso del mes' if not already else 'Descuento ajustado (mismo permiso)', f'sale-{sale.pk}', detail)
        return 'applied', d
    req = DiscountRequest.objects.create(kind=DiscountRequest.Kind.SALE, requested_by=user, month=month, sale=sale, lines=lines,
                                         amount=amount, gross=gross, reason=reason)
    event(user, 'Descuento enviado a la dirección (sin permisos del mes)', f'sale-{sale.pk}', detail)
    return 'pending', req


@transaction.atomic
def remove_discount(user, sale_id, token):
    require(user, ADMINS)
    operation(user, token, 'discount_remove')
    sale = _draft(sale_id)
    if not active_discount(sale) and not sale.lines.filter(discount__gt=0).exists():
        raise ValidationError('La venta no tiene descuento.')
    _end_active(sale, SaleDiscount.Status.REMOVED, f'Retirado por {user.display_name}')
    _write(sale, [{'line': l.pk, 'discount': '0'} for l in sale.lines.all()])
    event(user, 'Descuento retirado', f'sale-{sale.pk}', sale.party.name)
    return sale


def on_sale_changed(user, sale, note):
    """Al modificar o cancelar una venta, el descuento y las solicitudes pierden su base (el permiso ya gastado no se devuelve)."""
    if SaleDiscount.objects.filter(sale=sale, status=SaleDiscount.Status.ACTIVE).exists():
        _end_active(sale, SaleDiscount.Status.REMOVED, note)
        event(user, 'Descuento retirado', f'sale-{sale.pk}', note)
    n = DiscountRequest.objects.filter(sale=sale, status=DiscountRequest.Status.PENDING).update(
        status=DiscountRequest.Status.VOID, decided_at=timezone.now(), decision_note=note[:300])
    if n:
        event(user, 'Solicitud de descuento sin efecto', f'sale-{sale.pk}', note)


def check_before_issue(sale):
    if DiscountRequest.objects.filter(sale=sale, status=DiscountRequest.Status.PENDING).exists():
        raise ValidationError('Esta venta tiene un descuento esperando la confirmación de la dirección. Espere su decisión o retire la solicitud.')
    discounted = sum((l.discount for l in sale.lines.all()), ZERO)
    current = active_discount(sale)
    if discounted > 0 and (current is None or money(current.amount) != money(discounted)):
        raise ValidationError('La venta tiene descuentos sin autorización registrada. Pida a la administración que los conceda o retírelos.')


@transaction.atomic
def request_permits(user, quantity, reason, token):
    require(user, ('admin',))
    operation(user, token, 'discount_permits')
    if not quantity or quantity < 1 or quantity > 200:
        raise ValidationError('Indique cuántos permisos necesita (entre 1 y 200).')
    if not (reason or '').strip():
        raise ValidationError('Explique para qué necesita más permisos.')
    if DiscountRequest.objects.filter(requested_by=user, kind=DiscountRequest.Kind.QUOTA, status=DiscountRequest.Status.PENDING).exists():
        raise ValidationError('Ya tiene una solicitud de permisos en espera de la dirección.')
    req = DiscountRequest.objects.create(kind=DiscountRequest.Kind.QUOTA, requested_by=user, month=month_start(), quantity=quantity, reason=reason.strip())
    event(user, 'Permisos de descuento solicitados a la dirección', f'discount-request-{req.pk}', f'{quantity} permisos · {reason.strip()}'[:300])
    return req


@transaction.atomic
def withdraw_request(user, request_id, token):
    operation(user, token, 'discount_withdraw')
    req = DiscountRequest.objects.select_for_update().get(pk=request_id)
    if req.requested_by_id != user.pk and user.role != 'owner':
        raise PermissionDenied('Solo quien la pidió puede retirar la solicitud.')
    if req.status != DiscountRequest.Status.PENDING:
        raise ValidationError('La solicitud ya fue decidida.')
    req.status, req.decided_at, req.decision_note = DiscountRequest.Status.WITHDRAWN, timezone.now(), f'Retirada por {user.display_name}'
    req.save(update_fields=['status', 'decided_at', 'decision_note'])
    event(user, 'Solicitud de descuento retirada', f'discount-request-{req.pk}', req.get_kind_display())
    return req


@transaction.atomic
def decide_request(owner, request_id, approve, note, token, quantity=None):
    require(owner, OWNER)
    operation(owner, token, 'discount_decide')
    req = DiscountRequest.objects.select_for_update().select_related('requested_by', 'sale').get(pk=request_id)
    if req.status != DiscountRequest.Status.PENDING:
        raise ValidationError('La solicitud ya fue decidida o quedó sin efecto.')
    note = (note or '').strip()
    req.decided_by, req.decided_at, req.decision_note = owner, timezone.now(), note
    if not approve:
        req.status = DiscountRequest.Status.REJECTED
        req.save()
        event(owner, 'Solicitud de descuento rechazada', f'discount-request-{req.pk}', f'{req.requested_by.display_name} · {note}'[:300])
        return req
    if req.kind == DiscountRequest.Kind.SALE:
        sale = _draft(req.sale_id)
        _apply(sale, req.lines, req.amount, req.gross, req.requested_by, req.reason, consumed=False, approved_by=owner, req=req)
        req.status = DiscountRequest.Status.APPROVED
        req.save()
        event(owner, 'Descuento confirmado por la dirección', f'sale-{sale.pk}', f'{sale.party.name} · {cop(req.amount)} · pedido por {req.requested_by.display_name}')
        return req
    granted = quantity if quantity is not None else req.quantity
    if granted < 1 or granted > 200:
        raise ValidationError('Conceda entre 1 y 200 permisos.')
    DiscountGrant.objects.create(user=req.requested_by, month=month_start(), quantity=granted, reason=note or req.reason, granted_by=owner)
    req.status, req.granted = DiscountRequest.Status.APPROVED, granted
    req.save()
    event(owner, 'Permisos de descuento concedidos', f'user-{req.requested_by_id}', f'{granted} permisos para {req.requested_by.display_name}')
    return req


@transaction.atomic
def set_quota(owner, user_id, quota, token):
    require(owner, OWNER)
    operation(owner, token, 'discount_quota')
    target = User.objects.select_for_update().get(pk=user_id)
    if target.role != 'admin':
        raise ValidationError('Los permisos de descuento se asignan a cuentas de administración.')
    if quota is None or quota < 0 or quota > 500:
        raise ValidationError('El cupo mensual debe estar entre 0 y 500.')
    before = target.discount_quota
    target.discount_quota = quota
    target.save(update_fields=['discount_quota'])
    event(owner, 'Cupo mensual de descuentos actualizado', f'user-{target.pk}', f'{target.display_name}: {before} → {quota} por mes')
    return target


@transaction.atomic
def grant_extra(owner, user_id, quantity, reason, token):
    require(owner, OWNER)
    operation(owner, token, 'discount_extra')
    target = User.objects.get(pk=user_id)
    if target.role != 'admin':
        raise ValidationError('Los permisos de descuento se asignan a cuentas de administración.')
    if not quantity or quantity < 1 or quantity > 200:
        raise ValidationError('Conceda entre 1 y 200 permisos.')
    g = DiscountGrant.objects.create(user=target, month=month_start(), quantity=quantity, reason=(reason or '').strip(), granted_by=owner)
    event(owner, 'Permisos de descuento concedidos', f'user-{target.pk}', f'{quantity} adicionales para {target.display_name}')
    return g


def alerts(user):
    """(clave, nivel, título, detalle, enlace) para la dirección y la administración."""
    items = []
    url = reverse('discounts')
    if user.role == 'owner':
        for r in DiscountRequest.objects.filter(status=DiscountRequest.Status.PENDING).select_related('requested_by', 'sale__party'):
            if r.kind == DiscountRequest.Kind.SALE:
                items.append((f'discount-req-{r.pk}', 'warning', 'Descuento por confirmar',
                              f'{r.requested_by.display_name} · {r.sale.party.name} · {cop(r.amount)} ({r.percent} %) · {r.reason}'[:300], url))
            else:
                items.append((f'discount-req-{r.pk}', 'warning', 'Solicitud de más permisos de descuento',
                              f'{r.requested_by.display_name} pide {r.quantity} · {r.reason}'[:300], url))
    elif user.role == 'admin':
        since = timezone.now() - timedelta(days=7)
        for r in DiscountRequest.objects.filter(requested_by=user, status__in=(DiscountRequest.Status.APPROVED, DiscountRequest.Status.REJECTED), decided_at__gte=since):
            what = r.sale.party.name if r.sale_id else f'{r.quantity} permisos'
            items.append((f'discount-dec-{r.pk}', 'info', 'La dirección ' + ('aprobó' if r.status == 'approved' else 'rechazó') + ' su solicitud',
                          f'{r.get_kind_display()} · {what}' + (f' · {r.decision_note}' if r.decision_note else ''), url))
        q = quota_status(user)
        if q['total'] and q['available'] <= 2:
            items.append((f'discount-quota-{q["month"]:%Y%m}-{q["available"]}', 'warning' if not q['available'] else 'info',
                          'Sin permisos de descuento este mes' if not q['available'] else f'Le quedan {q["available"]} permisos de descuento',
                          f'Usados {q["used"]} de {q["total"]} en {q["month"]:%m/%Y}.', url))
    return items
