"""Modelo de pagos: aprende de cada ingreso bancario que contabilidad identifica.

Qué aprende: qué señales de un movimiento del extracto (menciones de factura, identificación y nombre del titular,
coincidencia de valores, palabras del texto, pagadores ya vistos, momento del pago) llevan a cada cliente. Es una
regresión logística en línea sobre rasgos binarios: ligera, explicable y sin dependencias; los rasgos «palabra ×
cliente» funcionan como una memoria de relaciones (por ejemplo, el nombre de quien suele transferir → su empresa) que crece sola.

Cómo aprende: después de cada identificación confirmada, el modelo primero predice (y anota si acertó: evaluación
secuencial) y luego ajusta sus pesos con ese ejemplo. La predicción anotada usa solo lo que se sabía antes de esa
identificación: el vínculo pagador–cliente que la propia identificación crea y los pagos posteriores no cuentan.

Correcciones: si una aplicación se revierte o el ingreso cambia de cliente, ese ejemplo deja de ser verdadero. Se
marca como anulado (`MatchFeedback.voided_at`) y el modelo se reconstruye con el historial vigente (`reconcile`,
`retrain`): queda igual a un modelo que nunca vio ese ejemplo.

Puntajes: la salida del modelo es un puntaje sin calibrar, no una probabilidad. Se muestra como «puntaje (0–100)»
junto a la tasa de acierto medida para puntajes similares (`reliability`).

Qué no hace: no aplica pagos. Sus puntajes entran a `cartera.services.suggestions` como una razón más; la decisión
sigue siendo de contabilidad o del agente de conciliación, con aprobación.
"""
import logging
import math
import random
import re
import unicodedata
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from itertools import combinations
from statistics import median
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from cartera.models import Party, Receivable, BankTransaction, PaymentApplication, PayerLink, ReceivableAdjustment, ZERO
from .models import LearnedModel, MatchFeedback

log = logging.getLogger(__name__)
KEY = 'pagos'
LR, L2, EPOCHS = 0.25, 0.002, 3
MIN_EXAMPLES = 8          # desde aquí el modelo opina en las sugerencias
NEG_LIMIT = 25            # negativos por ejemplo (los demás clientes con cartera abierta)
IDENTIFIED = ('applied', 'identified')
# Tramos del puntaje para la tabla de fiabilidad: (desde, hasta, rótulo en escala 0–100).
BINS = [(0.0, 0.5, '0 a 49'), (0.5, 0.7, '50 a 69'), (0.7, 0.85, '70 a 84'), (0.85, 1.01, '85 a 100')]
FEW = 10                  # con menos casos en un tramo, la tasa medida se muestra como «pocos casos»
STOP = {'transferencia', 'transf', 'trans', 'pago', 'pagos', 'pse', 'ref', 'referencia', 'factura', 'fact', 'abono', 'consignacion', 'cta',
        'cuenta', 'ach', 'sas', 'ltda', 'desde', 'para', 'por', 'del', 'los', 'las', 'con', 'the', 'and', 'nit', 'banco', 'interbancaria',
        'recibida', 'recibido', 'credito', 'cr', 'nal', 'col'}


def _norm(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(text or '')) if not unicodedata.combining(c)).lower()


def tokens(text):
    return {w for w in re.split(r'[^a-z0-9]+', _norm(text)) if len(w) >= 3 and not w.isdigit() and w not in STOP}


def _mentions(text, rec):
    return re.search(rf'\b(?:{re.escape(rec.prefix.lower())}|fact|factura|fe|fv)\s*0*{rec.number}\b', text) is not None


# ----------------------------------------------------------------------------------------- modelo

class OnlineLogit:
    def __init__(self, weights=None, meta=None):
        self.w = dict(weights or {})
        self.meta = dict(meta or {})

    def raw(self, feats):
        return sum(self.w.get(f, 0.0) for f in feats)

    def predict(self, feats):
        z = max(min(self.raw(feats), 30), -30)
        return 1 / (1 + math.exp(-z))

    def update(self, feats, y):
        p = self.predict(feats)
        g = y - p
        for f in feats:
            self.w[f] = self.w.get(f, 0.0) * (1 - LR * L2) + LR * g

    def fit_example(self, positive, negatives, rng):
        rows = [(positive, 1)] + [(n, 0) for n in negatives]
        for _ in range(EPOCHS):
            rng.shuffle(rows)
            for feats, y in rows:
                self.update(feats, y)
        self.meta['examples'] = self.meta.get('examples', 0) + 1

    @property
    def trained(self):
        return self.meta.get('examples', 0) >= MIN_EXAMPLES

    def explain(self, feats, limit=3):
        pairs = sorted(((self.w.get(f, 0.0), f) for f in feats if self.w.get(f)), reverse=True)
        return [(f, round(w, 2)) for w, f in pairs[:limit]]


def load():
    row = LearnedModel.objects.filter(key=KEY).first()
    return OnlineLogit(row.weights, row.meta) if row else OnlineLogit()


def save(model):
    model.meta['features'] = len(model.w)
    model.meta['updated'] = timezone.now().isoformat(timespec='seconds')
    LearnedModel.objects.update_or_create(key=KEY, defaults={'weights': model.w, 'meta': model.meta})


# ---------------------------------------------------------------------------- comportamiento de pago

def behaviour(until=None, exclude_txn=None):
    """Por cliente: cuántos pagos, mediana de días frente al vencimiento (negativo = antes), dispersión, tardíos y abonos.

    `until` y `exclude_txn` dan el comportamiento conocido hasta una fecha, sin el propio movimiento: así el
    aprendizaje de un ingreso no usa pagos posteriores a él."""
    rows = defaultdict(list)
    partial = defaultdict(int)
    qs = PaymentApplication.objects.filter(reversed_at__isnull=True).select_related('receivable', 'collection').order_by('collection__date')
    if until is not None:
        qs = qs.filter(collection__date__lte=until)
    if exclude_txn is not None:
        qs = qs.exclude(collection__transaction=exclude_txn)
    for a in qs:
        rows[a.receivable.party_id].append((a.collection.date - a.receivable.due_date).days)
        if a.amount < a.receivable.amount:
            partial[a.receivable.party_id] += 1
    out = {}
    for pid, days in rows.items():
        s = sorted(days)
        n = len(s)
        out[pid] = {'n': n, 'median': median(s), 'p10': s[max(int(n * 0.1) - 1, 0)], 'p90': s[min(int(math.ceil(n * 0.9)) - 1, n - 1)],
                    'late_share': sum(1 for d in s if d > 0) / n, 'partial_share': partial[pid] / n, 'last': s[-1]}
    return out


def history_label(beh):
    if not beh or beh['n'] < 2:
        return 'nuevo'
    return 'puntual' if beh['median'] <= 0 else 'tardio' if beh['median'] <= 20 else 'moroso'


def expected_payment(rec, beh):
    """Fecha probable de pago de una cuota según el comportamiento del cliente (None sin historial)."""
    if not beh or beh['n'] < 2:
        return None
    return rec.due_date + timedelta(days=int(round(beh['median'])))


# ---------------------------------------------------------------------------------------- rasgos

def _balances_before(txn):
    """Saldo de cada cuota justo antes de este movimiento: sin las aplicaciones del propio movimiento ni las posteriores."""
    recs = list(Receivable.objects.filter(issue_date__lte=txn.date).select_related('party'))
    applied = defaultdict(lambda: ZERO)
    for a in PaymentApplication.objects.filter(reversed_at__isnull=True, receivable__in=recs, collection__date__lte=txn.date).exclude(collection__transaction=txn).values_list('receivable_id', 'amount'):
        applied[a[0]] += a[1]
    adjusted = defaultdict(lambda: ZERO)
    for rid, amount in ReceivableAdjustment.objects.filter(receivable__in=recs).values_list('receivable_id', 'amount'):
        adjusted[rid] += amount
    open_by_party = defaultdict(list)
    for r in recs:
        r.balance = r.amount - applied[r.pk] - adjusted[r.pk]
        if r.balance > 0:
            open_by_party[r.party_id].append(r)
    return open_by_party


def _links_before(txn):
    """Vínculos pagador–cliente que ya existían antes de identificar este movimiento.

    Identificar un ingreso crea (o renueva) el vínculo con el cliente elegido; si ese vínculo contara al aprender
    de ese mismo ingreso, la predicción anotada ya conocería la respuesta. Cuenta solo si el pagador ya había
    pagado a ese cliente en un movimiento identificado antes, o si el vínculo se confirmó antes."""
    if not txn.payer_id:
        return {}
    when = txn.identified_at
    prior = BankTransaction.objects.filter(payer_id=txn.payer_id, party__isnull=False, status__in=IDENTIFIED).exclude(pk=txn.pk)
    prior = prior.filter(identified_at__lt=when) if when else prior.filter(Q(date__lt=txn.date) | Q(date=txn.date, pk__lt=txn.pk))
    paid = set(prior.values_list('party_id', flat=True))
    return {l.party_id: l.status for l in PayerLink.objects.filter(payer_id=txn.payer_id)
            if l.party_id in paid or (when and l.confirmed_at and l.confirmed_at < when)}


def context(txn, open_by_party=None, learning=False):
    """Lo que se sabe del movimiento. Con `learning=True`, solo lo que se sabía antes de identificarlo."""
    if not txn.payer_id:
        links = {}
    elif learning:
        links = _links_before(txn)
    else:
        links = {l.party_id: l.status for l in PayerLink.objects.filter(payer=txn.payer)}
    beh = behaviour(until=txn.date, exclude_txn=txn) if learning else behaviour()
    return {'txn': txn, 'text': _norm(f'{txn.description} {txn.reference}'), 'tokens': tokens(f'{txn.description} {txn.reference}'),
            'holder_tokens': tokens(txn.holder_name), 'holder_id': re.sub(r'\D', '', txn.holder_id or ''), 'links': links,
            'open': open_by_party if open_by_party is not None else _balances_before(txn), 'beh': beh}


def _late_bucket(days):
    return 'current' if days <= 0 else '30' if days <= 30 else '60' if days <= 60 else '90+'


def features(ctx, party):
    """Rasgos binarios del par (movimiento, cliente). Los que llevan «|p:» son propios del cliente (memoria de relaciones)."""
    txn, f, pid = ctx['txn'], {'b'}, party.pk
    recs = ctx['open'].get(pid, [])
    if any(_mentions(ctx['text'], r) for r in recs):
        f.add('mention')
    if ctx['holder_id'] and ctx['holder_id'] == re.sub(r'\D', '', party.identification):
        f.add('holder_id')
    status = ctx['links'].get(pid)
    if status:
        f.add(f'payer_{status}')
        f.add(f'payer:{txn.payer_id}|p:{pid}')
    amount = txn.amount
    if not recs:
        f.add('no_open')
    else:
        balances = [r.balance for r in recs]
        total = sum(balances, ZERO)
        if any(b == amount for b in balances):
            f.add('amt_one')
        if len(recs) > 1 and total == amount:
            f.add('amt_total')
        if 1 < len(recs) <= 8 and any(sum(c, ZERO) == amount for k in (2, 3) for c in combinations(balances, k)):
            f.add('amt_combo')
        if any(b != amount and abs(b - amount) <= b * Decimal('0.015') for b in balances):
            f.add('amt_near')
        if amount > total:
            f.add('amt_over')
        if amount < min(balances) and amount % 10000 == 0:
            f.add('amt_round_partial')
        f.add(f'open_n:{min(len(recs), 3)}')
        oldest = max((txn.date - r.due_date).days for r in recs)
        f.add(f'late:{_late_bucket(oldest)}')
        beh = ctx['beh'].get(pid)
        if beh and beh['n'] >= 3:
            nearest = min(((txn.date - r.due_date).days for r in recs), key=lambda d: abs(d - beh['median']))
            f.add('window_in' if beh['p10'] - 3 <= nearest <= beh['p90'] + 3 else 'window_out')
        else:
            f.add('window_na')
    name = tokens(party.name)
    shared = len((ctx['tokens'] | ctx['holder_tokens']) & name)
    f.add(f'name_overlap:{min(shared, 3)}')
    for w in ctx['tokens']:
        f.add(f'tok:{w}|p:{pid}')
    for w in ctx['holder_tokens']:
        f.add(f'hold:{w}|p:{pid}')
    f.add(f'kind:{party.kind}')
    f.add(f'ctx:{party.default_context}')
    return f


def alias_parties(model, ctx):
    """Clientes con memoria positiva para alguna palabra del movimiento (aunque las reglas no los propongan)."""
    hits = defaultdict(float)
    for w in ctx['tokens'] | ctx['holder_tokens']:
        for prefix in ('tok', 'hold'):
            for k, v in model.w.items():
                if v > 0.3 and k.startswith(f'{prefix}:{w}|p:'):
                    hits[int(k.rsplit(':', 1)[1])] += v
    if ctx['txn'].payer_id:
        for k, v in model.w.items():
            if v > 0.3 and k.startswith(f'payer:{ctx["txn"].payer_id}|p:'):
                hits[int(k.rsplit(':', 1)[1])] += v
    return {pid for pid, v in hits.items() if v >= 0.5}


def score(txn, parties, model=None, ctx=None):
    """Puntaje del modelo por cliente (0 a 1, sin calibrar) y los rasgos que más pesaron. Clientes: lista de Party."""
    model = model or load()
    ctx = ctx or context(txn)
    out = {}
    for party in parties:
        feats = features(ctx, party)
        value = model.predict(feats)
        out[party.pk] = {'score': value, 'prob': value, 'why': model.explain(feats)}   # 'prob': nombre anterior del mismo puntaje
    return out


def rules_level(feats):
    """Nivel que darían las reglas solas a un candidato con estos rasgos: 3 alto, 2 medio, 1 bajo, 0 nada."""
    if feats & {'mention', 'holder_id', 'payer_confirmed'}:
        return 3
    if feats & {'amt_one', 'amt_total', 'amt_combo'}:
        return 2
    return 1 if feats & {'name_overlap:2', 'name_overlap:3'} else 0


def rules_choice(feats_by_party):
    """Cliente que las reglas habrían sugerido sin el modelo: el único con el mayor nivel, si es medio o alto."""
    levels = {pid: rules_level(f) for pid, f in feats_by_party.items()}
    top = max(levels.values(), default=0)
    best = [pid for pid, lv in levels.items() if lv == top]
    return best[0] if top >= 2 and len(best) == 1 else None


def opinion(txn, candidates):
    """Para `suggestions`: devuelve (modelo entrenado, puntajes por cliente, clientes extra por memoria)."""
    model = load()
    if not model.trained:
        return False, {}, []
    ctx = context(txn, open_by_party=_open_now())
    extra_ids = alias_parties(model, ctx) - {p.pk for p in candidates}
    extra = list(Party.objects.filter(pk__in=extra_ids, active=True)) if extra_ids else []
    return True, score(txn, list(candidates) + extra, model, ctx), extra


def _open_now():
    from cartera.services import open_receivables
    by_party = defaultdict(list)
    for r in open_receivables():
        by_party[r.party_id].append(r)
    return by_party


def describe(why):
    names = {'mention': 'menciona la factura', 'holder_id': 'identificación del titular', 'payer_confirmed': 'pagador confirmado', 'amt_one': 'valor igual a una factura',
             'amt_total': 'valor igual al saldo total', 'amt_combo': 'valor igual a una suma de facturas', 'amt_near': 'valor cercano (retención)', 'window_in': 'paga en su fecha habitual',
             'name_overlap:2': 'nombre parecido', 'name_overlap:3': 'nombre parecido'}
    parts = []
    for f, w in why:
        if f.startswith(('tok:', 'hold:')):
            parts.append(f'«{f.split(":", 1)[1].split("|")[0]}» ya pagó a este cliente')
        elif f.startswith('payer:'):
            parts.append('este pagador ya pagó a este cliente')
        elif f in names:
            parts.append(names[f])
    return ', '.join(dict.fromkeys(parts))


# -------------------------------------------------------------------------------------- aprendizaje

def _candidates(ctx, party):
    ids = set(ctx['open'].keys()) | {party.pk}
    if len(ids) == 1:
        # Sin otros clientes con cartera abierta no habría contraejemplos: se toman algunos activos para no aprender solo «sí».
        ids |= set(Party.objects.filter(active=True).exclude(pk=party.pk).order_by('pk').values_list('pk', flat=True)[:5])
    if len(ids) > NEG_LIMIT + 1:
        keep = {party.pk}
        amount = ctx['txn'].amount
        for pid, recs in ctx['open'].items():
            if any(r.balance == amount for r in recs) or any(_mentions(ctx['text'], r) for r in recs):
                keep.add(pid)
        rest = sorted(ids - keep)
        random.Random(ctx['txn'].pk).shuffle(rest)
        ids = keep | set(rest[:max(NEG_LIMIT - len(keep), 0)])
    return {p.pk: p for p in Party.objects.filter(pk__in=ids)}


def learn_from(txn, party, model=None, persist=True):
    """Aprende de un movimiento identificado. Anota antes lo que habría predicho (evaluación secuencial)."""
    if MatchFeedback.objects.filter(transaction=txn, voided_at__isnull=True).exists():
        return None
    model = model or load()
    ctx = context(txn, learning=True)
    parties = _candidates(ctx, party)
    feats = {pid: features(ctx, p) for pid, p in parties.items()}
    scored = sorted(((model.predict(f), pid) for pid, f in feats.items()), reverse=True)
    top_score, top_pid = scored[0]
    by_rules = rules_choice(feats)
    fb = MatchFeedback(transaction=txn, predicted_party_id=top_pid, score=top_score, chosen_party=party, correct=top_pid == party.pk,
                       candidates=len(parties), trained=model.trained, rules_party_id=by_rules, rules_correct=by_rules == party.pk)
    model.fit_example(feats[party.pk], [f for pid, f in feats.items() if pid != party.pk], random.Random(txn.pk))
    if persist:
        with transaction.atomic():
            fb.save()
            save(model)
    return fb


def valid_history():
    """Movimientos identificados vigentes, en el orden en que se identificaron."""
    return (BankTransaction.objects.filter(party__isnull=False, amount__gt=0, status__in=IDENTIFIED).select_related('party', 'payer')
            .order_by(F('identified_at').asc(nulls_last=True), 'date', 'pk'))


def retrain():
    """Reconstruye el modelo solo con el historial vigente, en el orden en que se identificó cada ingreso.

    Los ejemplos anulados se conservan como registro de la corrección, pero no entran al modelo ni a la
    evaluación; la evaluación secuencial de los vigentes se vuelve a anotar con el modelo reconstruido."""
    with transaction.atomic():
        MatchFeedback.objects.filter(voided_at__isnull=True).delete()
        model = OnlineLogit()
        n = 0
        for txn in valid_history():
            fb = learn_from(txn, txn.party, model, persist=False)
            if fb:
                fb.save()
                n += 1
        save(model)
    return n


def reconcile(txn_id):
    """Corrige el aprendizaje de un movimiento cuyo cliente cambió o cuya aplicación se revirtió.

    Si el ejemplo aprendido ya no es verdadero (el movimiento quedó sin cliente o con otro), lo marca como
    anulado y reconstruye el modelo con el historial vigente. Devuelve True si reconstruyó."""
    txn = BankTransaction.objects.filter(pk=txn_id).first()
    if txn is None:
        return False
    truth = txn.party_id if txn.status in IDENTIFIED and txn.party_id else None
    stale = [fb for fb in MatchFeedback.objects.filter(transaction=txn, voided_at__isnull=True) if fb.chosen_party_id != truth]
    if not stale:
        return False
    reason = 'La aplicación se revirtió: el ingreso quedó sin identificar.' if truth is None else 'El ingreso cambió de cliente.'
    with transaction.atomic():
        MatchFeedback.objects.filter(pk__in=[fb.pk for fb in stale]).update(voided_at=timezone.now(), void_reason=reason)
        retrain()
    return True


def reliability():
    """Tabla de fiabilidad: tasa de acierto medida del primer candidato, por tramos del puntaje (evaluación secuencial)."""
    rows = [{'label': label, 'low': lo, 'high': hi, 'n': 0, 'hits': 0} for lo, hi, label in BINS]
    for value, correct in MatchFeedback.objects.filter(trained=True, voided_at__isnull=True).values_list('score', 'correct'):
        for r in rows:
            if r['low'] <= value < r['high']:
                r['n'] += 1
                r['hits'] += 1 if correct else 0
                break
    for r in rows:
        r['rate'] = (r['hits'] / r['n'] * 100) if r['n'] else None
        r['few'] = r['n'] < FEW
    return rows


def measured(value, table=None):
    """Tramo de la tabla de fiabilidad al que pertenece un puntaje, con su acierto medido."""
    for r in (table if table is not None else reliability()):
        if r['low'] <= value < r['high']:
            return r
    return None


def score_label(value, table=None):
    """Cómo se muestra un puntaje: en escala 0–100 y con el acierto medido para puntajes similares."""
    r = measured(value, table)
    text = f'puntaje {value * 100:.0f} de 100'
    if r is None or not r['n']:
        return text + '; aún sin casos evaluados con puntajes similares'
    seen = f'en la evaluación acertó {r["hits"]} de {r["n"]} veces con puntajes de {r["label"]}'
    return f'{text}; {seen}' + (' (pocos casos)' if r['few'] else '')


def status():
    model = load()
    valid = MatchFeedback.objects.filter(voided_at__isnull=True)
    evaluated = list(valid.filter(trained=True).values_list('correct', 'rules_correct', 'rules_party_id'))
    hits = sum(1 for c, _, _ in evaluated if c)
    rules_hits = sum(1 for _, r, _ in evaluated if r)
    rules_none = sum(1 for _, _, rp in evaluated if rp is None)
    aliases = defaultdict(list)
    for k, v in model.w.items():
        if v > 0.4 and k.startswith(('tok:', 'hold:', 'payer:')):
            kind, rest = k.split(':', 1)
            word, pid = rest.split('|p:')
            aliases[int(pid)].append((word if kind != 'payer' else f'pagador #{word}', round(v, 2)))
    names = {p.pk: p.name for p in Party.objects.filter(pk__in=aliases.keys())}
    def strongest(ws):       # la misma palabra puede venir del texto y del titular: se muestra una vez, con su mayor peso
        best = {}
        for word, weight in ws:
            best[word] = max(weight, best.get(word, 0.0))
        return sorted(best.items(), key=lambda x: (-x[1], x[0]))[:6]

    relations = sorted(({'party': names.get(pid, f'#{pid}'), 'words': strongest(ws)} for pid, ws in aliases.items()),
                       key=lambda r: -sum(w for _, w in r['words']))
    n = len(evaluated)
    return {'examples': model.meta.get('examples', 0), 'trained': model.trained, 'min_examples': MIN_EXAMPLES, 'features': len(model.w),
            'updated': model.meta.get('updated'),
            # Evaluación secuencial después de entrenado: el modelo predijo antes de conocer la respuesta.
            'eval_n': n, 'eval_hits': hits, 'eval_share': (hits / n * 100) if n else None,
            # Lo que habrían sugerido las reglas solas en esos mismos casos.
            'rules_hits': rules_hits, 'rules_share': (rules_hits / n * 100) if n else None, 'rules_none': rules_none,
            'recent_n': n, 'recent_hits': hits, 'recent_share': (hits / n * 100) if n else None,   # nombres anteriores
            'reliability': reliability(), 'voided': MatchFeedback.objects.filter(voided_at__isnull=False).count(),
            'relations': relations[:12],
            'feedback': list(valid.select_related('transaction', 'predicted_party', 'chosen_party', 'rules_party').order_by('-pk')[:12]),
            'corrections': list(MatchFeedback.objects.filter(voided_at__isnull=False).select_related('transaction', 'chosen_party').order_by('-voided_at')[:6])}
