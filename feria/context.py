"""Contexto de plantillas de la demostración.

`demo_profiles` solo se entrega cuando el cambio rápido de perfil está activo y la petición viene de este equipo;
si no, las plantillas no muestran los botones (y la vista respondería 404 o 403 de todos modos).
"""
from django.utils.functional import SimpleLazyObject
from . import perfiles


def demo(request):
    if not perfiles.enabled(request):
        return {}
    return {'demo_profiles': SimpleLazyObject(lambda: perfiles.profiles(request))}
