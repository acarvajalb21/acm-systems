"""Cobranza que aprende: elige el tono y el momento del recordatorio según el tipo de cliente y lo observado después.

Cada cliente pertenece a un segmento (persona o empresa × cómo ha pagado: sin historial, puntual, con retraso, con
mucho retraso). Para cada segmento hay nueve estrategias posibles: tono {cordial, formal, firme} × momento {3, 10,
21 días de mora}. La elección es un bandido bayesiano (muestreo de Thompson): cada estrategia tiene una creencia
Beta(con pago, sin pago) que parte de un punto de partida suave y se actualiza con cada recordatorio evaluado.

Tres precisiones que gobiernan lo que se aprende y lo que se dice:

- **Mensaje preparado ≠ envío registrado.** Un recordatorio aprobado sin canal de contacto queda solo preparado
  (`sent=False`) y no entra al aprendizaje. Solo cuentan los que tuvieron envío registrado; en la demostración
  ese envío es simulado y queda marcado como tal.
- **El resultado se evalúa solo al cumplirse la ventana** de 10 días: hubo pago, hubo abono o no hubo pago
  dentro de ella. Antes de eso el intento sigue «en ventana», aunque el cliente ya haya pagado.
- **Coincidencia temporal, no causa.** «Hubo pago en la ventana» no prueba que el recordatorio lo produjera: los
  clientes pagan también sin recordatorio. Como referencia se muestra la tasa de pago en una ventana igual de
  cuotas comparables que no tuvieron recordatorio (`reference`), que tampoco es un grupo de control.

La elección es estable dentro de la semana (semilla por cliente y semana), de modo que el agente no cambia de
estrategia entre revisiones. Es un método estadístico de asignación adaptativa, no IA generativa.
"""
import random
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.utils import timezone
from inventory.services import cop
from cartera.models import Receivable, PaymentApplication, ReceivableAdjustment, ZERO
from . import pagos
from .models import CollectionAttempt

TONES = {'cordial': 'Cordial', 'formal': 'Formal', 'firme': 'Firme'}
TIMINGS = [3, 10, 21]
ARMS = [(t, d) for t in TONES for d in TIMINGS]
WINDOW_DAYS = 10
REFERENCE_DAYS = 10      # mora en la que se observa a las cuotas sin recordatorio, para la tasa de referencia
EVALUATED = ('paid', 'partial', 'unpaid')
KINDS = {'person': 'Persona natural', 'company': 'Empresa'}
HISTORY = {'nuevo': 'sin historial de pagos', 'puntual': 'que paga puntual', 'tardio': 'que paga con retraso', 'moroso': 'que paga con mucho retraso'}
CAVEAT = 'Es una coincidencia en el tiempo: no prueba que el recordatorio causara el pago.'


def prior(segment, arm):
    """Creencia inicial (con pago, sin pago) antes de tener datos: cordial y pronto para quien paga puntual; más formal
    o firme y con más mora para quien se atrasa. Es un punto de partida suave: cuatro o cinco intentos ya pesan más."""
    kind, hist = segment.split(':')
    tone, days = arm
    a, b = 1.0, 1.0
    if hist in ('puntual', 'nuevo'):
        a += {'cordial': 1.0, 'formal': 0.5, 'firme': 0.0}[tone] + (0.5 if days == 3 else 0.0)
    elif hist == 'tardio':
        a += {'cordial': 0.5, 'formal': 1.0, 'firme': 0.3}[tone] + (0.5 if days == 10 else 0.0)
    else:
        a += {'cordial': 0.0, 'formal': 0.5, 'firme': 1.0}[tone] + (0.5 if days <= 10 else 0.0)
    if kind == 'company' and tone == 'formal':
        a += 0.3
    if kind == 'person' and tone == 'firme':
        b += 0.3
    return a, b


def segment_of(party, beh):
    return f'{party.kind}:{pagos.history_label(beh)}'


def segment_label(segment):
    kind, hist = segment.split(':')
    return f'{KINDS.get(kind, kind)} {HISTORY.get(hist, hist)}'


def counts(segment=None):
    """(segmento, tono, días) → [con pago en la ventana, sin pago] de los recordatorios enviados y ya evaluados."""
    qs = CollectionAttempt.objects.filter(sent=True, outcome__in=EVALUATED)
    if segment:
        qs = qs.filter(segment=segment)
    out = defaultdict(lambda: [0, 0])
    for seg, tone, timing, outcome in qs.values_list('segment', 'tone', 'timing_days', 'outcome'):
        out[(seg, tone, timing)][0 if outcome in ('paid', 'partial') else 1] += 1
    return out


def choose(party, beh, today, table=None):
    """Estrategia para este cliente esta semana, con la evidencia que la respalda."""
    seg = segment_of(party, beh)
    table = table if table is not None else counts(seg)
    rng = random.Random(f'{party.pk}:{today:%G-%V}')
    best, best_sample = None, -1.0
    for arm in ARMS:
        a0, b0 = prior(seg, arm)
        s, f = table.get((seg, *arm), (0, 0))
        sample = rng.betavariate(a0 + s, b0 + f)
        if sample > best_sample:
            best, best_sample = arm, sample
    s, f = table.get((seg, *best), (0, 0))
    in_segment = sum(v[0] + v[1] for k, v in table.items() if k[0] == seg)
    if s + f == 0:
        evidence = (f'Todavía explorando con «{segment_label(seg)}»: {in_segment} recordatorio(s) evaluado(s) en este tipo de cliente.' if in_segment
                    else f'Sin recordatorios evaluados para «{segment_label(seg)}»: se parte de un criterio inicial y se registra lo que ocurra.')
    else:
        evidence = (f'Con «{segment_label(seg)}», tras el tono {TONES[best[0]].lower()} a los {best[1]} días de mora hubo pago en la ventana '
                    f'en {s} de {s + f} recordatorios evaluados.')
    return {'segment': seg, 'tone': best[0], 'timing': best[1], 'evidence': evidence, 'successes': s, 'failures': f}


def message(party, docs, tone, contact_name, today=None):
    """docs: [(cuota con .balance, días de mora)]."""
    total = sum((r.balance for r, _ in docs), ZERO)
    oldest = max(late for _, late in docs)
    lines = '\n'.join(f'• {r.document} por {cop(r.balance)}, vencida el {r.due_date:%d/%m/%Y}' for r, _ in sorted(docs, key=lambda d: -d[1]))
    first = (contact_name or party.name).split()[0]
    company = party.kind == 'company'
    us = settings.COMPANY_NAME
    if tone == 'cordial':
        return (f'Hola {first}, le escribimos de {us}. Queremos recordarle con gusto que tiene pendiente:\n{lines}\n'
                f'Total: {cop(total)}. Si ya realizó el pago, compártanos el comprobante y lo aplicamos de inmediato. ¡Gracias por confiar en nosotros!')
    if tone == 'formal':
        greet = f'Estimados señores de {party.name}' if company else f'Estimado(a) {first}'
        return (f'{greet}: {us} le informa que a la fecha registra saldo pendiente:\n{lines}\n'
                f'Total: {cop(total)}, con {oldest} días desde el vencimiento más antiguo. Agradecemos programar el pago o enviarnos el soporte si ya fue realizado. Quedamos atentos.')
    greet = f'Señores {party.name}' if company else f'Señor(a) {first}'
    return (f'{greet}: la cartera con {us} presenta {oldest} días de mora:\n{lines}\n'
            f'Total: {cop(total)}. Le solicitamos regularizar el pago en los próximos 3 días hábiles para mantener activo su cupo de crédito. '
            f'Si ya pagó, envíenos el comprobante y actualizamos su estado de cuenta.')


def record(action, user, payload, now=None, sent=True):
    """Guarda el intento al aprobar el recordatorio.

    `sent=True`: quedó un envío registrado (simulado) y el intento se evaluará al cumplirse la ventana.
    `sent=False`: el mensaje solo quedó preparado (sin canal de contacto); se conserva, pero no se evalúa ni
    entra al aprendizaje."""
    strategy = payload.get('strategy') or {}
    return CollectionAttempt.objects.create(
        party_id=payload['party_id'], action=action, segment=strategy.get('segment', 'person:nuevo'), tone=strategy.get('tone', 'cordial'),
        timing_days=int(strategy.get('timing', 0)), channel=payload.get('channel', ''), receivables=list(payload.get('receivables', [])),
        amount=Decimal(str(payload.get('amount') or 0)), sent_at=now or timezone.now(), created_by=user, sent=bool(sent),
        outcome='pending' if sent else 'skipped')


def window_end(attempt):
    return timezone.localtime(attempt.sent_at).date() + timedelta(days=WINDOW_DAYS)


def evaluate(today=None):
    """Cierra los intentos enviados cuya ventana ya se cumplió: pagó, abonó o no pagó dentro de la ventana.

    Un pago que llega dentro de la ventana no cierra el intento antes de tiempo, y uno posterior a la ventana no
    cuenta. Devuelve cuántos intentos se cerraron con cada resultado."""
    today = today or timezone.localdate()
    now = timezone.now()
    changed = {'paid': 0, 'partial': 0, 'unpaid': 0}
    for at in CollectionAttempt.objects.filter(outcome='pending', sent=True):
        sent, end = timezone.localtime(at.sent_at).date(), window_end(at)
        if today <= end:
            continue
        apps = list(PaymentApplication.objects.filter(reversed_at__isnull=True, receivable_id__in=at.receivables, collection__date__gte=sent,
                                                      collection__date__lte=end).select_related('collection').order_by('collection__date', 'pk'))
        paid = sum((a.amount for a in apps), ZERO)
        if apps and paid >= at.amount:
            outcome = 'paid'
        elif apps:
            outcome = 'partial'
        else:
            outcome = 'unpaid'
        at.outcome, at.resolved_at = outcome, now
        at.days_to_pay = (apps[0].collection.date - sent).days if apps else None
        at.save(update_fields=['outcome', 'resolved_at', 'days_to_pay'])
        changed[outcome] += 1
    return changed


def waiting_parties(today=None):
    """Clientes con un recordatorio enviado cuya ventana sigue abierta: no se les insiste."""
    today = today or timezone.localdate()
    out = set()
    for party_id, sent_at in CollectionAttempt.objects.filter(outcome='pending', sent=True).values_list('party_id', 'sent_at'):
        if timezone.localtime(sent_at).date() + timedelta(days=WINDOW_DAYS) >= today:
            out.add(party_id)
    return out


def reference(today=None, days_late=REFERENCE_DAYS):
    """Tasa de pago en ventana de cuotas comparables SIN recordatorio, por segmento.

    Una cuota entra si llegó a `days_late` días de mora con saldo, no tuvo un recordatorio enviado antes de que
    terminara su ventana, y esa ventana (de la misma duración) ya se cumplió. No es un grupo de control: las
    cuotas sin recordatorio pueden diferir de las recordadas, y el segmento se toma con el historial de hoy."""
    today = today or timezone.localdate()
    reminded = defaultdict(list)
    for at in CollectionAttempt.objects.filter(sent=True):
        day = timezone.localtime(at.sent_at).date()
        for rid in at.receivables:
            reminded[rid].append(day)
    apps = defaultdict(list)
    for rid, day, amount in PaymentApplication.objects.filter(reversed_at__isnull=True).values_list('receivable_id', 'collection__date', 'amount'):
        apps[rid].append((day, amount))
    adjusted = defaultdict(lambda: ZERO)
    for rid, amount in ReceivableAdjustment.objects.values_list('receivable_id', 'amount'):
        adjusted[rid] += amount
    behaviour = pagos.behaviour()
    out = defaultdict(lambda: {'n': 0, 'paid': 0})
    limit = today - timedelta(days=days_late + WINDOW_DAYS)
    for r in Receivable.objects.filter(due_date__lt=limit).select_related('party'):
        start = r.due_date + timedelta(days=days_late)
        end = start + timedelta(days=WINDOW_DAYS)
        if any(day <= end for day in reminded.get(r.pk, [])):
            continue
        before = sum((a for day, a in apps.get(r.pk, []) if day < start), ZERO)
        if r.amount - before - adjusted[r.pk] <= 0:
            continue
        row = out[segment_of(r.party, behaviour.get(r.party_id))]
        row['n'] += 1
        row['paid'] += 1 if any(start <= day <= end for day, _ in apps.get(r.pk, [])) else 0
    for row in out.values():
        row['rate'] = row['paid'] / row['n'] * 100
    return dict(out)


def overview(today=None):
    table = counts()
    ref = reference(today)
    seen = set(CollectionAttempt.objects.filter(sent=True).values_list('segment', flat=True).distinct())
    rows = []
    for seg in sorted(seen):
        arms = []
        for arm in ARMS:
            s, f = table.get((seg, *arm), (0, 0))
            a0, b0 = prior(seg, arm)
            arms.append({'tone': TONES[arm[0]], 'timing': arm[1], 'successes': s, 'failures': f, 'n': s + f,
                         'rate': (s / (s + f) * 100) if s + f else None, 'expected': (a0 + s) / (a0 + b0 + s + f) * 100})
        best = max(arms, key=lambda a: a['expected'])
        n, paid = sum(a['n'] for a in arms), sum(a['successes'] for a in arms)
        rows.append({'segment': seg, 'label': segment_label(seg), 'arms': arms, 'best': best, 'n': n, 'successes': paid,
                     'rate': (paid / n * 100) if n else None, 'reference': ref.get(seg)})
    attempts = CollectionAttempt.objects.select_related('party')
    return {'segments': rows, 'attempts': list(attempts[:15]),
            'pending': attempts.filter(outcome='pending', sent=True).count(),
            'evaluated': attempts.filter(sent=True, outcome__in=EVALUATED).count(),
            'prepared_only': attempts.filter(sent=False).count(),
            'paid': attempts.filter(sent=True, outcome__in=('paid', 'partial')).count(),
            'window': WINDOW_DAYS, 'reference_days': REFERENCE_DAYS, 'caveat': CAVEAT,
            'reference_total': {'n': sum(r['n'] for r in ref.values()), 'paid': sum(r['paid'] for r in ref.values())}}
