"""Herramientas que ejecutan las propuestas aprobadas.

Contrato común: reciben a quien aprueba (sus permisos los vuelve a comprobar el servicio del módulo), el payload de
la propuesta y una clave de operación estable derivada de la propuesta, de modo que repetir la aprobación nunca
duplica el efecto. Cada herramienta recalcula con los datos del momento: si la base de la propuesta cambió,
lanza `Stale` y la propuesta pasa a «ya no aplica» sin tocar nada.
"""
from datetime import date, timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.utils import timezone
from inventory import services as S

ZERO = Decimal('0.00')


class Stale(Exception):
    """La situación cambió desde que el agente hizo la propuesta."""


def schedule_payment(user, payload, token, action=None):
    from inventory import payables as P
    from inventory.models import Invoice, Supplier, PaymentAllocation, SupplierPayment, with_payable_balance
    supplier = Supplier.objects.get(pk=payload['supplier_id'])
    today = timezone.localdate()
    when = max(today, date.fromisoformat(payload['scheduled_on']))
    busy = set(PaymentAllocation.objects.filter(payment__status=SupplierPayment.Status.SCHEDULED).values_list('invoice_id', flat=True))
    rows = []
    for inv in with_payable_balance(Invoice.objects.filter(pk__in=payload['invoices'], supplier=supplier, review=Invoice.Review.VALIDATED)):
        if inv.pk in busy or inv.balance <= 0:
            continue
        s = P.settlement(inv, when)
        rows.append({'invoice': inv, 'amount': s['due'], 'discount': s['discount']})
    if not rows:
        raise Stale('Las facturas ya se pagaron o ya tienen un pago programado.')
    if payload.get('reason') == 'discount' and not any(r['discount'] > 0 for r in rows):
        raise Stale('El descuento por pronto pago ya no está disponible.')
    p = P.schedule_payment(user, supplier, rows, when, 'transfer', 'Programado por el agente de pronto pago', token)
    return {'message': f'Pago programado para el {when:%d/%m/%Y} por {S.cop(p.amount)}.', 'supplier_payment': p.pk,
            'link': f'/cuentas-por-pagar/pago/{p.pk}/', 'link_label': 'Ver el pago programado'}


def apply_bank(user, payload, token, action=None):
    from cartera import services as C
    from cartera.models import BankTransaction, Party
    txn = BankTransaction.objects.get(pk=payload['txn_id'])
    if txn.status not in (BankTransaction.Status.PENDING, BankTransaction.Status.IDENTIFIED) or C.remaining(txn) <= 0:
        raise Stale('El ingreso ya fue identificado o aplicado.')
    party = Party.objects.get(pk=payload['party_id'])
    plan, _ = C.propose_allocation(txn, party)
    if not plan:
        raise Stale('El cliente ya no tiene facturas abiertas para este ingreso.')
    C.apply_transaction(user, txn.pk, party.pk, [(r.pk, a) for r, a, _ in plan], token)
    total = sum((a for _, a, _ in plan), ZERO)
    return {'message': f'Aplicado {S.cop(total)} a ' + ', '.join(r.document for r, _, _ in plan) + '. Recibo de caja enviado al sistema contable simulado.',
            'link': f'/banco/movimiento/{txn.pk}/', 'link_label': 'Ver el movimiento'}


def link_counter(user, payload, token, action=None):
    from cartera import services as C
    from cartera.models import BankTransaction
    txn = BankTransaction.objects.get(pk=payload['txn_id'])
    if txn.status != BankTransaction.Status.PENDING:
        raise Stale('El ingreso ya fue identificado.')
    C.link_counter(user, txn.pk, payload['collection_ids'], token)
    return {'message': 'Ingreso conciliado con el recaudo de contado.', 'link': f'/banco/movimiento/{txn.pk}/', 'link_label': 'Ver el movimiento'}


SIMULATED_CHANNELS = {'chat': 'mensajería', 'email': 'correo'}


def collection_reminder(user, payload, token, action=None):
    """Registra el recordatorio como envío simulado: el texto queda en pantalla y nada sale de la plataforma."""
    from cartera import services as C
    from cartera.models import Party
    from aprendizaje import cobranza as L
    S.require(user, S.FINANCE)
    S.operation(user, token, 'collection_reminder')
    party = Party.objects.get(pk=payload['party_id'])
    still = [r for r in C.open_receivables(party) if r.pk in payload['receivables']]
    if not still:
        raise Stale('El cliente ya pagó las facturas del recordatorio.')
    message = payload['message']
    channel, contact = payload.get('channel'), payload.get('contact', '')
    sent = channel in SIMULATED_CHANNELS and bool(contact)
    # Solo un envío registrado (aquí, simulado) se evalúa al cumplirse la ventana; un mensaje solo preparado no entra al aprendizaje.
    L.record(action, user, payload, sent=sent)
    S.event(user, 'Recordatorio de cobro preparado', f'party-{party.pk}', f'{party.name} · {len(still)} factura(s) · {channel or "sin canal"}')
    base = {'text': message, 'channel': channel or '', 'contact': contact, 'external': False, 'link': f'/terceros/{party.pk}/'}
    if sent:
        via = SIMULATED_CHANNELS[channel]
        S.event(user, 'Envío simulado de recordatorio de cobro', f'party-{party.pk}', f'{party.name} · por {via} a {contact} · no salió de la plataforma')
        return {**base, 'state': 'simulated', 'state_label': 'Envío simulado registrado', 'link_label': 'Ver el cliente',
                'message': f'Envío simulado registrado por {via} a {contact}. El mensaje no salió de la plataforma; el texto queda aquí para consulta.'}
    return {**base, 'state': 'prepared', 'state_label': 'Mensaje preparado', 'link_label': 'Registrar un contacto de cobranza',
            'message': ('Mensaje preparado. El cliente no tiene un contacto de correo o mensajería: regístrelo para simular el envío. '
                        'Sin envío registrado, este recordatorio no se evalúa.')}


def writeoff_expired(user, payload, token, action=None):
    from inventory.models import Batch
    b = Batch.objects.select_related('product').get(pk=payload['batch_id'])
    if not b.expired:
        raise Stale('El lote no está vencido.')
    free = b.physical - b.reserved
    if free <= 0:
        raise Stale('El lote ya no tiene unidades libres.')
    S.adjust_out(user, b.pk, free, 'writeoff', 'expiry', 'Baja por vencimiento propuesta por el agente de vencimientos', token)
    return {'message': f'Baja registrada: {b.product.qty(free)} de {b.product.name}, lote {b.lot}.', 'link': f'/inventario/lote/{b.pk}/', 'link_label': 'Ver el lote'}


def reserve_request(user, payload, token, action=None):
    from inventory.models import SupplyRequest, Batch
    r = SupplyRequest.objects.select_related('product', 'requester').get(pk=payload['request_id'])
    if r.status != 'pending':
        raise Stale('La solicitud ya fue atendida.')
    b = Batch.objects.get(pk=payload['batch_id'])
    if b.available < r.quantity:
        b = S.suggested_batch(r.product, r.quantity)
        if b is None:
            raise Stale('Ya no hay un lote vigente con disponibilidad suficiente.')
    S.approve(user, r.pk, b.pk, token)
    return {'message': f'Reservado el lote {b.lot} para {r.requester.display_name}. Falta registrar la entrega física.', 'link': '/reservas/', 'link_label': 'Ir a reservas y entregas'}


def purchase_order(user, payload, token, action=None):
    """Prepara el pedido al proveedor con las cantidades recalculadas hoy (no con las de la propuesta).

    Se recalcula todo: consumo, posición y presupuesto de compra disponible. Si ninguna referencia de la propuesta
    necesita ya pedido, o el presupuesto ya no alcanza, la propuesta queda «ya no aplica»."""
    from aprendizaje import reposicion as R
    from inventory import compras
    from inventory.models import Supplier
    supplier = Supplier.objects.get(pk=payload['supplier_id'])
    wanted = {int(pk) for pk in payload['products']}
    p = R.current_params()
    today = timezone.localdate()
    budget = R.budget_plan(R.plans(p, today), p['presupuesto'], R.committed_cost(today, p))
    ready = [row for row in budget['lines'] if row['plan'].product.pk in wanted]
    if not ready:
        if any(row['plan'].product.pk in wanted for row in budget['excluded']):
            raise Stale('El presupuesto de compra disponible ya no alcanza para estas referencias.')
        raise Stale('Las referencias ya no necesitan pedido: llegó mercancía o ya hay un pedido abierto.')
    lines = [{'product': row['plan'].product, 'quantity': row['quantity'], 'unit_cost': row['plan'].unit_cost,
              'reason': (row['plan'].summary + (' ' + row['note'] if row['note'] else ''))[:300]} for row in ready]
    order = compras.create_order(user, supplier, lines, 'Preparado por el agente de reposición', token, source='agent',
                                 expected_on=today + timedelta(days=ready[0]['plan'].lead_days))
    before = {int(k): v for k, v in (payload.get('quantities') or {}).items()}
    changed = [row for row in ready if before and before.get(row['plan'].product.pk) != row['quantity']]
    note = ' Las cantidades se recalcularon con los datos de hoy y difieren de las propuestas.' if changed else ''
    return {'message': f'Pedido {order.number} preparado: {compras.describe(order)}.{note} Revíselo y registre su envío simulado al proveedor.',
            'link': f'/compras/pedidos/{order.pk}/', 'link_label': 'Ver el pedido', 'recalculated': bool(changed)}


TOOLS = {
    'schedule_payment': schedule_payment, 'apply_bank': apply_bank, 'link_counter': link_counter,
    'collection_reminder': collection_reminder, 'writeoff_expired': writeoff_expired, 'reserve_request': reserve_request,
    'purchase_order': purchase_order,
}
