"""Etiqueta de plantilla de la barra del recorrido guiado."""
from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def recorrido_actual(context):
    """El paso en curso del recorrido (para la barra), o None si no hay recorrido iniciado."""
    request = context.get('request')
    if request is None or not getattr(request, 'user', None) or not request.user.is_authenticated:
        return None
    from feria import tour
    from feria.experiencia_views import tour_state
    state = tour_state(request)
    if not state:
        return None
    n, title, _, _, show, _, _ = tour.STEPS[state['n'] - 1]
    return {'n': n, 'total': tour.TOTAL, 'title': title, 'show': show, 'last': n == tour.TOTAL, 'first': n == 1, 'started': state['started']}
