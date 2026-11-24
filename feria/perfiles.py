"""Perfiles de demostración: entrar con un clic como cada cuenta `<perfil>.demo`.

El cambio rápido solo existe si `settings.FERIA['profile_switch']` está activo, la petición llega desde este mismo
equipo y la cuenta es una de las registradas en `DemoState` al generar el escenario. No concede permisos: inicia
sesión como esa cuenta y cada pantalla sigue comprobando el rol en el servidor.
"""
import ipaddress
from django.conf import settings
from inventory.models import User
from . import datos_base as B
from .models import DemoState


def is_local(request):
    """La petición viene de la propia máquina (dirección de bucle local). No se confía en cabeceras de reenvío."""
    try:
        return ipaddress.ip_address((request.META.get('REMOTE_ADDR') or '').split('%')[0]).is_loopback
    except ValueError:
        return False


def enabled(request):
    return bool(settings.FERIA.get('profile_switch')) and is_local(request)


def demo_accounts():
    """{usuario: cuenta} de las cuentas de demostración activas registradas en el escenario vigente."""
    names = DemoState.current().demo_usernames
    return {u.username: u for u in User.objects.filter(username__in=names, is_active=True)} if names else {}


def account_for(key):
    """La cuenta de demostración de un perfil (`owner`, `finance`…) o de un usuario (`proyectos.demo`); None si no es una de ellas."""
    accounts = demo_accounts()
    if key in accounts:
        return accounts[key]
    username = (DemoState.current().cases or {}).get('accounts', {}).get(key)
    return accounts.get(username)


def profiles(request=None):
    """Lista para la pantalla de ingreso y el menú: perfil, persona, área y qué puede probar."""
    accounts = demo_accounts()
    current = getattr(getattr(request, 'user', None), 'username', None)
    labels = dict(User.Role.choices)
    out = []
    for role, username, first, last, area, can in B.TEAM:
        user = accounts.get(username)
        if user is None:
            continue
        out.append({'key': username, 'role': role, 'label': labels[role], 'username': username, 'name': user.display_name or f'{first} {last}',
                    'area': area, 'can': can, 'current': username == current})
    return out
