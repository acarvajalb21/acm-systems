"""Mapa de flujo: las seis estaciones del recorrido solicitud → inventario → compra → recepción → venta → cobro.

`stations(user)` es el único servicio que cuenta lo pendiente de cada estación. Lo usan el mapa, el Resumen y el
recorrido, de modo que una misma cifra no se calcula de dos maneras. Cada conteo sale de la misma consulta que
usa la pantalla del módulo al que enlaza, y respeta los permisos: una estación ajena al perfil se devuelve con
`allowed=False`, sin cifras ni registros.

`Datos` reúne, una sola vez por petición, las lecturas costosas que comparten el mapa y el Resumen (planes de
reposición, cuentas por pagar, cartera abierta).
"""
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from functools import cached_property
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone
from inventory import services as S
from inventory.models import Batch, Invoice, PurchaseOrder, Settings, User

ZERO = Decimal('0.00')
ORDER = ('solicitud', 'inventario', 'compra', 'recepcion', 'venta', 'cobro')
ROLE_LABELS = dict(User.Role.choices)
STATE_LABELS = {'': 'Al día', 'warning': 'Atención', 'critical': 'Crítico'}
STATE_CHIPS = {'': 'ok', 'warning': 'warn', 'critical': 'bad'}


def when(days):
    """Horizonte en lenguaje llano: «hoy», «mañana», «en 9 días», «hace 3 días»."""
    if days == 0:
        return 'hoy'
    if days == 1:
        return 'mañana'
    if days == -1:
        return 'ayer'
    return f'en {days} días' if days > 0 else f'hace {-days} días'


def plural(n, one, many=None):
    """«1 lote», «3 lotes», «2 solicitudes». Para frases con más de una palabra se pasa el plural completo."""
    if n != 1 and many is None:
        many = one + ('s' if one[-1] in 'aeiouáéíóú' else 'es')
    return f'{n} {one if n == 1 else many}'


def profiles(roles, last=' y '):
    """Perfiles que atienden algo, en el orden de la jerarquía y con su nombre visible («A, B y C»; `last=' o '` para alternativas)."""
    order = ('requester', 'inventory', 'reception', 'billing', 'finance', 'admin', 'owner')
    names = [ROLE_LABELS[r] for r in order if r in roles]
    return names[0] if len(names) == 1 else ', '.join(names[:-1]) + last + names[-1]


class Datos:
    """Lecturas compartidas de una petición. Cada propiedad se calcula una vez y solo si el perfil puede verla."""

    def __init__(self, user, today=None):
        self.user, self.role = user, user.role
        self.today = today or timezone.localdate()
        self.manager = self.role in S.MANAGERS
        self.finance = self.role in S.FINANCE
        self.documents = self.role in S.DOCUMENTS
        self.sales = self.role in S.SALES
        self.receiver = self.role in S.RECEIVERS
        self.requests_role = self.role in S.REQUESTERS or self.manager
        self.plans_failed = False

    @cached_property
    def settings(self):
        return Settings.current()

    @cached_property
    def plans(self):
        """Planes de reposición (los mismos de la pantalla Reposición). Lista vacía si el perfil no los ve."""
        if not self.manager:
            return []
        try:
            from aprendizaje import reposicion as R
            return list(R.plans(today=self.today))
        except Exception:   # el Resumen y el mapa siguen en pie si el cálculo de reposición falla
            self.plans_failed = True
            return []

    @cached_property
    def to_order(self):
        return [pl for pl in self.plans if getattr(pl, 'needs_order', False)]

    @cached_property
    def payables(self):
        return S.payables() if self.finance else []

    @cached_property
    def receivables(self):
        if not self.finance:
            return []
        from cartera import services as C
        rows = list(C.open_receivables())
        self._aging = C.aging(rows, self.today)   # también deja `days_late` en cada fila
        return rows

    @property
    def aging(self):
        """Cartera abierta por tramos de mora (las mismas llaves de la pantalla Cartera)."""
        self.receivables
        return getattr(self, '_aging', {'current': ZERO, 'd30': ZERO, 'd60': ZERO, 'd90': ZERO, 'over': ZERO})

    @cached_property
    def soon_lots(self):
        """Lotes con saldo que vencen dentro del plazo de alerta configurado (incluye los ya vencidos)."""
        if not self.manager:
            return []
        limit = self.today + timedelta(days=self.settings.expiry_days)
        return list(Batch.objects.select_related('product').filter(physical__gt=0, expiry__lte=limit).order_by('expiry'))

    @cached_property
    def receivable_invoices(self):
        if not self.receiver:
            return []
        from inventory.recepcion import receivable_invoices
        rows = list(receivable_invoices().prefetch_related('lines'))
        for inv in rows:
            inv.pending_units = inv.expected_units - inv.received_units
        return rows

    @cached_property
    def pending_bank(self):
        """Ingresos del extracto por identificar: la misma condición de la pestaña «Por identificar» de Banco."""
        if not self.finance:
            return []
        from cartera.models import BankTransaction
        return list(BankTransaction.objects.filter(status=BankTransaction.Status.PENDING, amount__gt=0).order_by('date', 'pk'))


@dataclass
class Station:
    key: str
    order: int
    label: str
    explain: str
    rule: str
    roles: tuple
    module: str = ''
    module_label: str = ''
    allowed: bool = False
    state: str = ''
    count: int = None
    value: str = '—'
    fact: str = ''
    reason: str = ''
    records: list = field(default_factory=list)
    more: int = 0
    links: list = field(default_factory=list)
    scene: list = field(default_factory=list)
    clip: list = field(default_factory=list)
    selected: bool = False

    @property
    def state_label(self):
        return STATE_LABELS[self.state]

    @property
    def chip(self):
        return STATE_CHIPS[self.state]

    @property
    def long(self):
        """La cifra es una suma de dinero larga: se dibuja con un cuerpo menor para que no se parta."""
        return len(self.value) > 7

    @property
    def who(self):
        return profiles(self.roles)

    @property
    def summary(self):
        """Una frase para lectores de pantalla y para el copiloto."""
        if not self.allowed:
            return f'{self.label}: su perfil no abre esta estación.'
        return f'{self.label}: {self.value} {self.fact}. Estado: {self.state_label.lower()}.'


def _record(title, detail, url, chip='', chip_text='', when_text='', level=''):
    return {'title': title, 'detail': detail, 'url': url, 'chip': chip, 'chip_text': chip_text, 'when': when_text, 'level': level}


# ------------------------------------------------------------------ estaciones

def _solicitud(st, d):
    qs = S.visible_requests(d.user).filter(status='pending').order_by('needed_by', 'created_at')
    rows = list(qs[:60])
    st.count = len(rows) if len(rows) < 60 else qs.count()
    own = d.role == 'requester'
    st.value = str(st.count)
    st.fact = ('solicitud suya en revisión' if st.count == 1 else 'solicitudes suyas en revisión') if own else (
        'solicitud por atender' if st.count == 1 else 'solicitudes por atender')
    late = [r for r in rows if r.needed_by and r.needed_by < d.today]
    near = [r for r in rows if r.needed_by and 0 <= (r.needed_by - d.today).days <= 2]
    old = [r for r in rows if (d.today - timezone.localtime(r.created_at).date()).days >= 1]
    if late:
        st.state, st.reason = 'critical', f'{plural(len(late), "solicitud")} con la fecha requerida ya vencida.'
    elif near or old:
        st.state = 'warning'
        st.reason = (f'{plural(len(near), "solicitud")} se necesita{"n" if len(near) != 1 else ""} en dos días o menos.' if near
                     else f'{plural(len(old), "solicitud")} lleva{"n" if len(old) != 1 else ""} más de un día sin respuesta.')
    else:
        st.reason = 'Sin solicitudes atrasadas.' if st.count else 'No hay solicitudes en espera.'
    for r in rows[:4]:
        days = (r.needed_by - d.today).days if r.needed_by else None
        level = 'critical' if days is not None and days < 0 else 'warning' if days is not None and days <= 2 else ''
        st.records.append(_record(
            f'{r.product.qty(r.quantity)} de {r.product.name}',
            (r.purpose if own else f'{r.requester.display_name} · {r.purpose}'),
            reverse('requests') + '?estado=pending' if own else reverse('approve', args=[r.pk]),
            'warn', 'Pendiente', (f'se necesita {when(days)}' if days is not None else f'pedida {when(-(d.today - timezone.localtime(r.created_at).date()).days)}'), level))
    st.more = max(0, st.count - len(st.records))
    st.module, st.module_label = reverse('requests') + '?estado=pending', 'Mis solicitudes' if own else 'Solicitudes'
    if d.role in S.REQUESTERS:
        st.links.append(('Nueva solicitud', reverse('new_request')))
    if d.manager:
        st.links.append(('Reservas y entregas', reverse('reservations')))


def _inventario(st, d):
    plans = d.to_order
    st.count = len(plans)
    st.value = str(st.count)
    st.fact = 'referencia bajo su punto de pedido' if st.count == 1 else 'referencias bajo su punto de pedido'
    short = [pl for pl in plans if pl.coverage_days is not None and pl.coverage_days < pl.lead_days]
    empty = [pl for pl in d.plans if getattr(pl, 'available', 1) == 0 and getattr(pl, 'needs_order', False)]
    if d.plans_failed:
        st.count, st.value, st.fact = None, '—', 'no se pudo calcular la reposición'
        st.reason = 'El cálculo de reposición no respondió. Abra Reposición para ver el detalle.'
    elif empty or short:
        st.state = 'critical'
        st.reason = (f'{plural(len(empty), "referencia agotada", "referencias agotadas")}. ' if empty else '') + (
            f'{plural(len(short), "referencia")} se agotaría{"n" if len(short) != 1 else ""} antes de que llegue un pedido.' if short else '')
    elif plans:
        st.state, st.reason = 'warning', 'Hay referencias por pedir, todas con existencias para esperar la entrega.'
    else:
        st.reason = 'Todas las referencias están por encima de su punto de pedido.'
    for pl in plans[:4]:
        cover = pl.coverage_days
        level = 'critical' if (cover is not None and cover < pl.lead_days) or pl.available == 0 else 'warning'
        fallback = (f'Lo que hay y lo que viene en camino alcanza para {plural(cover, "día")}; un pedido nuevo tarda {plural(pl.lead_days, "día")}.' if cover is not None
                    else 'Sin existencias disponibles.' if pl.available == 0 else 'Por pedir.')
        detail = (getattr(pl, 'summary', '') or fallback)
        st.records.append(_record(pl.product.name, detail if len(detail) <= 190 else detail[:187].rstrip() + '…', reverse('forecast', args=[pl.product.pk]),
                                  'bad' if pl.status == 'pedir_ya' else 'warn', pl.status_label,
                                  f'alcanza {plural(cover, "día")}' if cover is not None else 'sin existencias', level))
    st.more = max(0, len(plans) - len(st.records))
    st.module, st.module_label = reverse('replenishment'), 'Reposición'
    st.links.append(('Existencias por lote', reverse('stock')))
    soon = [b for b in d.soon_lots if b.expiry >= d.today]
    if soon:
        st.links.append((f'{plural(len(soon), "lote")} por vencer en {d.settings.expiry_days} días', reverse('stock')))


def _compra(st, d):
    orders = list(PurchaseOrder.objects.filter(status__in=PurchaseOrder.OPEN).select_related('supplier').prefetch_related('lines')
                  .order_by('expected_on', 'created_at'))
    st.count = len(orders)
    st.value = str(st.count)
    sent = [o for o in orders if o.status == 'sent']
    draft = [o for o in orders if o.status == 'draft']
    st.fact = ('pedido abierto' if st.count == 1 else 'pedidos abiertos') + (f': {len(sent)} en tránsito' if sent else '')
    late = [o for o in sent if o.expected_on and o.expected_on < d.today]
    review = Invoice.objects.filter(review=Invoice.Review.PENDING).count()
    if late:
        st.state, st.reason = 'critical', f'{plural(len(late), "pedido")} {"superó" if len(late) == 1 else "superaron"} su fecha de llegada esperada.'
    elif draft or review:
        st.state = 'warning'
        st.reason = ' '.join(x for x in (
            f'{plural(len(draft), "pedido preparado", "pedidos preparados")} sin registrar su envío.' if draft else '',
            f'{plural(review, "factura")} de proveedor por revisar.' if review else '') if x)
    else:
        st.reason = 'Los pedidos enviados están dentro de su plazo.' if sent else 'No hay pedidos abiertos.'
    for o in orders[:4]:
        days = (o.expected_on - d.today).days if o.expected_on else None
        level = 'critical' if o.status == 'sent' and days is not None and days < 0 else 'warning' if o.status == 'draft' else ''
        st.records.append(_record(f'{o.number} · {o.supplier.name}', f'{plural(o.units, "unidad", "unidades")} en {plural(len(o.lines.all()), "referencia")}',
                                  reverse('purchase_order', args=[o.pk]), 'info' if o.status == 'sent' else 'warn',
                                  'En tránsito' if o.status == 'sent' else 'Por enviar',
                                  'sin fecha de llegada' if days is None else f'debía llegar {when(days)}' if days < 0 else f'llega {when(days)}', level))
    st.more = max(0, st.count - len(st.records))
    st.module, st.module_label = reverse('purchase_orders'), 'Pedidos a proveedores'
    st.links.append((f'{plural(review, "factura")} por revisar' if review else 'Facturas de proveedores', reverse('invoices') + ('?vista=revisar' if review else '')))


def _recepcion(st, d):
    rows = d.receivable_invoices
    st.count = len(rows)
    st.value = str(st.count)
    st.fact = 'factura por recibir' if st.count == 1 else 'facturas por recibir'
    waits = [(d.today - timezone.localtime(i.validated_at).date()).days if i.validated_at else 0 for i in rows]
    worst = max(waits, default=0)
    if worst > 5:
        st.state, st.reason = 'critical', f'Hay mercancía validada hace {worst} días que sigue sin recibirse.'
    elif worst > 1:
        st.state, st.reason = 'warning', f'La factura más antigua espera desde hace {worst} días.'
    else:
        st.reason = 'Lo pendiente se validó hoy o ayer.' if rows else 'No hay mercancía por recibir.'
    for inv, wait in list(zip(rows, waits))[:4]:
        st.records.append(_record(f'{inv.supplier.name} · factura {inv.number}', f'{plural(inv.pending_units, "unidad pendiente", "unidades pendientes")}',
                                  reverse('reception_invoice', args=[inv.pk]), 'info', 'Por recibir', f'validada {when(-wait)}',
                                  'critical' if wait > 5 else 'warning' if wait > 1 else ''))
    st.more = max(0, st.count - len(st.records))
    st.module, st.module_label = reverse('reception'), 'Recibir mercancía'


def _venta(st, d):
    from cartera.models import Sale
    open_sales = list(Sale.objects.filter(status__in=['draft', 'rejected', 'sending', 'uncertain']).select_related('party').order_by('created_at'))
    st.count = len(open_sales)
    st.value = str(st.count)
    st.fact = 'venta por emitir o confirmar' if st.count == 1 else 'ventas por emitir o confirmar'
    bad = [s for s in open_sales if s.status in ('rejected', 'uncertain')]
    if bad:
        st.state, st.reason = 'critical', f'{plural(len(bad), "venta")} con rechazo o envío incierto en el sistema contable simulado.'
    elif open_sales:
        st.state, st.reason = 'warning', 'Hay borradores por completar o envíos en curso.'
    else:
        st.reason = 'No hay ventas a medio camino.'
    chips = {'draft': ('', 'Borrador'), 'rejected': ('bad', 'Rechazada'), 'sending': ('info', 'Enviando'), 'uncertain': ('warn', 'Envío incierto')}
    for s in sorted(open_sales, key=lambda s: s.status not in ('rejected', 'uncertain'))[:4]:
        st.records.append(_record(f'{s.label} · {s.party.name}', f'{S.cop(s.total)} · {s.get_condition_display().lower()}', reverse('sale', args=[s.pk]),
                                  chips[s.status][0], chips[s.status][1], f'creada {when(-(d.today - timezone.localtime(s.created_at).date()).days)}',
                                  'critical' if s.status in ('rejected', 'uncertain') else 'warning'))
    st.more = max(0, st.count - len(st.records))
    today = Sale.objects.filter(status='issued', issue_date=d.today).aggregate(s=Sum('total'))
    st.module, st.module_label = reverse('sales') + ('?vista=borradores' if st.count else ''), 'Ventas'
    st.links.append((f'Emitido hoy, IVA incluido: {S.cop(today["s"] or ZERO)}', reverse('sales') + '?vista=emitidas'))


def _cobro(st, d):
    txns = d.pending_bank
    total = sum((t.amount for t in txns), ZERO)
    st.count = len(txns)
    st.value = S.cop(total)
    st.fact = f'por identificar en {plural(st.count, "ingreso")} del banco' if st.count else 'por identificar en el banco'
    rows = d.receivables
    late = [r for r in rows if r.days_late > 0]
    late_total = sum((r.balance for r in late), ZERO)
    over = [r for r in late if r.days_late > 90]
    old = [t for t in txns if (d.today - t.date).days > 3]
    if over:
        st.state, st.reason = 'critical', f'{plural(len(over), "factura")} de clientes con más de 90 días de mora.'
    elif old or late:
        st.state = 'warning'
        st.reason = ' '.join(x for x in (
            f'{plural(len(old), "ingreso")} sin identificar desde hace más de 3 días.' if old else '',
            f'Cartera vencida: {S.cop(late_total)} en {plural(len(late), "factura")}.' if late else '') if x)
    else:
        st.reason = 'Sin cartera vencida ni ingresos atrasados por identificar.'
    for t in txns[:3]:
        age = (d.today - t.date).days
        st.records.append(_record(f'{S.cop(t.amount)} · {t.holder_name or t.description or "Ingreso sin titular"}', (t.reference or t.description or 'Sin referencia')[:120],
                                  reverse('bank_txn', args=[t.pk]), 'warn', 'Por identificar', f'recibido {when(-age)}', 'warning' if age > 3 else ''))
    if late:
        worst = max(r.days_late for r in late)
        st.records.append(_record(f'Cartera vencida: {S.cop(late_total)}', f'{plural(len(late), "factura")} de clientes; la más antigua lleva {plural(worst, "día")} de mora.',
                                  reverse('portfolio'), 'bad', 'Vencida', '', 'critical' if worst > 90 else 'warning'))
    st.more = max(0, st.count - min(st.count, 3))
    st.module, st.module_label = reverse('bank'), 'Banco y recaudos'
    st.links.append(('Cartera', reverse('portfolio')))


DEFINITIONS = (
    ('solicitud', 'Solicitud', 'Un área pide unidades de una referencia y dice para qué las necesita. Inventario decide con qué lote atenderla.',
     'Atención si una solicitud lleva más de un día sin respuesta o se necesita en dos días o menos; crítico si su fecha requerida ya pasó.',
     (*S.REQUESTERS, *S.MANAGERS), _solicitud),
    ('inventario', 'Inventario', 'Las existencias por lote se comparan con el consumo esperado. Cuando lo disponible baja del punto de pedido, la referencia entra en la lista de reposición.',
     'Atención si hay referencias por pedir; crítico si alguna está agotada o se agotaría antes de que llegue el pedido.',
     S.MANAGERS, _inventario),
    ('compra', 'Compra', 'El pedido al proveedor se prepara con las cantidades sugeridas y queda como envío simulado. La factura del proveedor lo cierra.',
     'Atención si hay pedidos preparados sin enviar o facturas por revisar; crítico si un pedido enviado superó su fecha de llegada.',
     S.DOCUMENTS, _compra),
    ('recepcion', 'Recepción', 'La mercancía se recibe contra la factura validada: cada caja suma unidades a su lote y queda la trazabilidad de la entrada.',
     'Atención si una factura validada espera más de un día; crítico si espera más de cinco.',
     S.RECEIVERS, _recepcion),
    ('venta', 'Venta', 'Cada venta sale del lote que vence primero y se envía al sistema contable simulado, que responde con el número del documento.',
     'Atención si hay borradores o envíos en curso; crítico si hay ventas rechazadas o con envío incierto.',
     S.SALES, _venta),
    ('cobro', 'Cobro', 'Los ingresos del extracto bancario se cruzan con las facturas abiertas de cada cliente y se aplican a la cartera.',
     'Atención si hay ingresos sin identificar hace más de tres días o cartera vencida; crítico si alguna factura supera 90 días de mora.',
     S.FINANCE, _cobro),
)
KEYS = tuple(k for k, *_ in DEFINITIONS)


def stations(user, datos=None, selected=None):
    """Las seis estaciones en orden, con su estado, conteo y hasta cuatro registros reales para el perfil dado."""
    d = datos or Datos(user)
    out = []
    for i, (key, label, explain, rule, roles, build) in enumerate(DEFINITIONS, start=1):
        st = Station(key, i, label, explain, rule, tuple(dict.fromkeys(roles)), scene=SCENES[key], clip=CLIP)
        st.allowed = user.is_authenticated and user.is_active and user.role in roles
        if st.allowed:
            build(st, d)
        else:
            st.fact = 'sin acceso con su perfil'
        out.append(st)
    chosen = pick(out, selected)
    for st in out:
        st.selected = st is chosen
    return out


def pick(items, key=None):
    """La estación abierta: la pedida en `?etapa=`, si existe; si no, la más urgente que el perfil puede abrir."""
    by_key = {st.key: st for st in items}
    if key in by_key:
        return by_key[key]
    allowed = [st for st in items if st.allowed]
    for state in ('critical', 'warning'):
        for st in allowed:
            if st.state == state:
                return st
    return allowed[0] if allowed else items[0]


# --------------------------------------------------------------------- dibujo
# Cada estación es una plataforma atravesada por un conducto, con un objeto encima: tablilla, estante, camión,
# muelle, caja con factura y monedas. Proyección oblicua: el frente se dibuja sin deformar y la profundidad sube
# en diagonal, así el conducto queda horizontal y empata de una estación a la siguiente.
VIEW = (0, 22, 200, 100)
BASE, KX, KY = 112.0, 0.433, 0.25
DUCT_D, DUCT_W, DUCT_H = 24, 12, 6
PLAT_X, PLAT_W, PLAT_D, PLAT_H = 34, 116, 60, 12
TOP = PLAT_H


def _p(x, d, z):
    return x + d * KX, BASE - z - d * KY


def _pts(points):
    return ' '.join(f'{x:.1f},{y:.1f}' for x, y in points)


def _n(v):
    return f'{v:.1f}'


def _box(x, d, z, w, dep, h, tone=''):
    """Las tres caras visibles de un bloque: frente, tapa y costado derecho."""
    suffix = f' {tone}' if tone else ''
    return [
        {'t': 'polygon', 'c': 'fl-side' + suffix, 'p': _pts([_p(x + w, d, z), _p(x + w, d + dep, z), _p(x + w, d + dep, z + h), _p(x + w, d, z + h)])},
        {'t': 'polygon', 'c': 'fl-top' + suffix, 'p': _pts([_p(x, d, z + h), _p(x + w, d, z + h), _p(x + w, d + dep, z + h), _p(x, d + dep, z + h)])},
        {'t': 'polygon', 'c': 'fl-front' + suffix, 'p': _pts([_p(x, d, z), _p(x + w, d, z), _p(x + w, d, z + h), _p(x, d, z + h)])},
    ]


def _front_rect(x, d, z, w, h, cls, rx=0):
    """Rectángulo sobre un plano frontal (no se deforma)."""
    sx, sy = _p(x, d, z + h)
    return {'t': 'rect', 'c': cls, 'x': _n(sx), 'y': _n(sy), 'w': _n(w), 'h': _n(h), 'rx': _n(rx)}


def _front_line(x1, z1, x2, z2, d, cls='fl-ink'):
    (a, b), (c, e) = _p(x1, d, z1), _p(x2, d, z2)
    return {'t': 'line', 'c': cls, 'x1': _n(a), 'y1': _n(b), 'x2': _n(c), 'y2': _n(e)}


def _circle(x, d, z, r, cls):
    cx, cy = _p(x, d, z)
    return {'t': 'circle', 'c': cls, 'cx': _n(cx), 'cy': _n(cy), 'r': _n(r)}


def _coin(x, d, z, r, h):
    """Moneda acostada: canto y tapa elíptica."""
    cx, cy = _p(x, d, z)
    ry = r * 0.34
    side = f'M{cx - r:.1f},{cy - h:.1f}V{cy:.1f}A{r:.1f},{ry:.1f} 0 0 0 {cx + r:.1f},{cy:.1f}V{cy - h:.1f}Z'
    return [{'t': 'path', 'c': 'fl-front is-coin', 'd': side},
            {'t': 'ellipse', 'c': 'fl-top is-coin', 'cx': _n(cx), 'cy': _n(cy - h), 'rx': _n(r), 'ry': _n(ry)}]


def _glyph_solicitud():
    s = _box(68, 26, TOP, 50, 5, 56, 'is-obj')
    s.append(_front_rect(85, 26, TOP + 52, 16, 8, 'fl-accent', 2))
    for i, width in enumerate((34, 34, 22)):
        s.append(_front_line(76, TOP + 40 - i * 11, 76 + width, TOP + 40 - i * 11, 26))
    s.append(_front_rect(76, 26, TOP + 6, 9, 9, 'fl-accent-line', 1.5))
    s.append(_front_line(90, TOP + 10.5, 108, TOP + 10.5, 26))
    return s


def _glyph_inventario():
    s = []
    for x in (52, 129):                      # parales de atrás
        s += _box(x, 38, TOP, 3, 3, 56, 'is-post')
    levels = ((TOP, [(58, 20, 16), (80, 26, 13), (112, 14, 17)]), (TOP + 21, [(60, 28, 14), (96, 18, 12)]), (TOP + 39, [(72, 22, 13)]))
    for i, (z, boxes) in enumerate(levels):
        if i:
            s += _box(52, 14, z - 3, 80, 27, 3, 'is-obj')
        for x, w, h in boxes:
            s += _box(x, 19, z, w, 16, h, 'is-accent' if (i, x) in ((0, 80), (1, 96)) else 'is-obj')
    s += _box(52, 14, TOP + 53, 80, 27, 3, 'is-obj')
    for x in (52, 129):                      # parales del frente
        s += _box(x, 14, TOP, 3, 3, 56, 'is-post')
    return s


def _glyph_compra():
    floor = TOP + 7
    s = _box(48, 18, floor, 56, 26, 34, 'is-obj')
    s += _box(104, 18, floor, 24, 26, 24, 'is-accent')
    s.append(_front_rect(110, 18, floor + 10, 13, 10, 'fl-window', 1.5))
    s.append(_front_rect(56, 18, floor + 13, 20, 13, 'fl-accent-line', 1.5))
    s.append(_front_line(60, floor + 21.5, 72, floor + 21.5, 18))
    s.append(_front_line(60, floor + 17.5, 68, floor + 17.5, 18))
    for x in (64, 114):
        s.append(_circle(x, 18, TOP + 7, 7, 'fl-wheel'))
        s.append(_circle(x, 18, TOP + 7, 2.4, 'fl-hub'))
    return s


def _glyph_recepcion():
    s = _box(54, 26, TOP, 84, 28, 54, 'is-obj')
    s.append(_front_rect(72, 26, TOP, 42, 38, 'fl-hole', 1.5))
    for i in range(3):                       # persiana a medio abrir
        s.append(_front_line(72, TOP + 34 - i * 5, 114, TOP + 34 - i * 5, 26, 'fl-ink is-light'))
    s.append(_front_rect(72, 26, TOP + 22, 42, 16, 'fl-shutter', 1.5))
    s.append(_front_line(60, TOP + 46, 132, TOP + 46, 26, 'fl-accent-stroke'))
    s += _box(82, 6, TOP, 20, 15, 15, 'is-accent')
    s += _box(106, 9, TOP, 13, 11, 10, 'is-obj')
    return s


def _glyph_venta():
    s = _box(58, 22, TOP, 40, 28, 30, 'is-obj')
    s.append(_front_line(78, TOP, 78, TOP + 30, 22, 'fl-ink is-light'))
    (a, b), (c, e) = _p(78, 22, TOP + 30), _p(78, 50, TOP + 30)
    s.append({'t': 'line', 'c': 'fl-ink is-light', 'x1': _n(a), 'y1': _n(b), 'x2': _n(c), 'y2': _n(e)})
    s += _box(104, 12, TOP, 30, 3, 44, 'is-obj')
    for i, width in enumerate((18, 18, 12)):
        s.append(_front_line(110, TOP + 34 - i * 8, 110 + width, TOP + 34 - i * 8, 12))
    s.append(_front_line(110, TOP + 8, 128, TOP + 8, 12, 'fl-accent-stroke'))
    return s


def _glyph_cobro():
    s = []
    for i in range(4):
        s += _coin(76, 30, TOP + i * 6, 21, 6)
    cx, cy = _p(126, 20, TOP + 19)
    s.append({'t': 'circle', 'c': 'fl-coin-face', 'cx': _n(cx), 'cy': _n(cy), 'r': '19.0'})
    s.append({'t': 'circle', 'c': 'fl-accent-line', 'cx': _n(cx), 'cy': _n(cy), 'r': '13.5'})
    s.append({'t': 'text', 'c': 'fl-sign', 'x': _n(cx), 'y': _n(cy + 6.5), 'text': '$'})
    return s


def _scene(glyph):
    front = DUCT_D * KX
    s = _box(-14, DUCT_D, 0, PLAT_X + 14, DUCT_W, DUCT_H, 'is-duct')
    s += _box(PLAT_X, 0, 0, PLAT_W, PLAT_D, PLAT_H, 'is-base')
    s.append({'t': 'rect', 'c': 'fl-lamp', 'x': _n(PLAT_X + 9), 'y': _n(BASE - PLAT_H + 3.5), 'w': '22.0', 'h': '5.0', 'rx': '2.5'})
    s += glyph
    s += _box(PLAT_X + PLAT_W, DUCT_D, 0, 70, DUCT_W, DUCT_H, 'is-duct')
    # Sentido del flujo: una punta fija sobre el tramo que sale hacia la estación siguiente.
    ax, ay = PLAT_X + PLAT_W + front + 19, BASE - DUCT_H / 2 - DUCT_D * KY
    s.append({'t': 'path', 'c': 'fl-arrow', 'd': f'M{ax:.1f},{ay - 2.6:.1f}L{ax + 3.2:.1f},{ay:.1f}L{ax:.1f},{ay + 2.6:.1f}'})
    return s


# Tramos visibles del conducto (a la izquierda de la plataforma y al salir por su costado): recortan el pulso.
CLIP = [{'x': '0.0', 'y': _n(BASE - DUCT_H - DUCT_D * KY - 2), 'w': _n(PLAT_X), 'h': '12.0'},
        {'x': _n(PLAT_X + PLAT_W + DUCT_D * KX), 'y': _n(BASE - DUCT_H - DUCT_D * KY - 2), 'w': '60.0', 'h': '12.0'}]
PULSE = {'y': _n(BASE - DUCT_H / 2 - DUCT_D * KY - 1.5), 'w': '14.0', 'h': '3.0'}
SCENES = {'solicitud': _scene(_glyph_solicitud()), 'inventario': _scene(_glyph_inventario()), 'compra': _scene(_glyph_compra()),
          'recepcion': _scene(_glyph_recepcion()), 'venta': _scene(_glyph_venta()), 'cobro': _scene(_glyph_cobro())}
