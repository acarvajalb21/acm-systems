"""Interfaz del conector contable.

En esta plataforma el sistema contable es siempre el simulador local (`contable/simulator.py`): no hay cliente de
red, credenciales ni llamadas externas. Este módulo es el punto donde se conectaría un proveedor contable real:
bastaría una clase con el mismo método `request(method, path, body=None, idempotency_key=None, params=None)` que
devuelva el documento creado o lance una de estas excepciones, y que `get_client()` la entregue. La cola de envíos
(`contable/services.py`) no cambiaría.

Los tres desenlaces que la cola distingue:

- Rejected: el sistema contable respondió con un error de validación; el documento no se creó.
- Uncertain: no hubo respuesta confiable; el documento pudo crearse. Se reintenta con la misma clave de idempotencia.
- Unavailable: no se llegó a enviar nada; queda pendiente para un nuevo intento.
"""
from django.conf import settings


class ConnectorError(Exception):
    def __init__(self, message, errors=None, status=None):
        super().__init__(message)
        self.errors = errors or []
        self.status = status

    @property
    def codes(self):
        return {e.get('Code', '') for e in self.errors}


class Rejected(ConnectorError):
    pass


class Uncertain(ConnectorError):
    pass


class Unavailable(ConnectorError):
    pass


def config():
    return settings.CONTABLE


def get_client():
    """Devuelve el conector vigente: siempre el simulador local."""
    from .simulator import Simulator
    return Simulator()
