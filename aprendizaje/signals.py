"""Aprendizaje por evento.

- Cada pago aplicado enseña al modelo de pagos y da ocasión de cerrar los recordatorios cuya ventana ya se cumplió.
- Cada aplicación revertida corrige el modelo de pagos: si el ingreso quedó sin cliente (o con otro), el ejemplo
  aprendido se anula y el modelo se reconstruye sin él.
- Cualquier cambio en los datos que intervienen en la reposición invalida la sugerencia memorizada.

Todo corre después de confirmar la transacción del negocio; si el aprendizaje falla, la operación ya quedó
registrada y el error va al registro del servidor.
"""
import logging
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

log = logging.getLogger(__name__)


@receiver(post_save, sender='cartera.PaymentApplication')
def application_saved(sender, instance, created, **kwargs):
    reversed_now = not created and instance.reversed_at is not None
    if not created and not reversed_now:
        return

    def later():
        try:
            from cartera.models import BankTransaction
            from . import pagos, cobranza
            col = instance.collection
            if col.source == 'bank' and col.transaction_id:
                if reversed_now:
                    pagos.reconcile(col.transaction_id)
                else:
                    txn = BankTransaction.objects.select_related('party', 'payer').filter(pk=col.transaction_id, party__isnull=False).first()
                    if txn:
                        pagos.reconcile(txn.pk)       # si el ingreso cambió de cliente, primero se olvida el ejemplo anterior
                        pagos.learn_from(txn, txn.party)
            cobranza.evaluate()
        except Exception:
            log.exception('El aprendizaje posterior al pago falló')

    transaction.on_commit(later)


def _forget_plans(sender, **kwargs):
    from . import reposicion
    reposicion.invalidate()


for _model in ('inventory.Movement', 'inventory.Batch', 'inventory.Product', 'inventory.Supplier', 'inventory.Invoice', 'inventory.InvoiceLine',
               'inventory.Receipt', 'inventory.PurchaseOrder', 'inventory.PurchaseOrderLine', 'inventory.SupplyRequest', 'agentes.AgentConfig'):
    post_save.connect(_forget_plans, sender=_model, dispatch_uid=f'aprendizaje-planes-save-{_model}')
    post_delete.connect(_forget_plans, sender=_model, dispatch_uid=f'aprendizaje-planes-delete-{_model}')
