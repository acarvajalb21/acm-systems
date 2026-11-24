"""Política de reposición por referencia: cuándo y cuánto pedir, con los movimientos de la plataforma.

Qué es cada pieza (para no confundirlas en pantalla):
- **Método estadístico o modelo entrenado**: el pronóstico de consumo (`series.fit`), elegido por referencia con
  una ventana de selección y evaluado en una ventana de prueba que no se usó para elegir.
- **Regla automática**: la política (R, s, S) que convierte ese pronóstico en una sugerencia de pedido, el recorte
  por presupuesto y el tope por vencimiento. No aprende: aplica parámetros visibles y editables.

Para cada referencia:
1. Serie semanal: salidas observadas (despachos y entregas) y, aparte, la demanda no atendida registrada
   (solicitudes rechazadas). Las semanas que terminaron sin existencias y sin salidas están censuradas: no se sabe
   cuánto se habría consumido, así que se excluyen del ajuste y de la evaluación. Las salidas no son la demanda.
2. Horizonte de la decisión: H = ceil((tiempo de entrega + revisión) / 7) semanas. El pronóstico se evalúa a una
   semana y acumulado a H semanas.
3. Tiempo de entrega: mediana de (recepción − fecha del pedido) de las facturas del proveedor habitual; si no hay
   dato, el parámetro.
4. Punto de pedido s = consumo esperado en el periodo de protección + inventario de seguridad. La seguridad es el
   cuantil empírico del nivel de servicio sobre los errores acumulados de selección (con 12 o más errores) o, si
   no alcanzan, la aproximación normal z · RMSE · √H. Nivel objetivo S = s + consumo del ciclo de pedido.
5. Posición = disponible (físico − reservado de lotes vigentes) + por recibir de facturas no anuladas + pedidos
   abiertos sin factura vinculada. Cada unidad cuenta una sola vez y las reservas no se vuelven a restar.
6. Cantidad: lleva la posición a S, redondeada al empaque y limitada por lo que alcanza a usarse antes del
   vencimiento. Con presupuesto, `budget_plan` prioriza por urgencia y recorta en múltiplos de empaque.

Es una sugerencia con sus supuestos a la vista, calculada con datos sintéticos; la decisión de pedir es de una
persona y nada aquí demuestra eficacia en una operación real.
"""
import copy
import json
import logging
import threading
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from math import ceil, floor, log10, sqrt
from statistics import median
from django.db.models import Count, F, Max, Min, Q, Sum
from django.utils import timezone
from inventory.models import (Product, Batch, Movement, SupplyRequest, Invoice, InvoiceLine, Receipt, Supplier, PurchaseOrder,
                              PurchaseOrderLine, money)
from inventory.services import OUTFLOWS
from inventory import compras
from . import series
from .models import ForecastRecord

Z = {90: 1.2816, 91: 1.3408, 92: 1.4051, 93: 1.4758, 94: 1.5548, 95: 1.6449, 96: 1.7507, 97: 1.8808, 98: 2.0537, 99: 2.3263}
Z80 = 1.2816
DEFAULTS = {'nivel_servicio': 95, 'revision_dias': 7, 'ciclo_dias': 14, 'tiempo_entrega_dias': 7, 'incluir_rechazadas': 1, 'margen_vencimiento_dias': 30,
            'presupuesto': 0}
BOUNDS = {'nivel_servicio': (90, 99), 'revision_dias': (1, 30), 'ciclo_dias': (7, 90), 'tiempo_entrega_dias': (1, 60), 'incluir_rechazadas': (0, 1),
          'margen_vencimiento_dias': (0, 180), 'presupuesto': (0, 10 ** 12)}
EDITABLE = ('nivel_servicio', 'presupuesto')   # supuestos que se editan desde la pantalla de reposición
STATUS = {'pedir_ya': 'Pedir ya', 'pedir_pronto': 'Pedir pronto', 'ok': 'Cubierta', 'exceso': 'Más de lo que se usa', 'sin_consumo': 'Sin consumo reciente',
          'sin_datos': 'Sin historial', 'no_calculable': 'No calculable'}
ORDER = ['pedir_ya', 'pedir_pronto', 'exceso', 'ok', 'sin_consumo', 'sin_datos', 'no_calculable']
# Una tasa esperada por debajo de este umbral (unidades por semana) se trata como «sin consumo reciente»: un
# suavizado exponencial tras muchas semanas en cero deja un residuo positivo ínfimo que no es un pronóstico útil.
MIN_RATE = 0.05
# Ninguna cobertura se proyecta más allá de este tope: con consumos muy bajos la división da cifras sin sentido.
MAX_COVERAGE_DAYS = 3650
log = logging.getLogger(__name__)
SPARK_WEEKS = 12
CHART_WEEKS = 26
ZERO = Decimal('0.00')


def current_params():
    from agentes.models import AgentConfig
    stored = AgentConfig.objects.filter(key='reposicion').values_list('params', flat=True).first() or {}
    return clean_params(stored)


def clean_params(stored):
    out = dict(DEFAULTS)
    for k, (lo, hi) in BOUNDS.items():
        try:
            out[k] = min(max(int((stored or {}).get(k, DEFAULTS[k])), lo), hi)
        except (TypeError, ValueError):
            pass
    return out


def save_assumptions(user, changes):
    """Edita nivel de servicio y presupuesto (administración y dirección). Queda en auditoría."""
    from agentes.models import AgentConfig
    from inventory import services as S
    S.require(user, S.ADMINS)
    cfg, _ = AgentConfig.objects.get_or_create(key='reposicion')
    before = clean_params(cfg.params)
    after = clean_params({**before, **{k: v for k, v in changes.items() if k in EDITABLE}})
    done = {k: (before[k], after[k]) for k in EDITABLE if before[k] != after[k]}
    if done:
        cfg.params, cfg.updated_by = after, user
        cfg.save()
        S.event(user, 'Supuestos de reposición ajustados', 'agent-reposicion', '; '.join(f'{k}: {a}→{b}' for k, (a, b) in done.items())[:300])
    return done


def week_start(d):
    return d - timedelta(days=d.weekday())


MONTHS = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']


def month_add(m, k):
    y, mo = divmod(m.month - 1 + k, 12)
    return m.replace(year=m.year + y, month=mo + 1, day=1)


@dataclass
class Series:
    weeks: list = field(default_factory=list)
    demand: list = field(default_factory=list)    # salidas observadas
    unmet: list = field(default_factory=list)     # demanda no atendida registrada (solicitudes rechazadas)
    stockout: list = field(default_factory=list)  # semana censurada: terminó sin existencias y sin salidas
    monthly: dict = field(default_factory=dict)   # meses completos → unidades (incluye ceros)
    this_month: int = 0                            # lo que va del mes en curso

    @property
    def usable(self):
        return [d + u for d, u, s in zip(self.demand, self.unmet, self.stockout) if not s]

    @property
    def usable_weeks(self):
        return [w for w, s in zip(self.weeks, self.stockout) if not s]

    @property
    def observed(self):
        return [d + u for d, u in zip(self.demand, self.unmet)]

    def at(self, week):
        try:
            i = self.weeks.index(week)
        except ValueError:
            return None
        return self.demand[i] + self.unmet[i], self.stockout[i]


def history(products, today, include_rejected=True):
    """Serie semanal por referencia hasta la última semana completa, con las semanas sin existencias marcadas."""
    last = week_start(today) - timedelta(days=7)
    per = defaultdict(list)
    for pid, kind, qty, at in Movement.objects.filter(batch__product__in=products).values_list('batch__product_id', 'kind', 'quantity', 'created_at').order_by('created_at', 'id'):
        per[pid].append((timezone.localtime(at).date(), kind, qty))
    rejected = defaultdict(list)
    if include_rejected:
        for pid, qty, at in SupplyRequest.objects.filter(product__in=products, status='rejected').values_list('product_id', 'quantity', 'created_at'):
            rejected[pid].append((timezone.localtime(at).date(), qty))
    out = {}
    for p in products:
        rows = per.get(p.pk, [])
        if not rows or week_start(rows[0][0]) > last:
            out[p.pk] = Series()
            continue
        weeks, w = [], week_start(rows[0][0])
        while w <= last:
            weeks.append(w)
            w += timedelta(days=7)
        idx = {w: i for i, w in enumerate(weeks)}
        demand, unmet, end = [0] * len(weeks), [0] * len(weeks), [None] * len(weeks)
        balance = 0
        for d, kind, qty in rows:
            balance += qty
            i = idx.get(week_start(d))
            if i is not None:
                if kind in OUTFLOWS:
                    demand[i] += -qty
                end[i] = balance
        prev = 0
        for i in range(len(weeks)):
            end[i] = prev if end[i] is None else end[i]
            prev = end[i]
        for d, qty in rejected.get(p.pk, []):
            i = idx.get(week_start(d))
            if i is not None:
                unmet[i] += qty
        by_month = defaultdict(int)
        for d, kind, qty in rows:
            if kind in OUTFLOWS:
                by_month[d.replace(day=1)] += -qty
        for d, qty in rejected.get(p.pk, []):
            by_month[d.replace(day=1)] += qty
        current = today.replace(day=1)
        monthly, m = {}, month_add(rows[0][0].replace(day=1), 1)   # el primer mes suele estar incompleto
        while m < current:
            monthly[m] = by_month.get(m, 0)
            m = month_add(m, 1)
        out[p.pk] = Series(weeks, demand, unmet, [end[i] <= 0 and demand[i] == 0 for i in range(len(weeks))], monthly, by_month.get(current, 0))
    return out


def _norm(text):
    import unicodedata
    return ''.join(c for c in unicodedata.normalize('NFKD', text or '') if not unicodedata.combining(c)).lower()


def suppliers_for(products):
    """Proveedor habitual: el de la última factura de la referencia; si no hay, el que coincide con el fabricante."""
    out = {}
    for l in InvoiceLine.objects.filter(product__in=products).exclude(invoice__review=Invoice.Review.VOID).select_related('invoice__supplier').order_by('-invoice__issue_date', '-pk'):
        out.setdefault(l.product_id, l.invoice.supplier)
    active = list(Supplier.objects.filter(active=True))
    for p in products:
        if p.pk in out or not p.manufacturer:
            continue
        key = _norm(p.manufacturer).split()[0] if _norm(p.manufacturer).split() else ''
        match = [s for s in active if key and (key in _norm(s.name) or _norm(s.name).split()[0] in _norm(p.manufacturer))]
        if len(match) == 1:
            out[p.pk] = match[0]
    return out


def lead_times():
    """Mediana de días entre la fecha del pedido y la primera recepción, por proveedor."""
    obs = defaultdict(list)
    for inv in Invoice.objects.exclude(review=Invoice.Review.VOID).exclude(ordered_on=None).prefetch_related('receipts'):
        first = min((r.received_on for r in inv.receipts.all()), default=None)
        if first and first >= inv.ordered_on:
            obs[inv.supplier_id].append((first - inv.ordered_on).days)
    return {sid: (int(round(median(v))), len(v)) for sid, v in obs.items()}


def shelf_lives(products, today):
    """Vida útil al recibir (vencimiento − recepción), mediana por referencia; respaldo: lotes actuales."""
    obs = defaultdict(list)
    for pid, expiry, at in Movement.objects.filter(kind='receipt', batch__product__in=products).values_list('batch__product_id', 'batch__expiry', 'created_at'):
        obs[pid].append((expiry - timezone.localtime(at).date()).days)
    current = defaultdict(list)
    for pid, expiry in Batch.objects.filter(product__in=products, expiry__gte=today).values_list('product_id', 'expiry'):
        current[pid].append((expiry - today).days)
    out = {}
    for p in products:
        if obs.get(p.pk):
            out[p.pk] = max(int(median(obs[p.pk])), 30)
        else:
            out[p.pk] = max(int(median(current[p.pk])), 30) if current.get(p.pk) else 365
    return out


@dataclass
class Plan:
    product: object
    supplier: object = None
    status: str = 'sin_datos'
    rate: float = 0.0                 # consumo semanal esperado en el periodo de protección
    sigma: float = 0.0                # RMSE de selección a una semana (con piso), para la aproximación normal
    method: str = ''
    label: str = ''
    kind: str = 'estadistico'         # 'estadistico' | 'entrenado'
    weeks: int = 0                    # semanas utilizables
    enough: bool = False              # hubo historial para elegir y probar un método
    improvement: float = None         # % de reducción del error de PRUEBA frente al promedio simple (puede ser negativo)
    mae: float = None                 # error de selección del método elegido (criterio de elección)
    mae_base: float = None
    horizon_weeks: int = 1            # H: semanas del periodo de protección
    forecast_week: float = 0.0        # pronóstico de la próxima semana
    forecast_h: float = 0.0           # pronóstico acumulado de H semanas
    interval_week: dict = None        # {'low', 'high', 'kind': 'empirico' | 'aproximado', 'n_errors'}
    interval_h: dict = None
    fit: dict = field(default_factory=dict)        # evaluación completa (`series.fit`)
    safety_method: str = ''           # 'empirico' | 'normal'
    available: int = 0
    in_transit: int = 0
    on_order: int = 0
    position: int = 0
    physical: int = 0                 # físico de lotes vigentes
    reserved: int = 0                 # reservado en lotes vigentes (ya descontado de `available`)
    pending_requests: int = 0         # solicitudes pendientes sin reservar: se muestran, no se restan
    lead_days: int = 0
    lead_n: int = 0
    protection_days: int = 0
    expected_demand: float = 0.0
    safety: float = 0.0
    reorder_point: float = 0.0
    target: float = 0.0
    quantity: int = 0
    days_until: int = None
    order_on: object = None
    coverage_days: int = None         # acotada a MAX_COVERAGE_DAYS (`coverage_capped`)
    coverage_capped: bool = False     # la cobertura supera el tope: «más de 10 años», sin fecha de agotamiento
    runout_on: object = None          # fecha estimada de agotamiento de la posición (None si no es previsible)
    idle_weeks: int = 0               # semanas utilizables seguidas sin salidas, hasta la última cerrada
    error: str = ''                   # motivo cuando la referencia no se pudo calcular
    urgency_days: int = None          # días de cobertura − tiempo de entrega (negativo: se agotaría antes de recibir)
    shelf_days: int = 0
    capped: bool = False
    at_risk: int = 0
    excess: int = 0        # unidades de la posición (incluye lo que viene) que no se alcanzarían a usar antes del vencimiento
    stockout_weeks: int = 0
    observed_units: int = 0           # salidas observadas en todo el historial
    unmet_units: int = 0
    total_weeks: int = 0
    unit_cost: Decimal = None
    reasons: list = field(default_factory=list)
    spark: list = field(default_factory=list)
    accuracy: dict = None
    evidence: dict = field(default_factory=dict)   # hechos en lenguaje llano para la pantalla
    summary: str = ''                               # la recomendación en una frase
    series: object = None                           # la serie semanal completa (para el gráfico)
    today: object = None

    @property
    def status_label(self):
        return STATUS[self.status]

    @property
    def estimated_cost(self):
        return money(self.unit_cost * self.quantity) if self.unit_cost is not None and self.quantity else None

    @property
    def needs_order(self):
        return self.status in ('pedir_ya', 'pedir_pronto') and self.quantity > 0

    @property
    def rate_label(self):
        return f'{self.rate:.1f}'.replace('.', ',')

    @property
    def coverage_label(self):
        """Cobertura para mostrar: «23 días», «más de 10 años» o cadena vacía si no aplica."""
        if self.coverage_days is None:
            return ''
        return f'más de {MAX_COVERAGE_DAYS // 365} años' if self.coverage_capped else f'{self.coverage_days} días'

    @property
    def covers(self):
        """La misma cobertura dentro de una frase: «unos 23 días» o «para más de 10 años»."""
        if self.coverage_days is None:
            return ''
        return f'para más de {MAX_COVERAGE_DAYS // 365} años' if self.coverage_capped else f'unos {self.coverage_days} días'

    @property
    def test(self):
        """Error de PRUEBA del pronóstico semanal (ventana que no se usó para elegir): {'mae', 'bias', 'wape', 'n'} o None."""
        m = (self.fit.get('test') or {}).get('week') if self.fit else None
        if m is None or not m.n:
            return None
        return {'mae': m.mae, 'bias': m.bias, 'wape': m.wape * 100 if m.wape is not None else None, 'wape_defined': m.wape is not None, 'n': m.n}

    @property
    def test_h(self):
        """Error de PRUEBA del pronóstico acumulado al horizonte de la decisión, o None si no hubo orígenes."""
        m = (self.fit.get('test') or {}).get('h') if self.fit else None
        if m is None or not m.n:
            return None
        return {'mae': m.mae, 'bias': m.bias, 'wape': m.wape * 100 if m.wape is not None else None, 'wape_defined': m.wape is not None, 'n': m.n}

    @property
    def severity(self):
        return {'pedir_ya': 'is-critical', 'pedir_pronto': 'is-warning', 'ok': 'is-ok', 'no_calculable': 'is-warning'}.get(self.status, '')

    @property
    def chip(self):
        return {'pedir_ya': 'bad', 'pedir_pronto': 'warn', 'exceso': 'info', 'ok': 'ok', 'no_calculable': 'warn'}.get(self.status, 'plain')


def _n(x, decimals=1):
    return f'{x:.{decimals}f}'.replace('.', ',')


def _round_up(q, pack):
    return int(ceil(q / pack) * pack) if q > 0 else 0


def _round_down(q, pack):
    return int(floor(q / pack) * pack) if q > 0 else 0


# ------------------------------------------------------------------ memoria del cálculo

_CACHE = OrderedDict()
_LOCK = threading.Lock()
_GENERATION = [0]
CACHE_SIZE = 6
stats = {'hits': 0, 'misses': 0}


def invalidate(*args, **kwargs):
    """Olvida los planes memorizados (lo llaman las señales de los modelos que intervienen)."""
    _GENERATION[0] += 1


def fingerprint(products):
    """Huella barata de los datos que intervienen en la sugerencia: si no cambia, el resultado memorizado sirve."""
    mv = Movement.objects.aggregate(n=Count('pk'), q=Sum('quantity'), a=Max('created_at'), b=Min('created_at'))
    lots = hash(tuple(Batch.objects.order_by('pk').values_list('pk', 'physical', 'reserved', 'expiry')))
    lines = InvoiceLine.objects.aggregate(n=Count('pk'), q=Sum('quantity'), r=Sum('received'), m=Max('pk'))
    inv = Invoice.objects.aggregate(n=Count('pk'), m=Max('pk'), void=Count('pk', filter=Q(review=Invoice.Review.VOID)),
                                    ok=Count('pk', filter=Q(review=Invoice.Review.VALIDATED)), o=Max('ordered_on'))
    rec = Receipt.objects.aggregate(n=Count('pk'), m=Max('pk'), d=Max('received_on'))
    po = PurchaseOrder.objects.aggregate(n=Count('pk'), m=Max('pk'), open=Count('pk', filter=Q(status__in=PurchaseOrder.OPEN)),
                                         linked=Count('invoice'), c=Max('closed_at'))
    pol = PurchaseOrderLine.objects.aggregate(n=Count('pk'), q=Sum('quantity'))
    req = SupplyRequest.objects.aggregate(n=Count('pk'), rej=Count('pk', filter=Q(status='rejected')), pend=Count('pk', filter=Q(status='pending')),
                                          u=Max('updated_at'))
    sup = Supplier.objects.aggregate(n=Count('pk'), act=Count('pk', filter=Q(active=True)), m=Max('pk'))
    prod = tuple((p.pk, p.name, p.unit, p.pack_size, p.minimum, p.manufacturer, p.active) for p in products)
    return hash((tuple(sorted(mv.items(), key=str)), lots, *(tuple(sorted(d.items())) for d in (lines, inv, rec, po, pol, req, sup)), prod))


def plans(p=None, today=None, products=None, use_cache=True):
    """Sugerencia de reposición de cada referencia activa (o de `products`), ordenada por prioridad.

    El modelo global se ajusta siempre con todas las referencias activas, de modo que pedir el plan de una sola
    referencia da lo mismo que verla en la lista completa. El resultado se memoriza con una huella de los datos
    (`fingerprint`), la fecha y los parámetros; `use_cache=False` obliga a recalcular.
    """
    p = clean_params(p) if p is not None else current_params()
    today = today or timezone.localdate()
    universe = list(Product.objects.filter(active=True).order_by('name'))
    wanted = None
    if products is not None:
        wanted = [x.pk for x in products]
        known = {x.pk for x in universe}
        universe += [x for x in products if x.pk not in known]
    computed = None
    key = None
    if use_cache:
        key = (today, tuple(sorted((k, v) for k, v in p.items() if k != 'presupuesto')), _GENERATION[0], fingerprint(universe))
        with _LOCK:
            computed = _CACHE.get(key)
            if computed is not None:
                _CACHE.move_to_end(key)
                stats['hits'] += 1
    if computed is None:
        stats['misses'] += 1
        computed = _compute(p, today, universe)
        if use_cache and not any(pl.status == 'no_calculable' for pl in computed):   # un fallo no se memoriza: se reintenta en la siguiente consulta
            with _LOCK:
                _CACHE[key] = computed
                while len(_CACHE) > CACHE_SIZE:
                    _CACHE.popitem(last=False)
    out = computed if wanted is None else [pl for pl in computed if pl.product.pk in set(wanted)]
    return [copy.copy(pl) for pl in out]   # quien recibe puede anotar el plan sin alterar lo memorizado


def _compute(p, today, products):
    hist = history(products, today, bool(p['incluir_rechazadas']))
    supplier_of = suppliers_for(products)
    leads = lead_times()
    shelves = shelf_lives(products, today)
    ordered = compras.on_order(products)
    from cartera.results import Costs
    costs = Costs().by_product
    usable = Q(expiry__gte=today)
    lots = {r['product_id']: r for r in Batch.objects.filter(product__in=products).values('product_id').annotate(
        physical=Sum('physical', filter=usable), reserved=Sum('reserved', filter=usable))}
    transit = {r['product_id']: r['u'] for r in InvoiceLine.objects.filter(product__in=products).exclude(invoice__review=Invoice.Review.VOID)
               .values('product_id').annotate(u=Sum(F('quantity') - F('received')))}
    pending = {r['product_id']: r['u'] for r in SupplyRequest.objects.filter(product__in=products, status='pending').values('product_id').annotate(u=Sum('quantity'))}
    live = defaultdict(list)
    for b in Batch.objects.filter(product__in=products, physical__gt=0, expiry__gte=today).order_by('expiry'):
        live[b.product_id].append(b)
    z = Z[p['nivel_servicio']]
    now_week = week_start(today)
    ridge = series.GlobalRidge({pr.pk: (hist[pr.pk].usable, hist[pr.pk].usable_weeks) for pr in products if pr.active and len(hist[pr.pk].usable) > series.WARMUP})
    out = []
    for prod in products:
        s = hist[prod.pk]
        plan = Plan(prod, supplier_of.get(prod.pk), series=s, today=today)
        lot = lots.get(prod.pk) or {}
        plan.physical, plan.reserved = lot.get('physical') or 0, lot.get('reserved') or 0
        plan.available = max(plan.physical - plan.reserved, 0)
        plan.in_transit = max(transit.get(prod.pk, 0) or 0, 0)
        plan.on_order = ordered.get(prod.pk, 0)
        plan.position = plan.available + plan.in_transit + plan.on_order
        plan.pending_requests = pending.get(prod.pk, 0) or 0
        plan.unit_cost = costs.get(prod.pk)
        plan.shelf_days = shelves[prod.pk]
        plan.stockout_weeks, plan.total_weeks = sum(s.stockout), len(s.weeks)
        plan.observed_units, plan.unmet_units = sum(s.demand), sum(s.unmet)
        if plan.supplier and plan.supplier.pk in leads:
            plan.lead_days, plan.lead_n = leads[plan.supplier.pk]
        else:
            plan.lead_days, plan.lead_n = p['tiempo_entrega_dias'], 0
        plan.protection_days = plan.lead_days + p['revision_dias']
        plan.horizon_weeks = max(ceil(plan.protection_days / 7), 1)
        try:
            # Una referencia defectuosa no puede tumbar el cálculo de las demás: su fallo se aísla y queda a la vista.
            plan.spark = _spark(s, prod)
            fit = series.fit(s.usable, s.usable_weeks, plan.horizon_weeks, ridge if prod.pk in ridge.panel else None, prod.pk, now_week)
            plan.fit = fit
            plan.rate, plan.sigma, plan.method, plan.label, plan.kind = fit['forecast'], fit['sigma'], fit['method'], fit['label'], fit['kind']
            plan.weeks, plan.enough, plan.improvement, plan.mae, plan.mae_base = fit['weeks'], fit['enough'], fit['improvement'], fit['mae'], fit['mae_base']
            plan.forecast_week, plan.forecast_h = fit['forecast_week'], fit['forecast_h']
            plan.interval_week = _interval(fit, 'week', plan.forecast_week, plan.sigma, 1)
            plan.interval_h = _interval(fit, 'h', plan.forecast_h, plan.sigma, plan.horizon_weeks)
            plan.idle_weeks = _idle(s.usable)
            _decide(plan, p, z, today, live.get(prod.pk, []))
            _explain(plan, p)
            _evidence(plan, s, today)
        except Exception as exc:
            log.exception('Reposición: no se pudo calcular la referencia %s', prod.code)
            _not_computable(plan, exc)
        out.append(plan)
    out.sort(key=lambda pl: (ORDER.index(pl.status), pl.days_until if pl.days_until is not None else 10 ** 6, pl.product.name))
    return out


def _idle(usable):
    n = 0
    for v in reversed(usable):
        if v > 0:
            break
        n += 1
    return n


def _not_computable(plan, exc):
    """Deja la referencia como «no calculable», con su motivo, sin sugerencia y sin cifras a medias."""
    plan.status, plan.error = 'no_calculable', f'{type(exc).__name__}: {exc}'[:160]
    plan.quantity, plan.days_until, plan.order_on, plan.coverage_days, plan.runout_on, plan.urgency_days = 0, None, None, None, None, None
    plan.coverage_capped, plan.capped, plan.at_risk, plan.excess = False, False, 0, 0
    plan.rate = plan.forecast_week = plan.forecast_h = plan.sigma = 0.0
    plan.weeks, plan.enough, plan.fit, plan.interval_week, plan.interval_h = 0, False, {}, None, None
    plan.reasons = [f'No se pudo calcular esta referencia ({plan.error}). Las demás referencias no se ven afectadas.']
    plan.evidence = {'unit': plan.product.units, 'incoming': plan.in_transit + plan.on_order, 'runout': None, 'typical_label': '', 'low': 0.0, 'high': 0.0}
    plan.summary = 'No se pudo calcular la sugerencia de esta referencia; revísela a mano. Las demás referencias no se ven afectadas.'


def _spark(s, product, width=8, height=28):
    rows = [{'week': w, 'value': d + u, 'stockout': so} for w, d, u, so in zip(s.weeks, s.demand, s.unmet, s.stockout)][-SPARK_WEEKS:]
    peak = max((x['value'] for x in rows), default=0)
    for i, x in enumerate(rows):
        x['h'] = int(round(x['value'] / peak * 100)) if peak else 0
        x['x'] = i * width
        x['bh'] = max(2, round(x['h'] * (height - 2) / 100)) if x['value'] else 2
        x['by'] = height - x['bh']
        x['label'] = f'Semana del {x["week"]:%d/%m}: ' + ('sin existencias' if x['stockout'] else product.qty(x['value']))
    return rows


def _interval(fit, name, forecast, sigma, h):
    """Intervalo del 80 %: empírico (cuantiles de los errores de selección) o, sin evaluación, aproximado."""
    found = fit.get(f'interval_{name}')
    if found:
        return {**found, 'kind': 'empirico'}
    spread = Z80 * sigma * sqrt(h)
    return {'low': max(forecast - spread, 0.0), 'high': forecast + spread, 'n_errors': 0, 'few': True, 'kind': 'aproximado'}


def _decide(plan, p, z, today, lots=()):
    r, pack = plan.rate, max(plan.product.pack_size, 1)
    unit = plan.product.units
    H = plan.horizon_weeks
    if plan.weeks == 0 or (not plan.enough and r == 0):
        plan.status = 'sin_datos'
        minimum = plan.product.minimum
        if minimum and plan.position <= minimum:
            plan.status, plan.quantity, plan.order_on, plan.days_until = 'pedir_ya', _round_up(2 * minimum - plan.position, pack), today, 0
            plan.reasons.append(f'Sin historial suficiente: se aplica la regla del mínimo del catálogo ({minimum} {unit}) y se propone llevar la posición al doble.')
        return
    if r < MIN_RATE:
        # Sin consumo reciente: no hay tasa con la cual proyectar cobertura ni fechas. Lo que haya en bodega no está rotando.
        plan.status = 'sin_consumo'
        plan.coverage_days = None
        plan.rate = plan.forecast_week = plan.forecast_h = 0.0
        plan.at_risk = sum(b.physical for b in lots)
        plan.excess = plan.position
        return
    daily = r / 7
    plan.expected_demand = plan.forecast_h * plan.protection_days / (7 * H)
    errors = plan.fit.get('errors_h') or []
    if len(errors) >= series.MIN_QUANTILE:
        plan.safety, plan.safety_method = max(series.quantile(errors, p['nivel_servicio'] / 100), 0.0), 'empirico'
    else:
        plan.safety, plan.safety_method = z * plan.sigma * sqrt(H), 'normal'
    plan.reorder_point = plan.expected_demand + plan.safety
    plan.target = plan.reorder_point + daily * p['ciclo_dias']
    cover = plan.position / daily
    plan.coverage_capped = cover > MAX_COVERAGE_DAYS
    plan.coverage_days = MAX_COVERAGE_DAYS if plan.coverage_capped else int(cover)
    if plan.position > 0 and not plan.coverage_capped:
        plan.runout_on = today + timedelta(days=plan.coverage_days)
    plan.urgency_days = plan.coverage_days - plan.lead_days
    usable_days = max(plan.shelf_days - p['margen_vencimiento_dias'], 7)
    cap_units = daily * usable_days
    if plan.position <= plan.reorder_point:
        plan.status, plan.days_until, plan.order_on = 'pedir_ya', 0, today
        raw = plan.target - plan.position
        at_order = plan.position
    else:
        plan.days_until = int(min((plan.position - plan.reorder_point) / daily, MAX_COVERAGE_DAYS))
        plan.order_on = today + timedelta(days=plan.days_until) if plan.days_until < MAX_COVERAGE_DAYS else None
        plan.status = 'pedir_pronto' if plan.days_until <= p['revision_dias'] else 'ok'
        raw = plan.target - plan.reorder_point
        at_order = plan.reorder_point
    q = _round_up(raw, pack)
    room = cap_units - at_order
    if q > room:
        q, plan.capped = max(_round_down(room, pack), 0), True
    plan.quantity = q if plan.status in ('pedir_ya', 'pedir_pronto') else 0
    if plan.status == 'ok' and plan.coverage_days is not None and plan.coverage_days > usable_days and plan.position > plan.target:
        plan.status = 'exceso'
    plan.excess = int(round(max(plan.position - cap_units, 0)))
    plan.at_risk = _at_risk(lots, daily, today)
    if plan.status in ('pedir_ya', 'pedir_pronto') and plan.quantity == 0:
        plan.status = 'ok'


def _at_risk(lots, daily, today):
    """Unidades que vencerían antes de usarse si el consumo sigue igual (primero vence, primero sale)."""
    consumed, risk = 0.0, 0
    for b in lots:
        capacity = daily * (b.expiry - today).days
        can = max(capacity - consumed, 0.0)
        use = min(b.physical, can)
        risk += int(round(b.physical - use))
        consumed += use
    return risk


def _explain(plan, p):
    """Supuestos y cálculo, en orden. Es el detalle técnico: se muestra en el pronóstico de la referencia."""
    unit, r, fit = plan.product.units, plan.reasons, plan.fit
    position = (f'Posición: {plan.available} disponibles (físico {plan.physical} − reservado {plan.reserved}) + {plan.in_transit} por recibir de facturas '
                f'+ {plan.on_order} en pedidos abiertos sin factura = {plan.position}')
    lead = (f'Tiempo de entrega: {plan.lead_days} días (mediana de {plan.lead_n} {"pedido" if plan.lead_n == 1 else "pedidos"} a {plan.supplier.name}).' if plan.lead_n
            else f'Tiempo de entrega: {plan.lead_days} días (parámetro; aún no hay pedidos con fecha de pedido y recepción).')
    if plan.weeks == 0 or plan.status == 'sin_datos':
        r.insert(0, 'Sin movimientos suficientes en la plataforma para estimar el consumo: la referencia se sigue con el mínimo del catálogo.')
        if plan.status != 'sin_datos':
            r.append(position + '.')
            r.append(lead)
        return
    if plan.enough:
        d = fit['design']
        r.append(f'Consumo esperado: {plan.rate_label} {unit} por semana con «{plan.label}» ({series.KINDS[plan.kind].lower()}), elegido por su error en '
                 f'{d["selection"]} semanas de selección y evaluado en las últimas {d["test"]} semanas, que no se usaron para elegir.')
    else:
        r.append(f'Historial insuficiente ({plan.weeks} semanas utilizables): se usa el promedio como respaldo y la sugerencia es provisional.')
    if plan.stockout_weeks:
        r.append((f'{plan.stockout_weeks} semanas terminaron' if plan.stockout_weeks != 1 else '1 semana terminó') + f' sin existencias y sin salidas: no se sabe cuánto se habría consumido y no cuentan para estimar ni para evaluar.')
    if plan.unmet_units:
        r.append(f'Demanda no atendida registrada: {plan.unmet_units} {unit} de solicitudes rechazadas'
                 + (', sumadas al consumo de su semana.' if p['incluir_rechazadas'] else '.'))
    if plan.status == 'sin_consumo':
        r.append(f'Sin consumo reciente: {plan.idle_weeks} {"semana" if plan.idle_weeks == 1 else "semanas seguidas"} sin salidas y una tasa esperada por debajo de {_n(MIN_RATE, 2)} {unit} por semana. '
                 'No se proyecta cobertura ni se sugiere pedir.')
        if plan.position:
            r.append(position + '. No está rotando: es candidata a revisar por exceso.')
        return
    r.append(lead)
    how = (f'cuantil {p["nivel_servicio"]} % de {len(fit.get("errors_h") or [])} errores acumulados medidos' if plan.safety_method == 'empirico'
           else f'aproximación normal con el error medido a una semana, por haber menos de {series.MIN_QUANTILE} errores acumulados')
    r.append(f'Punto de pedido: {plan.reorder_point:.0f} {unit} = consumo de {plan.protection_days} días ({plan.expected_demand:.0f}) + seguridad ({plan.safety:.0f}; {how}) '
             f'para un nivel de servicio del {p["nivel_servicio"]} %. Nivel objetivo: {plan.target:.0f}.')
    r.append(position + (f' ({plan.coverage_label} de cobertura).' if plan.coverage_days is not None else '.'))
    if plan.pending_requests:
        r.append(f'Solicitudes pendientes sin reservar: {plan.pending_requests} {unit}. Se muestran como dato y no se restan de la posición: su efecto ya está en el consumo esperado.')
    if plan.status == 'pedir_ya':
        r.append(f'La posición ya está en el punto de pedido: pedir {plan.quantity} {unit} hoy' + (f' (empaques de {plan.product.pack_size}).' if plan.product.pack_size > 1 else '.'))
    elif plan.status == 'pedir_pronto':
        r.append(f'Llega al punto de pedido en {plan.days_until} {"día" if plan.days_until == 1 else "días"}, el {plan.order_on:%d/%m/%Y}: pedir {plan.quantity} {unit} esa semana.')
    elif plan.status == 'exceso':
        r.append(f'La posición cubre {plan.coverage_label} y la vida útil al recibir es de {plan.shelf_days} días: parte se vencería antes de usarse.')
    if plan.capped:
        r.append(f'Cantidad limitada por vencimiento: con {plan.shelf_days} días de vida útil solo se alcanza a usar lo sugerido antes de que venza.')
    if plan.at_risk:
        r.append(f'Riesgo actual: {plan.at_risk} {unit} de lotes vigentes vencerían antes de usarse al ritmo actual.')


# ------------------------------------------------------------- evidencia para la pantalla

def _short(v):
    v = float(v)
    return str(int(round(v))) if abs(v - round(v)) < 0.05 else f'{v:.1f}'.replace('.', ',')


def _nice(v):
    if v <= 0:
        return 1.0
    mag = 10 ** floor(log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * mag:
            return m * mag
    return 10.0 * mag


def _evidence(plan, s, today):
    """Hechos en lenguaje llano: lo que pasó este mes, la semana típica, la tendencia reciente, hasta cuándo alcanza."""
    unit = plan.product.units
    usable = sorted(s.usable)
    observed = s.observed
    e = {'unit': unit, 'typical': median(usable) if usable else 0.0, 'p10': series.quantile(usable, .1), 'p90': series.quantile(usable, .9),
         'last4': sum(observed[-4:]) if observed else 0, 'prev4': sum(observed[-8:-4]) if len(observed) >= 8 else None,
         'month_name': MONTHS[today.month - 1], 'this_month': s.this_month, 'months_n': len(s.monthly),
         'month_avg': (sum(s.monthly.values()) / len(s.monthly)) if s.monthly else None, 'month_max': max(s.monthly.values()) if s.monthly else None}
    e['change'] = ((e['last4'] - e['prev4']) / e['prev4'] * 100) if e['prev4'] else None
    e['change_label'] = f'{e["change"]:+.0f} %'.replace('-', '−') if e['change'] is not None else ''
    last_year = month_add(today.replace(day=1), -12)
    e['same_month_last_year'] = s.monthly.get(last_year)
    e['last_year_label'] = f'{MONTHS[last_year.month - 1][:3]}. {last_year.year}'
    mean = (sum(usable) / len(usable)) if usable else 0.0
    with_sales = sum(1 for v in usable if v > 0)
    e['intermittent'] = e['typical'] == 0 and mean > 0
    e['typical_label'] = _short(mean if e['intermittent'] else e['typical'])
    e['range_label'] = f'{_short(e["p10"])} y {_short(e["p90"])}'
    e['typical_sub'] = (f'en promedio; hubo salidas en {with_sales} de {len(usable)} semanas' if e['intermittent']
                        else f'entre {e["range_label"]} en 8 de cada 10 semanas')
    e['month_avg_label'] = _short(e['month_avg']) if e['month_avg'] is not None else ''
    e['runout'] = plan.runout_on if plan.rate > 0 else None
    iv = plan.interval_week or {'low': 0.0, 'high': 0.0}
    e['low'], e['high'] = iv['low'], iv['high']
    e['band_label'] = f'{_short(e["low"])} a {_short(e["high"])}'
    e['weeks_after'] = int(round(plan.target / plan.rate)) if plan.rate > 0 and plan.target else None
    e['incoming'] = plan.in_transit + plan.on_order
    plan.evidence = e
    plan.summary = _summary(plan, e)


def _summary(plan, e):
    unit, q = plan.product.units, plan.quantity
    after = f' Con el pedido quedan existencias para unas {e["weeks_after"]} semanas.' if e['weeks_after'] else ''
    if plan.status == 'pedir_ya':
        if plan.weeks == 0:
            return f'Pedir {q} {unit} hoy: no hay existencias y el catálogo fija un mínimo de {plan.product.minimum} {unit} para esta referencia.'
        if plan.coverage_days and plan.coverage_days > plan.protection_days:
            # Alcanzaría al ritmo promedio, pero la reserva para salidas irregulares ya no está completa: se dice, para que «pedir hoy» no parezca un error.
            return (f'Pedir {q} {unit} hoy: al ritmo promedio lo que hay alcanzaría {plan.covers}, pero las salidas de esta referencia son irregulares y '
                    f'una semana alta podría agotarla antes de recibir un pedido ({plan.lead_days} días de entrega).{after}')
        have = f'lo que hay alcanza {plan.covers}' if plan.coverage_days else 'no quedan existencias'
        return f'Pedir {q} {unit} hoy: {have} y el proveedor tarda {plan.lead_days} días en entregar.{after}'
    if plan.status == 'pedir_pronto':
        return (f'Pedir {q} {unit} hacia el {plan.order_on:%d/%m}: lo que hay alcanza {plan.covers} y el proveedor tarda '
                f'{plan.lead_days} días en entregar.{after}')
    if plan.status == 'exceso':
        stock = 'entre lo disponible y lo que está en camino hay' if e['incoming'] else 'hay existencias'
        tail = (f' {plan.at_risk} {unit} de los lotes actuales podrían vencer sin usarse.' if plan.at_risk
                else f' Sobrarían unas {plan.excess} {unit} al vencer los lotes.' if plan.excess else '')
        span = plan.covers if plan.coverage_capped else f'para {plan.covers}'
        return f'No pedir: {stock} {span}, más de lo que se alcanza a usar antes del vencimiento.{tail}'
    if plan.status == 'ok':
        when = f'; conviene revisar hacia el {plan.order_on:%d/%m}' if plan.order_on else ''
        return f'No hace falta pedir todavía: lo que hay alcanza {plan.covers}{when}.'
    if plan.status == 'sin_consumo':
        stock = f' Hay {plan.position} {unit} que no están rotando: conviene revisarlas.' if plan.position else ''
        return f'Sin salidas en las últimas {plan.idle_weeks} semanas: no se sugiere pedir.{stock}'
    return 'Sin historial suficiente para sugerir una cantidad: la referencia se sigue con el mínimo del catálogo.'


def chart(plan, weeks=CHART_WEEKS):
    """Geometría del gráfico de la referencia (SVG sin JS): salidas semanales, semanas sin existencias marcadas,
    demanda no atendida aparte, pronóstico de las próximas semanas e intervalo del 80 %."""
    s = plan.series
    if s is None or not s.weeks:
        return None
    unit = plan.product.units
    width, height, left, right, top, base, label_y = 640, 240, 40, 628, 16, 196, 216
    past = list(zip(s.weeks, s.demand, s.unmet, s.stockout))[-weeks:]
    H = max(plan.horizon_weeks, 1)
    future_n = max(H, 4)
    n = len(past) + future_n
    slot = (right - left) / n
    iv = plan.interval_week or {'low': plan.forecast_week, 'high': plan.forecast_week}
    later = (plan.forecast_h - plan.forecast_week) / (H - 1) if H > 1 else plan.forecast_week
    future = [plan.forecast_week] + [later] * (future_n - 1)
    spread_low, spread_high = plan.forecast_week - iv['low'], iv['high'] - plan.forecast_week
    ymax = _nice(max([d + u for _, d, u, _ in past] + [f + spread_high for f in future] + [1.0]))

    def x(i):
        return round(left + slot * (i + 0.5), 1)

    def y(v):
        return round(base - max(v, 0.0) / ymax * (base - top), 1)

    points, segments, current, outs, unmet, cols = [], [], [], [], [], []
    for i, (w, d, u, so) in enumerate(past):
        tip = {'title': f'Semana del {w:%d/%m/%Y}', 'rows': [['Salidas', 'sin existencias' if so else f'{d} {unit}', 'actual']], 'hint': ''}
        if u:
            tip['rows'].append(['No atendida', f'{u} {unit}', 'unmet'])
            unmet.append({'x': round(x(i) - 3, 1), 'y': y(d + u), 'w': 6, 'h': round(y(d) - y(d + u), 1)})
        if so:
            tip['hint'] = 'Semana sin existencias: no se sabe cuánto se habría consumido y no entra al cálculo.'
            outs.append({'x': round(left + slot * i, 1), 'w': round(slot, 1)})
            if current:
                segments.append(current)
                current = []
        else:
            current.append((x(i), y(d)))
            points.append({'x': x(i), 'y': y(d)})
        cols.append({'hit_x': round(left + slot * i, 1), 'hit_w': round(slot, 1), 'tip': json.dumps(tip, ensure_ascii=False),
                     'aria': f'Semana del {w:%d/%m}: ' + ('sin existencias' if so else f'{d} {unit}' + (f', {u} no atendidas' if u else '')),
                     'label': f'{w:%d/%m}', 'cx': x(i), 'show_label': (len(past) - 1 - i) % 4 == 0, 'minor': (len(past) - 1 - i) % 8 != 0, 'anchor': 'middle'})
    if current:
        segments.append(current)
    k0 = len(past)
    fpoints = [{'x': x(k0 + k), 'y': y(f), 'value': f} for k, f in enumerate(future)]
    upper = [(x(k0 + k), y(f + spread_high)) for k, f in enumerate(future)]
    lower = [(x(k0 + k), y(f - spread_low)) for k, f in enumerate(future)]
    band = 'M' + ' L'.join(f'{a},{b}' for a, b in upper + lower[::-1]) + ' Z'
    last_week = past[-1][0]
    fcols = []
    for k, f in enumerate(future):
        w = last_week + timedelta(days=7 * (k + 1))
        tip = {'title': f'Semana del {w:%d/%m/%Y}', 'rows': [['Pronóstico', f'{_short(f)} {unit}', 'forecast'],
                                                              ['Intervalo del 80 %', f'{_short(max(f - spread_low, 0))} a {_short(f + spread_high)}', 'band']],
               'hint': 'Intervalo evaluado a una semana; para semanas posteriores es una aproximación.' if k else ''}
        fcols.append({'hit_x': round(left + slot * (k0 + k), 1), 'hit_w': round(slot, 1), 'tip': json.dumps(tip, ensure_ascii=False),
                      'aria': f'Semana del {w:%d/%m}: pronóstico {_short(f)} {unit}', 'label': f'{w:%d/%m}', 'cx': right, 'show_label': k == future_n - 1, 'minor': False, 'anchor': 'end'})
    divider = round(left + slot * k0, 1)
    return {'width': width, 'height': height, 'left': left, 'right': right, 'top': top, 'base': base, 'label_y': label_y, 'plot_h': base - top,
            'ticks': [{'y': y(v), 'label': _short(v)} for v in (0, ymax / 2, ymax)],
            'lines': ['M' + ' L'.join(f'{a},{b}' for a, b in seg) for seg in segments if len(seg) > 1], 'points': points, 'outs': outs, 'unmet': unmet,
            'cols': cols, 'future_cols': fcols, 'forecast_line': 'M' + ' L'.join(f'{pt["x"]},{pt["y"]}' for pt in fpoints), 'forecast_points': fpoints,
            'band': band, 'divider': divider, 'now_x': right,
            'title': f'Salidas semanales de {plan.product.name} y pronóstico',
            'desc': (f'Últimas {len(past)} semanas de salidas observadas; {len(outs)} {"semana" if len(outs) == 1 else "semanas"} sin existencias marcadas; pronóstico de las próximas '
                     f'{future_n} semanas con intervalo del 80 %.'),
            'weeks': len(past), 'future_n': future_n,
            'table': [{'week': w, 'demand': d, 'unmet': u, 'stockout': so} for w, d, u, so in past][::-1]}


# ----------------------------------------------------------- aprendizaje: pronóstico vs. realidad

def record_forecasts(plan_list, today=None):
    """Guarda una vez el pronóstico de la semana en curso (con su intervalo) y completa con lo real las semanas ya cerradas."""
    today = today or timezone.localdate()
    ws = week_start(today)
    for plan in plan_list:
        if plan.weeks:
            iv = plan.interval_week or {}
            ForecastRecord.objects.get_or_create(product=plan.product, week_start=ws, defaults={
                'forecast': plan.forecast_week, 'sigma': plan.sigma, 'method': plan.method, 'made_on': today,
                'low': iv.get('low'), 'high': iv.get('high'), 'interval_kind': iv.get('kind', '')})
    pending = list(ForecastRecord.objects.filter(actual__isnull=True, week_start__lt=ws).select_related('product'))
    if pending:
        hist = history({r.product for r in pending}, today, bool(current_params()['incluir_rechazadas']))
        for rec in pending:
            found = hist[rec.product_id].at(rec.week_start)
            rec.actual, rec.stockout = (found if found else (0, False))
            rec.save(update_fields=['actual', 'stockout'])
    return len(pending)


def _score(rows):
    """Error prospectivo: pronósticos guardados antes de conocer la semana, comparados con lo observado."""
    if not rows:
        return {'n': 0, 'mae': None, 'bias': None, 'wape': None, 'wape_defined': False, 'inside': 0, 'interval_n': 0, 'coverage': None}
    errs = [r.actual - r.forecast for r in rows]
    total = sum(r.actual for r in rows)
    with_interval = [r for r in rows if r.covered is not None]
    inside = sum(1 for r in with_interval if r.covered)
    return {'n': len(rows), 'mae': sum(abs(e) for e in errs) / len(errs), 'bias': sum(errs) / len(errs),
            'wape': (sum(abs(e) for e in errs) / total * 100) if total > 0 else None, 'wape_defined': total > 0,
            'inside': inside, 'interval_n': len(with_interval), 'coverage': (inside / len(with_interval) * 100) if with_interval else None}


def accuracy(products=None, weeks=8, today=None):
    """Error del pronóstico semanal guardado frente a lo observado, en las últimas semanas cerradas (sin las censuradas)."""
    today = today or timezone.localdate()
    since = week_start(today) - timedelta(days=7 * weeks)
    qs = ForecastRecord.objects.filter(actual__isnull=False, stockout=False, week_start__gte=since)
    if products is not None:
        qs = qs.filter(product__in=products)
    return _score(list(qs))


def with_accuracy(plan_list, today=None):
    today = today or timezone.localdate()
    since = week_start(today) - timedelta(days=7 * 8)
    by_product = defaultdict(list)
    for r in ForecastRecord.objects.filter(actual__isnull=False, stockout=False, week_start__gte=since):
        by_product[r.product_id].append(r)
    for plan in plan_list:
        rows = by_product.get(plan.product.pk, [])
        plan.accuracy = _score(rows) if rows else None
    return plan_list


def comparison(plan_list):
    """Tabla comparativa de candidatos sobre todas las referencias con historial suficiente.

    Para cada candidato: en cuántas referencias compitió, en cuántas fue elegido y su error absoluto total
    relativo al del promedio simple en esas mismas referencias y orígenes (1,00 = igual; menos = mejor), en la
    ventana de selección y en la de prueba, al horizonte de la decisión. La fila «elegido por referencia» mide
    el procedimiento completo: lo que de verdad se usa.
    """
    agg = {}
    chosen_row = {'method': 'elegido', 'label': 'Método elegido en cada referencia (lo que se usa)', 'kind_label': '', 'refs': 0, 'chosen': 0,
                  'sel': [0.0, 0.0, 0.0], 'test': [0.0, 0.0, 0.0], 'sel_n': 0, 'test_n': 0}
    refs = 0
    for pl in plan_list:
        fit = pl.fit
        if not fit or not fit.get('enough'):
            continue
        refs += 1
        rows = {r['method']: r for r in fit['evaluations'] if r['available']}
        ref = rows[series.FALLBACK]
        hs = fit['selection_horizon']
        key = 'sel_h' if hs == fit['horizon'] else 'sel_week'
        tkey = 'test_h' if hs == fit['horizon'] else 'test_week'
        for name, r in rows.items():
            a = agg.setdefault(name, {'method': name, 'label': r['label'], 'kind_label': r['kind_label'], 'refs': 0, 'chosen': 0,
                                      'sel': [0.0, 0.0, 0.0], 'test': [0.0, 0.0, 0.0], 'sel_n': 0, 'test_n': 0})
            targets = [a] + ([chosen_row] if r['chosen'] else [])
            for t in targets:
                t['refs'] += 1
                t['chosen'] += 1 if r['chosen'] else 0
                for win, k in (('sel', key), ('test', tkey)):
                    m, mref = r[k], ref[k]
                    t[win][0] += m.abs_error
                    t[win][1] += mref.abs_error
                    t[win][2] += m.observed
                    t[f'{win}_n'] += m.n
    out = []
    for a in list(agg.values()) + [chosen_row]:
        for win in ('sel', 'test'):
            err, base, obs = a[win]
            a[f'{win}_rel'] = (err / base) if base > 0 else None
            a[f'{win}_wape'] = (err / obs * 100) if obs > 0 else None
        out.append(a)
    order = {n: i for i, n in enumerate(series.ORDER)}
    out.sort(key=lambda a: order.get(a['method'], 99))
    return {'rows': out, 'refs': refs, 'ridge': agg.get(series.RIDGE.name), 'chosen': chosen_row}


def coverage_summary(plan_list):
    """Cobertura medida del intervalo del 80 % en la ventana de prueba, sumando todas las referencias."""
    out = {}
    for name in ('week', 'h'):
        inside = n = refs = 0
        for pl in plan_list:
            c = (pl.fit or {}).get(f'coverage_{name}')
            if c:
                inside, n, refs = inside + c['inside'], n + c['n'], refs + 1
        out[name] = {'inside': inside, 'n': n, 'refs': refs, 'share': (inside / n * 100) if n else None}
    return out


# ------------------------------------------------------------------- pedidos sugeridos

def suggested_orders(plan_list):
    """Agrupa por proveedor las referencias que hay que pedir. Devuelve (por proveedor, sin proveedor)."""
    groups, orphans = defaultdict(list), []
    for plan in plan_list:
        if not plan.needs_order:
            continue
        (groups[plan.supplier] if plan.supplier else orphans).append(plan)
    return groups, orphans


def committed_cost(today=None, p=None):
    """Costo estimado de los pedidos abiertos preparados en el periodo de revisión vigente: ya consume presupuesto."""
    today = today or timezone.localdate()
    p = p or current_params()
    since = today - timedelta(days=p['revision_dias'] - 1)
    total = ZERO
    for o in PurchaseOrder.objects.filter(status__in=PurchaseOrder.OPEN, created_at__date__gte=since).prefetch_related('lines'):
        total += o.estimated_cost
    return money(total)


def budget_plan(plan_list, presupuesto=0, committed=ZERO):
    """Ajusta la sugerencia a un presupuesto de compra (COP; 0 o None = sin tope).

    Prioridad por urgencia: primero lo que se agotaría antes frente a su tiempo de entrega (`urgency_days` menor).
    En ese orden cada referencia recibe su cantidad completa si cabe; si no, la mayor cantidad en múltiplos de su
    empaque que quepa; si no cabe ni un empaque, queda por fuera. No modifica los planes.

    Devuelve un diccionario:
      budget, unlimited, committed, available, total (costo de todo lo sugerido), spent, left, restrictive,
      lines:    [{'plan', 'suggested', 'quantity', 'cost', 'cut', 'note', 'urgency_days'}]   lo que entra, en orden de prioridad
      excluded: [{'plan', 'suggested', 'quantity', 'cost', 'reason'}]   lo que quedó por fuera (`quantity` = unidades que no entran)
      included: alias de `lines`;  by_product: {pk: cantidad que entra}
    """
    budget = money(Decimal(str(presupuesto or 0)))
    unlimited = budget <= 0
    committed = money(Decimal(str(committed or 0)))
    need = sorted((pl for pl in plan_list if pl.needs_order),
                  key=lambda pl: (pl.urgency_days if pl.urgency_days is not None else -10 ** 6, ORDER.index(pl.status), pl.product.name))
    total = sum((pl.estimated_cost or ZERO for pl in need), ZERO)
    available = None if unlimited else max(budget - committed, ZERO)
    left = available
    lines, excluded = [], []
    for pl in need:
        cost = pl.estimated_cost
        row = {'plan': pl, 'suggested': pl.quantity, 'quantity': pl.quantity, 'cost': cost, 'cut': False, 'note': '', 'urgency_days': pl.urgency_days}
        if unlimited:
            lines.append(row)
            continue
        if pl.unit_cost is None:
            excluded.append({'plan': pl, 'suggested': pl.quantity, 'quantity': pl.quantity, 'cost': None,
                             'reason': 'Sin costo conocido: no se puede presupuestar. Revísela a mano.'})
            continue
        if cost <= left:
            left -= cost
            lines.append(row)
            continue
        pack = max(pl.product.pack_size, 1)
        fits = _round_down(int(left / pl.unit_cost), pack) if pl.unit_cost > 0 else pl.quantity
        if fits > 0:
            part = money(pl.unit_cost * fits)
            left -= part
            lines.append({**row, 'quantity': fits, 'cost': part, 'cut': True, 'note': f'Recortada de {pl.quantity} a {fits} por presupuesto' + (f' (empaques de {pack}).' if pack > 1 else '.')})
            excluded.append({'plan': pl, 'suggested': pl.quantity, 'quantity': pl.quantity - fits, 'cost': money(cost - part),
                             'reason': f'Recortada a {fits} de {pl.quantity} {pl.product.units}' + (f' (empaques de {pack})' if pack > 1 else '') + ': el presupuesto no alcanza para el resto.'})
        else:
            excluded.append({'plan': pl, 'suggested': pl.quantity, 'quantity': pl.quantity, 'cost': cost,
                             'reason': f'El presupuesto disponible no alcanza para un empaque de {pack}.' if pack > 1 else 'El presupuesto disponible no alcanza.'})
    spent = sum((r['cost'] or ZERO for r in lines), ZERO)
    return {'budget': budget, 'unlimited': unlimited, 'committed': committed, 'available': available, 'total': money(total), 'spent': money(spent),
            'left': None if unlimited else money(left), 'restrictive': (not unlimited) and bool(excluded), 'lines': lines, 'included': lines,
            'excluded': excluded,
            'by_product': {r['plan'].product.pk: r['quantity'] for r in lines}}
