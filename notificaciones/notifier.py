"""Avisos internos: convierte las alertas nuevas de cada persona en un registro dentro de la plataforma.

Nada sale de este equipo: no hay envíos a celulares, correo ni servicios externos. Cada alerta (clave + revisión)
se registra una sola vez por persona; la pantalla de alertas muestra los más recientes.
"""
import logging
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.utils import timezone
from inventory import services as S
from .models import Notice

log = logging.getLogger(__name__)
KEEP_DAYS = 60


def register(user):
    """Registra las alertas sin leer que la persona todavía no tiene como aviso. Devuelve cuántas eran nuevas."""
    items = S.alerts(user)
    done = set(Notice.objects.filter(user=user).values_list('key', 'revision'))
    new = [a for a in items if not a['read'] and (a['key'][:120], a['revision']) not in done]
    Notice.objects.bulk_create([Notice(user=user, key=a['key'][:120], revision=a['revision'], level=a['level'], title=a['title'][:200],
                                       detail=a['detail'][:300], url=a['url'][:200]) for a in new], ignore_conflicts=True)
    return len(new)


def dispatch(now=None):
    """Revisa a todas las personas activas. Seguro de repetir: lo ya registrado no se duplica."""
    now = now or timezone.now()
    people = created = users = 0
    for user in get_user_model().objects.filter(is_active=True):
        users += 1
        try:
            n = register(user)
        except Exception:
            log.exception('No se pudieron calcular las alertas de %s', user)
            continue
        if n:
            people += 1
            created += n
    Notice.objects.filter(created_at__lt=now - timedelta(days=KEEP_DAYS)).delete()
    return {'people': people, 'new': created, 'users': users}


def recent(user, limit=20):
    return list(Notice.objects.filter(user=user)[:limit])
