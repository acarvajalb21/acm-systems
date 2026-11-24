"""Mapa de flujo y recorrido guiado de la demostración."""
import time
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from . import flow, tour as T

COOKIE, SALT, MAX_AGE = 'feria_recorrido', 'feria.recorrido', 6 * 60 * 60


@login_required
def flow_map(request):
    """Las seis estaciones con su panel. `?etapa=` abre una estación sin JavaScript; con JavaScript cambia sin recargar."""
    stations = flow.stations(request.user, selected=request.GET.get('etapa'))
    return render(request, 'feria/flujo.html', {'title': 'Mapa de flujo', 'stations': stations, 'today': timezone.localdate()})


def tour_state(request):
    """Paso en curso del recorrido (número e instante de inicio), leído de una cookie firmada.

    Va en una cookie y no en la sesión para que sobreviva al cambio de perfil de la demostración."""
    raw = request.get_signed_cookie(COOKIE, default=None, salt=SALT, max_age=MAX_AGE)
    try:
        n, started = (int(x) for x in (raw or '').split(':'))
    except ValueError:
        return None
    return {'n': n, 'started': started} if 1 <= n <= T.TOTAL else None


def _go(request, step, started):
    """Redirige al registro del paso y deja la barra en ese paso. Si el perfil no puede abrirlo, vuelve al guion."""
    if step['allowed'] and step['url']:
        response = redirect(step['url'])
    else:
        messages.info(request, f'El paso {step["n"]} necesita el perfil {step["needs"]}. Cambie de perfil desde el menú de su cuenta y continúe desde ese paso.')
        response = redirect(reverse('tour') + f'#paso-{step["n"]}')
    response.set_signed_cookie(COOKIE, f'{step["n"]}:{started}', salt=SALT, max_age=MAX_AGE, httponly=True, samesite='Lax')
    return response


@login_required
def tour(request):
    """Guion de los siete pasos. Al iniciar, una barra acompaña en cada pantalla con el paso actual y «Siguiente»."""
    state = tour_state(request)
    steps = T.steps(request.user)
    if request.method == 'POST':
        action = request.POST.get('accion', '')
        current = state['n'] if state else 0
        started = state['started'] if state else int(time.time())
        if action == 'terminar':
            response = redirect('tour')
            response.delete_cookie(COOKIE)
            messages.success(request, 'Recorrido terminado. Puede iniciarlo de nuevo cuando quiera.')
            return response
        if action == 'iniciar':
            return _go(request, steps[0], int(time.time()))
        if action == 'ir':
            try:
                n = int(request.POST.get('paso', ''))
            except ValueError:
                n = current or 1
        else:
            n = current + (1 if action == 'siguiente' else -1 if action == 'anterior' else 0)
        return _go(request, steps[min(max(n, 1), T.TOTAL) - 1], started)
    for s in steps:
        s['current'] = bool(state) and s['n'] == state['n']
        s['done'] = bool(state) and s['n'] < state['n']
    # El recorrido figura en el grupo Inteligencia de la navegación.
    return render(request, 'feria/recorrido.html', {'title': 'Recorrido guiado', 'nav_group': 'inteligencia', 'steps': steps, 'active': state, 'ready': bool(T.cases())})
