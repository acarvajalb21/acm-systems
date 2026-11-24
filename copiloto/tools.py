"""Herramientas del copiloto: consultas de solo lectura sobre los servicios de la plataforma.

Cada herramienta declara su nombre, su descripción, el esquema de sus argumentos (tipos y rangos) y los perfiles que
pueden usarla, con las mismas tuplas de `inventory.services`. Ninguna escribe en la base ni ejecuta SQL libre: llaman
a los servicios que ya usan las pantallas y devuelven un resultado estructurado con periodo, fuentes, limitaciones y
enlaces internos a los registros. Las acciones (aprobar, pedir, aplicar) siguen ocurriendo en sus pantallas.

El texto que viene de la base (descripciones de extractos, notas, nombres) viaja siempre como dato dentro de los
bloques; nunca se interpreta, no elige herramientas y no cambia permisos.
"""
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from math import floor
from urllib.parse import urlencode
from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from inventory import services as S
from inventory.models import Batch, Product, SupplyRequest

log = logging.getLogger(__name__)

ZERO = Decimal('0')
VIEWERS = ('inventory', 'finance', 'admin', 'owner')        # los mismos perfiles que ven Reposición
MAX_ROWS = 12
MAX_TEXT = 160
DISCOUNT_WINDOW = 10                                          # días para «descuento por vencer»
MAX_BUDGET = 50_000_000_000

# Quién puede abrir cada pantalla enlazada: un enlace solo se entrega si el perfil que pregunta puede abrirlo.
LINK_ROLES = {
    'home': S.EVERYONE, 'alerts': S.EVERYONE, 'copilot': S.EVERYONE,
    'replenishment': VIEWERS, 'forecast': VIEWERS, 'learning': VIEWERS, 'agents': VIEWERS,
    'stock': S.MANAGERS, 'trace': S.MANAGERS, 'requests': (*S.REQUESTERS, *S.MANAGERS),
    'bank': S.FINANCE, 'bank_txn': S.FINANCE, 'portfolio': S.FINANCE, 'payables': S.FINANCE, 'invoice_pay': S.FINANCE,
    'results': S.FINANCE, 'party': S.SALES, 'sale': S.SALES, 'invoice': S.DOCUMENTS, 'purchase_orders': S.DOCUMENTS,
    'discounts': S.ADMINS,
}


class ToolError(Exception):
    """Argumentos inválidos o herramienta desconocida. El mensaje es apto para mostrarse."""


# ------------------------------------------------------------------ utilidades

def clean(value, limit=MAX_TEXT):
    """Texto de la base como dato: sin caracteres de control ni saltos, con longitud acotada."""
    text = ''.join(' ' if unicodedata.category(c)[0] == 'C' else c for c in str(value if value is not None else ''))
    text = re.sub(r'\s+', ' ', text).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def fdate(d):
    return d.strftime('%d/%m/%Y') if d else '—'


def fnum(n):
    try:
        return f'{int(round(float(n))):,}'.replace(',', '.')
    except (TypeError, ValueError):
        return str(n)


def fdec(x, decimals=1):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return '—'
    return str(int(round(x))) if abs(x - round(x)) < 0.05 else f'{x:.{decimals}f}'.replace('.', ',')


def cop(v):
    return S.cop(v or 0)


def days(n):
    return f'{n} día' if n == 1 else f'{n} días'


def plural(n, one, many):
    return f'{fnum(n)} {one if n == 1 else many}'


def now_label():
    return timezone.localtime().strftime('%d/%m/%Y %H:%M')


def link(user, label, name, *args, query=None):
    """Enlace interno a una pantalla que el perfil puede abrir, o None."""
    if user.role not in LINK_ROLES.get(name, ()):
        return None
    try:
        url = reverse(name, args=args)
    except NoReverseMatch:
        return None
    if query:
        url += '?' + urlencode(query)
    return {'label': label, 'url': url}


def cell(user, text, name, *args, query=None):
    """Celda con enlace si el perfil puede abrir el registro; si no, solo el texto."""
    ln = link(user, text, name, *args, query=query)
    return {'text': text, 'url': ln['url']} if ln else text


def internal(url):
    return isinstance(url, str) and url.startswith('/') and not url.startswith('//') and '\\' not in url and '://' not in url


def result(title, period, summary, blocks=None, links=None, sources=None, limitations=None, **extra):
    stamp = now_label()
    out = {'title': title, 'period': period, 'as_of': stamp, 'summary': summary,
           'blocks': [b for b in (blocks or []) if b and b.get('rows')],
           'links': [ln for ln in (links or []) if ln and internal(ln.get('url'))],
           'sources': [f'{s} · consultado el {stamp}' for s in (sources or [])],
           'limitations': list(limitations or [])}
    out.update(extra)
    return out


def table(title, columns, rows, numeric=(), more=0, note=''):
    return {'kind': 'table', 'title': title, 'columns': list(columns), 'rows': rows, 'numeric': list(numeric), 'more': more, 'note': note}


def facts(title, rows):
    return {'kind': 'facts', 'title': title, 'columns': ['Dato', 'Valor'], 'rows': [[k, v] for k, v in rows if v not in (None, '')]}


def listing(title, rows, more=0):
    return {'kind': 'list', 'title': title, 'columns': [], 'rows': rows, 'more': more}


def cut(rows, limit=MAX_ROWS):
    rows = list(rows)
    return rows[:limit], max(len(rows) - limit, 0)


def denied(tool, user):
    from inventory.models import User
    labels = dict(User.Role.choices)
    allowed = ', '.join(labels.get(r, r) for r in tool.roles)
    return result(tool.title, '—',
                  f'Acceso restringido: el perfil {labels.get(user.role, user.role)} no puede consultar «{tool.title}». '
                  f'Esta consulta está disponible para: {allowed}.',
                  limitations=['Los permisos se verifican en el servidor con las mismas reglas de las pantallas; el copiloto no puede ampliarlos.'],
                  denied=True)


# ----------------------------------------------------------- reposición (acceso tolerante)

def _plans(products=None):
    """Planes de reposición con su error medido. La interfaz de `aprendizaje.reposicion` puede crecer: se usa lo que exista."""
    from aprendizaje import reposicion as R
    today = timezone.localdate()
    plan_list = R.plans(today=today, products=products) if products is not None else R.plans(today=today)
    try:
        plan_list = R.with_accuracy(plan_list, today)
    except Exception:
        pass
    return R, list(plan_list)


def _interval(plan):
    """Intervalo del 80 % del pronóstico semanal: (bajo, alto, 'empirico' | 'aproximado') o None."""
    found = getattr(plan, 'interval_week', None)
    low = high = None
    kind = ''
    if isinstance(found, dict):
        low, high, kind = found.get('low'), found.get('high'), found.get('kind') or ''
    if low is None or high is None:
        ev = getattr(plan, 'evidence', None) or {}
        low, high, kind = ev.get('low'), ev.get('high'), 'aproximado'
    if low is None or high is None:
        return None
    try:
        return float(low), float(high), kind
    except (TypeError, ValueError):
        return None


def _weekly(plan):
    value = getattr(plan, 'forecast_week', None)
    return plan.rate if value is None else value


def _forecast_label(plan):
    unit = plan.product.units
    if not getattr(plan, 'weeks', 0):
        return 'sin historial'
    text = f'{fdec(_weekly(plan))} {unit} por semana'
    band = _interval(plan)
    if band:
        text += f' (intervalo del 80 %: {fdec(band[0])} a {fdec(band[1])}' + (', aproximado' if band[2] == 'aproximado' else '') + ')'
    return text


def _error_label(plan):
    """Error medido del pronóstico semanal: el de la ventana de prueba; si no hay, el prospectivo; si no, el de selección."""
    test = getattr(plan, 'test', None)
    if isinstance(test, dict) and test.get('mae') is not None:
        return f'±{fdec(test["mae"])} por semana en la ventana de prueba (n = {test.get("n", 0)})'
    acc = getattr(plan, 'accuracy', None)
    if isinstance(acc, dict) and acc.get('mae') is not None:
        return f'±{fdec(acc["mae"])} por semana frente a lo observado (n = {acc.get("n", 0)})'
    if getattr(plan, 'enough', False) and getattr(plan, 'mae', None) is not None:
        return f'±{fdec(plan.mae)} por semana en la ventana de selección'
    return 'sin medir (historial insuficiente)'


def _urgency(plan):
    """Días de cobertura menos días de entrega: negativo = se agotaría antes de recibir un pedido hecho hoy."""
    value = getattr(plan, 'urgency_days', None)
    if value is None:
        value = (plan.coverage_days if plan.coverage_days is not None else 0) - plan.lead_days
    return value


def _method_label(plan):
    label = clean(getattr(plan, 'label', '') or '')
    if not label:
        return None
    kind = {'estadistico': 'método estadístico', 'entrenado': 'modelo entrenado'}.get(getattr(plan, 'kind', ''), '')
    return f'{label} ({kind})' if kind else label


def _coverage_label(plan):
    """Proporción de semanas de prueba en las que lo observado cayó dentro del intervalo del 80 %, con su n."""
    found = (getattr(plan, 'fit', None) or {}).get('coverage_week')
    if not isinstance(found, dict) or not found.get('n'):
        return None
    return f'{found.get("inside", 0)} de {found["n"]} semanas de prueba dentro del intervalo'


def _runout(plan):
    on = getattr(plan, 'runout_on', None) or (getattr(plan, 'evidence', None) or {}).get('runout')
    return fdate(on) if on else 'sin dato'


def _budget_from_params(R):
    """Presupuesto de compra configurado en Reposición (0 = sin tope), o None."""
    try:
        value = int(R.current_params().get('presupuesto') or 0)
    except Exception:
        return None
    return value or None


def _own_budget_plan(plan_list, budget, committed=ZERO):
    """Recorte por presupuesto propio del copiloto: respaldo si `reposicion.budget_plan` no está o cambia de forma.

    Misma regla: prioridad por urgencia (días de cobertura menos días de entrega, lo más negativo primero); cada
    cantidad entra completa si cabe o recortada en múltiplos del empaque; lo que no cabe queda listado con su motivo.
    """
    needed = [pl for pl in plan_list if getattr(pl, 'needs_order', False)]
    needed.sort(key=lambda pl: (_urgency(pl), pl.product.name))
    committed = Decimal(committed or 0)
    left = max(Decimal(budget) - committed, ZERO) if budget else None
    included, excluded, spent, total = [], [], ZERO, ZERO
    for pl in needed:
        unit = pl.unit_cost
        pack = max(pl.product.pack_size, 1)
        if unit is None:
            if left is None:
                included.append({'plan': pl, 'quantity': pl.quantity, 'cost': None, 'note': 'Sin costo de compra registrado: no se valora.'})
            else:
                excluded.append({'plan': pl, 'quantity': pl.quantity, 'cost': None, 'reason': 'Sin costo conocido: no se puede presupuestar. Revísela a mano.'})
            continue
        unit = Decimal(unit)
        cost = unit * pl.quantity
        total += cost
        if left is None or cost <= left:
            qty, note = pl.quantity, ''
        else:
            qty = int(floor(left / (unit * pack))) * pack if unit > 0 else 0
            if qty <= 0:
                excluded.append({'plan': pl, 'quantity': pl.quantity, 'cost': cost, 'reason': 'El presupuesto disponible no alcanza.'})
                continue
            note = f'Recortada de {pl.quantity} a {qty} por presupuesto' + (f' (empaques de {pack}).' if pack > 1 else '.')
            excluded.append({'plan': pl, 'quantity': pl.quantity - qty, 'cost': unit * (pl.quantity - qty),
                             'reason': f'Recortada a {qty} de {pl.quantity}: el presupuesto no alcanza para el resto.'})
        cost = unit * qty
        included.append({'plan': pl, 'quantity': qty, 'cost': cost, 'note': note})
        spent += cost
        if left is not None:
            left -= cost
    return {'included': included, 'excluded': excluded, 'spent': spent, 'total': total, 'budget': Decimal(budget) if budget else None,
            'committed': committed, 'left': left, 'engine': 'copiloto'}


def _from_reposicion(raw, budget):
    """Lleva el resultado de `reposicion.budget_plan` a la forma del copiloto. Lanza si falta algo de su contrato."""
    def rows(items, text_key):
        out = []
        for it in items:
            pl = it['plan']
            cost = it.get('cost')
            out.append({'plan': pl, 'quantity': int(it['quantity']), 'cost': Decimal(cost) if cost is not None else None, text_key: clean(it.get(text_key) or '', 220)})
        return out

    included, excluded = rows(raw['included'], 'note'), rows(raw['excluded'], 'reason')
    spent = Decimal(raw['spent']) if raw.get('spent') is not None else sum((r['cost'] or ZERO for r in included), ZERO)
    total = Decimal(raw['total']) if raw.get('total') is not None else spent
    unlimited = bool(raw.get('unlimited')) if 'unlimited' in raw else not budget
    return {'included': included, 'excluded': excluded, 'spent': spent, 'total': total, 'budget': None if unlimited else Decimal(budget),
            'committed': Decimal(raw.get('committed') or 0), 'left': None if unlimited or raw.get('left') is None else Decimal(raw['left']), 'engine': 'reposicion'}


def budget_plan(R, plan_list, budget):
    """Propuesta ajustada al presupuesto con el motor de Reposición (el mismo de su pantalla); si no responde, el propio."""
    committed = ZERO
    try:
        committed = Decimal(R.committed_cost() or 0)
    except Exception:
        log.warning('No se pudo leer el costo comprometido en pedidos abiertos; se asume cero.', exc_info=True)
    fn = getattr(R, 'budget_plan', None)
    if callable(fn):
        try:
            return _from_reposicion(fn(plan_list, budget or 0, committed), budget)
        except Exception:
            log.warning('reposicion.budget_plan no respondió con la forma esperada; se usa el recorte del copiloto.', exc_info=True)
    return _own_budget_plan(plan_list, budget, committed)


MILLION = 1_000_000


def suggested_budget(plans=None):
    """Presupuesto de ejemplo para la pregunta de compra, calculado con los datos del momento (pesos), o None.

    Lo ya comprometido en pedidos abiertos del periodo más alrededor del 60 % de lo sugerido sin tope, redondeado al
    millón superior: así el ejemplo deja ver referencias que entran, alguna recortada al empaque y alguna por fuera.
    Entre fracciones cercanas (60, 50, 70 y 40 %) se prefiere la primera que muestre los tres casos; si ninguna, 60 %.
    Es solo la cifra que se propone en el campo: quien pregunta puede cambiarla.
    """
    R, plan_list = plans if plans is not None else _plans()
    total = budget_plan(R, plan_list, None)['total']
    if not total or total <= 0:
        return None
    try:
        committed = Decimal(R.committed_cost() or 0)
    except Exception:
        committed = ZERO

    def figure(share):
        return int(-(-(committed + total * Decimal(share)) // MILLION)) * MILLION

    for share in ('0.6', '0.5', '0.7', '0.4'):
        bp = budget_plan(R, plan_list, figure(share))
        whole = [it for it in bp['included'] if not it.get('note')]
        cut = [it for it in bp['included'] if it.get('note')]
        if whole and cut and len(bp['excluded']) > len(cut):
            return figure(share)
    return figure('0.6')


# ------------------------------------------------------------------ herramientas

def atencion_hoy(user):
    from agentes import runner
    from agentes.agents import REGISTRY
    alerts = [a for a in S.alerts(user) if not a['key'].startswith('agent-')]
    proposals = list(runner.pending_for(user).order_by('created_at'))
    proposals.sort(key=lambda p: -(p.benefit or ZERO))
    levels = {'critical': 0, 'warning': 0, 'info': 0}
    for a in alerts:
        levels[a['level']] += 1
    rows = [{'text': clean(a['title']), 'detail': clean(a['detail'], 220), 'url': a['url'] if internal(a['url']) else '', 'level': a['level']} for a in alerts]
    shown, more = cut(rows, 10)
    agents_link = link(user, 'Revisar las propuestas en Agentes', 'agents')
    prop_rows = []
    for p in proposals[:MAX_ROWS]:
        agent = REGISTRY.get(p.agent)
        value = cop(p.benefit) if p.benefit else cop(p.amount) if p.amount else '—'
        first = {'text': clean(p.title), 'url': agents_link['url']} if agents_link else clean(p.title)
        prop_rows.append([first, agent.name if agent else p.agent, clean(p.detail, 220), value])
    benefit = sum((p.benefit or ZERO for p in proposals), ZERO)
    total = len(alerts) + len(proposals)
    if not total:
        summary = 'No hay alertas ni propuestas pendientes para su perfil en este momento.'
    else:
        parts = [plural(levels['critical'], 'asunto crítico', 'asuntos críticos'), plural(levels['warning'], 'aviso', 'avisos'),
                 plural(levels['info'], 'asunto informativo', 'asuntos informativos')]
        summary = f'Hay {plural(len(alerts), "alerta", "alertas")} para su perfil ({", ".join(parts)})'
        summary += (f' y {plural(len(proposals), "propuesta de agentes por decidir", "propuestas de agentes por decidir")}'
                    + (f', con un beneficio estimado de {cop(benefit)}' if benefit else '')) if proposals else ''
        summary += '. Cada renglón dice qué pasa y por qué; lo crítico va primero.'
    return result(
        'Qué necesita atención hoy', f'Hoy, {fdate(timezone.localdate())}', summary,
        blocks=[listing('Alertas por prioridad', shown, more),
                table('Propuestas de agentes por decidir', ['Propuesta', 'Agente', 'Por qué', 'Ahorro, recaudo o valor'], prop_rows, numeric=[3],
                      more=max(len(proposals) - MAX_ROWS, 0))],
        links=[link(user, 'Ver todas las alertas', 'alerts'), agents_link],
        sources=['Alertas del perfil: lotes, existencias, solicitudes, facturas, cartera y banco', 'Propuestas pendientes de los agentes'],
        limitations=['Solo incluye lo que su perfil puede ver y decidir.',
                     'Las propuestas se recalculan al aprobarlas: una puede dejar de aplicar si la situación cambió.',
                     'El copiloto no aprueba ni ejecuta nada: cada asunto se resuelve en su pantalla.'])


def riesgo_agotamiento(user, dias=30):
    R, plan_list = _plans()
    risky = [pl for pl in plan_list if pl.status in ('pedir_ya', 'pedir_pronto') or (pl.coverage_days is not None and pl.coverage_days <= dias)]
    risky.sort(key=lambda pl: (_urgency(pl), pl.product.name))
    rows = []
    for pl in risky:
        lead = days(pl.lead_days) + (' (mediana observada)' if pl.lead_n else ' (parámetro)')
        rows.append([cell(user, clean(pl.product.name), 'forecast', pl.product.pk), pl.product.code, fnum(pl.available),
                     days(pl.coverage_days) if pl.coverage_days is not None else 'sin dato', _runout(pl), lead, _forecast_label(pl), _error_label(pl),
                     pl.status_label])
    shown, more = cut(rows)
    now = sum(1 for pl in risky if pl.status == 'pedir_ya')
    soon = sum(1 for pl in risky if pl.status == 'pedir_pronto')
    if not risky:
        summary = f'Ninguna de las {len(plan_list)} referencias activas tiene cobertura inferior a {days(dias)} ni está en su punto de pedido.'
    else:
        top = risky[0]
        cover = f'cobertura de {days(top.coverage_days)}' if top.coverage_days is not None else 'sin existencias suficientes para estimar cobertura'
        summary = (f'{plural(len(risky), "referencia podría agotarse", "referencias podrían agotarse")} en los próximos {days(dias)} o ya '
                   f'{"está" if len(risky) == 1 else "están"} en su punto de pedido (de {len(plan_list)} activas): {now} para pedir ya y {soon} para pedir pronto. '
                   f'La más urgente es {clean(top.product.name)}: {cover} frente a {days(top.lead_days)} de entrega.')
    no_history = sum(1 for pl in plan_list if pl.status == 'sin_datos')
    limitations = ['Pronóstico calculado con los movimientos sintéticos de la demostración; demuestra el método, no un resultado comercial.',
                   'Las salidas observadas no son la demanda total: las semanas sin existencias se excluyen al estimar el consumo.',
                   'El error medido es el de la ventana de prueba, que no se usó para elegir el método; con pocas semanas es solo indicativo.',
                   'El tiempo de entrega es la mediana observada de cada proveedor o, si no hay pedidos recibidos, un parámetro.',
                   'La cobertura supone que el consumo sigue como el pronóstico; es una sugerencia y la decisión de pedir es de una persona.']
    if no_history:
        limitations.append(f'{plural(no_history, "referencia no tiene", "referencias no tienen")} historial suficiente y se siguen solo con el mínimo del catálogo.')
    return result(
        'Referencias que podrían agotarse', f'Próximos {days(dias)} desde el {fdate(timezone.localdate())}', summary,
        blocks=[table('Cobertura frente a tiempo de entrega', ['Referencia', 'Código', 'Disponible', 'Cobertura', 'Se agotaría hacia', 'Entrega del proveedor', 'Pronóstico semanal', 'Error medido', 'Estado'],
                      shown, numeric=[2], more=more)],
        links=[link(user, 'Abrir Reposición', 'replenishment')],
        sources=['Plan de reposición por referencia: posición, pronóstico semanal y punto de pedido', 'Movimientos de inventario, facturas por recibir y pedidos abiertos'],
        limitations=limitations)


def propuesta_compra(user, presupuesto=None):
    R, plan_list = _plans()
    configured = False
    if presupuesto is None:
        presupuesto = _budget_from_params(R)
        configured = presupuesto is not None
    bp = budget_plan(R, plan_list, presupuesto)
    inc_rows, exc_rows = [], []
    for it in bp['included']:
        pl = it['plan']
        cover = days(pl.coverage_days) if pl.coverage_days is not None else 'sin dato'
        why = f'Cobertura {cover}; entrega en {days(pl.lead_days)}.' + (f' {it["note"]}' if it.get('note') else '')
        inc_rows.append([cell(user, clean(pl.product.name), 'forecast', pl.product.pk), clean(pl.supplier.name) if pl.supplier else 'Sin proveedor habitual',
                         f'{fnum(it["quantity"])} {pl.product.units}', cop(it['cost']) if it['cost'] is not None else 'sin costo registrado', why])
    for it in bp['excluded']:
        pl = it['plan']
        exc_rows.append([cell(user, clean(pl.product.name), 'forecast', pl.product.pk), f'{fnum(it["quantity"])} {pl.product.units}',
                         cop(it['cost']) if it['cost'] is not None else 'sin costo registrado', it.get('reason') or 'Queda por fuera del presupuesto.'])
    inc_shown, inc_more = cut(inc_rows)
    exc_shown, exc_more = cut(exc_rows)
    budget = bp['budget']
    committed = bp.get('committed') or ZERO
    available = max(budget - committed, ZERO) if budget else None
    if not bp['included'] and not bp['excluded']:
        summary = 'Ninguna referencia está hoy en su punto de pedido: no hay propuesta de compra que preparar.'
    elif budget and committed >= budget:
        # El presupuesto es por periodo de revisión e incluye lo ya pedido: se dice, en vez de mostrar un simple cero.
        summary = (f'El presupuesto de {cop(budget)} ya está comprometido en pedidos abiertos: suman {cop(committed)} en este periodo de revisión, '
                   'así que no queda saldo para pedidos nuevos. '
                   + ('La referencia sugerida queda ' if len(bp['excluded']) == 1 else f'Las {len(bp["excluded"])} referencias sugeridas quedan ')
                   + f'por fuera ({cop(bp["total"])} en total); para incluir alguna, el presupuesto debe superar lo comprometido.')
    else:
        summary = (f'La propuesta incluye {plural(len(bp["included"]), "referencia", "referencias")} por {cop(bp["spent"])}'
                   + (f', dentro de un presupuesto de {cop(budget)}' if budget else ', sin tope de presupuesto')
                   + (f'; {plural(len(bp["excluded"]), "renglón queda", "renglones quedan")} por fuera' if bp['excluded'] else '') + '.'
                   + (f' De ese presupuesto, {cop(committed)} ya están comprometidos en pedidos abiertos de este periodo de revisión y quedan '
                      f'{cop(available)} disponibles para pedidos nuevos.' if budget and committed else '')
                   + (' El presupuesto es por periodo de revisión y hoy no hay pedidos abiertos que lo consuman.' if budget and not committed else '')
                   + ' Se prioriza lo que se agotaría antes de que alcance a llegar un pedido.')
    if presupuesto is None:
        summary += ' No indicó una cifra y no hay presupuesto configurado: escriba, por ejemplo, «con 8 millones» para ver el recorte.'
    elif configured:
        summary += ' No indicó una cifra: se usó el presupuesto de compra configurado.'
    query = {'presupuesto': int(budget)} if budget else None
    return result(
        'Propuesta de compra', f'Posición al {fdate(timezone.localdate())}', summary,
        blocks=[facts('Resumen de la propuesta', [
                    ['Presupuesto', cop(budget) if budget else 'Sin tope'], ['Valor de la propuesta', cop(bp['spent'])],
                    ['Costo de todo lo sugerido', cop(bp['total']) if bp['total'] != bp['spent'] else None],
                    ['Comprometido en pedidos abiertos', cop(committed) if committed else None],
                    ['Disponible para pedidos nuevos', cop(available) if available is not None and committed else None],
                    ['Saldo sin usar', cop(bp['left']) if bp.get('left') is not None else None],
                    ['Referencias incluidas', fnum(len(bp['included']))], ['Renglones por fuera', fnum(len(bp['excluded']))]]),
                table('Incluidas, por urgencia', ['Referencia', 'Proveedor habitual', 'Cantidad', 'Costo estimado', 'Por qué'], inc_shown, numeric=[3], more=inc_more),
                table('Lo que queda por fuera', ['Referencia', 'Cantidad', 'Costo estimado', 'Motivo'], exc_shown, numeric=[2], more=exc_more)],
        links=[link(user, 'Revisar y preparar el pedido en Reposición', 'replenishment', query=query)],
        sources=['Plan de reposición por referencia y sus cantidades sugeridas, con la misma regla de presupuesto de la pantalla de Reposición',
                 'Último costo de compra validado de cada referencia'],
        limitations=['El copiloto no crea pedidos: la propuesta se revisa y se prepara en Reposición, con su aprobación.',
                     'El costo es una estimación con el último costo validado; no incluye impuestos, fletes ni descuentos por pronto pago.',
                     'Las cantidades se redondean al empaque de compra y se limitan por vencimiento.',
                     'Cifras calculadas con datos sintéticos de la demostración.'],
        budget=int(budget) if budget else None)


DECISIONS = {'suggested': 'Sugerencia clara', 'ambiguous': 'Ambiguo: revisar a mano', 'weak': 'Indicio débil', 'none': 'Sin coincidencias'}


def pagos_por_revisar(user, ingreso_id=None):
    from cartera import services as C
    from cartera.models import BankTransaction
    today = timezone.localdate()
    if ingreso_id is not None:
        txn = BankTransaction.objects.select_related('account', 'party').filter(pk=ingreso_id).first()
        if txn is None:
            return result('Ingreso bancario', '—', 'No existe un ingreso bancario con ese identificador.',
                          links=[link(user, 'Abrir Banco y recaudos', 'bank')], sources=['Movimientos del extracto bancario importado'])
        s = C.suggestions(txn)
        cands = [[cell(user, clean(c['party'].name), 'party', c['party'].pk), c.get('label', ''), ' '.join(clean(r, 200) for r in c['reasons'])] for c in s['candidates'][:6]]
        return result(
            'Ingreso bancario', f'Movimiento del {fdate(txn.date)}',
            f'Ingreso de {cop(txn.amount)} del {fdate(txn.date)}, estado «{txn.get_status_display()}». Resultado del cruce con la cartera: '
            f'{DECISIONS[s["decision"]].lower()}, con {plural(len(s["candidates"]), "cliente candidato", "clientes candidatos")}.',
            blocks=[facts('Datos del extracto (texto del banco, tal como llegó)', [
                        ['Fecha', fdate(txn.date)], ['Valor', cop(txn.amount)], ['Saldo por aplicar', cop(C.remaining(txn))],
                        ['Descripción', clean(txn.description, 200)], ['Referencia', clean(txn.reference)], ['Titular', clean(txn.holder_name)],
                        ['Cuenta', clean(str(txn.account))]]),
                    table('Candidatos y evidencia', ['Cliente', 'Nivel de coincidencia', 'Razones'], cands)],
            links=[link(user, 'Abrir el ingreso para identificarlo', 'bank_txn', txn.pk), link(user, 'Abrir Banco y recaudos', 'bank')],
            sources=['Movimiento del extracto bancario importado', 'Cruce con la cartera abierta: menciones de factura, titular, pagadores confirmados y valores'],
            limitations=['La identificación la confirma contabilidad en la pantalla del ingreso; el copiloto no aplica pagos.',
                         'El modelo de pagos solo opina: su puntaje no es una probabilidad calibrada.',
                         'El extracto y el banco son simulados.'])
    pending = BankTransaction.objects.filter(status=BankTransaction.Status.PENDING, amount__gt=0).select_related('account').order_by('date', 'pk')
    count = pending.count()
    total = sum((t.amount for t in pending), ZERO)
    by_decision = {k: 0 for k in DECISIONS}
    rows = []
    for t in pending[:MAX_ROWS]:
        s = C.suggestions(t)
        by_decision[s['decision']] += 1
        top = s['candidates'][:2] if s['decision'] == 'ambiguous' else s['candidates'][:1]
        who = ' / '.join(clean(c['party'].name, 60) for c in top) or '—'
        evidence = ' '.join(clean(r, 200) for c in top for r in c['reasons'][:2]) or 'Ningún dato del movimiento coincide con la cartera abierta.'
        rows.append([cell(user, fdate(t.date), 'bank_txn', t.pk), cop(t.amount), clean(t.description, 120) or '—', DECISIONS[s['decision']], who, evidence])
    discounts = []
    for inv in S.payables():
        offer = inv.offer
        if offer.get('deadline') and offer.get('amount') and 0 <= (offer['deadline'] - today).days <= DISCOUNT_WINDOW:
            discounts.append((offer['deadline'], inv))
    discounts.sort(key=lambda x: (x[0], x[1].pk))
    saving = sum((inv.offer['amount'] for _, inv in discounts), ZERO)
    disc_rows = [[cell(user, clean(inv.number), 'invoice_pay', inv.pk), clean(inv.supplier.name), S.pct(inv.offer['percent']) + ' %', cop(inv.offer['amount']),
                  f'{fdate(deadline)} ({days((deadline - today).days)})', cop(inv.offer['balance'])] for deadline, inv in discounts[:MAX_ROWS]]
    if not count and not discounts:
        summary = 'No hay ingresos bancarios por identificar ni descuentos por pronto pago próximos a vencer.'
    else:
        summary = f'Hay {plural(count, "ingreso bancario por identificar", "ingresos bancarios por identificar")} por {cop(total)}'
        if rows:
            listed = '' if count <= MAX_ROWS else f' (de los {len(rows)} más antiguos)'
            summary += (f'{listed}: {by_decision["suggested"]} con sugerencia clara, {by_decision["ambiguous"]} '
                        f'{"ambiguo que requiere" if by_decision["ambiguous"] == 1 else "ambiguos que requieren"} revisión manual, '
                        f'{by_decision["weak"]} con indicio débil y {by_decision["none"]} sin coincidencias')
        summary += '. '
        summary += (f'Además, {plural(len(discounts), "factura de proveedor tiene", "facturas de proveedores tienen")} descuento por pronto pago que vence en los '
                    f'próximos {days(DISCOUNT_WINDOW)}: ahorro posible de {cop(saving)}.') if discounts else 'No hay descuentos por pronto pago próximos a vencer.'
    return result(
        'Pagos por revisar', f'Pendientes al {fdate(today)}', summary,
        blocks=[table('Ingresos bancarios por identificar', ['Fecha', 'Valor', 'Descripción del extracto', 'Resultado del cruce', 'Candidatos', 'Evidencia'],
                      rows, numeric=[1], more=max(count - len(rows), 0), note='La descripción es el texto del banco tal como llegó en el extracto.'),
                table(f'Pagos a proveedores con descuento por vencer ({days(DISCOUNT_WINDOW)})', ['Factura', 'Proveedor', 'Descuento', 'Ahorro', 'Fecha límite', 'Saldo'],
                      disc_rows, numeric=[3, 5], more=max(len(discounts) - MAX_ROWS, 0))],
        links=[link(user, 'Abrir Banco y recaudos', 'bank', query={'estado': 'pending'}), link(user, 'Abrir Cuentas por pagar', 'payables')],
        sources=['Ingresos del extracto bancario importado, sin identificar', 'Cruce con la cartera abierta: menciones de factura, titular, pagadores confirmados y valores',
                 'Facturas de proveedores validadas con sus condiciones de pronto pago'],
        limitations=['Cada identificación la confirma contabilidad en la pantalla del ingreso; el copiloto no aplica pagos ni los programa.',
                     'Un caso ambiguo significa que la evidencia apunta por igual a más de un cliente.',
                     'El modelo de pagos solo opina: su puntaje no es una probabilidad calibrada.',
                     'El extracto, el banco y el sistema contable son simulados.'])


def _month(periodo):
    from cartera.results import parse_month, shift
    today = timezone.localdate()
    current = today.replace(day=1)
    if periodo in (None, '', 'actual'):
        return current
    if periodo == 'anterior':
        return shift(current, -1)
    return parse_month(periodo, today)


def estado_operacion(user, periodo=None):
    from agentes import runner
    from cartera import results
    today = timezone.localdate()
    month = _month(periodo)
    is_current = month == today.replace(day=1)
    r = results.report(month)
    sales = r['sales']
    R, plan_list = _plans()
    now = sum(1 for pl in plan_list if pl.status == 'pedir_ya')
    soon = sum(1 for pl in plan_list if pl.status == 'pedir_pronto')
    lots = Batch.objects.filter(physical__gt=0)
    expired = lots.filter(expiry__lt=today).count()
    expiring = lots.filter(expiry__gte=today, expiry__lte=today + timedelta(days=30)).count()
    proposals = runner.pending_for(user).count()
    decisions = []
    agents_link = link(user, 'Agentes', 'agents')
    if proposals:
        decisions.append({'text': plural(proposals, 'propuesta de agentes por decidir', 'propuestas de agentes por decidir'), 'detail': 'Se aprueban o descartan en Agentes.',
                          'url': agents_link['url'] if agents_link else '', 'level': 'warning'})
    requests = SupplyRequest.objects.filter(status='pending').count() if user.role in S.MANAGERS else 0
    if requests:
        ln = link(user, 'Solicitudes', 'requests')
        decisions.append({'text': plural(requests, 'solicitud interna por atender', 'solicitudes internas por atender'), 'detail': 'Inventario aprueba o rechaza cada una.',
                          'url': ln['url'] if ln else '', 'level': 'warning'})
    if r.get('pending_requests') and user.role == 'owner':
        ln = link(user, 'Descuentos', 'discounts')
        decisions.append({'text': plural(r['pending_requests'], 'descuento de venta espera', 'descuentos de venta esperan') + ' confirmación de la dirección',
                          'detail': 'La venta no se emite con descuento hasta que se decida.', 'url': ln['url'] if ln else '', 'level': 'warning'})
    if r['owe_late_count']:
        ln = link(user, 'Cuentas por pagar', 'payables')
        decisions.append({'text': plural(r['owe_late_count'], 'factura de proveedor vencida', 'facturas de proveedores vencidas'), 'detail': f'Suman {cop(r["owe_late"])}.',
                          'url': ln['url'] if ln else '', 'level': 'critical'})
    margin = f'{str(sales["margin"]).replace(".", ",")} %' if sales.get('margin') is not None else 'sin ventas'
    summary = (f'En {r["month_name"]}{" (mes en curso, cifras parciales)" if is_current else ""} se facturaron {cop(sales["revenue"])} en '
               f'{plural(sales["count"], "venta", "ventas")} y se recaudaron {cop(r["collected"])}. Al día de hoy la cartera suma {cop(r["ar_total"])}, de los cuales '
               f'{cop(r["ar_overdue"])} están vencidos; a proveedores se deben {cop(r["owe"])}, con {cop(r["owe_late"])} vencidos. En inventario, '
               f'{plural(now, "referencia está", "referencias están")} en su punto de pedido y {soon} más lo {"alcanza" if soon == 1 else "alcanzan"} pronto; '
               f'{plural(expiring, "lote vence", "lotes vencen")} en 30 días y {plural(expired, "lote vencido conserva", "lotes vencidos conservan")} saldo. '
               f'Hay {plural(len(decisions), "frente con decisiones pendientes", "frentes con decisiones pendientes")}.')
    return result(
        'Estado de la operación', f'{r["month_name"].capitalize()} · saldos al {fdate(today)}', summary,
        blocks=[facts(f'Ventas y recaudo de {r["month_name"]}', [
                    ['Ventas facturadas (antes de impuestos)', cop(sales['revenue'])], ['Ventas emitidas', fnum(sales['count'])],
                    ['Utilidad bruta de gestión', cop(sales['profit'])], ['Margen bruto', margin],
                    ['Unidades vendidas sin costo de compra registrado', fnum(sales['uncosted']) if sales.get('uncosted') else None],
                    ['Recaudo aplicado en el periodo', cop(r['collected'])],
                    ['Pronto pago aprovechado', cop(r['saved'])], ['Pronto pago no aprovechado', cop(r['lost'])], ['Bajas de inventario al costo', cop(r['inventory_losses'])]]),
                facts(f'Cartera y cuentas por pagar al {fdate(today)}', [
                    ['Cartera total', f'{cop(r["ar_total"])} en {plural(r["ar_count"], "documento", "documentos")}'],
                    ['Cartera vencida', f'{cop(r["ar_overdue"])} en {plural(r["ar_late_count"], "documento", "documentos")}'],
                    ['Cuentas por pagar', f'{cop(r["owe"])} en {plural(r["owe_count"], "factura", "facturas")}'],
                    ['Cuentas por pagar vencidas', f'{cop(r["owe_late"])} en {plural(r["owe_late_count"], "factura", "facturas")}'],
                    ['Por pagar en los próximos 30 días', cop(r['owe_soon'])]]),
                facts(f'Inventario al {fdate(today)}', [
                    ['Referencias para pedir ya', fnum(now)], ['Referencias para pedir pronto', fnum(soon)], ['Lotes que vencen en 30 días', fnum(expiring)],
                    ['Lotes vencidos con saldo', fnum(expired)], ['Inventario al costo', cop(r['stock_value'])], ['Del cual vence en 60 días', cop(r['stock_soon'])]]),
                listing('Decisiones pendientes', decisions)],
        links=[link(user, 'Abrir Resultados', 'results', query={'mes': month.strftime('%Y-%m')}), link(user, 'Abrir Cartera', 'portfolio'),
               link(user, 'Abrir Cuentas por pagar', 'payables'), link(user, 'Abrir Reposición', 'replenishment'), agents_link],
        sources=['Ventas emitidas y recaudos aplicados del periodo', 'Cartera abierta y cuentas por pagar con su saldo actual',
                 'Lotes de inventario y plan de reposición', 'Propuestas pendientes de los agentes'],
        limitations=['Es una lectura de gestión, no un estado de resultados contable: no incluye nómina, arriendo, servicios ni impuestos.',
                     'Cartera, cuentas por pagar e inventario son saldos al momento de la consulta, no al cierre del periodo.',
                     (f'La utilidad bruta no descuenta el costo de {plural(sales["uncosted"], "unidad vendida", "unidades vendidas")} sin costo de compra registrado: está sobrestimada.'
                      if sales.get('uncosted') else 'La utilidad bruta usa el costo de compra de cada lote vendido.'),
                     'Si un lote no tiene costo de compra registrado, no entra en el inventario al costo.',
                     'Cifras de un escenario sintético: demuestran el funcionamiento, no resultados de una empresa.'])


def _find_products(texto):
    text = clean(texto, 80)
    if not text:
        return []
    exact = list(Product.objects.filter(Q(code__iexact=text) | Q(name__iexact=text)))
    if exact:
        return exact
    code = re.search(r'\b[A-Za-z]{2,5}-\d{1,4}\b', text)
    if code:
        found = list(Product.objects.filter(code__iexact=code.group(0)))
        if found:
            return found
    words = [w for w in re.split(r'[^\w]+', text) if len(w) >= 3][:6]
    if not words:
        return []
    qs = Product.objects.filter(active=True)
    for w in words:
        qs = qs.filter(Q(name__icontains=w) | Q(code__icontains=w) | Q(generic__icontains=w))
    found = list(qs[:9])
    if found:
        return found
    any_q = Q()
    for w in words:
        any_q |= Q(name__icontains=w) | Q(code__icontains=w)
    return list(Product.objects.filter(active=True).filter(any_q)[:9])


def detalle_referencia(user, texto=None, referencia_id=None):
    today = timezone.localdate()
    if referencia_id is not None:
        found = list(Product.objects.filter(pk=referencia_id))
    else:
        found = _find_products(texto or '')
    sources = ['Catálogo de referencias', 'Lotes y movimientos de inventario', 'Plan de reposición de la referencia']
    if not found:
        return result('Detalle de una referencia', '—', 'No encontré una referencia con ese nombre o código. Pruebe con el código interno (por ejemplo, el que aparece en Existencias).',
                      links=[link(user, 'Abrir Reposición', 'replenishment')], sources=sources[:1])
    if len(found) > 1:
        rows = [[cell(user, clean(p.name), 'forecast', p.pk), p.code, p.get_category_display()] for p in found]
        return result('Detalle de una referencia', '—', f'{len(found)} referencias coinciden con lo que escribió. Indique el código de la que quiere consultar.',
                      blocks=[table('Referencias que coinciden', ['Referencia', 'Código', 'Categoría'], rows)], sources=sources[:1],
                      limitations=['La búsqueda compara nombre, código y descripción del catálogo.'])
    p = found[0]
    R, plan_list = _plans(products=[p])
    pl = plan_list[0] if plan_list else None
    lots = list(Batch.objects.filter(product=p, physical__gt=0).order_by('expiry', 'pk'))
    usable = [b for b in lots if b.expiry >= today]
    physical, reserved = sum(b.physical for b in usable), sum(b.reserved for b in usable)
    expired_units = sum(b.physical for b in lots if b.expiry < today)
    lot_rows = [[cell(user, clean(b.lot), 'trace', b.pk), clean(b.location), fdate(b.expiry), 'Vencido' if b.expiry < today else days((b.expiry - today).days),
                 fnum(b.physical), fnum(b.reserved), fnum(0 if b.expiry < today else b.physical - b.reserved)] for b in lots[:MAX_ROWS]]
    stock_rows = [['Código', p.code], ['Categoría', p.get_category_display()], ['Unidad de registro', p.unit], ['Empaque de compra', f'{p.pack_size} {p.units if p.pack_size != 1 else p.unit}'],
                  ['Físico vigente', fnum(physical)], ['Reservado', fnum(reserved)], ['Disponible', fnum(physical - reserved)],
                  ['Vencido con saldo', fnum(expired_units) if expired_units else None], ['Mínimo del catálogo', fnum(p.minimum) if p.minimum is not None else 'sin definir']]
    blocks = [facts('Existencias', stock_rows)]
    summary = f'{clean(p.name)} ({p.code}): {fnum(physical - reserved)} {p.units} disponibles en {plural(len(usable), "lote vigente", "lotes vigentes")}.'
    if pl is not None:
        blocks.append(facts('Posición y pronóstico', [
            ['Disponible', fnum(pl.available)], ['Por recibir de facturas', fnum(pl.in_transit)], ['En pedidos abiertos sin factura', fnum(pl.on_order)],
            ['Posición', fnum(pl.position)], ['Cobertura', days(pl.coverage_days) if pl.coverage_days is not None else 'sin dato'],
            ['Se agotaría hacia', _runout(pl) if pl.coverage_days is not None else None],
            ['Solicitudes pendientes sin reservar', fnum(getattr(pl, 'pending_requests', 0)) if getattr(pl, 'pending_requests', 0) else None],
            ['Pronóstico', _forecast_label(pl)], ['Método', _method_label(pl)], ['Error medido', _error_label(pl)],
            ['Cobertura medida del intervalo', _coverage_label(pl)],
            ['Tiempo de entrega', days(pl.lead_days) + (' (mediana observada)' if pl.lead_n else ' (parámetro)')],
            ['Proveedor habitual', clean(pl.supplier.name) if pl.supplier else 'sin definir'],
            ['Punto de pedido', f'{fdec(pl.reorder_point, 0)} {p.units}' if pl.reorder_point else None],
            ['Estado', pl.status_label], ['Cantidad sugerida', f'{fnum(pl.quantity)} {p.units}' if pl.quantity else 'ninguna por ahora'],
            ['Unidades que vencerían sin usarse', fnum(pl.at_risk) if pl.at_risk else None]]))
        summary += ' ' + clean(pl.summary, 320)
    blocks.append(table('Lotes con saldo (primero vence, primero sale)', ['Lote', 'Ubicación', 'Vence', 'Faltan', 'Físico', 'Reservado', 'Disponible'], lot_rows,
                        numeric=[4, 5, 6], more=max(len(lots) - MAX_ROWS, 0)))
    return result(
        f'Detalle de {clean(p.name, 80)}', f'Posición al {fdate(today)}', summary, blocks=blocks,
        links=[link(user, 'Ver el pronóstico de la referencia', 'forecast', p.pk), link(user, 'Ver sus lotes en Existencias', 'stock', query={'q': p.code}),
               link(user, 'Abrir Reposición', 'replenishment')],
        sources=sources,
        limitations=['La sugerencia es provisional y supone que el consumo sigue como el pronóstico.',
                     'Las salidas observadas no son la demanda total: las semanas sin existencias se excluyen al estimar.',
                     'Datos sintéticos de la demostración.'])


def cartera_vencida(user, cliente_id=None):
    from agentes import runner
    from cartera import services as C
    from cartera.models import Party
    today = timezone.localdate()
    party = None
    if cliente_id is not None:
        party = Party.objects.filter(pk=cliente_id).first()
        if party is None:
            return result('Cartera vencida', '—', 'No existe un cliente con ese identificador.', links=[link(user, 'Abrir Cartera', 'portfolio')], sources=['Clientes registrados'])
    rows = list(C.open_receivables(party))
    buckets = C.aging(rows, today)
    late = [r for r in rows if r.days_late > 0]
    total = sum((r.balance for r in rows), ZERO)
    overdue = sum((r.balance for r in late), ZERO)
    by_party = {}
    for r in late:
        d = by_party.setdefault(r.party_id, {'party': r.party, 'n': 0, 'balance': ZERO, 'oldest': 0})
        d['n'] += 1
        d['balance'] += r.balance
        d['oldest'] = max(d['oldest'], r.days_late)
    party_rows = [[cell(user, clean(d['party'].name), 'party', d['party'].pk), fnum(d['n']), cop(d['balance']), days(d['oldest'])]
                  for d in sorted(by_party.values(), key=lambda d: -d['balance'])]
    doc_rows = []
    for r in sorted(late, key=lambda r: -r.days_late):
        doc = clean(r.document)
        doc_rows.append([cell(user, doc, 'sale', r.sale_id) if r.sale_id else doc, clean(r.party.name), fdate(r.due_date), days(r.days_late), cop(r.balance)])
    party_shown, party_more = cut(party_rows)
    doc_shown, doc_more = cut(doc_rows)
    reminders = runner.pending_for(user).filter(agent='cobranza').count()
    who = f' de {clean(party.name)}' if party else ''
    if not late:
        summary = f'No hay cartera vencida{who}. La cartera abierta suma {cop(total)} en {plural(len(rows), "documento", "documentos")}.'
    else:
        oldest = max(r.days_late for r in late)
        summary = (f'La cartera vencida{who} suma {cop(overdue)} en {plural(len(late), "documento", "documentos")} de {plural(len(by_party), "cliente", "clientes")} '
                   f'(de {cop(total)} de cartera abierta). El documento más antiguo lleva {days(oldest)} vencido.')
        if reminders:
            summary += f' Hay {plural(reminders, "recordatorio de cobranza preparado", "recordatorios de cobranza preparados")} por aprobar.'
    return result(
        'Cartera vencida' + (f' · {clean(party.name, 60)}' if party else ''), f'Saldos al {fdate(today)}', summary,
        blocks=[facts('Antigüedad de la cartera', [['Al día', cop(buckets['current'])], ['1 a 30 días', cop(buckets['d30'])], ['31 a 60 días', cop(buckets['d60'])],
                                                    ['61 a 90 días', cop(buckets['d90'])], ['Más de 90 días', cop(buckets['over'])]]),
                table('Clientes con saldo vencido', ['Cliente', 'Documentos vencidos', 'Saldo vencido', 'Más antiguo'], party_shown, numeric=[1, 2], more=party_more),
                table('Documentos vencidos, del más antiguo al más reciente', ['Documento', 'Cliente', 'Venció', 'Días vencido', 'Saldo'], doc_shown, numeric=[4], more=doc_more)],
        links=[link(user, 'Abrir Cartera', 'portfolio'), link(user, f'Abrir la ficha de {clean(party.name, 50)}', 'party', party.pk) if party else None,
               link(user, 'Revisar recordatorios en Agentes', 'agents') if reminders else None],
        sources=['Cuentas por cobrar abiertas con su saldo (valor menos pagos aplicados y ajustes)'],
        limitations=['Saldos al momento de la consulta; un ingreso bancario sin identificar todavía no reduce la cartera.',
                     'Los recordatorios de cobranza son envíos simulados: nada sale de la plataforma.',
                     'Datos sintéticos de la demostración.'])


def lotes_por_vencer(user, dias=30):
    today = timezone.localdate()
    lots = list(Batch.objects.select_related('product').filter(physical__gt=0, expiry__lte=today + timedelta(days=dias)).order_by('expiry', 'pk'))
    expired = [b for b in lots if b.expiry < today]
    soon = [b for b in lots if b.expiry >= today]
    rows = [[cell(user, clean(b.product.name), 'trace', b.pk), clean(b.lot), clean(b.location), fdate(b.expiry),
             'Vencido: registrar baja' if b.expiry < today else f'Vence en {days((b.expiry - today).days)}', fnum(b.physical), fnum(b.reserved)] for b in lots]
    shown, more = cut(rows)
    if not lots:
        summary = f'Ningún lote con saldo vence en los próximos {days(dias)} y no hay lotes vencidos con unidades.'
    else:
        summary = (f'{plural(len(soon), "lote con saldo vence", "lotes con saldo vencen")} en los próximos {days(dias)} ({fnum(sum(b.physical for b in soon))} unidades) y '
                   f'{plural(len(expired), "lote ya vencido conserva", "lotes ya vencidos conservan")} saldo ({fnum(sum(b.physical for b in expired))} unidades) y '
                   f'{"espera" if len(expired) == 1 else "esperan"} su baja.')
    return result(
        'Lotes por vencer', f'Próximos {days(dias)} desde el {fdate(today)}', summary,
        blocks=[table('Lotes con saldo, por fecha de vencimiento', ['Referencia', 'Lote', 'Ubicación', 'Vence', 'Estado', 'Físico', 'Reservado'], shown, numeric=[5, 6], more=more)],
        links=[link(user, 'Abrir Existencias', 'stock'), link(user, 'Revisar bajas propuestas en Agentes', 'agents') if expired else None],
        sources=['Lotes de inventario con unidades físicas y su fecha de vencimiento'],
        limitations=['Cuenta unidades físicas del lote, incluidas las reservadas.',
                     'Un lote vencido no se despacha: su baja se registra en Inventario o al aprobar la propuesta del agente.',
                     'Datos sintéticos de la demostración.'])


def historial_decisiones(user, dias=30):
    from agentes import runner
    from agentes.agents import REGISTRY
    from agentes.models import AgentAction
    keys = [a.key for a in REGISTRY.values() if runner.can_see(user, a)]
    since = timezone.now() - timedelta(days=dias)
    qs = AgentAction.objects.filter(agent__in=keys, decided_at__gte=since).exclude(status=AgentAction.Status.PROPOSED).select_related('decided_by').order_by('-decided_at')
    actions = list(qs[:200])
    counts = {}
    for a in actions:
        counts[a.status] = counts.get(a.status, 0) + 1
    labels = dict(AgentAction.Status.choices)
    rows = [[timezone.localtime(a.decided_at).strftime('%d/%m/%Y %H:%M'), REGISTRY[a.agent].name if a.agent in REGISTRY else a.agent, clean(a.title),
             labels.get(a.status, a.status), a.decided_by.display_name if a.decided_by else 'Automático', clean(a.note) or '—'] for a in actions]
    shown, more = cut(rows)
    if not actions:
        summary = f'En los últimos {days(dias)} no se decidió ninguna propuesta de los agentes que su perfil puede ver.'
    else:
        summary = (f'En los últimos {days(dias)} se cerraron {plural(len(actions), "propuesta", "propuestas")}: {counts.get("executed", 0)} aprobadas y ejecutadas, '
                   f'{counts.get("rejected", 0)} descartadas, {counts.get("superseded", 0)} que dejaron de aplicar y {counts.get("failed", 0)} que no se pudieron ejecutar.')
    return result(
        'Historial de decisiones', f'Últimos {days(dias)} hasta el {fdate(timezone.localdate())}', summary,
        blocks=[facts('Resultado de las propuestas', [[labels.get(k, k), fnum(v)] for k, v in sorted(counts.items())]),
                table('Propuestas decididas, de la más reciente a la más antigua', ['Fecha', 'Agente', 'Propuesta', 'Decisión', 'Quién', 'Nota'], shown, more=more)],
        links=[link(user, 'Abrir Agentes', 'agents')],
        sources=['Propuestas de los agentes con su decisión, la persona que decidió y la fecha'],
        limitations=['Solo incluye los agentes que su perfil puede ver.',
                     '«Ya no aplica» significa que la situación cambió antes de aprobar la propuesta.',
                     'Las acciones aprobadas quedan además en la auditoría de la plataforma.'])


# ------------------------------------------------------------------ registro y validación

@dataclass
class Tool:
    name: str
    title: str
    description: str
    roles: tuple
    run: callable
    params: dict = field(default_factory=dict)

    def schema(self):
        """Esquema JSON de los argumentos, para un proveedor generativo."""
        props, required = {}, []
        for key, spec in self.params.items():
            kind = spec['type']
            prop = {'description': spec.get('description', '')}
            if kind == 'int':
                prop.update(type='integer', minimum=spec['min'], maximum=spec['max'])
            else:
                prop.update(type='string', maxLength=spec.get('max_length', 80))
                if spec.get('pattern'):
                    prop['pattern'] = spec['pattern']
            props[key] = prop
            if spec.get('required'):
                required.append(key)
        return {'type': 'object', 'properties': props, 'required': required, 'additionalProperties': False}


TOOLS = {t.name: t for t in [
    Tool('atencion_hoy', 'Qué necesita atención hoy', 'Alertas del perfil y propuestas de agentes pendientes, priorizadas, con el porqué de cada una.',
         S.EVERYONE, atencion_hoy),
    Tool('riesgo_agotamiento', 'Referencias que podrían agotarse',
         'Referencias cuya cobertura es menor que el horizonte o que ya están en su punto de pedido: cobertura frente a tiempo de entrega, pronóstico con su rango y error medido.',
         VIEWERS, riesgo_agotamiento,
         {'dias': {'type': 'int', 'min': 1, 'max': 365, 'default': 30, 'description': 'Horizonte en días (1 a 365).'}}),
    Tool('propuesta_compra', 'Propuesta de compra',
         'Arma una propuesta de compra con un presupuesto en pesos, priorizada por urgencia y recortada en múltiplos de empaque. No crea pedidos: enlaza a Reposición.',
         VIEWERS, propuesta_compra,
         {'presupuesto': {'type': 'int', 'min': 0, 'max': MAX_BUDGET, 'description': 'Presupuesto en pesos colombianos, sin decimales. 0 u omitido = sin tope o el configurado.'}}),
    Tool('pagos_por_revisar', 'Pagos por revisar',
         'Ingresos bancarios por identificar con candidatos, razones y evidencia (señala los ambiguos) y pagos a proveedores con descuento por pronto pago por vencer.',
         S.FINANCE, pagos_por_revisar,
         {'ingreso_id': {'type': 'int', 'min': 1, 'max': 2_000_000_000, 'description': 'Identificador de un ingreso bancario, para ver solo ese.'}}),
    Tool('estado_operacion', 'Estado de la operación',
         'Resumen para la dirección: ventas y recaudo del periodo, cartera y cuentas por pagar vencidas, riesgo de inventario, vencimientos y decisiones pendientes.',
         S.FINANCE, estado_operacion,
         {'periodo': {'type': 'str', 'pattern': r'^(actual|anterior|20\d\d-(0[1-9]|1[0-2]))$', 'max_length': 8,
                      'description': 'Mes a resumir: «actual», «anterior» o AAAA-MM.'}}),
    Tool('detalle_referencia', 'Detalle de una referencia',
         'Existencias, lotes, posición, pronóstico y sugerencia de una referencia, buscada por nombre o código.',
         VIEWERS, detalle_referencia,
         {'texto': {'type': 'str', 'max_length': 80, 'description': 'Nombre o código de la referencia.'},
          'referencia_id': {'type': 'int', 'min': 1, 'max': 2_000_000_000, 'description': 'Identificador interno de la referencia.'}}),
    Tool('cartera_vencida', 'Cartera vencida', 'Cuentas por cobrar vencidas por cliente y antigüedad, con sus documentos.',
         S.FINANCE, cartera_vencida,
         {'cliente_id': {'type': 'int', 'min': 1, 'max': 2_000_000_000, 'description': 'Identificador de un cliente, para ver solo su cartera.'}}),
    Tool('lotes_por_vencer', 'Lotes por vencer', 'Lotes con saldo que vencen dentro del horizonte y lotes ya vencidos que esperan baja.',
         S.MANAGERS, lotes_por_vencer,
         {'dias': {'type': 'int', 'min': 1, 'max': 365, 'default': 30, 'description': 'Horizonte en días (1 a 365).'}}),
    Tool('historial_decisiones', 'Historial de decisiones', 'Propuestas de agentes aprobadas, descartadas o que dejaron de aplicar en los últimos días.',
         VIEWERS, historial_decisiones,
         {'dias': {'type': 'int', 'min': 1, 'max': 365, 'default': 30, 'description': 'Días hacia atrás (1 a 365).'}}),
]}


def allowed(user):
    """Herramientas que el perfil puede usar."""
    return [t for t in TOOLS.values() if getattr(user, 'is_authenticated', False) and user.is_active and user.role in t.roles]


def validate(tool, args):
    """Devuelve los argumentos limpios o lanza ToolError. Nada fuera del esquema pasa a la herramienta."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError('Los argumentos deben ser un objeto con nombre y valor.')
    unknown = sorted(set(args) - set(tool.params))
    if unknown:
        raise ToolError(f'Argumento no admitido en «{tool.name}»: {", ".join(clean(u, 30) for u in unknown)}.')
    out = {}
    for key, spec in tool.params.items():
        value = args.get(key)
        if value is None or value == '':
            if spec.get('required'):
                raise ToolError(f'Falta el argumento «{key}».')
            if 'default' in spec:
                out[key] = spec['default']
            continue
        if spec['type'] == 'int':
            if isinstance(value, bool):
                raise ToolError(f'«{key}» debe ser un número entero.')
            if isinstance(value, float):
                if value != int(value):
                    raise ToolError(f'«{key}» debe ser un número entero.')
                value = int(value)
            elif isinstance(value, Decimal):
                if value != value.to_integral_value():
                    raise ToolError(f'«{key}» debe ser un número entero.')
                value = int(value)
            elif isinstance(value, str):
                if not re.fullmatch(r'-?\d{1,15}', value.strip()):
                    raise ToolError(f'«{key}» debe ser un número entero.')
                value = int(value.strip())
            elif not isinstance(value, int):
                raise ToolError(f'«{key}» debe ser un número entero.')
            if not spec['min'] <= value <= spec['max']:
                raise ToolError(f'«{key}» está fuera de rango: debe estar entre {fnum(spec["min"])} y {fnum(spec["max"])}.')
        else:
            if not isinstance(value, str):
                raise ToolError(f'«{key}» debe ser texto.')
            value = clean(value, 400)
            if len(value) > spec.get('max_length', 80):
                raise ToolError(f'«{key}» es demasiado largo (máximo {spec.get("max_length", 80)} caracteres).')
            if spec.get('pattern') and not re.fullmatch(spec['pattern'], value):
                raise ToolError(f'«{key}» no tiene un valor admitido.')
        out[key] = value
    return out


def run(user, name, args=None):
    """Ejecuta una herramienta con los permisos del usuario. Devuelve (resultado, estado, argumentos limpios).

    Estado: «ok» o «denied» (acceso restringido: sin datos). Lanza ToolError si el nombre o los argumentos no valen.
    """
    tool = TOOLS.get(name) if isinstance(name, str) else None
    if tool is None:
        raise ToolError('Esa herramienta no existe.')
    if not getattr(user, 'is_authenticated', False) or not user.is_active or user.role not in tool.roles:
        return denied(tool, user), 'denied', {}
    cleaned = validate(tool, args)
    return tool.run(user, **cleaned), 'ok', cleaned
