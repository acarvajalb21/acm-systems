"""Una pregunta al copiloto, de principio a fin: límites, proveedor, caída al modo local y registro del turno."""
import logging
import re
import time
from datetime import timedelta
from django.utils import timezone
from . import conf, providers, tools
from .models import CopilotTurn

log = logging.getLogger(__name__)
CONTEXT_KINDS = {'referencia': 'Referencia', 'cliente': 'Cliente', 'ingreso': 'Ingreso bancario'}
CONTEXT_TOOL = {'referencia': 'detalle_referencia', 'cliente': 'cartera_vencida', 'ingreso': 'pagos_por_revisar'}


class Rejected(Exception):
    """La pregunta no se procesa. `status` es el código HTTP y `message` lo que ve la persona."""

    def __init__(self, status, message, retry_after=0):
        super().__init__(message)
        self.status, self.message, self.retry_after = status, message, retry_after


def parse_context(raw, user=None):
    """«tipo:id» → {'kind', 'id', 'label', 'raw'} o {}. Solo tipos conocidos, ids numéricos y registros que el perfil puede consultar."""
    m = re.fullmatch(r'(referencia|cliente|ingreso):(\d{1,10})', str(raw or '').strip())
    if not m:
        return {}
    kind, ident = m.group(1), int(m.group(2))
    if user is not None and user.role not in tools.TOOLS[CONTEXT_TOOL[kind]].roles:
        return {}
    label = CONTEXT_KINDS[kind]
    if kind == 'referencia':
        from inventory.models import Product
        row = Product.objects.filter(pk=ident).values_list('name', 'code').first()
        if not row:
            return {}
        label = f'{label}: {tools.clean(row[0], 80)} ({row[1]})'
    elif kind == 'cliente':
        from cartera.models import Party
        name = Party.objects.filter(pk=ident).values_list('name', flat=True).first()
        if not name:
            return {}
        label = f'{label}: {tools.clean(name, 80)}'
    else:
        from cartera.models import BankTransaction
        row = BankTransaction.objects.filter(pk=ident).values_list('date', 'amount').first()
        if not row:
            return {}
        label = f'{label} del {tools.fdate(row[0])} por {tools.cop(row[1])}'
    return {'kind': kind, 'id': ident, 'label': label, 'raw': f'{kind}:{ident}'}


def parse_budget_field(raw):
    """Presupuesto del campo del formulario («8.000.000», «8 millones»): entero en pesos o None."""
    raw = str(raw or '').strip()[:40]
    if not raw:
        return None
    value = providers.parse_budget(raw)
    if value is None and re.fullmatch(r'\d{1,12}', raw):
        value = int(raw)
    return value


def check_limits(user, question, cfg):
    if not question:
        raise Rejected(400, 'Escriba una pregunta.')
    if len(question) > conf.MAX_QUESTION:
        raise Rejected(400, f'La pregunta supera los {conf.MAX_QUESTION} caracteres. Acórtela e intente de nuevo.')
    since = timezone.now() - timedelta(seconds=60)
    recent = list(CopilotTurn.objects.filter(user=user, created_at__gte=since).order_by('created_at').values_list('created_at', flat=True))
    if len(recent) >= cfg.per_minute:
        wait = max(1, int(60 - (timezone.now() - recent[0]).total_seconds()) + 1)
        raise Rejected(429, f'Alcanzó el límite de {cfg.per_minute} preguntas por minuto. Espere {wait} segundos y vuelva a enviar la pregunta.', wait)


def ask(user, question, context=None, provider=None):
    """Responde y deja el turno registrado. Lanza `Rejected` si la pregunta no pasa los límites."""
    cfg = conf.current()
    question = tools.clean(question, conf.MAX_QUESTION + 1) if len(str(question or '')) <= 4 * conf.MAX_QUESTION else 'x' * (conf.MAX_QUESTION + 1)
    check_limits(user, question, cfg)
    context = context or {}
    started = time.monotonic()
    provider = provider or providers.active(cfg)
    error = ''
    try:
        answer = provider.answer(user, question, context)
    except providers.ProviderError as e:
        error = f'{getattr(provider, "mode", "")}:{e.code}'
        answer = providers.LocalProvider().answer(user, question, context)
        answer.notes.insert(0, f'{e.public} Respondió el asistente local.')
    except Exception:      # el copiloto nunca rompe la pantalla ni filtra detalles: se registra y responde el modo local
        log.exception('El proveedor del copiloto falló')
        error = f'{getattr(provider, "mode", "")}:unexpected'
        if isinstance(provider, providers.LocalProvider):
            answer = providers.Answer(text='No pude completar la consulta. Intente de nuevo o abra la pantalla correspondiente desde el menú.',
                                      notes=['La consulta falló en el servidor; el error quedó registrado.'])
        else:
            answer = providers.LocalProvider().answer(user, question, context)
            answer.notes.insert(0, 'El modelo generativo falló. Respondió el asistente local.')
    own = [getattr(getattr(provider, 'cfg', None), 'api_key', '')]
    answer.text = conf.scrub(answer.text, own)
    answer.notes = [conf.scrub(n, own) for n in answer.notes]
    answer.error = error
    duration = int((time.monotonic() - started) * 1000)
    sources = []
    for res in answer.results:
        for s in res.get('sources', []):
            if s not in sources:
                sources.append(s)
    turn = CopilotTurn.objects.create(
        user=user, question=question, context=context.get('raw', '')[:60], mode=answer.mode, model=answer.model[:80], tools=answer.calls,
        sources=sources[:20], answer=answer.text[:2000], notes=answer.notes[:10], duration_ms=duration, error=conf.scrub(error)[:200])
    return answer, turn


def history(user, limit=6, exclude=None):
    qs = CopilotTurn.objects.filter(user=user)
    if exclude is not None:
        qs = qs.exclude(pk=exclude)
    return list(qs[:limit])


def suggestions(user, context=None):
    """Preguntas sugeridas según el perfil: primero las cinco obligatorias que el perfil puede consultar."""
    allowed = {t.name for t in tools.allowed(user)}
    order = ['atencion_hoy', 'riesgo_agotamiento', 'propuesta_compra', 'pagos_por_revisar', 'estado_operacion', 'cartera_vencida', 'lotes_por_vencer', 'historial_decisiones']
    out = []
    if context and context.get('kind'):
        out.append({'tool': CONTEXT_TOOL[context['kind']], 'question': {'referencia': '¿Cómo está esta referencia?', 'cliente': '¿Qué debe este cliente?',
                                                                         'ingreso': '¿A quién corresponde este ingreso y qué evidencia hay?'}[context['kind']], 'budget': False})
    for name in order:
        if name in allowed:
            item = {'tool': name, 'question': providers.EXAMPLES[name], 'budget': name == 'propuesta_compra'}
            if item['budget']:
                item['example'] = budget_example()
            out.append(item)
    return out


def budget_example():
    """Cifra que se propone en el campo de presupuesto, calculada con los datos del momento («32.000.000»), o ''."""
    try:
        value = tools.suggested_budget()
    except Exception:
        log.warning('No se pudo calcular el presupuesto de ejemplo del copiloto.', exc_info=True)
        return ''
    return f'{value:,}'.replace(',', '.') if value else ''
