"""Disparadores por evento: cuando llega una solicitud o un extracto, el agente correspondiente revisa enseguida.

Se ejecutan después de confirmar la transacción que creó los datos; si el agente falla, la operación original ya
quedó guardada y el error queda en la bitácora del agente.
"""
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver


def _later(key):
    from .runner import trigger
    transaction.on_commit(lambda: trigger(key))


@receiver(post_save, sender='inventory.SupplyRequest')
def request_created(sender, instance, created, **kwargs):
    if created:
        _later('solicitudes')


@receiver(post_save, sender='cartera.BankTransaction')
def bank_movement_created(sender, instance, created, **kwargs):
    if created and instance.amount > 0:
        _later('conciliacion')
