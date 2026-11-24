"""Registro de las preguntas al copiloto.

Cada turno guarda quién preguntó, qué herramientas se consultaron y con qué argumentos, las fuentes citadas, cuánto
tardó y, si algo falló, un código de error. No guarda claves, encabezados ni respuestas crudas del proveedor.
"""
from django.conf import settings
from django.db import models


class CopilotTurn(models.Model):
    class Mode(models.TextChoices):
        LOCAL = 'local', 'Asistente local'
        GENERATIVE = 'generativo', 'Modelo generativo'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='copilot_turns')
    question = models.CharField('pregunta', max_length=500)
    context = models.CharField('registro de contexto', max_length=60, blank=True)
    mode = models.CharField('modo', max_length=12, choices=Mode.choices, default=Mode.LOCAL)
    model = models.CharField('modelo', max_length=80, blank=True)
    tools = models.JSONField('herramientas y argumentos', default=list, blank=True)
    sources = models.JSONField('fuentes', default=list, blank=True)
    answer = models.CharField('respuesta', max_length=2000, blank=True)
    notes = models.JSONField('avisos', default=list, blank=True)
    duration_ms = models.PositiveIntegerField('duración (ms)', default=0)
    error = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        indexes = [models.Index(fields=['user', 'created_at'])]

    def __str__(self):
        return f'{self.user_id} · {self.question[:60]}'

    @property
    def tool_names(self):
        return [t.get('name', '') for t in self.tools if isinstance(t, dict)]
