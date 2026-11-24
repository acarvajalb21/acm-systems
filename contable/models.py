from django.conf import settings
from django.db import models
from django.db.models import Q


class Submission(models.Model):
    """Intención de envío al sistema contable. Se guarda antes de llamar al conector y conserva su clave de idempotencia."""
    class Kind(models.TextChoices):
        INVOICE = 'invoice', 'Factura de venta'
        VOUCHER = 'voucher', 'Recibo de caja'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pendiente de envío'
        SENDING = 'sending', 'Enviando'
        ACCEPTED = 'accepted', 'Aceptado por el sistema contable'
        REJECTED = 'rejected', 'Rechazado por el sistema contable'
        UNCERTAIN = 'uncertain', 'Resultado incierto'
        REVIEW = 'review', 'Requiere revisión'

    kind = models.CharField(max_length=10, choices=Kind.choices)
    target_id = models.CharField(max_length=40)
    idempotency_key = models.CharField(max_length=30, unique=True)
    environment = models.CharField(max_length=12)
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True)
    errors = models.JSONField(default=list, blank=True)
    response = models.JSONField(null=True, blank=True)
    contable_id = models.CharField(max_length=60, blank=True)
    contable_name = models.CharField(max_length=40, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # Un documento solo puede tener un envío vivo; los rechazados quedan como historial.
            models.UniqueConstraint(fields=['kind', 'target_id'], condition=~Q(status='rejected'), name='one_live_submission'),
        ]

    def __str__(self):
        return f'{self.get_kind_display()} · {self.target_id} · {self.get_status_display()}'


class CatalogItem(models.Model):
    """Copia local de los catálogos activos del sistema contable (formas de pago, vendedores, impuestos, comprobantes, bodegas)."""
    KINDS = [('document_fv', 'Comprobante de factura'), ('document_rc', 'Comprobante de recibo de caja'),
             ('payment_fv', 'Forma de pago en facturas'), ('payment_rc', 'Forma de pago en recibos'),
             ('seller', 'Vendedor'), ('tax', 'Impuesto'), ('warehouse', 'Bodega')]
    kind = models.CharField(max_length=12, choices=KINDS)
    contable_id = models.CharField(max_length=60)
    code = models.CharField(max_length=60, blank=True)
    name = models.CharField(max_length=160)
    active = models.BooleanField(default=True)
    extra = models.JSONField(default=dict, blank=True)
    fetched_at = models.DateTimeField()

    class Meta:
        ordering = ['kind', 'name']
        constraints = [models.UniqueConstraint(fields=['kind', 'contable_id'], name='unique_catalog_item')]

    def __str__(self):
        return self.name


class SimulatedDocument(models.Model):
    """Almacén del simulador local. No es un documento fiscal ni sale de este equipo."""
    kind = models.CharField(max_length=10)
    contable_id = models.CharField(max_length=60, unique=True)
    prefix = models.CharField(max_length=10, blank=True)
    number = models.PositiveIntegerField()
    name = models.CharField(max_length=40)
    idempotency_key = models.CharField(max_length=40, blank=True, db_index=True)
    customer = models.CharField(max_length=30, blank=True)
    payload = models.JSONField()
    total = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    balance = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    mail = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
