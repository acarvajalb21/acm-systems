"""Definición de los agentes de la plataforma.

Cada agente lee los datos actuales y devuelve propuestas con su razón. No escribe en los módulos del negocio:
las propuestas se ejecutan en `tools.py`, al aprobarlas, con la identidad de quien aprueba. Cada ejecución
recalcula todas las propuestas abiertas del agente, de modo que las que perdieron su base quedan «ya no aplica».
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone
from inventory import services as S
from inventory.services import cop, FINANCE, MANAGERS, ADMINS


def q(n, one, many):
    """«1 factura vencida» / «3 facturas vencidas»: la cantidad con su sustantivo concordado."""
    return f'{n} {one if n == 1 else many}'

ZERO = Decimal('0.00')
CHANNELS = {'chat': 'mensajería', 'email': 'correo', 'phone': 'teléfono'}


@dataclass
class Param:
    key: str
    label: str
    default: int
    minimum: int
    maximum: int
    help: str = ''


@dataclass
class Proposal:
    kind: str
    key: str
    title: str
    detail: str
    payload: dict
    amount: Decimal = None
    benefit: Decimal = None
    url: str = ''
    confidence: str = ''


@dataclass
class Result:
    proposals: list = field(default_factory=list)
    summary: str = ''
    metrics: dict = field(default_factory=dict)


@dataclass
class Agent:
    key: str
    name: str
    icon: str
    area: str
    purpose: str
    schedule: str            # 'every:<minutos>' o 'daily:<HH:MM>'
    approve_roles: tuple
    view_roles: tuple
    run: callable
    params: list = field(default_factory=list)
    automatic: bool = False  # actúa sin propuestas (efectos idempotentes y reversibles por diseño)

    @property
    def schedule_label(self):
        kind, value = self.schedule.split(':', 1)
        if kind == 'every':
            minutes = int(value)
            return f'Cada {minutes // 60} hora{"s" if minutes >= 120 else ""}' if minutes % 60 == 0 else f'Cada {minutes} minutos'
        return f'Todos los días a las {value}'


# ------------------------------------------------------------------ pronto pago
def run_pronto_pago(p, today):
    from inventory.payables import settlement
    horizon = today + timedelta(days=p['horizonte_dias'])
    groups, overdue = defaultdict(list), defaultdict(list)
    candidates = 0
    for inv in S.payables():
        if inv.scheduled_on:
            continue
        offer = inv.offer
        if offer['discount'] > 0 and offer['deadline'] and offer['deadline'] <= horizon:
            when = max(today, offer['deadline'] - timedelta(days=p['margen_dias']))
            s = settlement(inv, when)
            if s['discount'] > 0:
                groups[(inv.supplier, when)].append((inv, s))
                candidates += 1
        elif inv.days_to_due < 0:
            overdue[inv.supplier].append(inv)
    proposals = []
    for (supplier, when), items in sorted(groups.items(), key=lambda kv: kv[0][1]):
        ids = sorted(inv.pk for inv, _ in items)
        saving = sum((s['discount'] for _, s in items), ZERO)
        pay = sum((s['due'] for _, s in items), ZERO)
        deadline = min(s['deadline'] for _, s in items)
        docs = '; '.join(f"{inv.number}: {S.pct(s['percent'])} % hasta el {s['deadline']:%d/%m}" for inv, s in items)
        proposals.append(Proposal(
            kind='schedule_payment', key=f'pago:{supplier.pk}:{when:%Y%m%d}:{"-".join(map(str, ids))}',
            title=f'Programar pago a {supplier.name} el {when:%d/%m/%Y} y ahorrar {cop(saving)}',
            detail=(f'El descuento por pronto pago vence el {deadline:%d/%m/%Y}. Se programa {p["margen_dias"]} {"día" if p["margen_dias"] == 1 else "días"} antes para tener margen. '
                    f'Facturas: {docs}. A pagar: {cop(pay)}.'),
            payload={'supplier_id': supplier.pk, 'scheduled_on': when.isoformat(), 'invoices': ids, 'reason': 'discount'},
            amount=pay, benefit=saving, url=reverse('supplier_account', args=[supplier.pk]), confidence='Alta'))
    for supplier, invs in overdue.items():
        ids = sorted(inv.pk for inv in invs)
        total = sum((inv.offer['due'] for inv in invs), ZERO)
        worst = max(-inv.days_to_due for inv in invs)
        proposals.append(Proposal(
            kind='schedule_payment', key=f'vencidas:{supplier.pk}:{"-".join(map(str, ids))}',
            title=f'Programar hoy las facturas vencidas de {supplier.name} ({cop(total)})',
            detail=f'{q(len(invs), "factura vencida", "facturas vencidas")}, la más antigua hace {worst} días: ' + ', '.join(inv.number for inv in invs) + '. Ya no tienen descuento.',
            payload={'supplier_id': supplier.pk, 'scheduled_on': today.isoformat(), 'invoices': ids, 'reason': 'overdue'},
            amount=total, url=reverse('supplier_account', args=[supplier.pk]), confidence='Alta'))
    saving = sum((pr.benefit or ZERO for pr in proposals), ZERO)
    return Result(proposals, f'{q(len(groups), "pago", "pagos")} para ahorrar {cop(saving)} en pronto pago y {q(len(overdue), "proveedor", "proveedores")} con facturas vencidas.',
                  {'ahorro_posible': str(saving), 'facturas_con_descuento': candidates, 'proveedores_vencidos': len(overdue)})


# --------------------------------------------------------------- conciliación
def run_conciliacion(p, today):
    from cartera import services as C
    from cartera.models import BankTransaction
    proposals, ambiguous, unknown = [], 0, 0
    for txn in BankTransaction.objects.filter(status=BankTransaction.Status.PENDING, amount__gt=0).select_related('payer').order_by('date', 'pk'):
        hint = C.suggestions(txn)
        who = txn.holder_name or txn.description or 'Ingreso'
        if hint['decision'] == 'suggested':
            cand = hint['candidates'][0]
            plan, excess = C.propose_allocation(txn, cand['party'])
            if not plan:
                continue
            applied = sum((a for _, a, _ in plan), ZERO)
            docs = ', '.join(f'{r.document} {cop(a)}' for r, a, _ in plan)
            extra = f' Quedan {cop(excess)} sin aplicar como saldo del movimiento.' if excess > 0 else ''
            proposals.append(Proposal(
                kind='apply_bank', key=f'banco:{txn.pk}:{cand["party"].pk}',
                title=f'Aplicar {cop(applied)} de «{who}» a {cand["party"].name}',
                detail=' '.join(cand['reasons']) + f' Reparto: {docs}.{extra} Se registra el recibo de caja en el sistema contable simulado.',
                payload={'txn_id': txn.pk, 'party_id': cand['party'].pk}, amount=txn.amount, benefit=applied,
                url=reverse('bank_txn', args=[txn.pk]), confidence=cand['label']))
            continue
        exact = [c for c in hint['counter'] if c.amount == txn.amount]
        if len(exact) == 1:
            col = exact[0]
            proposals.append(Proposal(
                kind='link_counter', key=f'contado:{txn.pk}:{col.pk}',
                title=f'Conciliar «{who}» con el recaudo de contado de {col.party.name}',
                detail=f'El ingreso de {cop(txn.amount)} del {txn.date:%d/%m} coincide con el recaudo de contado del {col.date:%d/%m} ({col.get_method_display()}, {col.reference or "sin referencia"}).',
                payload={'txn_id': txn.pk, 'collection_ids': [col.pk]}, amount=txn.amount, benefit=txn.amount,
                url=reverse('bank_txn', args=[txn.pk]), confidence='Alta'))
        elif hint['decision'] == 'ambiguous':
            ambiguous += 1
        else:
            unknown += 1
    ready = sum((pr.benefit or ZERO for pr in proposals), ZERO)
    return Result(proposals, f'{q(len(proposals), "ingreso listo", "ingresos listos")} para aplicar ({cop(ready)}); {q(ambiguous, "ambiguo", "ambiguos")} y {unknown} sin coincidencias quedan para contabilidad.',
                  {'listo_para_aplicar': str(ready), 'ambiguos': ambiguous, 'sin_coincidencia': unknown})


# -------------------------------------------------------------------- cobranza
def _contact(party):
    c = (party.contacts.filter(role='collection', preferred=True).first() or party.contacts.filter(role='collection').exclude(channel='phone').first()
         or party.contacts.exclude(channel='phone').first())
    if c:
        return c.channel, c.value, c.name
    branch = party.branches.filter(office=0).first()
    if branch and branch.einvoice_email:
        return 'email', branch.einvoice_email, party.name
    return '', '', party.name


def run_cobranza(p, today):
    from cartera import services as C
    from aprendizaje import cobranza as L, pagos
    from .models import AgentAction
    closed = L.evaluate(today)   # primero cierra los recordatorios enviados cuya ventana ya se cumplió
    recent = set(AgentAction.objects.filter(kind='collection_reminder', status='executed', decided_at__date__gte=today - timedelta(days=p['repetir_cada_dias']))
                 .values_list('payload__party_id', flat=True)) | L.waiting_parties(today)
    behaviour, table = pagos.behaviour(), L.counts()
    by_party = defaultdict(list)
    for r in C.open_receivables():
        late = (today - r.due_date).days
        if late >= max(p['dias_minimos'], 1):
            by_party[r.party].append((r, late))
    proposals, skipped, without, not_yet = [], 0, 0, 0
    for party, docs in sorted(by_party.items(), key=lambda kv: -sum(r.balance for r, _ in kv[1])):
        if party.pk in recent:
            skipped += 1
            continue
        oldest = max(l for _, l in docs)
        plan = L.choose(party, behaviour.get(party.pk), today, table)
        if oldest < max(plan['timing'], p['dias_minimos']):
            not_yet += 1   # la estrategia elegida para este tipo de cliente espera más días de mora
            continue
        channel, value, name = _contact(party)
        total = sum((r.balance for r, _ in docs), ZERO)
        message = L.message(party, docs, plan['tone'], name, today)
        if not channel:
            without += 1
        proposals.append(Proposal(
            kind='collection_reminder', key=f'cobro:{party.pk}:{today:%G%V}',
            title=f'Recordar a {party.name} el pago de {cop(total)} (tono {L.TONES[plan["tone"]].lower()})',
            detail=(f'{q(len(docs), "factura vencida", "facturas vencidas")}, la más antigua hace {oldest} días. Estrategia: tono {L.TONES[plan["tone"]].lower()} desde {plan["timing"]} días de mora. '
                    f'{plan["evidence"]} ' + (f'Canal: {CHANNELS.get(channel, channel)} {value}.' if channel else 'Sin contacto de cobranza registrado: agréguelo en Clientes.')),
            payload={'party_id': party.pk, 'receivables': [r.pk for r, _ in docs], 'channel': channel, 'contact': value, 'message': message, 'amount': str(total),
                     'strategy': {'segment': plan['segment'], 'tone': plan['tone'], 'timing': plan['timing'], 'evidence': plan['evidence']}},
            amount=total, url=reverse('party', args=[party.pk]), confidence='Alta' if plan['successes'] + plan['failures'] >= 3 or oldest > 30 else 'Media'))
    total = sum((pr.amount for pr in proposals), ZERO)
    learned = sum(closed.values())
    return Result(proposals, (f'{q(len(proposals), "recordatorio", "recordatorios")} de cobro por {cop(total)}; {q(skipped, "cliente", "clientes")} con recordatorio reciente; {not_yet} esperan el momento '
                              f'de su estrategia' + (f'; {q(learned, "recordatorio anterior evaluado", "recordatorios anteriores evaluados")} al cumplirse su ventana' if learned else '') + '.'),
                  {'por_recordar': str(total), 'ya_recordados': skipped, 'sin_contacto': without, 'esperan_estrategia': not_yet, **{f'resultado_{k}': v for k, v in closed.items()}})


# ------------------------------------------------------------------ reposición
def run_reposicion(p, today):
    """Regla automática sobre un pronóstico estadístico: propone pedidos por proveedor dentro del presupuesto de compra."""
    from aprendizaje import reposicion as R
    plan_list = R.plans(p, today)
    completed = R.record_forecasts(plan_list, today)
    budget = R.budget_plan(plan_list, p.get('presupuesto', 0), R.committed_cost(today, p))
    groups, orphans = defaultdict(list), []
    for row in budget['lines']:
        (groups[row['plan'].supplier] if row['plan'].supplier else orphans).append(row)
    proposals = []
    for supplier, rows in sorted(groups.items(), key=lambda kv: kv[0].name):
        items = [row['plan'] for row in rows]
        ids = sorted(pl.product.pk for pl in items)
        cost = sum((row['cost'] or ZERO for row in rows), ZERO)
        urgent = [pl for pl in items if pl.status == 'pedir_ya']
        facts = '; '.join(f'{row["plan"].product.name}: {row["plan"].product.qty(row["quantity"])}'
                          + (f' (recortado de {row["suggested"]} por presupuesto)' if row['cut'] else '')
                          + (f' (se usan {row["plan"].evidence["typical_label"]} por semana y lo que hay alcanza {row["plan"].covers})'
                             if row['plan'].weeks and row['plan'].coverage_days is not None else ' (sin existencias)' if row['plan'].position == 0 else '') for row in rows)
        detail = f'{facts}. El proveedor tarda {items[0].lead_days} días en entregar.' + (' Algunas referencias tienen poco historial.' if any(not pl.enough for pl in items) else '')
        if not budget['unlimited']:
            detail += f' Presupuesto de compra disponible: {cop(budget["available"])}.'
        proposals.append(Proposal(
            kind='purchase_order', key=f'pedido:{supplier.pk}:{"-".join(map(str, ids))}',
            title=f'Preparar pedido a {supplier.name}: ' + ', '.join(f'{row["quantity"]} {row["plan"].product.name}' for row in rows),
            detail=detail[:600], payload={'supplier_id': supplier.pk, 'products': ids, 'quantities': {str(row['plan'].product.pk): row['quantity'] for row in rows}},
            amount=cost or None, url=reverse('replenishment'), confidence='Alta' if urgent and all(pl.enough for pl in items) else 'Media'))
    now_n = sum(1 for pl in plan_list if pl.status == 'pedir_ya')
    soon_n = sum(1 for pl in plan_list if pl.status == 'pedir_pronto')
    risk = sum(pl.at_risk for pl in plan_list)
    left_out = len({row['plan'].product.pk for row in budget['excluded']})
    acc = R.accuracy(today=today)
    text = f'{q(now_n, "referencia", "referencias")} por pedir ya y {soon_n} por pedir pronto, en {q(len(groups), "pedido sugerido", "pedidos sugeridos")}'
    text += f'; {len(orphans)} sin proveedor habitual' if orphans else ''
    text += f'; {q(left_out, "recortada", "recortadas")} o por fuera del presupuesto de {cop(budget["budget"])}' if left_out else ''
    text += f'; {risk} unidades en riesgo de vencer antes de usarse' if risk else ''
    if acc['n']:
        mae = f'{acc["mae"]:.1f}'.replace('.', ',')
        text += f'. Pronóstico semanal guardado: error medio de {mae} unidades en {q(acc["n"], "semana cerrada", "semanas cerradas")}'
        text += f'; {acc["inside"]} de {acc["interval_n"]} dentro del intervalo del 80 %.' if acc['interval_n'] else '.'
    else:
        text += '. El pronóstico semanal se evaluará cuando cierren las primeras semanas.'
    return Result(proposals, text, {'pedir_ya': now_n, 'pedir_pronto': soon_n, 'pedidos_sugeridos': len(groups), 'sin_proveedor': len(orphans),
                                    'riesgo_vencimiento': risk, 'semanas_evaluadas': acc['n'], 'dentro_intervalo': acc['inside'],
                                    'con_intervalo': acc['interval_n'], 'semanas_cerradas': completed, 'presupuesto': str(budget['budget']),
                                    'por_fuera_presupuesto': left_out})


# ---------------------------------------------------------------- vencimientos
def run_vencimientos(p, today):
    from inventory.models import Batch
    from cartera.results import Costs
    costs = Costs()
    proposals = []
    soon = Batch.objects.filter(physical__gt=0, expiry__gte=today, expiry__lte=today + timedelta(days=p['dias_aviso'])).count()
    for b in Batch.objects.select_related('product').filter(physical__gt=0, expiry__lt=today).order_by('expiry'):
        free = b.physical - b.reserved
        if free <= 0:
            continue
        unit = costs.of(b)
        value = (unit * free).quantize(Decimal('0.01')) if unit is not None else None
        proposals.append(Proposal(
            kind='writeoff_expired', key=f'baja:{b.pk}:{free}',
            title=f'Dar de baja {b.product.qty(free)} de {b.product.name} por vencimiento (lote {b.lot})',
            detail=(f'El lote venció el {b.expiry:%d/%m/%Y} y sigue con saldo en {b.location}. La baja deja el inventario igual al físico utilizable'
                    + (f'; {q(b.reserved, "unidad reservada queda", "unidades reservadas quedan")} para revisar a mano.' if b.reserved else '.')
                    + (f' Valor al costo: {cop(value)}.' if value is not None else '')),
            payload={'batch_id': b.pk}, amount=value, url=reverse('trace', args=[b.pk]), confidence='Alta'))
    return Result(proposals, f'{q(len(proposals), "lote vencido", "lotes vencidos")} por dar de baja; {q(soon, "lote vence", "lotes vencen")} en los próximos {p["dias_aviso"]} días.',
                  {'lotes_vencidos': len(proposals), 'por_vencer': soon})


# ----------------------------------------------------------------- solicitudes
def run_solicitudes(p, today):
    from inventory.models import SupplyRequest
    proposals, without = [], 0
    for r in SupplyRequest.objects.filter(status='pending').select_related('product', 'requester').order_by('needed_by', 'created_at'):
        b = S.suggested_batch(r.product, r.quantity)
        if b and r.needed_by and b.expiry < r.needed_by:
            b = None
        if b is None:
            without += 1
            continue
        when = f' para el {r.needed_by:%d/%m/%Y}' if r.needed_by else ''
        proposals.append(Proposal(
            kind='reserve_request', key=f'reserva:{r.pk}',
            title=f'Reservar {r.product.qty(r.quantity)} de {r.product.name} para {r.requester.display_name}{when}',
            detail=(f'Primero vence, primero sale: lote {b.lot} ({b.location}), vence el {b.expiry:%d/%m/%Y}, con {b.available} disponibles. '
                    f'Destino: {r.purpose}.' + (' La fecha requerida ya pasó: confirme con el solicitante.' if r.needed_by and r.needed_by < today else '')),
            payload={'request_id': str(r.pk), 'batch_id': b.pk}, url=reverse('approve', args=[r.pk]), confidence='Alta'))
    return Result(proposals, f'{q(len(proposals), "solicitud lista", "solicitudes listas")} para reservar; {without} sin existencias suficientes en un lote vigente.',
                  {'listas': len(proposals), 'sin_existencias': without})


# ------------------------------------------------------------ sistema contable
def run_contable(p, today):
    from contable import services as Q
    done = Q.process_pending()
    states = defaultdict(int)
    for sub in done:
        states[sub.get_status_display()] += 1
    detail = ', '.join(f'{n} {s.lower()}' for s, n in states.items()) or 'sin envíos pendientes'
    return Result([], f'{q(len(done), "envío", "envíos")} al sistema contable simulado, revisados con su misma clave: {detail}.', {'revisados': len(done), **states})


# --------------------------------------------------------------- avisos internos
def run_avisos(p, today):
    from notificaciones.notifier import dispatch
    from .models import AgentRun
    r = dispatch()
    # Este agente corre cada 5 minutos: se conserva su bitácora de 30 días.
    AgentRun.objects.filter(agent='avisos', started_at__lt=timezone.now() - timedelta(days=30)).delete()
    if not r['new']:
        return Result([], f'Sin alertas nuevas para {q(r["users"], "persona", "personas")}.', r)
    return Result([], f'{q(r["new"], "aviso interno registrado", "avisos internos registrados")} para {q(r["people"], "persona", "personas")}. Nada salió de la plataforma.', r)


# -------------------------------------------------------------- resumen diario
def run_resumen(p, today):
    from cartera.models import Sale, PaymentApplication, DiscountRequest
    from cartera import services as C
    from inventory.models import Batch, SupplyRequest
    from .models import AgentAction
    yesterday = today - timedelta(days=1)
    sales = Sale.objects.filter(status='issued', issue_date=yesterday)
    sold = sales.aggregate(s=Sum('subtotal'))['s'] or ZERO
    collected = PaymentApplication.objects.filter(reversed_at__isnull=True, collection__date=yesterday).aggregate(s=Sum('amount'))['s'] or ZERO
    rows = list(C.open_receivables())
    late = sum((r.balance for r in rows if r.due_date < today), ZERO)
    owe_late = sum((i.offer['balance'] for i in S.payables() if i.days_to_due < 0), ZERO)
    pending = AgentAction.objects.filter(status='proposed').count()
    discounts = DiscountRequest.objects.filter(status='pending').count()
    expired = Batch.objects.filter(physical__gt=0, expiry__lt=today).count()
    requests = SupplyRequest.objects.filter(status='pending').count()
    attention = []
    if pending:
        attention.append(f'{q(pending, "propuesta", "propuestas")} de los agentes por aprobar')
    if discounts:
        attention.append(f'{q(discounts, "descuento", "descuentos")} por confirmar')
    if late:
        attention.append(f'cartera vencida de {cop(late)}')
    if owe_late:
        attention.append(f'{cop(owe_late)} vencidos con proveedores')
    if expired:
        attention.append(f'{q(expired, "lote vencido", "lotes vencidos")} con saldo')
    if requests:
        attention.append(q(requests, 'solicitud de inventario pendiente', 'solicitudes de inventario pendientes'))
    text = f'Ayer ({yesterday:%d/%m}) se facturaron {cop(sold)} en {q(sales.count(), "venta", "ventas")} y se recaudaron {cop(collected)}.'
    text += (' Atención: ' + '; '.join(attention) + '.') if attention else ' No hay pendientes críticos.'
    return Result([], text[:500], {'facturado_ayer': str(sold), 'recaudado_ayer': str(collected), 'cartera_vencida': str(late),
                                   'proveedores_vencido': str(owe_late), 'propuestas': pending, 'descuentos': discounts, 'lotes_vencidos': expired,
                                   'solicitudes': requests, 'puntos': attention})


AGENTS = [
    Agent('pronto_pago', 'Agente de pronto pago', 'percent', 'Cuentas por pagar',
          'Revisa las facturas de proveedores y propone programar cada pago antes de que venza su descuento por pronto pago. También avisa de las vencidas.',
          'daily:07:00', FINANCE, FINANCE, run_pronto_pago,
          [Param('horizonte_dias', 'Mirar descuentos que vencen en los próximos (días)', 15, 1, 60),
           Param('margen_dias', 'Programar el pago con estos días de margen', 2, 0, 10)]),
    Agent('conciliacion', 'Agente de conciliación bancaria', 'inbox', 'Banco y recaudos',
          'Cruza cada ingreso del extracto con la cartera: menciones de factura, titular, pagadores confirmados, valores y lo que el modelo de pagos ha aprendido '
          'de identificaciones anteriores. Propone el reparto y el recibo de caja; los casos dudosos quedan para contabilidad.',
          'every:60', FINANCE, FINANCE, run_conciliacion),
    Agent('cobranza', 'Agente de cobranza', 'dollar', 'Cartera',
          'Prepara un recordatorio por cliente con sus facturas vencidas y el canal registrado. Elige el tono y el momento según el tipo de cliente y '
          'registra si hubo pago en los 10 días siguientes a cada envío, para orientar la elección siguiente (coincidencia en el tiempo, no prueba de '
          'causa). Al aprobarlo queda un envío simulado y el texto se muestra en pantalla.',
          'daily:08:00', FINANCE, FINANCE, run_cobranza,
          [Param('dias_minimos', 'Recordar facturas vencidas hace al menos (días)', 1, 0, 120),
           Param('repetir_cada_dias', 'No repetir el recordatorio antes de (días)', 7, 1, 60)]),
    Agent('vencimientos', 'Agente de vencimientos', 'calendar', 'Inventario',
          'Detecta lotes vencidos con saldo y propone su baja con la causa «vencimiento», para que el inventario refleje lo utilizable.',
          'daily:06:30', MANAGERS, MANAGERS, run_vencimientos,
          [Param('dias_aviso', 'Contar como «por vencer» los lotes de los próximos (días)', 30, 7, 120)]),
    Agent('reposicion', 'Agente de reposición', 'package', 'Inventario',
          'Pronostica el consumo de cada referencia con el método que tuvo menos error en una ventana de selección, calcula el punto de pedido con el '
          'tiempo de entrega de cada proveedor y propone pedidos agrupados por proveedor, dentro del presupuesto de compra. Cada semana guarda su '
          'pronóstico y después lo compara con lo que salió.',
          'daily:06:45', MANAGERS, (*MANAGERS, 'finance'), run_reposicion,
          [Param('nivel_servicio', 'Nivel de servicio objetivo (%)', 95, 90, 99),
           Param('revision_dias', 'Revisar el inventario cada (días)', 7, 1, 30),
           Param('ciclo_dias', 'Cada pedido debe cubrir (días)', 14, 7, 90),
           Param('tiempo_entrega_dias', 'Tiempo de entrega si el proveedor no tiene historial (días)', 7, 1, 60),
           Param('margen_vencimiento_dias', 'Margen antes del vencimiento para no pedir de más (días)', 30, 0, 180),
           Param('incluir_rechazadas', 'Contar solicitudes rechazadas como demanda no atendida (1 sí, 0 no)', 1, 0, 1),
           Param('presupuesto', 'Presupuesto de compra por revisión (COP; 0 = sin tope)', 0, 0, 10 ** 12)]),
    Agent('solicitudes', 'Agente de solicitudes', 'clipboard', 'Inventario',
          'Para cada solicitud pendiente, elige el lote vigente que vence primero y alcanza la cantidad, y propone la reserva.',
          'every:30', MANAGERS, MANAGERS, run_solicitudes),
    Agent('contable', 'Agente de envíos al sistema contable', 'activity', 'Sistema contable',
          'Retoma los envíos pendientes o inciertos con su misma clave de idempotencia, sin duplicar documentos. Actúa solo.',
          'every:30', FINANCE, FINANCE, run_contable, automatic=True),
    Agent('avisos', 'Avisos internos', 'bell', 'Equipo',
          'Registra para cada persona las alertas nuevas de su perfil, una sola vez por alerta. Los avisos se consultan en Alertas; nada sale de la plataforma. Actúa solo.',
          'every:5', ADMINS, ADMINS, run_avisos, automatic=True),
    Agent('resumen', 'Resumen diario', 'file-text', 'Dirección',
          'Cada mañana resume lo facturado y recaudado el día anterior y lo que necesita atención.',
          'daily:06:00', ADMINS, (*ADMINS, 'finance'), run_resumen, automatic=True),
]
REGISTRY = {a.key: a for a in AGENTS}


def params_for(agent, stored):
    values = {p.key: p.default for p in agent.params}
    for p in agent.params:
        try:
            v = int((stored or {}).get(p.key, p.default))
        except (TypeError, ValueError):
            continue
        values[p.key] = min(max(v, p.minimum), p.maximum)
    return values
