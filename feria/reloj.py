"""Reloj controlado e identificadores sembrados para generar y verificar el escenario.

Mientras el contexto está activo, `django.utils.timezone.now` devuelve la hora simulada (por tanto también
`localdate`, los campos `auto_now` y todo servicio que pregunte la fecha), `uuid.uuid4` sale de un generador
sembrado (también como valor por defecto de las llaves UUID) y las carpetas `%Y/%m` de los adjuntos usan la
fecha simulada. Al salir todo vuelve a su estado normal. No se usa fuera de la generación y la verificación.
"""
import datetime as dt
import random
import uuid
from contextlib import contextmanager
from zoneinfo import ZoneInfo
from django.apps import apps
from django.conf import settings
from django.db.models.fields import files as file_fields
from django.utils import timezone


class Clock:
    """Hora simulada. `at(día, hora, minuto)` fija el instante; `tick(segundos)` lo adelanta."""

    def __init__(self, day, hour=7, minute=0):
        self.tz = ZoneInfo(settings.TIME_ZONE)
        self.at(day, hour, minute)

    def at(self, day, hour=7, minute=0):
        self.now = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.tz)
        return self

    def tick(self, seconds=60):
        self.now += dt.timedelta(seconds=seconds)
        return self.now

    @property
    def today(self):
        return self.now.date()

    def __call__(self):
        return self.now


class _SimulatedDatetime:
    """Sustituto mínimo del módulo `datetime` para las rutas `upload_to` con fecha."""

    def __init__(self, clock):
        self._clock = clock
        self.datetime = self

    def now(self, tz=None):
        return self._clock.now.replace(tzinfo=None)

    def __getattr__(self, name):
        return getattr(dt, name)


def _uuid_fields():
    for model in apps.get_models():
        for field in model._meta.concrete_fields:
            if getattr(field, 'default', None) is _REAL_UUID4:
                yield field


_REAL_UUID4 = uuid.uuid4
_REAL_NOW = timezone.now


@contextmanager
def controlled(clock, seed=None):
    """Activa el reloj simulado. Con `seed`, además siembra `uuid4`."""
    rng = random.Random(f'uuid:{seed}') if seed is not None else None

    def seeded_uuid4():
        return uuid.UUID(int=rng.getrandbits(128), version=4)

    fields = list(_uuid_fields()) if rng else []
    timezone.now = clock
    real_datetime = file_fields.datetime
    file_fields.datetime = _SimulatedDatetime(clock)
    if rng:
        uuid.uuid4 = seeded_uuid4
        for field in fields:
            field.default = seeded_uuid4
            field.__dict__.pop('_get_default', None)
    try:
        yield clock
    finally:
        timezone.now = _REAL_NOW
        file_fields.datetime = real_datetime
        if rng:
            uuid.uuid4 = _REAL_UUID4
            for field in fields:
                field.default = _REAL_UUID4
                field.__dict__.pop('_get_default', None)


@contextmanager
def frozen(day, hour=12):
    """Solo fija la fecha (para verificar los casos preparados en su fecha de referencia)."""
    with controlled(Clock(day, hour)) as clock:
        yield clock
