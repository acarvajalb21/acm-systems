import uuid
from decimal import Decimal, ROUND_HALF_UP
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.db.models import Q, F, Sum, Value, DecimalField, OuterRef
from django.db.models.functions import Coalesce, Round
from django.utils import timezone

CENT = Decimal('0.01')


def money(value):
    return Decimal(value or 0).quantize(CENT, ROUND_HALF_UP)


def private_storage():
    # Los adjuntos viven fuera de static/media y solo se entregan por una vista autenticada.
    return FileSystemStorage(location=settings.PRIVATE_DIR / 'adjuntos')


PAYMENT_METHODS = [('transfer', 'Transferencia'), ('check', 'Cheque'), ('cash', 'Efectivo'), ('card', 'Tarjeta'), ('other', 'Otro')]


INVARIABLE_UNITS = {'kg', 'g', 'mg', 'l', 'lt', 'ml', 'm', 'cm', 'mm', 'gal', 'und', 'un', 'u', 'oz', 'lb'}


def plural_unit(unit):
    """Plural de la unidad de registro: unidad → unidades, galón → galones, caja de 12 → cajas de 12, kg → kg."""
    unit = (unit or '').strip() or 'unidad'
    head, sep, tail = unit.partition(' ')
    low = head.lower()
    if low in INVARIABLE_UNITS or low.endswith(('s', 'x')) or not low[-1].isalpha():
        return unit
    if low.endswith('ón'):
        head = head[:-2] + 'ones'
    elif low.endswith('z'):
        head = head[:-1] + 'ces'
    elif low[-1] in 'aeoáéó':
        head += 's'
    else:
        head += 'es'
    return head + sep + tail


def nit_check_digit(number):
    """Dígito de verificación de un NIT (solo dígitos, sin DV)."""
    weights = [3, 7, 13, 17, 19, 23, 29, 37, 41, 43, 47, 53, 59, 67, 71]
    total = sum(int(d) * w for d, w in zip(reversed(number), weights))
    rest = total % 11
    return str(rest if rest in (0, 1) else 11 - rest)


class User(AbstractUser):
    class Role(models.TextChoices):
        REQUESTER = 'requester', 'Solicitante'
        INVENTORY = 'inventory', 'Inventario'
        FINANCE = 'finance', 'Contabilidad'
        BILLING = 'billing', 'Facturación'
        RECEPTION = 'reception', 'Recepción'
        ADMIN = 'admin', 'Administración'
        OWNER = 'owner', 'Dirección'
    role = models.CharField(max_length=12, choices=Role.choices, default=Role.REQUESTER)
    can_issue_invoices = models.BooleanField('puede emitir facturas de venta', default=False)
    # Descuentos de venta que la dirección concede a la administración cada mes (1 permiso = 1 venta con descuento).
    discount_quota = models.PositiveSmallIntegerField('permisos de descuento por mes', default=0)
    must_change_password = models.BooleanField(default=True)
    session_version = models.PositiveIntegerField(default=1)

    @property
    def display_name(self):
        return self.get_full_name() or self.username

    @property
    def initials(self):
        first, last = self.first_name.split()[:1], self.last_name.split()[:1]
        if first or last:
            return ''.join(p[0] for p in first + last).upper()
        return ''.join(p[0] for p in self.username.replace('.', ' ').split()[:2]).upper()


class Supplier(models.Model):
    class Base(models.TextChoices):
        SUBTOTAL = 'subtotal', 'Subtotal antes de impuestos'
        TOTAL = 'total', 'Total de la factura'
    name = models.CharField('razón social', max_length=160)
    nit = models.CharField('NIT', max_length=15, unique=True)
    email = models.EmailField('correo de facturación', blank=True)
    phone = models.CharField('teléfono', max_length=40, blank=True)
    payment_days = models.PositiveIntegerField('plazo de pago (días)', default=30)
    discount_base = models.CharField('base del descuento', max_length=10, choices=Base.choices, default=Base.SUBTOTAL)
    active = models.BooleanField('activo', default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    @property
    def dv(self):
        return nit_check_digit(self.nit)

    @property
    def nit_display(self):
        return f'{int(self.nit):,}'.replace(',', '.') + '-' + self.dv

    def terms(self):
        return {'payment_days': self.payment_days, 'base': self.discount_base,
                'tiers': [[t.days, str(t.percent)] for t in self.tiers.order_by('days')]}

    def __str__(self):
        return self.name


class DiscountTier(models.Model):
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name='tiers')
    days = models.PositiveIntegerField('pago dentro de (días)')
    percent = models.DecimalField('descuento (%)', max_digits=5, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['supplier', 'days'], name='unique_tier_days'),
            models.CheckConstraint(condition=Q(percent__gt=0, percent__lte=100, days__gt=0), name='valid_tier'),
        ]


class Product(models.Model):
    class Category(models.TextChoices):
        MAINTENANCE = 'mantenimiento', 'Mantenimiento'
        ADHESIVES = 'adhesivos', 'Adhesivos'
        SEALANTS = 'sellantes', 'Sellantes'
        COATINGS = 'recubrimientos', 'Recubrimientos'
        OTHER = 'otros', 'Otros'
    code = models.CharField('código interno', max_length=50, unique=True)
    name = models.CharField('nombre comercial', max_length=160)
    category = models.CharField('categoría', max_length=16, choices=Category.choices, default=Category.OTHER)
    generic = models.CharField('descripción', max_length=200, blank=True)
    manufacturer = models.CharField('fabricante', max_length=120, blank=True)
    presentation = models.CharField('presentación', max_length=120, blank=True)
    contable_code = models.CharField('código en el sistema contable', max_length=60, blank=True, help_text='Si se deja vacío se usa el código interno.')
    unit = models.CharField('unidad de registro', max_length=30, default='unidad')
    minimum = models.PositiveIntegerField('mínimo disponible', null=True, blank=True)
    pack_size = models.PositiveIntegerField('unidades por empaque de compra', default=1, help_text='Los pedidos sugeridos se redondean a este múltiplo.')
    controlled_storage = models.BooleanField('requiere almacenamiento controlado', default=False,
                                             help_text='Por ejemplo, lejos del calor, de la humedad o en estantería ventilada.')
    active = models.BooleanField('activo', default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} · {self.code}'

    @property
    def units(self):
        """La unidad de registro en plural, para acompañar cantidades («24 galones»)."""
        return plural_unit(self.unit)

    def qty(self, n):
        """Cantidad con su unidad concordada: «1 unidad», «24 unidades»."""
        return f'{n} {self.unit if n == 1 else self.units}'


class SupplierCode(models.Model):
    """Equivalencia entre el código del proveedor en su factura y el producto interno."""
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name='codes')
    code = models.CharField(max_length=60)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['supplier', 'code'], name='unique_supplier_code')]


class ProductBarcode(models.Model):
    """Código de barras del empaque (GTIN-14 normalizado) y cuántas unidades representa cada caja escaneada."""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='barcodes')
    gtin = models.CharField('código (GTIN)', max_length=14, unique=True)
    units = models.PositiveIntegerField('unidades por caja', default=1)
    created_by = models.ForeignKey('User', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['product__name', 'gtin']
        constraints = [models.CheckConstraint(condition=Q(units__gt=0), name='positive_barcode_units')]

    def __str__(self):
        return f'{self.gtin} · {self.product.name}'


class Batch(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    lot = models.CharField(max_length=80)
    expiry = models.DateField()
    location = models.CharField(max_length=120)
    physical = models.PositiveIntegerField(default=0)
    reserved = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['product', 'lot', 'location'], name='unique_batch'),
            models.CheckConstraint(condition=Q(physical__gte=F('reserved')), name='stock_covers_reservations'),
        ]

    @property
    def expired(self):
        return self.expiry < timezone.localdate()

    @property
    def days_left(self):
        return (self.expiry - timezone.localdate()).days

    @property
    def available(self):
        return 0 if self.expired else self.physical - self.reserved

    def __str__(self):
        state = 'vencido' if self.expired else f'disponible {self.available}'
        return f'{self.product.name} · lote {self.lot} · {self.location} · vence {self.expiry:%d/%m/%Y} · {state}'


class SupplyRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pendiente'
        APPROVED = 'approved', 'Aprobada'
        DELIVERED = 'delivered', 'Entregada'
        REJECTED = 'rejected', 'Rechazada'
        CANCELLED = 'cancelled', 'Cancelada'
        RELEASED = 'released', 'Liberada'
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    requester = models.ForeignKey(User, on_delete=models.PROTECT, related_name='requests')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    purpose = models.CharField(max_length=300)
    needed_by = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    resolution = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name='positive_request')]


class Reservation(models.Model):
    STATUS = [('active', 'Activa'), ('delivered', 'Entregada'), ('released', 'Liberada')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.OneToOneField(SupplyRequest, null=True, blank=True, on_delete=models.PROTECT, related_name='reservation')
    batch = models.ForeignKey(Batch, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    purpose = models.CharField(max_length=300)
    status = models.CharField(max_length=12, choices=STATUS, default='active')
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name='positive_reservation')]


class Invoice(models.Model):
    class Review(models.TextChoices):
        PENDING = 'pending', 'Por revisar'
        VALIDATED = 'validated', 'Validada'
        VOID = 'void', 'Anulada'
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='invoices')
    number = models.CharField('número de factura', max_length=40)
    issue_date = models.DateField('fecha de emisión')
    due_date = models.DateField('fecha de vencimiento')
    ordered_on = models.DateField('fecha del pedido', null=True, blank=True)
    order_number = models.CharField('orden de compra', max_length=40, blank=True)
    cufe = models.CharField('CUFE', max_length=120, blank=True)
    currency = models.CharField(max_length=3, default='COP')
    global_discount = models.DecimalField('descuento global', max_digits=16, decimal_places=2, default=0)
    retention = models.DecimalField('retenciones', max_digits=16, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    notes = models.CharField('observaciones', max_length=300, blank=True)
    source = models.CharField(max_length=10, choices=[('manual', 'Registro manual'), ('xml', 'Factura electrónica XML'), ('pdf', 'Factura PDF')],
                              default='manual')
    reception_required = models.BooleanField('requiere recepción física', default=True)
    review = models.CharField(max_length=10, choices=Review.choices, default=Review.PENDING)
    terms = models.JSONField(null=True, blank=True)
    document_terms = models.JSONField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    validated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    validated_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['supplier', 'number'], name='unique_invoice_number'),
            models.UniqueConstraint(fields=['cufe'], condition=~Q(cufe=''), name='unique_invoice_cufe'),
            models.CheckConstraint(condition=Q(global_discount__gte=0, retention__gte=0), name='non_negative_invoice_adjustments'),
        ]

    def recalculate(self):
        lines = list(self.lines.all())
        self.subtotal = money(sum((l.net for l in lines), Decimal(0)))
        self.tax = money(sum((l.tax for l in lines), Decimal(0)))
        self.total = money(self.subtotal - self.global_discount + self.tax - self.retention)

    @property
    def expected_units(self):
        return sum(l.quantity for l in self.lines.all())

    @property
    def received_units(self):
        return sum(l.received for l in self.lines.all())

    @property
    def reception(self):
        if self.review == self.Review.VOID:
            return 'void'
        if not self.reception_required:
            return 'not_required'
        received, expected = self.received_units, self.expected_units
        return 'complete' if expected and received >= expected else 'partial' if received else 'pending'

    @property
    def reception_label(self):
        return {'void': '—', 'complete': 'Recibida', 'partial': 'Recepción parcial', 'pending': 'Por recibir',
                'not_required': 'Sin recepción física'}[self.reception]

    # Saldo = total − pagos y descuentos confirmados − notas crédito aplicadas ± ajustes documentados.
    def _settled(self):
        if not hasattr(self, '_settlement'):
            if hasattr(self, 'paid_sum'):
                self._settlement = (self.paid_sum, self.discount_sum, self.credit_sum, self.adjust_sum)
            else:
                done = self.allocations.filter(payment__status__in=SupplierPayment.SETTLED).aggregate(a=Sum('amount'), d=Sum('discount'))
                credit = self.credit_applications.aggregate(s=Sum('amount'))['s']
                adjust = sum((a.signed for a in self.adjustments.all()), Decimal(0))
                self._settlement = (done['a'] or Decimal(0), done['d'] or Decimal(0), credit or Decimal(0), adjust)
        return self._settlement

    @property
    def amount_paid(self):
        return self._settled()[0]

    @property
    def discount_taken(self):
        return self._settled()[1]

    @property
    def credit_applied(self):
        return self._settled()[2]

    @property
    def adjustments_total(self):
        return self._settled()[3]

    @property
    def balance(self):
        paid, discount, credit, adjust = self._settled()
        return money(self.total - paid - discount - credit + adjust)

    @property
    def paid(self):
        return self.review == self.Review.VALIDATED and self.balance <= 0

    @property
    def last_payment(self):
        a = self.allocations.filter(payment__status__in=SupplierPayment.SETTLED).select_related('payment').order_by('-payment__paid_on', '-pk').first()
        return a.payment if a else None

    @property
    def payment_state(self):
        if self.review == self.Review.VOID:
            return 'void'
        if self.paid:
            return 'paid'
        if self.due_date < timezone.localdate():
            return 'overdue'
        paid, discount, _, _ = self._settled()
        return 'partial' if paid or discount else 'pending'

    @property
    def payment_label(self):
        return {'void': '—', 'paid': 'Pagada', 'overdue': 'Vencida', 'pending': 'Por pagar', 'partial': 'Abono parcial'}[self.payment_state]

    def __str__(self):
        return f'{self.number} · {self.supplier.name}'


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='lines')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    supplier_code = models.CharField(max_length=60, blank=True)
    description = models.CharField(max_length=200)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=16, decimal_places=2)
    discount = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    expected_lot = models.CharField(max_length=80, blank=True)
    expected_expiry = models.DateField(null=True, blank=True)
    received = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['pk']
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name='positive_invoice_line'),
            models.CheckConstraint(condition=Q(received__lte=F('quantity')), name='no_unapproved_excess'),
            models.CheckConstraint(condition=Q(unit_price__gte=0, discount__gte=0, tax_rate__gte=0), name='non_negative_line_values'),
        ]

    @property
    def gross(self):
        return money(self.quantity * self.unit_price)

    @property
    def net(self):
        return money(self.gross - self.discount)

    @property
    def tax(self):
        return money(self.net * self.tax_rate / 100)

    @property
    def line_total(self):
        return money(self.net + self.tax)

    @property
    def pending(self):
        return self.quantity - self.received

    @property
    def unit_cost(self):
        return money(self.net / self.quantity) if self.quantity else Decimal(0)


class InvoiceFile(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='files')
    file = models.FileField(upload_to='facturas/%Y/%m/', storage=private_storage)
    name = models.CharField(max_length=200)
    content_type = models.CharField(max_length=80)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(User, on_delete=models.PROTECT)
    uploaded_at = models.DateTimeField(auto_now_add=True)


class Receipt(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='receipts')
    received_on = models.DateField()
    note = models.CharField(max_length=300, blank=True)
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


def with_payable_balance(qs):
    """Anota pagos, descuentos, notas crédito y ajustes sin multiplicar filas.

    El saldo se redondea a centavos en la propia consulta: SQLite opera estos valores en coma flotante y, sin
    redondear, una factura saldada puede quedar con un residuo ínfimo mayor que cero y aparecer como pendiente."""
    def total(model, expr, **filters):
        return Coalesce(models.Subquery(model.objects.filter(invoice=OuterRef('pk'), **filters).order_by().values('invoice')
                                        .annotate(s=Sum(expr)).values('s')), Value(Decimal(0)), output_field=DecimalField(max_digits=16, decimal_places=2))
    return qs.annotate(paid_sum=total(PaymentAllocation, 'amount', payment__status__in=SupplierPayment.SETTLED),
                       discount_sum=total(PaymentAllocation, 'discount', payment__status__in=SupplierPayment.SETTLED),
                       credit_sum=total(CreditNoteApplication, 'amount'),
                       adjust_plus=total(PayableAdjustment, 'amount', kind__in=PayableAdjustment.INCREASE),
                       adjust_minus=total(PayableAdjustment, 'amount', kind__in=PayableAdjustment.DECREASE)) \
             .annotate(adjust_sum=F('adjust_plus') - F('adjust_minus')) \
             .annotate(balance_due=Round(F('total') - F('paid_sum') - F('discount_sum') - F('credit_sum') + F('adjust_sum'), 2,
                                         output_field=DecimalField(max_digits=16, decimal_places=2)))


class SupplierPayment(models.Model):
    """Pago a proveedor: programado (intención) → confirmado (con soporte) → conciliado con el extracto."""
    class Status(models.TextChoices):
        SCHEDULED = 'scheduled', 'Programado'
        CONFIRMED = 'confirmed', 'Confirmado con soporte'
        RECONCILED = 'reconciled', 'Conciliado con el banco'
        CANCELLED = 'cancelled', 'Anulado'
    SETTLED = ('confirmed', 'reconciled')
    METHODS = PAYMENT_METHODS
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='payments')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED)
    scheduled_on = models.DateField('fecha programada', null=True, blank=True)
    paid_on = models.DateField('fecha del pago', null=True, blank=True)
    method = models.CharField('medio', max_length=10, choices=PAYMENT_METHODS, default='transfer')
    reference = models.CharField('referencia o comprobante', max_length=80, blank=True)
    amount = models.DecimalField('valor transferido', max_digits=16, decimal_places=2)
    contable_receipt = models.CharField('comprobante de egreso en el sistema contable', max_length=40, blank=True)
    note = models.CharField('observaciones', max_length=300, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-paid_on', '-scheduled_on', '-pk']
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name='positive_supplier_payment')]

    def __str__(self):
        return f'Pago {self.pk} · {self.supplier.name}'

    @property
    def settled(self):
        return self.status in self.SETTLED

    @property
    def discount_total(self):
        return sum((a.discount for a in self.allocations.all()), Decimal(0))


class PaymentAllocation(models.Model):
    payment = models.ForeignKey(SupplierPayment, on_delete=models.CASCADE, related_name='allocations')
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='allocations')
    amount = models.DecimalField('valor aplicado', max_digits=16, decimal_places=2)
    discount = models.DecimalField('descuento', max_digits=16, decimal_places=2, default=0)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    days_elapsed = models.IntegerField(null=True, blank=True)
    potential_discount = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    lost_discount = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    discount_note = models.CharField('soporte del descuento', max_length=200, blank=True)

    class Meta:
        ordering = ['pk']
        constraints = [models.UniqueConstraint(fields=['payment', 'invoice'], name='unique_payment_invoice'),
                       models.CheckConstraint(condition=Q(amount__gte=0, discount__gte=0, lost_discount__gte=0), name='non_negative_allocation')]


class PaymentFile(models.Model):
    payment = models.ForeignKey(SupplierPayment, on_delete=models.PROTECT, related_name='files')
    file = models.FileField(upload_to='pagos/%Y/%m/', storage=private_storage)
    name = models.CharField(max_length=200)
    content_type = models.CharField(max_length=80)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(User, on_delete=models.PROTECT)
    uploaded_at = models.DateTimeField(auto_now_add=True)


class SupplierCreditNote(models.Model):
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='credit_notes')
    number = models.CharField('número', max_length=60)
    issue_date = models.DateField('fecha', null=True, blank=True)
    amount = models.DecimalField('valor', max_digits=16, decimal_places=2)
    note = models.CharField('motivo', max_length=300, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-issue_date', '-pk']
        constraints = [models.UniqueConstraint(fields=['supplier', 'number'], name='unique_credit_note'),
                       models.CheckConstraint(condition=Q(amount__gt=0), name='positive_credit_note')]

    @property
    def applied(self):
        return self.applications.aggregate(s=Sum('amount'))['s'] or Decimal(0)

    @property
    def available(self):
        return money(self.amount - self.applied)


class CreditNoteApplication(models.Model):
    credit_note = models.ForeignKey(SupplierCreditNote, on_delete=models.PROTECT, related_name='applications')
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='credit_applications')
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name='positive_credit_application')]


class PayableAdjustment(models.Model):
    KINDS = [('discount_claim', 'Diferencia de descuento cobrada por el proveedor'), ('charge', 'Otro cargo del proveedor'),
             ('retention', 'Retención practicada'), ('writeoff', 'Ajuste a favor de la empresa')]
    INCREASE, DECREASE = ('discount_claim', 'charge'), ('retention', 'writeoff')
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='adjustments')
    kind = models.CharField(max_length=16, choices=KINDS)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    reference = models.CharField('soporte', max_length=120)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name='positive_payable_adjustment')]

    @property
    def signed(self):
        return self.amount if self.kind in self.INCREASE else -self.amount


class Movement(models.Model):
    class Kind(models.TextChoices):
        RECEIPT = 'receipt', 'Entrada por factura'
        INITIAL = 'initial', 'Saldo inicial'
        ADJUST_IN = 'adjust_in', 'Ajuste positivo'
        ADJUST_OUT = 'adjust_out', 'Ajuste negativo'
        WRITEOFF = 'writeoff', 'Baja'
        EXIT = 'exit', 'Salida / despacho'
        DISPATCH = 'dispatch', 'Entrega a solicitante'
    LOSS_REASONS = [('expiry', 'Vencimiento'), ('storage', 'Almacenamiento inadecuado'), ('damage', 'Daño o derrame'), ('other', 'Otra causa')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(Batch, on_delete=models.PROTECT, related_name='movements')
    quantity = models.IntegerField()
    kind = models.CharField(max_length=12, choices=Kind.choices)
    loss_reason = models.CharField(max_length=12, choices=LOSS_REASONS, blank=True)
    reference = models.CharField(max_length=80, blank=True)
    note = models.CharField(max_length=300, blank=True)
    invoice_line = models.ForeignKey(InvoiceLine, null=True, blank=True, on_delete=models.PROTECT)
    receipt = models.ForeignKey(Receipt, null=True, blank=True, on_delete=models.PROTECT, related_name='movements')
    reservation = models.ForeignKey(Reservation, null=True, blank=True, on_delete=models.PROTECT)
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=~Q(quantity=0), name='nonzero_movement')]


class ReceivingSession(models.Model):
    """Recepción de una factura con lector de códigos: las cajas se suman y al confirmar se registra la entrada por lotes."""
    class Status(models.TextChoices):
        OPEN = 'open', 'En curso'
        CONFIRMED = 'confirmed', 'Confirmada'
        DISCARDED = 'discarded', 'Descartada'
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='receiving_sessions')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    location = models.CharField('ubicación', max_length=120, blank=True)
    receipt = models.OneToOneField(Receipt, null=True, blank=True, on_delete=models.PROTECT, related_name='receiving_session')
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    closed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['invoice'], condition=Q(status='open'), name='one_open_receiving_per_invoice')]


class ReceivingScan(models.Model):
    session = models.ForeignKey(ReceivingSession, on_delete=models.CASCADE, related_name='scans')
    line = models.ForeignKey(InvoiceLine, on_delete=models.PROTECT, related_name='+')
    gtin = models.CharField(max_length=14, blank=True)
    lot = models.CharField(max_length=80)
    expiry = models.DateField()
    serial = models.CharField(max_length=40, blank=True)
    quantity = models.PositiveIntegerField()
    source = models.CharField(max_length=8, choices=[('scan', 'Escaneada'), ('manual', 'Registrada a mano')], default='scan')
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name='positive_receiving_scan'),
                       models.UniqueConstraint(fields=['session', 'gtin', 'serial'], condition=~Q(serial=''), name='unique_scanned_box')]


class PurchaseOrder(models.Model):
    """Pedido a un proveedor: nace de la sugerencia de reposición (o a mano) y se cierra con la factura.

    El envío es simulado: en «draft» el mensaje está preparado; en «sent» quedó registrado un envío simulado."""
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Mensaje preparado'
        SENT = 'sent', 'Envío simulado registrado'
        RECEIVED = 'received', 'Recibido'
        CANCELLED = 'cancelled', 'Cancelado'
    OPEN = ('draft', 'sent')
    SOURCES = [('agent', 'Agente de reposición'), ('manual', 'Registro manual')]
    number = models.CharField('número', max_length=20, unique=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='purchase_orders')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    source = models.CharField(max_length=8, choices=SOURCES, default='manual')
    note = models.CharField('observaciones', max_length=300, blank=True)
    expected_on = models.DateField('llegada esperada', null=True, blank=True)
    invoice = models.ForeignKey(Invoice, null=True, blank=True, on_delete=models.SET_NULL, related_name='purchase_orders')
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    sent_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    sent_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    closed_at = models.DateTimeField(null=True, blank=True)
    close_note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.number} · {self.supplier.name}'

    @property
    def open(self):
        return self.status in self.OPEN

    @property
    def units(self):
        return sum(l.quantity for l in self.lines.all())

    @property
    def estimated_cost(self):
        return money(sum((l.quantity * l.unit_cost for l in self.lines.all() if l.unit_cost is not None), Decimal(0)))


class PurchaseOrderLine(models.Model):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='lines')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField('cantidad')
    unit_cost = models.DecimalField('costo unitario estimado', max_digits=16, decimal_places=2, null=True, blank=True)
    reason = models.CharField('por qué', max_length=300, blank=True)

    class Meta:
        ordering = ['pk']
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name='positive_purchase_line'),
                       models.UniqueConstraint(fields=['order', 'product'], name='unique_purchase_line')]


class Operation(models.Model):
    token = models.UUIDField(primary_key=True)
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    kind = models.CharField(max_length=30)
    created_at = models.DateTimeField(auto_now_add=True)


class Audit(models.Model):
    actor = models.ForeignKey(User, null=True, on_delete=models.PROTECT)
    event = models.CharField(max_length=80)
    target = models.CharField(max_length=100, blank=True)
    detail = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class AlertRead(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    key = models.CharField(max_length=120)
    revision = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'key'], name='unique_alert_reader')]


class Settings(models.Model):
    expiry_days = models.PositiveIntegerField(default=90)
    discount_alert_days = models.PositiveIntegerField(default=5)
    due_alert_days = models.PositiveIntegerField(default=7)

    @classmethod
    def current(cls):
        return cls.objects.get_or_create(pk=1)[0]


class LoginAttempt(models.Model):
    key = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
