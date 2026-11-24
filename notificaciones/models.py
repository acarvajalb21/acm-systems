"""Avisos internos: registro de las alertas nuevas de cada persona, una sola vez por alerta."""
from django.conf import settings
from django.db import models


class Notice(models.Model):
    """Una alerta (clave + revisión) que apareció para la persona. Queda como historial dentro de la plataforma."""
    LEVELS = [('critical', 'Urgente'), ('warning', 'Advertencia'), ('info', 'Informativa')]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notices')
    key = models.CharField(max_length=120)
    revision = models.CharField(max_length=64)
    level = models.CharField(max_length=10, choices=LEVELS)
    title = models.CharField(max_length=200)
    detail = models.CharField(max_length=300, blank=True)
    url = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        constraints = [models.UniqueConstraint(fields=['user', 'key', 'revision'], name='one_notice_per_alert_revision')]

    def __str__(self):
        return self.title
