"""Ejecución de agentes: turnos programados, ejecuciones manuales o por evento, y decisiones sobre propuestas."""
import logging
import uuid
from datetime import datetime, timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from inventory import services as S
from .agents import REGISTRY, params_for
from .models import AgentConfig, AgentRun, AgentAction
from .tools import TOOLS, Stale

log = logging.getLogger(__name__)
NAMESPACE = uuid.UUID('6f0f4d1e-61a4-4b51-9a1c-6a2f0d6b7a10')
STALE_AFTER = timedelta(minutes=30)


def config(key):
    cfg, _ = AgentConfig.objects.get_or_create(key=key)
    return cfg


def period_key(agent, now):
    """Turno vigente del agente, o None si todavía no le toca (agentes diarios antes de su hora)."""
    kind, value = agent.schedule.split(':', 1)
    local = timezone.localtime(now)
    if kind == 'every':
        minutes = int(value)
        slot = (local.hour * 60 + local.minute) // minutes
        return f'{agent.key}:{local:%Y%m%d}:{slot}'
    hour, minute = (int(x) for x in value.split(':'))
    if (local.hour, local.minute) < (hour, minute):
        return None
    return f'{agent.key}:{local:%Y%m%d}'


def next_run(agent, now=None):
    now = timezone.localtime(now or timezone.now())
    kind, value = agent.schedule.split(':', 1)
    if kind == 'every':
        minutes = int(value)
        start = now.replace(second=0, microsecond=0)
        slot = ((start.hour * 60 + start.minute) // minutes + 1) * minutes
        return start.replace(hour=0, minute=0) + timedelta(minutes=slot)
    hour, minute = (int(x) for x in value.split(':'))
    today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return today if today > now else today + timedelta(days=1)


def _store(agent, run, result):
    keys = set()
    for pr in result.proposals:
        keys.add(pr.key)
        values = {'kind': pr.kind, 'title': pr.title[:200], 'detail': pr.detail[:600], 'payload': pr.payload, 'amount': pr.amount,
                  'benefit': pr.benefit, 'url': pr.url[:200], 'confidence': pr.confidence, 'run': run}
        current = AgentAction.objects.filter(dedupe_key=pr.key, status=AgentAction.Status.PROPOSED).first()
        if current:
            for k, v in values.items():
                setattr(current, k, v)
            current.save()
        else:
            AgentAction.objects.create(agent=agent.key, dedupe_key=pr.key, **values)
    # Cada ejecución recalcula todo: lo que ya no aparece perdió su base.
    AgentAction.objects.filter(agent=agent.key, status=AgentAction.Status.PROPOSED).exclude(dedupe_key__in=keys).update(
        status=AgentAction.Status.SUPERSEDED, note='La situación cambió antes de aprobarla.', decided_at=timezone.now())


def run_agent(key, trigger='manual', actor=None, now=None, force_period=None):
    agent = REGISTRY[key]
    cfg = config(key)
    if trigger != 'manual' and not cfg.enabled:
        return None
    now = now or timezone.now()
    try:
        run = AgentRun.objects.create(agent=key, trigger=trigger, actor=actor, period_key=force_period)
    except IntegrityError:
        return None  # otro proceso ya tomó este turno
    try:
        # La lectura del agente va fuera de la transacción: los automáticos (envíos al sistema contable) manejan sus propios commits.
        result = agent.run(params_for(agent, cfg.params), timezone.localtime(now).date())
        with transaction.atomic():
            _store(agent, run, result)
        run.status, run.summary, run.metrics = AgentRun.Status.SUCCEEDED, result.summary[:500], result.metrics
        run.proposed = len(result.proposals)
    except Exception as e:  # el agente nunca rompe la operación que lo disparó
        log.exception('Agente %s falló', key)
        run.status, run.error = AgentRun.Status.FAILED, f'{type(e).__name__}: {e}'[:500]
    run.finished_at = timezone.now()
    run.save()
    return run


def run_due(now=None):
    """Corre los agentes activos a los que les toca turno. Seguro de llamar con frecuencia y desde varios procesos."""
    now = now or timezone.now()
    AgentRun.objects.filter(status=AgentRun.Status.RUNNING, started_at__lt=now - STALE_AFTER).update(
        status=AgentRun.Status.FAILED, error='Interrumpida (el proceso se detuvo antes de terminar).', finished_at=now)
    done = []
    for agent in REGISTRY.values():
        key = period_key(agent, now)
        if key is None or AgentRun.objects.filter(period_key=key).exists():
            continue
        run = run_agent(agent.key, 'schedule', now=now, force_period=key)
        if run:
            done.append(run)
    return done


def trigger(key):
    """Ejecución por evento (después de guardar los datos que la originan). Evita ráfagas de un mismo agente."""
    if AgentRun.objects.filter(agent=key, trigger='event', started_at__gte=timezone.now() - timedelta(seconds=15)).exists():
        return None
    try:
        return run_agent(key, 'event')
    except Exception:
        log.exception('Disparo del agente %s falló', key)
        return None


def can_see(user, agent):
    return user.is_authenticated and user.role in agent.view_roles


def can_approve(user, agent):
    return user.is_authenticated and user.is_active and user.role in agent.approve_roles


def decide(user, action_id, approve, note=''):
    """Aprueba (ejecuta) o descarta una propuesta. Devuelve la propuesta actualizada."""
    with transaction.atomic():
        action = AgentAction.objects.select_for_update().get(pk=action_id)
        agent = REGISTRY[action.agent]
        if not can_approve(user, agent):
            raise PermissionDenied('Su perfil no puede decidir las propuestas de este agente.')
        if action.status != AgentAction.Status.PROPOSED:
            raise ValidationError('Esta propuesta ya fue decidida o ya no aplica.')
        action.decided_by, action.decided_at, action.note = user, timezone.now(), (note or '').strip()[:300]
        if not approve:
            action.status = AgentAction.Status.REJECTED
            action.save()
            S.event(user, 'Propuesta de agente descartada', f'agent-action-{action.pk}', action.title)
            return action
        token = uuid.uuid5(NAMESPACE, f'agent-action-{action.pk}')
        try:
            with transaction.atomic():
                action.result = TOOLS[action.kind](user, action.payload, token, action)
            action.status = AgentAction.Status.EXECUTED
            S.event(user, 'Propuesta de agente aprobada', f'agent-action-{action.pk}', action.title)
        except Stale as e:
            action.status, action.result = AgentAction.Status.SUPERSEDED, {'message': str(e)}
        except ValidationError as e:
            action.status, action.result = AgentAction.Status.FAILED, {'message': ' '.join(e.messages)}
        action.save()
    if action.status == AgentAction.Status.EXECUTED:
        AgentRun.objects.filter(pk=action.run_id).update(executed=F('executed') + 1)
    return action


def pending_for(user):
    keys = [a.key for a in REGISTRY.values() if can_approve(user, a)]
    return AgentAction.objects.filter(status=AgentAction.Status.PROPOSED, agent__in=keys)


def strip(user):
    """Datos compactos para el menú y el Inicio: si ve agentes, cuántas propuestas tiene y el último resumen."""
    from django.db.models import Sum
    visible = any(can_see(user, a) for a in REGISTRY.values())
    if not visible:
        return {'visible': False, 'pending': 0, 'strip': None}
    qs = pending_for(user)
    pending = qs.count()
    resumen = None
    if can_see(user, REGISTRY['resumen']):
        resumen = AgentRun.objects.filter(agent='resumen', status=AgentRun.Status.SUCCEEDED).first()
    benefit = qs.aggregate(s=Sum('benefit'))['s'] or 0
    return {'visible': True, 'pending': pending, 'strip': {'pending': pending, 'benefit': benefit, 'resumen': resumen}}


def alerts(user):
    items = []
    by_agent = {}
    for a in pending_for(user).values_list('agent', flat=True):
        by_agent[a] = by_agent.get(a, 0) + 1
    for key, n in by_agent.items():
        items.append((f'agent-{key}-{n}', 'info', f'{REGISTRY[key].name}: {n} {"propuesta" if n == 1 else "propuestas"} por aprobar', REGISTRY[key].purpose[:200], '/agentes/'))
    return items
