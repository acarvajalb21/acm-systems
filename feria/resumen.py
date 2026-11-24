"""Resumen (inicio por perfil): qué necesita atención, qué puede ocurrir, qué se recomienda y pocos indicadores.

Todas las cifras salen de servicios que ya usan las pantallas de detalle (alertas, cuentas por pagar, cartera,
banco, reposición, resultados), de modo que el número del Resumen coincide con el del módulo al que enlaza. Nada
se muestra a un perfil que no pueda abrir el módulo de origen. Las lecturas costosas se hacen una vez por
petición en `flow.Datos`.
"""
import logging
from calendar import monthrange
from datetime import timedelta
from decimal import Decimal
from statistics import median
from django.db.models import Min, Sum
from django.urls import reverse
from django.utils import timezone
from inventory import services as S
from . import flow
from .flow import Datos, plural, when

log = logging.getLogger(__name__)
ZERO = Decimal('0.00')
MONTHS = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']
DAYS = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']
RANK = {'critical': 0, 'warning': 1, 'ok': 2, 'info': 3}
CHIPS = {'critical': ('bad', 'Crítico'), 'warning': ('warn', 'Atención'), 'ok': ('ok', 'Propuesta'), 'info': ('info', 'Aviso')}
MAX_ATTENTION, MAX_RISKS = 6, 5
RECENT_DAYS = 7
EXPIRY_HORIZON = 120   # días: hasta dónde un lote por vencer cuenta como riesgo próximo


def long_date(day):
    return f'{DAYS[day.weekday()].capitalize()} {day.day} de {MONTHS[day.month - 1]} de {day.year}'


def pct_change(current, previous):
    """Variación relativa en porcentaje, o None si la base es cero (no hay contra qué comparar)."""
    if not previous:
        return None
    return ((current - previous) * 100 / previous).quantize(Decimal('0.1'))


def pct_text(value):
    return ('+' if value > 0 else '−' if value < 0 else '') + f'{abs(value)}'.replace('.', ',') + ' %'


def same_days(today):
    """Mes en curso hasta hoy y el mismo número de días del mes anterior: (inicio, fin, inicio anterior, fin anterior)."""
    start = today.replace(day=1)
    prev_end_month = start - timedelta(days=1)
    prev_start = prev_end_month.replace(day=1)
    prev_end = prev_start.replace(day=min(today.day, monthrange(prev_start.year, prev_start.month)[1]))
    return start, today, prev_start, prev_end


def sales_between(start, end):
    """Ventas facturadas sin IVA entre dos fechas (ambas incluidas): la misma base de Finanzas → Resultados."""
    from cartera.results import _month_sales
    return _month_sales(start, end + timedelta(days=1)).aggregate(s=Sum('subtotal'))['s'] or ZERO


def collected_between(start, end):
    """Recaudo aplicado a cartera entre dos fechas (ambas incluidas), sin las aplicaciones reversadas."""
    from cartera.models import PaymentApplication
    return PaymentApplication.objects.filter(reversed_at__isnull=True, collection__date__gte=start, collection__date__lte=end).aggregate(s=Sum('amount'))['s'] or ZERO


def expiring_stock_value(today, days=60):
    """Inventario con saldo que ya venció o vence en `days` días, al costo de compra (igual que en Resultados)."""
    from cartera.results import Costs
    from inventory.models import Batch, money
    costs, total = Costs(), ZERO
    for b in Batch.objects.filter(physical__gt=0, expiry__lte=today + timedelta(days=days)):
        unit = costs.of(b)
        if unit is not None:
            total += unit * b.physical
    return money(total)


def comparison(current, previous, today, noun):
    """Texto y sentido de una comparación equivalente (mismos días del mes anterior)."""
    prev_month = MONTHS[(today.replace(day=1) - timedelta(days=1)).month - 1]
    span = 'el mismo día' if today.day == 1 else f'los mismos {today.day} días'
    change = pct_change(current, previous)
    if change is None:
        return {'text': f'Sin {noun} en {span} de {prev_month}: no hay base para comparar.', 'dir': ''}
    return {'text': f'{pct_text(change)} frente a {span} de {prev_month} ({S.cop(previous)})', 'dir': 'up' if change > 0 else 'down' if change < 0 else ''}


# -------------------------------------------------------- qué necesita atención
# prefijo de la alerta → (singular, plural, por qué importa, nombre de URL, sufijo, acción)
GROUPS = (
    ('expiry-', {'critical': ('lote vencido con saldo', 'lotes vencidos con saldo', 'No se pueden despachar; la baja deja el inventario igual a lo utilizable.', 'stock', '', 'Ver lotes'),
                 'warning': ('lote por vencer', 'lotes por vencer', 'Conviene darles salida primero para no perder las unidades.', 'stock', '', 'Ver lotes')}),
    ('stock-', {'critical': ('referencia agotada', 'referencias agotadas', 'Las solicitudes y ventas de estas referencias no se pueden atender.', 'replenishment', '', 'Ver reposición'),
                'warning': ('referencia en su mínimo o por debajo', 'referencias en su mínimo o por debajo', 'Queda poco margen antes de un faltante.', 'replenishment', '', 'Ver reposición')}),
    ('pending-', ('solicitud por atender', 'solicitudes por atender', 'Un área espera respuesta para continuar su trabajo.', 'requests', '?estado=pending', 'Atender')),
    ('receipt-', ('factura con mercancía por recibir', 'facturas con mercancía por recibir', 'Hasta recibirla, la mercancía no cuenta como disponible.', 'reception', '', 'Recibir')),
    ('review-', ('factura de proveedor por revisar', 'facturas de proveedor por revisar', 'Sin validarla no se puede recibir ni programar su pago.', 'invoices', '?vista=revisar', 'Revisar')),
    ('overdue-', ('factura de proveedor vencida', 'facturas de proveedores vencidas', 'Ya superaron su fecha de vencimiento.', 'payables', '', 'Ver pagos')),
    ('due-', ('factura de proveedor por vencer', 'facturas de proveedores por vencer', 'Vencen dentro del plazo de aviso configurado.', 'payables', '', 'Ver pagos')),
    ('discount-req-', ('descuento por confirmar', 'descuentos por confirmar', 'La venta no se emite hasta que la dirección decida.', 'discounts', '#por-confirmar', 'Decidir')),
    ('discount-dec-', None),
    ('discount-quota-', None),
    ('discount-', ('descuento por pronto pago por aprovechar', 'descuentos por pronto pago por aprovechar', 'Se pierden si el pago se registra después de la fecha límite.', 'payables', '', 'Ver pagos')),
    ('scheduled-', ('pago programado sin confirmar', 'pagos programados sin confirmar', 'La fecha programada ya pasó y falta el soporte del pago.', 'payables', '?vista=programados', 'Confirmar')),
    ('contable-', ('envío al sistema contable por revisar', 'envíos al sistema contable por revisar', 'El simulador no confirmó el documento.', 'contable_status', '', 'Revisar')),
    ('voucher-', ('recibo de caja rechazado', 'recibos de caja rechazados', 'El recaudo quedó aplicado aquí, pero sin recibo en el sistema contable simulado.', 'contable_status', '', 'Revisar')),
    ('blocked-', ('entrega bloqueada por vencimiento', 'entregas bloqueadas por vencimiento', 'Inventario debe asignar otro lote.', 'requests', '', 'Ver solicitudes')),
    ('request-', ('solicitud con respuesta', 'solicitudes con respuesta', 'Inventario respondió en los últimos 7 días.', 'requests', '', 'Ver solicitudes')),
)


def _group_of(key, level):
    for prefix, spec in GROUPS:
        if key.startswith(prefix):
            if spec is None:
                return None
            spec = spec.get(level) if isinstance(spec, dict) else spec
            return (prefix + level, spec) if spec else None
    return None


def attention(d, alerts, proposals):
    """Lista priorizada: alertas del perfil agrupadas por tipo y propuestas de agentes que el perfil puede decidir."""
    from agentes.agents import REGISTRY
    buckets, singles = {}, []
    recent = None
    for a in alerts:
        if a['key'].startswith('agent-'):
            continue   # las propuestas entran abajo, una por una o por agente
        if a['key'].startswith('request-'):
            # Una respuesta de inventario es novedad unos días; las antiguas siguen en Alertas, no aquí.
            if recent is None:
                since = timezone.now() - timedelta(days=RECENT_DAYS)
                recent = {str(pk) for pk in S.visible_requests(d.user).exclude(status='pending').filter(updated_at__gte=since).values_list('pk', flat=True)}
            if a['key'][len('request-'):] not in recent:
                continue
        group = _group_of(a['key'], a['level'])
        if group is None:
            singles.append(a)
        else:
            buckets.setdefault(group, []).append(a)
    items = []
    for (_, (one, many, why, url_name, suffix, action)), rows in buckets.items():
        first = rows[0]
        if len(rows) == 1:
            items.append({'level': first['level'], 'title': first['title'], 'detail': f'{first["detail"]}. {why}', 'url': first['url'] if first['url'] != reverse('home') else reverse(url_name) + suffix,
                          'action': action, 'source': 'Alertas', 'count': 1})
        else:
            items.append({'level': first['level'], 'title': f'{len(rows)} {many}', 'detail': f'{why} Por ejemplo: {first["detail"]}.', 'url': reverse(url_name) + suffix,
                          'action': action, 'source': 'Alertas', 'count': len(rows)})
    for a in singles:
        items.append({'level': a['level'], 'title': a['title'], 'detail': a['detail'], 'url': a['url'], 'action': 'Abrir', 'source': 'Alertas', 'count': 1})
    by_agent = {}
    for p in proposals:
        by_agent.setdefault(p.agent, []).append(p)
    for key, rows in by_agent.items():
        agent = REGISTRY[key]
        if len(rows) == 1:
            p = rows[0]
            items.append({'level': 'ok', 'title': p.title, 'detail': p.detail[:220] + ('…' if len(p.detail) > 220 else ''), 'url': reverse('agent_action', args=[p.pk]),
                          'action': 'Revisar y decidir', 'source': agent.name, 'count': 1})
        else:
            benefit = sum((p.benefit or ZERO for p in rows), ZERO)
            items.append({'level': 'ok', 'title': f'{agent.name}: {len(rows)} propuestas por decidir',
                          'detail': f'La primera: {rows[0].title}.' + (f' En conjunto suman {S.cop(benefit)} entre descuentos y recaudo por aplicar.' if benefit else ''),
                          'url': reverse('agents') + '#por-aprobar', 'action': 'Revisar propuestas', 'source': agent.name, 'count': len(rows)})
    items.sort(key=lambda i: (RANK[i['level']], -i['count']))
    for i in items:
        i['chip'], i['chip_text'] = CHIPS[i['level']]
        i['mark'] = {'critical': 'is-critical', 'warning': 'is-warning', 'ok': 'is-ok'}.get(i['level'], '')
    return items


def visible(items, limit=MAX_ATTENTION, reserved=2):
    """Los que caben en la lista. Si las alertas la llenan, se guardan hasta dos lugares para propuestas por decidir."""
    top = items[:limit]
    proposals = [i for i in items if i['level'] == 'ok']
    if not proposals or any(i['level'] == 'ok' for i in top):
        return top
    keep = min(len(proposals), reserved)
    urgent = [i for i in items if RANK[i['level']] < RANK['ok']][:limit - keep]
    return urgent + proposals[:keep]


# ---------------------------------------------------------- qué puede ocurrir
def risks(d):
    """Riesgos próximos calculados con los datos de hoy, cada uno con su horizonte y el registro que lo explica."""
    out = []

    def add(days, level, title, detail, url, label):
        out.append({'days': days, 'when': when(days), 'level': level, 'title': title, 'detail': detail, 'url': url, 'label': label})

    if d.manager:
        short = sorted((pl for pl in d.to_order if pl.coverage_days is not None and pl.coverage_days < pl.lead_days), key=lambda pl: pl.coverage_days)
        for pl in short[:3]:
            add(pl.coverage_days, 'critical', f'{pl.product.name} se agotaría antes de que llegue un pedido',
                f'Lo que hay y lo que viene en camino alcanza para {plural(pl.coverage_days, "día")}; un pedido nuevo tarda {plural(pl.lead_days, "día")} en llegar.',
                reverse('forecast', args=[pl.product.pk]), 'Ver pronóstico')
        at_risk = [pl for pl in d.plans if getattr(pl, 'at_risk', 0) > 0]
        if at_risk:
            from inventory.models import Batch
            first = dict(Batch.objects.filter(product__in=[pl.product for pl in at_risk], physical__gt=0, expiry__gte=d.today)
                         .values('product_id').annotate(e=Min('expiry')).values_list('product_id', 'e'))
            for pl in sorted(at_risk, key=lambda pl: first.get(pl.product.pk) or d.today)[:2]:
                expiry = first.get(pl.product.pk)
                if expiry is None or (expiry - d.today).days > EXPIRY_HORIZON:
                    continue   # un vencimiento lejano no es un riesgo próximo
                add((expiry - d.today).days, 'warning', f'{pl.product.qty(pl.at_risk)} de {pl.product.name} vencerían sin usarse',
                    f'Al ritmo de salida actual no se alcanzan a consumir. El primer lote vence el {expiry:%d/%m/%Y}.',
                    reverse('forecast', args=[pl.product.pk]), 'Ver pronóstico')
    if d.finance:
        offers = sorted((i for i in d.payables if i.offer['deadline'] and i.offer['discount'] > 0 and not i.scheduled_on), key=lambda i: i.offer['deadline'])
        for inv in offers[:2]:
            o = inv.offer
            add((o['deadline'] - d.today).days, 'warning', f'Se perdería el descuento de {S.cop(o["discount"])} con {inv.supplier.name}',
                f'Factura {inv.number}: {S.pct(o["percent"])} % por pronto pago hasta el {o["deadline"]:%d/%m/%Y}, sin pago programado.',
                reverse('invoice_pay', args=[inv.pk]), 'Ver el pago')
        due = [r for r in d.receivables if -7 <= r.days_late <= 0]
        if due:
            first = min(due, key=lambda r: r.due_date)
            total = sum((r.balance for r in due), ZERO)
            add(-first.days_late, 'warning', f'{S.cop(total)} de cartera entraría en mora',
                f'{plural(len(due), "factura")} de clientes vence{"n" if len(due) != 1 else ""} en los próximos 7 días; la primera, {first.document} de {first.party.name.rstrip(".")}.',
                reverse('portfolio'), 'Ver cartera')
        soon = [i for i in d.payables if 0 <= i.days_to_due <= 7 and not i.scheduled_on]
        if soon:
            first = min(soon, key=lambda i: i.due_date)
            total = sum((i.offer['balance'] for i in soon), ZERO)
            add(first.days_to_due, 'warning', f'{S.cop(total)} por pagar a proveedores vence sin pago programado',
                f'{plural(len(soon), "factura")} en los próximos 7 días; la primera, {first.number} de {first.supplier.name.rstrip(".")}.',
                reverse('payables'), 'Ver pagos')
    if d.role == 'requester':
        for r in S.visible_requests(d.user).filter(status='pending', needed_by__isnull=False, needed_by__lte=d.today + timedelta(days=3)).order_by('needed_by')[:3]:
            add((r.needed_by - d.today).days, 'warning', f'Su solicitud de {r.product.name} se necesita pronto',
                f'{r.product.qty(r.quantity)} para «{r.purpose}». Sigue en revisión de inventario.', reverse('requests') + '?estado=pending', 'Ver la solicitud')
    out.sort(key=lambda r: (r['days'], RANK[r['level']]))
    for r in out:
        r['mark'] = 'is-critical' if r['level'] == 'critical' else 'is-warning'
    return out[:MAX_RISKS]


# --------------------------------------------------------- qué se recomienda
def _from_proposal(p, title, reason):
    return {'title': title, 'reason': reason, 'url': reverse('agent_action', args=[p.pk]), 'label': 'Revisar la propuesta', 'source': 'Propuesta de un agente'}


def recommendation(d, proposals, stations):
    """Una sola acción principal para el perfil, con su sustento en una frase y el enlace para revisarla."""
    by_agent = {}
    for p in proposals:
        by_agent.setdefault(p.agent, []).append(p)

    def best(key, by):
        rows = by_agent.get(key) or []
        return max(rows, key=by) if rows else None

    def restock():
        p = best('reposicion', lambda p: p.amount or ZERO)
        if p is None:
            return None
        n = len(p.payload.get('products', []))
        cost = f' por {S.cop(p.amount)} al costo estimado' if p.amount else ''
        return _from_proposal(p, p.title.replace('Preparar pedido', 'Aprobar el pedido sugerido', 1) if p.title.startswith('Preparar pedido') else p.title,
                              f'Cubre {plural(n, "referencia")} bajo su punto de pedido{cost}. Las cantidades se recalculan al aprobar y el envío al proveedor es simulado.')

    def prompt_pay():
        p = best('pronto_pago', lambda p: p.benefit or ZERO)
        if p is None or not p.benefit:
            return None
        return _from_proposal(p, p.title, f'Aprovecha {S.cop(p.benefit)} de descuento por pronto pago antes de su fecha límite. El pago queda programado, no ejecutado.')

    def reconcile():
        p = best('conciliacion', lambda p: p.benefit or ZERO)
        if p is None:
            return None
        return _from_proposal(p, p.title, f'Identifica un ingreso del banco y lo aplica a la cartera del cliente (coincidencia {(p.confidence or "sugerida").lower()}).')

    def writeoff():
        p = best('vencimientos', lambda p: p.amount or ZERO)
        return _from_proposal(p, p.title, 'El lote está vencido: la baja deja las existencias iguales a lo que sí se puede despachar.') if p else None

    def reserve():
        p = best('solicitudes', lambda p: -p.pk)
        return _from_proposal(p, p.title, 'Atiende una solicitud pendiente con el lote vigente que vence primero.') if p else None

    def collect():
        p = best('cobranza', lambda p: p.amount or ZERO)
        return _from_proposal(p, p.title, 'Recuerda al cliente sus facturas vencidas. El envío es simulado y el texto queda a la vista.') if p else None

    def discounts():
        from cartera.models import DiscountRequest
        n = DiscountRequest.objects.filter(status=DiscountRequest.Status.PENDING).count()
        if not n:
            return None
        return {'title': f'Decidir {plural(n, "descuento pendiente", "descuentos pendientes")}', 'reason': 'Las ventas con descuento fuera del cupo no se emiten hasta que la dirección decida.',
                'url': reverse('discounts') + '#por-confirmar', 'label': 'Ver los descuentos', 'source': 'Decisión de la dirección'}

    def receive():
        rows = d.receivable_invoices
        if not rows:
            return None
        inv = rows[0]
        return {'title': f'Recibir la factura {inv.number} de {inv.supplier.name}', 'reason': f'Es la más antigua por recibir: {plural(inv.pending_units, "unidad pendiente", "unidades pendientes")}. Use el lector o teclee los códigos de las cajas.',
                'url': reverse('reception_invoice', args=[inv.pk]), 'label': 'Recibir esta factura', 'source': 'Mercancía por recibir'}

    def sale():
        from cartera.models import Sale
        bad = Sale.objects.filter(status__in=['rejected', 'uncertain']).select_related('party').order_by('created_at').first()
        if bad:
            return {'title': f'Resolver la venta {bad.label} de {bad.party.name}', 'reason': f'Estado: {bad.get_status_display().lower()}. Mientras siga así no queda emitida ni entra en cartera.',
                    'url': reverse('sale', args=[bad.pk]), 'label': 'Abrir la venta', 'source': 'Ventas'}
        draft = Sale.objects.filter(status='draft').select_related('party').order_by('created_at').first()
        if draft:
            return {'title': f'Completar y emitir la venta a {draft.party.name}', 'reason': f'Es el borrador más antiguo ({S.cop(draft.total)}). La emisión va al sistema contable simulado.',
                    'url': reverse('sale', args=[draft.pk]), 'label': 'Abrir el borrador', 'source': 'Ventas'}
        return {'title': 'Preparar una venta', 'reason': 'No hay borradores ni envíos por resolver.', 'url': reverse('sale_new'), 'label': 'Nueva venta', 'source': 'Ventas'}

    def request():
        ready = S.visible_requests(d.user).filter(status='approved').count()
        if ready:
            return {'title': f'Coordinar la entrega de {plural(ready, "solicitud reservada", "solicitudes reservadas")}', 'reason': 'Inventario ya apartó las unidades; falta registrar la entrega física.',
                    'url': reverse('requests') + '?estado=approved', 'label': 'Ver mis reservas', 'source': 'Mis solicitudes'}
        return {'title': 'Pedir lo que su área necesita', 'reason': 'Consulte las unidades disponibles y deje la solicitud con su destino y la fecha en que la requiere.',
                'url': reverse('new_request'), 'label': 'Nueva solicitud', 'source': 'Mis solicitudes'}

    def flow_default():
        worst = flow.pick(stations)
        if worst.allowed and worst.state:
            return {'title': f'Revisar la estación {worst.label} del mapa de flujo', 'reason': worst.reason, 'url': reverse('flow_map') + f'?etapa={worst.key}', 'label': 'Abrir el mapa', 'source': 'Mapa de flujo'}
        return {'title': 'Todo está al día', 'reason': 'No hay propuestas por decidir ni estaciones en atención para su perfil.', 'url': reverse('flow_map'), 'label': 'Ver el mapa de flujo', 'source': 'Mapa de flujo', 'calm': True}

    plan = {
        'owner': (restock, prompt_pay, discounts, reconcile, writeoff, collect, flow_default),
        'admin': (restock, prompt_pay, reconcile, writeoff, reserve, collect, flow_default),
        'inventory': (restock, writeoff, reserve, flow_default),
        'finance': (prompt_pay, reconcile, collect, flow_default),
        'reception': (receive, flow_default),
        'billing': (sale,),
        'requester': (request,),
    }
    for option in plan.get(d.role, (flow_default,)):
        found = option()
        if found:
            return found
    return None


# ------------------------------------------------------------------ indicadores
def _kpi(label, value, period, url, sub='', icon='activity', tone='', delta=None, key=''):
    return {'label': label, 'value': value, 'period': period, 'url': url, 'sub': sub, 'icon': icon, 'tone': tone, 'delta': delta, 'key': key}


def indicators(d, stations):
    """Entre tres y cinco cifras por perfil. Cada una dice su periodo y enlaza al detalle que la sustenta."""
    today, role = d.today, d.role
    hoy = f'Hoy, {today:%d/%m/%Y}'
    by_key = {st.key: st for st in stations}
    out = []
    if role in S.ADMINS:
        start, end, prev_start, prev_end = same_days(today)
        month = MONTHS[today.month - 1]
        span = f'Del 1 al {today.day} de {month}' if today.day > 1 else f'1 de {month}'
        sold, sold_prev = sales_between(start, end), sales_between(prev_start, prev_end)
        got, got_prev = collected_between(start, end), collected_between(prev_start, prev_end)
        late = d.aging['d30'] + d.aging['d60'] + d.aging['d90'] + d.aging['over']
        open_total = sum((r.balance for r in d.receivables), ZERO)
        owe_late = sum((i.offer['balance'] for i in d.payables if i.days_to_due < 0), ZERO)
        owe_n = sum(1 for i in d.payables if i.days_to_due < 0)
        soon = expiring_stock_value(today)
        results = reverse('results')
        out = [
            _kpi('Ventas facturadas sin IVA', S.cop(sold), span, results, icon='file-text', delta=comparison(sold, sold_prev, today, 'ventas'), key='ventas'),
            _kpi('Recaudo aplicado a cartera', S.cop(got), span, results + '#cartera', icon='dollar', delta=comparison(got, got_prev, today, 'recaudo'), key='recaudo'),
            _kpi('Cartera vencida', S.cop(late), hoy, reverse('portfolio'), sub=f'de {S.cop(open_total)} por cobrar', icon='clock', tone='bad' if late else 'ok', key='cartera_vencida'),
            _kpi('Cuentas por pagar vencidas', S.cop(owe_late), hoy, reverse('payables'), sub=plural(owe_n, 'factura de proveedor', 'facturas de proveedores'), icon='credit-card',
                 tone='bad' if owe_late else 'ok', key='pagar_vencidas'),
            _kpi('Inventario vencido o por vencer en 60 días, al costo de compra', S.cop(soon), hoy, results + '#inventario', sub='Valor en riesgo de vencimiento', icon='calendar',
                 tone='warn' if soon else 'ok', key='riesgo_vencimiento'),
        ]
    elif role == 'inventory':
        cover = [pl.coverage_days for pl in d.plans if getattr(pl, 'coverage_days', None) is not None]
        units = sum(pl.quantity for pl in d.to_order)
        soon = [b for b in d.soon_lots if b.expiry >= today]
        expired = len(d.soon_lots) - len(soon)
        out = [
            _kpi('Referencias por pedir', str(len(d.to_order)), hoy, reverse('replenishment'), sub=f'{units} unidades sugeridas en total' if d.to_order else 'Ninguna bajo su punto de pedido',
                 icon='package', tone='warn' if d.to_order else 'ok', key='por_pedir'),
            _kpi('Cobertura mediana', plural(int(median(cover)), 'día') if cover else '—', hoy, reverse('replenishment'),
                 sub=f'Mitad de las {len(cover)} referencias con consumo alcanza menos' if cover else 'Aún no hay consumo para estimarla', icon='layers', key='cobertura'),
            _kpi('Lotes por vencer', str(len(soon)), f'Próximos {d.settings.expiry_days} días', reverse('stock'), sub=plural(expired, 'lote ya vencido con saldo', 'lotes ya vencidos con saldo') if expired else 'Ninguno vencido con saldo',
                 icon='calendar', tone='bad' if expired else 'warn' if soon else 'ok', key='por_vencer'),
            _kpi('Solicitudes por atender', by_key['solicitud'].value, hoy, reverse('requests') + '?estado=pending', sub=by_key['solicitud'].reason, icon='clipboard',
                 tone='warn' if by_key['solicitud'].count else 'ok', key='solicitudes'),
        ]
    elif role == 'finance':
        txns = d.pending_bank
        week = [i for i in d.payables if 0 <= i.days_to_due <= 7]
        late = d.aging['d30'] + d.aging['d60'] + d.aging['d90'] + d.aging['over']
        offer = sum((i.offer['discount'] for i in d.payables), ZERO)
        out = [
            _kpi('Ingresos por identificar', S.cop(sum((t.amount for t in txns), ZERO)), hoy, reverse('bank'), sub=plural(len(txns), 'movimiento del extracto', 'movimientos del extracto'), icon='inbox',
                 tone='warn' if txns else 'ok', key='por_identificar'),
            _kpi('Pagos a proveedores de la semana', S.cop(sum((i.offer['balance'] for i in week), ZERO)), f'Vencen entre hoy y el {today + timedelta(days=7):%d/%m/%Y}', reverse('payables'),
                 sub=plural(len(week), 'factura'), icon='credit-card', key='pagos_semana'),
            _kpi('Descuentos por pronto pago disponibles', S.cop(offer), 'Si se pagara hoy', reverse('payables'), sub='Suma de las facturas con descuento vigente', icon='percent', tone='ok' if offer else '', key='descuentos'),
            _kpi('Cartera vencida', S.cop(late), hoy, reverse('portfolio'), sub=f'de {S.cop(sum((r.balance for r in d.receivables), ZERO))} por cobrar', icon='clock', tone='bad' if late else 'ok', key='cartera_vencida'),
        ]
    elif role == 'reception':
        from inventory.models import ReceivingSession
        rows = d.receivable_invoices
        done = ReceivingSession.objects.filter(status='confirmed', closed_at__date=today).count()
        out = [
            _kpi('Facturas por recibir', str(len(rows)), hoy, reverse('reception'), sub='Validadas por compras', icon='truck', tone='warn' if rows else 'ok', key='por_recibir'),
            _kpi('Unidades por recibir', f'{sum(i.pending_units for i in rows):,}'.replace(',', '.'), hoy, reverse('reception'), sub='En esas facturas', icon='package', key='unidades'),
            _kpi('Recepciones confirmadas', str(done), hoy, reverse('reception'), sub='Con entrada por lote registrada', icon='check-circle', key='recibidas_hoy'),
        ]
    elif role == 'billing':
        from cartera.models import Sale
        day = Sale.objects.filter(status='issued', issue_date=today)
        drafts = Sale.objects.filter(status__in=['draft', 'rejected']).count()
        sending = Sale.objects.filter(status__in=['sending', 'uncertain']).count()
        out = [
            _kpi('Borradores y rechazadas', str(drafts), hoy, reverse('sales') + '?vista=borradores', sub='Por completar o corregir', icon='edit', tone='warn' if drafts else 'ok', key='borradores'),
            _kpi('Facturado hoy, IVA incluido', S.cop(day.aggregate(s=Sum('total'))['s'] or ZERO), hoy, reverse('sales') + '?vista=emitidas',
                 sub=plural(day.count(), 'venta emitida', 'ventas emitidas') + ' en el sistema contable simulado', icon='file-text', key='ventas_hoy'),
            _kpi('Envíos por confirmar', str(sending), hoy, reverse('sales') + '?vista=pendientes', sub='Enviando o con resultado incierto', icon='clock', tone='bad' if sending else 'ok', key='envios'),
        ]
    elif role == 'requester':
        mine = S.visible_requests(d.user)
        pending, ready = mine.filter(status='pending').count(), mine.filter(status='approved').count()
        start = today.replace(day=1)
        delivered = mine.filter(status='delivered', updated_at__date__gte=start).count()
        out = [
            _kpi('Solicitudes en revisión', str(pending), hoy, reverse('requests') + '?estado=pending', sub='Esperan respuesta de inventario', icon='clipboard', tone='warn' if pending else 'ok', key='pendientes'),
            _kpi('Reservadas, listas para entrega', str(ready), hoy, reverse('requests') + '?estado=approved', sub='Inventario ya apartó las unidades', icon='bookmark', key='reservadas'),
            _kpi('Entregadas', str(delivered), f'Del 1 al {today.day} de {MONTHS[today.month - 1]}' if today.day > 1 else f'1 de {MONTHS[today.month - 1]}', reverse('requests') + '?estado=delivered',
                 sub='Solicitudes suyas entregadas este mes', icon='check-circle', key='entregadas'),
        ]
    return out


# ----------------------------------------------------------------------- vista
def context(request):
    """Contexto completo del Resumen. Cada bloque se calcula por separado: si uno falla, los demás se muestran."""
    from agentes import runner
    user = request.user
    d = Datos(user)
    failed = []

    def safe(name, fn, default):
        try:
            return fn()
        except Exception:
            log.exception('Resumen: no se pudo calcular «%s»', name)
            failed.append(name)
            return default

    stations = safe('el mapa de flujo', lambda: flow.stations(user, d), [])
    alerts = safe('las alertas', lambda: S.alerts(user), [])
    proposals = safe('las propuestas de los agentes', lambda: list(runner.pending_for(user).order_by('-benefit', 'created_at')), [])
    items = safe('la lista de atención', lambda: attention(d, alerts, proposals), [])
    ctx = {
        'title': 'Resumen', 'today': d.today, 'long_date': long_date(d.today), 'stations': stations,
        'flow_wide': sum(1 for st in stations if st.allowed) >= 3,
        'attention': visible(items), 'attention_total': len(items), 'attention_more': max(0, len(items) - MAX_ATTENTION),
        'alerts_total': len(alerts), 'proposals_total': len(proposals),
        'risks': safe('los riesgos próximos', lambda: risks(d), []),
        'recommendation': safe('la recomendación', lambda: recommendation(d, proposals, stations), None),
        'indicators': safe('los indicadores', lambda: indicators(d, stations), []),
        'unread_alerts': sum(1 for a in alerts if not a['read']),   # la navegación reutiliza esta cuenta en lugar de recalcular las alertas
        'failed': failed, 'plans_failed': d.plans_failed,
    }
    if user.role == 'requester':
        requests = S.visible_requests(user)
        products = list(S.availability())
        ctx.update({'products': products, 'requests': requests[:6], 'k_available': sum(p.available for p in products),
                    'k_pending': requests.filter(status='pending').count(), 'k_ready': requests.filter(status='approved').count()})
    elif user.role == 'reception':
        rows = d.receivable_invoices
        ctx.update({'pending': rows[:8], 'k_pending': len(rows), 'k_units': sum(i.pending_units for i in rows)})
    elif user.role == 'billing':
        from cartera.models import Sale
        ctx['recent_sales'] = Sale.objects.select_related('party').order_by('-created_at')[:8]
    return ctx
