"""Pantalla del copiloto y envío de preguntas.

La página se lee con GET; toda pregunta entra por POST con CSRF. Sin JavaScript, el formulario devuelve la página
con la respuesta; con JavaScript, la misma ruta devuelve el fragmento ya renderizado en el servidor (JSON con HTML),
de modo que la respuesta se dibuja igual en los dos casos y nada se arma en el navegador.
"""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.views.decorators.http import require_GET, require_POST
from . import conf, providers, service

LEVELS = {'critical': ('is-critical', 'bad', 'Crítico'), 'warning': ('is-warning', 'warn', 'Aviso'), 'info': ('', 'info', 'Informativo')}


def present(res):
    """Normaliza un resultado de herramienta para la plantilla (celdas con texto, enlace y alineación)."""
    blocks = []
    for b in res.get('blocks', []):
        numeric = set(b.get('numeric') or [])
        if b['kind'] == 'list':
            rows = [{'text': r.get('text', ''), 'detail': r.get('detail', ''), 'url': r.get('url', ''),
                     'css': LEVELS.get(r.get('level'), LEVELS['info'])[0], 'chip': LEVELS.get(r.get('level'), LEVELS['info'])[1],
                     'level': LEVELS.get(r.get('level'), LEVELS['info'])[2]} for r in b['rows']]
        else:
            rows = []
            for row in b['rows']:
                cells = []
                for i, c in enumerate(row):
                    label = b['columns'][i] if i < len(b.get('columns', [])) else ''
                    cells.append({'text': c.get('text', '') if isinstance(c, dict) else c, 'url': c.get('url', '') if isinstance(c, dict) else '',
                                  'num': i in numeric, 'label': label})
                rows.append(cells)
        blocks.append({'kind': b['kind'], 'title': b.get('title', ''), 'columns': [{'label': c, 'num': i in numeric} for i, c in enumerate(b.get('columns', []))],
                       'rows': rows, 'more': b.get('more') or 0, 'note': b.get('note', '')})
    return {**res, 'blocks': blocks}


def _turn_context(question, answer):
    info = providers.mode_info()
    generative = answer.mode == providers.MODE_GENERATIVE
    return {'question': question, 'answer': answer, 'results': [present(r) for r in answer.results],
            'paragraphs': [p for p in answer.text.split('\n') if p.strip()],
            'mode_label': 'Modelo generativo' if generative else providers.LOCAL_LABEL,
            'mode_detail': answer.model if generative else '', 'generative': generative, 'configured': info['mode']}


def _page(request, context=None, extra=None, status=200):
    context = context if context is not None else service.parse_context(request.GET.get('contexto', ''), request.user)
    ctx = {'title': 'Copiloto', 'mode': providers.mode_info(), 'suggestions': service.suggestions(request.user, context), 'context': context,
           'history': service.history(request.user), 'max_question': conf.MAX_QUESTION, 'per_minute': conf.current().per_minute}
    ctx.update(extra or {})
    return render(request, 'copiloto/copilot.html', ctx, status=status)


@login_required
@require_GET
def copilot(request):
    return _page(request)


@login_required
@require_POST
def copilot_ask(request):
    wants_json = request.headers.get('X-Requested-With') == 'fetch'
    question = request.POST.get('q', '')
    context = service.parse_context(request.POST.get('contexto', ''), request.user)
    budget = service.parse_budget_field(request.POST.get('presupuesto', ''))
    ask_context = dict(context, budget=budget) if budget is not None else dict(context)
    try:
        answer, turn = service.ask(request.user, question, ask_context)
    except service.Rejected as e:
        if wants_json:
            response = JsonResponse({'ok': False, 'error': e.message, 'retry_after': e.retry_after}, status=e.status)
        else:
            response = _page(request, context, {'error': e.message, 'draft': question[:conf.MAX_QUESTION]}, status=e.status)
        if e.retry_after:
            response['Retry-After'] = str(e.retry_after)
        return response
    turn_ctx = _turn_context(turn.question, answer)
    if wants_json:
        html = render_to_string('copiloto/_turno.html', {'t': turn_ctx}, request=request)
        return JsonResponse({'ok': True, 'html': html, 'mode': answer.mode})
    return _page(request, context, {'turn': turn_ctx, 'history': service.history(request.user, exclude=turn.pk)})
