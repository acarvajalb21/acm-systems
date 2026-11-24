"""Configuración del copiloto.

El modo por defecto es el asistente local. El modelo generativo solo se activa en el servidor, con las variables de
entorno `FERIA_LLM_PROVIDER`, `FERIA_LLM_API_KEY` y `FERIA_LLM_MODEL` (o con el diccionario `settings.COPILOTO`, que
tiene prioridad y sirve a las pruebas). La clave se lee aquí y no sale de este proceso: no va a plantillas, a
respuestas, al registro de turnos ni a mensajes de error.
"""
import os
from dataclasses import dataclass
from django.conf import settings

PROVIDERS = ('anthropic',)
DEFAULT_MODEL = 'claude-opus-5-5'
MAX_QUESTION = 500
PER_MINUTE = 8
TOTAL_TIMEOUT = 25.0
MAX_TOOL_CALLS = 4


@dataclass(frozen=True)
class Config:
    provider: str = ''
    api_key: str = ''
    model: str = DEFAULT_MODEL
    per_minute: int = PER_MINUTE
    total_timeout: float = TOTAL_TIMEOUT
    refusal_fallback: bool = True
    requested: str = ''          # proveedor pedido, aunque no se reconozca (para avisarlo en pantalla)

    @property
    def generative(self):
        return bool(self.provider and self.api_key)

    def __repr__(self):          # la clave nunca aparece en trazas ni registros
        return f'Config(provider={self.provider!r}, model={self.model!r}, generative={self.generative})'


def current():
    cfg = getattr(settings, 'COPILOTO', None) or {}
    requested = str(cfg.get('provider', os.environ.get('FERIA_LLM_PROVIDER', '')) or '').strip().lower()
    key = str(cfg.get('api_key', os.environ.get('FERIA_LLM_API_KEY', '')) or '').strip()
    model = str(cfg.get('model', os.environ.get('FERIA_LLM_MODEL', '')) or '').strip() or DEFAULT_MODEL
    fallback = cfg.get('refusal_fallback', os.environ.get('FERIA_LLM_REFUSAL_FALLBACK', '1') != '0')
    return Config(provider=requested if requested in PROVIDERS else '', api_key=key, model=model[:80],
                  per_minute=int(cfg.get('per_minute', PER_MINUTE)), total_timeout=float(cfg.get('total_timeout', TOTAL_TIMEOUT)),
                  refusal_fallback=bool(fallback), requested=requested)


def secrets():
    """Valores que jamás deben aparecer en una respuesta."""
    out = [current().api_key, getattr(settings, 'SECRET_KEY', '')]
    return [s for s in out if s and len(s) >= 6]


def scrub(text, extra=()):
    text = str(text or '')
    for secret in [*secrets(), *[s for s in extra if s and len(s) >= 6]]:
        text = text.replace(secret, '[dato reservado]')
    return text
