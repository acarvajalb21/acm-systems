"""Agentes de la plataforma: configuración, ejecuciones y propuestas.

Un agente revisa los datos de la plataforma con reglas del negocio y prepara propuestas concretas. Las acciones
que cambian datos se ejecutan al aprobarlas, con la identidad y los permisos de quien aprueba; el estado vive en
la base de datos, así que un reinicio del servidor no pierde trabajo ni repite efectos.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q

USER = settings.AUTH_USER_MODEL


class AgentConfig(models.Model):
    key = models.CharField(max_length=40, unique=True)
    enabled = models.BooleanField('activo', default=True)
    params = models.JSONField('parámetros', default=dict, blank=True)
    updated_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key


class AgentRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = 'running', 'En curso'
        SUCCEEDED = 'succeeded', 'Completada'
        FAILED = 'failed', 'Con error'

    TRIGGERS = [('schedule', 'Programada'), ('manual', 'Manual'), ('event', 'Por evento')]
    agent = models.CharField(max_length=40)
    trigger = models.CharField(max_length=10, choices=TRIGGERS)
    # Clave del periodo para las ejecuciones programadas: impide correr dos veces el mismo turno.
    period_key = models.CharField(max_length=80, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    actor = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    summary = models.CharField(max_length=500, blank=True)
    metrics = models.JSONField(default=dict, blank=True)
    proposed = models.PositiveIntegerField(default=0)
    executed = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['-started_at']
        constraints = [models.UniqueConstraint(fields=['period_key'], condition=Q(period_key__isnull=False), name='one_run_per_period')]


class AgentAction(models.Model):
    class Status(models.TextChoices):
        PROPOSED = 'proposed', 'Por aprobar'
        EXECUTED = 'executed', 'Ejecutada'
        REJECTED = 'rejected', 'Descartada'
        FAILED = 'failed', 'No se pudo ejecutar'
        SUPERSEDED = 'superseded', 'Ya no aplica'

    agent = models.CharField(max_length=40)
    kind = models.CharField(max_length=30)
    run = models.ForeignKey(AgentRun, null=True, blank=True, on_delete=models.SET_NULL, related_name='actions')
    dedupe_key = models.CharField(max_length=160)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PROPOSED)
    title = models.CharField(max_length=200)
    detail = models.CharField('por qué', max_length=600, blank=True)
    amount = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    benefit = models.DecimalField('ahorro o recaudo', max_digits=16, decimal_places=2, null=True, blank=True)
    url = models.CharField(max_length=200, blank=True)
    payload = models.JSONField(default=dict)
    confidence = models.CharField(max_length=10, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    refreshed_at = models.DateTimeField(auto_now=True)
    decided_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)
    result = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['dedupe_key'], condition=Q(status='proposed'), name='one_open_action_per_key')]
        indexes = [models.Index(fields=['agent', 'status'])]

    def __str__(self):
        return self.title
