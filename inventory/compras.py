"""Pedidos a proveedores.

Un pedido abierto cuenta como unidades pedidas para la reposición, así la sugerencia no vuelve a pedir lo que ya
se pidió. Se cierra al recibir la factura que lo menciona en «orden de compra», o a mano.

El envío al proveedor es simulado: la plataforma prepara el texto (estado «mensaje preparado») y, al «enviar»,
registra un evento de auditoría de envío simulado (estado «envío simulado registrado»). No abre correo ni
mensajería ni se comunica con nadie fuera de este equipo.
"""
from collections import defaultdict
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone
from .models import Invoice, InvoiceLine, PurchaseOrder, PurchaseOrderLine
from .services import require, event, operation, MANAGERS, cop


def on_order(products=None):
    """Unidades pedidas que todavía no figuran en una factura, por referencia.

    Cuando la factura del proveedor ya está vinculada al pedido, sus unidades pendientes de recibir se cuentan
    como «en tránsito» (líneas de factura). Para que cada unidad pendiente cuente una sola vez en la posición de
    reposición, del pedido solo queda lo que la factura vinculada no cubre. Una factura anulada no cubre nada.
    """
    qs = PurchaseOrderLine.objects.filter(order__status__in=PurchaseOrder.OPEN)
    if products is not None:
        qs = qs.filter(product__in=products)
    rows = list(qs.values_list('order__invoice_id', 'product_id', 'quantity'))
    linked = {inv for inv, _, _ in rows if inv}
    invoiced = {}
    if linked:
        for r in (InvoiceLine.objects.filter(invoice_id__in=linked).exclude(invoice__review=Invoice.Review.VOID)
                  .values('invoice_id', 'product_id').annotate(u=Sum('quantity'))):
            invoiced[(r['invoice_id'], r['product_id'])] = r['u']
    out = defaultdict(int)
    for invoice_id, product_id, quantity in rows:
        left = quantity - invoiced.get((invoice_id, product_id), 0) if invoice_id else quantity
        if left > 0:
            out[product_id] += left
    return dict(out)


def _number():
    year = timezone.localdate().year
    return f'PED-{year}-{PurchaseOrder.objects.filter(number__startswith=f"PED-{year}-").count() + 1:04d}'


@transaction.atomic
def create_order(user, supplier, lines, note, token, source='manual', expected_on=None):
    """lines: [{'product', 'quantity', 'unit_cost'?, 'reason'?}]. Una referencia por línea."""
    require(user, MANAGERS)
    operation(user, token, 'purchase_order')
    rows = [(l['product'], int(l['quantity']), l.get('unit_cost'), (l.get('reason') or '')[:300]) for l in lines if int(l.get('quantity') or 0) > 0]
    if not rows:
        raise ValidationError('Indique al menos una referencia con cantidad.')
    if len({p.pk for p, *_ in rows}) != len(rows):
        raise ValidationError('Cada referencia va una sola vez en el pedido.')
    order = None
    for _ in range(5):
        try:
            with transaction.atomic():
                order = PurchaseOrder.objects.create(number=_number(), supplier=supplier, source=source, note=(note or '').strip()[:300],
                                                     expected_on=expected_on, created_by=user)
            break
        except IntegrityError:
            continue
    if order is None:
        raise ValidationError('No fue posible numerar el pedido. Intente de nuevo.')
    PurchaseOrderLine.objects.bulk_create([PurchaseOrderLine(order=order, product=p, quantity=q, unit_cost=c, reason=r) for p, q, c, r in rows])
    event(user, 'Pedido a proveedor preparado', f'po-{order.pk}', f'{order.number} · {supplier.name} · ' + ', '.join(f'{q} {p.name}' for p, q, _, _ in rows))
    return order


@transaction.atomic
def mark_sent(user, order_id, token, expected_on=None):
    """«Envía» el pedido: registra el envío simulado en la auditoría y deja el pedido como enviado."""
    require(user, MANAGERS)
    operation(user, token, 'purchase_order_sent')
    o = PurchaseOrder.objects.select_for_update().select_related('supplier').get(pk=order_id)
    if o.status != PurchaseOrder.Status.DRAFT:
        raise ValidationError('Solo se envían los pedidos con el mensaje preparado y sin enviar.')
    o.status, o.sent_by, o.sent_at = PurchaseOrder.Status.SENT, user, timezone.now()
    if expected_on:
        o.expected_on = expected_on
    o.save(update_fields=['status', 'sent_by', 'sent_at', 'expected_on'])
    to = o.supplier.email or 'sin correo registrado'
    event(user, 'Envío simulado de pedido al proveedor', f'po-{o.pk}', f'{o.number} · {o.supplier.name} · {to} · no salió de la plataforma')
    return o


@transaction.atomic
def close_order(user, order_id, token, received=True, note=''):
    require(user, MANAGERS)
    operation(user, token, 'purchase_order_close')
    o = PurchaseOrder.objects.select_for_update().get(pk=order_id)
    if not o.open:
        raise ValidationError('El pedido ya está cerrado.')
    if not received and not note.strip():
        raise ValidationError('Explique por qué se cancela el pedido.')
    o.status = PurchaseOrder.Status.RECEIVED if received else PurchaseOrder.Status.CANCELLED
    o.closed_by, o.closed_at, o.close_note = user, timezone.now(), note.strip()[:300]
    o.save(update_fields=['status', 'closed_by', 'closed_at', 'close_note'])
    event(user, 'Pedido recibido' if received else 'Pedido cancelado', f'po-{o.pk}', f'{o.number} {note}'.strip())
    return o


def link_invoice(invoice):
    """Vincula la factura al pedido abierto que menciona en «orden de compra» (mismo proveedor)."""
    number = (invoice.order_number or '').strip().upper()
    if not number:
        return None
    o = PurchaseOrder.objects.filter(number__iexact=number, supplier=invoice.supplier, status__in=PurchaseOrder.OPEN, invoice__isnull=True).first()
    if o:
        o.invoice = invoice
        o.save(update_fields=['invoice'])
    return o


def close_with_invoice(invoice, user):
    """Al completar la recepción de la factura, los pedidos vinculados pasan a recibidos."""
    now = timezone.now()
    for o in PurchaseOrder.objects.filter(invoice=invoice, status__in=PurchaseOrder.OPEN):
        o.status, o.closed_by, o.closed_at, o.close_note = PurchaseOrder.Status.RECEIVED, user, now, f'Recibido con la factura {invoice.number}'
        o.save(update_fields=['status', 'closed_by', 'closed_at', 'close_note'])


def order_text(order):
    """Texto preparado para el proveedor. Se muestra en pantalla; la plataforma no lo envía."""
    company = settings.COMPANY_NAME
    lines = list(order.lines.select_related('product'))
    body = ['Buenos días.', '', f'{company} solicita el siguiente pedido ({order.number}):']
    for l in lines:
        pres = f' ({l.product.presentation})' if l.product.presentation else ''
        body.append(f'• {l.product.qty(l.quantity)} de {l.product.name}{pres}')
    if order.expected_on:
        body.append(f'Fecha de entrega deseada: {order.expected_on:%d/%m/%Y}.')
    body += ['', 'Agradecemos confirmar disponibilidad, lotes, vencimientos, precio y fecha de entrega.', '', 'Cordialmente,', f'Inventario · {company}']
    return '\n'.join(body)


def delivery(order):
    """Estado del mensaje al proveedor: preparado o con envío simulado registrado."""
    if order.status == PurchaseOrder.Status.DRAFT:
        return {'state': 'prepared', 'label': 'Mensaje preparado',
                'detail': 'El texto está listo. Al registrar el envío queda un evento de envío simulado; no sale ningún correo.'}
    if order.sent_at:
        return {'state': 'simulated', 'label': 'Envío simulado registrado',
                'detail': f'Envío simulado registrado el {timezone.localtime(order.sent_at):%d/%m/%Y %H:%M}. No salió ningún mensaje de la plataforma.'}
    return {'state': 'none', 'label': 'Sin envío', 'detail': 'El pedido se cerró sin registrar un envío.'}


def describe(order):
    return ', '.join(f'{l.quantity} {l.product.name}' for l in order.lines.select_related('product')) + (f' · {cop(order.estimated_cost)} estimados' if order.estimated_cost else '')
