from decimal import Decimal
from django.conf import settings
from django.db import models
from django.db.models import Q, F, Sum, Value, DecimalField
from django.db.models.functions import Coalesce, Round
from django.utils import timezone
from inventory.models import Product, Batch, Reservation, Movement, private_storage, money

USER = settings.AUTH_USER_MODEL
ZERO = Decimal('0.00')
MONEY = {'max_digits': 16, 'decimal_places': 2}


class Context(models.TextChoices):
    COUNTER = 'counter', 'Mostrador'
    COMPANY = 'company', 'Empresa'
    CONTRACTOR = 'contractor', 'Contratista'


class Party(models.Model):
    """Tercero con identidad fiscal propia. El contexto comercial se elige en cada venta."""
    ID_TYPES = [('13', 'Cédula de ciudadanía'), ('31', 'NIT'), ('22', 'Cédula de extranjería'), ('41', 'Pasaporte'),
                ('12', 'Tarjeta de identidad'), ('11', 'Registro civil')]
    KINDS = [('person', 'Persona natural'), ('company', 'Persona jurídica')]
    id_type = models.CharField('tipo de identificación', max_length=2, choices=ID_TYPES, default='13')
    identification = models.CharField('número de identificación', max_length=20)
    name = models.CharField('nombre o razón social', max_length=160)
    kind = models.CharField('tipo de persona', max_length=8, choices=KINDS, default='person')
    default_context = models.CharField('contexto habitual', max_length=10, choices=Context.choices, default=Context.COUNTER)
    price_list = models.ForeignKey('PriceList', null=True, blank=True, on_delete=models.PROTECT, verbose_name='lista de precios acordada')
    credit_days = models.PositiveIntegerField('plazo de crédito (días)', default=30)
    requires_po = models.BooleanField('exige orden de compra', default=False)
    active = models.BooleanField('activo', default=True)
    notes = models.CharField('notas internas', max_length=300, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        constraints = [models.UniqueConstraint(fields=['id_type', 'identification'], name='unique_party_identification')]

    def __str__(self):
        return f'{self.name} · {self.get_id_type_display()} {self.identification}'


class PartyBranch(models.Model):
    party = models.ForeignKey(Party, on_delete=models.CASCADE, related_name='branches')
    office = models.PositiveIntegerField('sucursal en el sistema contable', default=0)
    name = models.CharField('nombre de la sucursal', max_length=120, default='Principal')
    einvoice_email = models.EmailField('correo de facturación electrónica', blank=True)
    address = models.CharField('dirección', max_length=200, blank=True)
    city = models.CharField('ciudad', max_length=80, blank=True)

    class Meta:
        ordering = ['office']
        constraints = [models.UniqueConstraint(fields=['party', 'office'], name='unique_party_branch')]

    def __str__(self):
        return f'{self.office} · {self.name}'


class PartyContact(models.Model):
    ROLES = [('collection', 'Cobranza'), ('billing', 'Facturación'), ('other', 'Otro')]
    CHANNELS = [('chat', 'Mensajería'), ('email', 'Correo'), ('phone', 'Teléfono')]
    party = models.ForeignKey(Party, on_delete=models.CASCADE, related_name='contacts')
    name = models.CharField('persona', max_length=120)
    role = models.CharField('uso', max_length=12, choices=ROLES, default='collection')
    channel = models.CharField('canal', max_length=10, choices=CHANNELS)
    value = models.CharField('número o correo', max_length=160)
    preferred = models.BooleanField('canal preferido de cobranza', default=False)
    notes = models.CharField('notas', max_length=200, blank=True)

    class Meta:
        ordering = ['-preferred', 'name']


class PriceList(models.Model):
    code = models.CharField('código', max_length=20, unique=True)
    name = models.CharField('nombre', max_length=80)
    context = models.CharField('contexto', max_length=10, choices=Context.choices)
    active = models.BooleanField('activa', default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class PriceItem(models.Model):
    price_list = models.ForeignKey(PriceList, on_delete=models.PROTECT, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    price = models.DecimalField('precio', **MONEY)
    valid_from = models.DateField('vigente desde')
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['product__name', '-valid_from']
        constraints = [models.UniqueConstraint(fields=['price_list', 'product', 'valid_from'], name='unique_price_version'),
                       models.CheckConstraint(condition=Q(price__gte=0), name='non_negative_price')]


class Sale(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Borrador'
        SENDING = 'sending', 'Enviando al sistema contable'
        ISSUED = 'issued', 'Emitida'
        REJECTED = 'rejected', 'Rechazada por el sistema contable'
        UNCERTAIN = 'uncertain', 'Envío incierto'
        CANCELLED = 'cancelled', 'Cancelada'
    CONDITIONS = [('cash', 'Contado'), ('credit', 'Crédito')]
    CASH_METHODS = [('cash', 'Efectivo'), ('card', 'Datáfono'), ('transfer', 'Transferencia'), ('other', 'Otro')]

    party = models.ForeignKey(Party, on_delete=models.PROTECT, related_name='sales')
    branch = models.ForeignKey(PartyBranch, on_delete=models.PROTECT)
    context = models.CharField('contexto comercial', max_length=10, choices=Context.choices)
    price_list = models.ForeignKey(PriceList, on_delete=models.PROTECT, verbose_name='lista de precios')
    issue_date = models.DateField('fecha de la factura')
    seller_id = models.CharField('vendedor en el sistema contable', max_length=60)
    condition = models.CharField('condición de pago', max_length=6, choices=CONDITIONS)
    payment_type_id = models.CharField('forma de pago en el sistema contable', max_length=60)
    payment_name = models.CharField(max_length=160)
    due_date = models.DateField('vencimiento', null=True, blank=True)
    cash_method = models.CharField('medio del recaudo', max_length=10, choices=CASH_METHODS, blank=True)
    cash_reference = models.CharField('referencia del recaudo', max_length=80, blank=True)
    purchase_order = models.CharField('orden de compra', max_length=20, blank=True)
    observations = models.CharField('observaciones de la factura', max_length=1000, blank=True)
    # Datos internos de acceso restringido: no se envían al sistema contable.
    beneficiary_name = models.CharField('quien recibe el pedido', max_length=160, blank=True)
    beneficiary_document = models.CharField('documento de quien recibe', max_length=30, blank=True)
    beneficiary_relation = models.CharField('relación con el cliente', max_length=60, blank=True)
    internal_note = models.CharField('nota interna', max_length=300, blank=True)
    subtotal = models.DecimalField(**MONEY, default=0)
    tax = models.DecimalField(**MONEY, default=0)
    total = models.DecimalField(**MONEY, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    status_detail = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    issued_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    issued_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=300, blank=True)
    # Respuesta fiscal del sistema contable (separada del estado del envío y del saldo de cartera).
    environment = models.CharField(max_length=12, blank=True)
    contable_id = models.CharField(max_length=60, blank=True)
    fiscal_prefix = models.CharField(max_length=10, blank=True)
    fiscal_number = models.PositiveIntegerField(null=True, blank=True)
    fiscal_name = models.CharField(max_length=40, blank=True)
    cufe = models.CharField(max_length=120, blank=True)
    stamp_status = models.CharField(max_length=20, blank=True)
    stamp_errors = models.JSONField(default=list, blank=True)
    mail_status = models.CharField(max_length=120, blank=True)
    contable_balance = models.DecimalField(**MONEY, null=True, blank=True)
    balance_checked_at = models.DateTimeField(null=True, blank=True)
    pdf = models.FileField(upload_to='ventas/%Y/%m/', storage=private_storage, blank=True)
    pdf_sha256 = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['contable_id'], condition=~Q(contable_id=''), name='unique_sale_contable_id')]

    def __str__(self):
        return self.fiscal_name or f'Borrador {self.pk}'

    @property
    def label(self):
        return self.fiscal_name or f'Venta {self.pk}'

    @property
    def editable(self):
        return self.status in (self.Status.DRAFT, self.Status.REJECTED)


class SaleLine(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='lines')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    price_item = models.ForeignKey(PriceItem, null=True, blank=True, on_delete=models.PROTECT)
    list_price = models.DecimalField(**MONEY, null=True, blank=True)
    unit_price = models.DecimalField(**MONEY)
    price_reason = models.CharField('motivo del precio distinto', max_length=200, blank=True)
    discount = models.DecimalField(**MONEY, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    prior_movement = models.ForeignKey(Movement, null=True, blank=True, on_delete=models.PROTECT, related_name='+',
                                       verbose_name='entrega ya registrada')

    class Meta:
        ordering = ['pk']
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name='positive_sale_line'),
                       models.CheckConstraint(condition=Q(unit_price__gte=0, discount__gte=0), name='non_negative_sale_line')]

    @property
    def base(self):
        return money(self.quantity * self.unit_price - self.discount)

    @property
    def tax_value(self):
        return money(self.base * self.tax_rate / 100)

    @property
    def line_total(self):
        return money(self.base + self.tax_value)

    @property
    def price_changed(self):
        return self.list_price is None or self.unit_price != self.list_price


class SaleStock(models.Model):
    """Descuento físico de la venta: reserva al emitir y salida al ser aceptada, o enlace a una entrega previa."""
    line = models.ForeignKey(SaleLine, on_delete=models.CASCADE, related_name='stock')
    batch = models.ForeignKey(Batch, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    reservation = models.OneToOneField(Reservation, null=True, blank=True, on_delete=models.PROTECT, related_name='sale_stock')
    movement = models.OneToOneField(Movement, null=True, blank=True, on_delete=models.PROTECT, related_name='sale_stock')


class Receivable(models.Model):
    """Cuota de cartera identificada como en el sistema contable: prefijo, consecutivo y cuota."""
    SOURCES = [('sale', 'Venta emitida en la plataforma'), ('opening', 'Saldo inicial de cartera')]
    party = models.ForeignKey(Party, on_delete=models.PROTECT, related_name='receivables')
    branch_office = models.PositiveIntegerField(default=0)
    sale = models.OneToOneField(Sale, null=True, blank=True, on_delete=models.PROTECT, related_name='receivable')
    source = models.CharField(max_length=8, choices=SOURCES)
    prefix = models.CharField('prefijo', max_length=10)
    number = models.PositiveIntegerField('número')
    quote = models.PositiveIntegerField('cuota', default=1)
    issue_date = models.DateField('emisión')
    due_date = models.DateField('vencimiento')
    amount = models.DecimalField('valor', **MONEY)
    note = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['due_date', 'number']
        constraints = [models.UniqueConstraint(fields=['prefix', 'number', 'quote'], name='unique_receivable_due'),
                       models.CheckConstraint(condition=Q(amount__gt=0), name='positive_receivable')]

    def __str__(self):
        return f'{self.prefix}-{self.number}'

    @property
    def document(self):
        return f'{self.prefix}-{self.number}' + (f' · cuota {self.quote}' if self.quote != 1 else '')


def with_balance(qs):
    applied = Coalesce(Sum('applications__amount', filter=Q(applications__reversed_at__isnull=True)), Value(ZERO), output_field=DecimalField(**MONEY))
    return qs.annotate(applied=applied).annotate(
        adjusted=Coalesce(models.Subquery(ReceivableAdjustment.objects.filter(receivable=models.OuterRef('pk')).order_by()
                                          .values('receivable').annotate(s=Sum('amount')).values('s')), Value(ZERO), output_field=DecimalField(**MONEY))
    ).annotate(balance=Round(F('amount') - F('applied') - F('adjusted'), 2, output_field=DecimalField(**MONEY)))   # a centavos: sin residuos de coma flotante


class ReceivableAdjustment(models.Model):
    KINDS = [('discount', 'Descuento otorgado'), ('retention_source', 'Retención en la fuente'), ('retention_ica', 'Retención de ICA'),
             ('retention_iva', 'Retención de IVA'), ('credit_note', 'Nota crédito')]
    receivable = models.ForeignKey(Receivable, on_delete=models.PROTECT, related_name='adjustments')
    kind = models.CharField(max_length=20, choices=KINDS)
    amount = models.DecimalField(**MONEY)
    reference = models.CharField('soporte o documento', max_length=80)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name='positive_adjustment')]


class BankAccount(models.Model):
    name = models.CharField('nombre', max_length=80)
    bank = models.CharField('banco', max_length=80)
    last4 = models.CharField('últimos 4 dígitos', max_length=4)
    contable_payment_id = models.CharField('forma de pago en recibos del sistema contable', max_length=60)
    active = models.BooleanField('activa', default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} · {self.bank} ****{self.last4}'


class StatementImport(models.Model):
    STATUS = [('preview', 'Por confirmar'), ('confirmed', 'Confirmada'), ('discarded', 'Descartada')]
    account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name='imports')
    file = models.FileField(upload_to='extractos/%Y/%m/', storage=private_storage)
    file_name = models.CharField(max_length=200)
    sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=10, choices=STATUS, default='preview')
    rows = models.JSONField(default=list)
    warnings = models.JSONField(default=list, blank=True)
    total_rows = models.PositiveIntegerField(default=0)
    new_rows = models.PositiveIntegerField(default=0)
    known_rows = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class Payer(models.Model):
    """Origen del dinero: cuenta de origen o titular que transfiere (no necesariamente el cliente facturado)."""
    key = models.CharField(max_length=80, unique=True)
    name = models.CharField('titular', max_length=160, blank=True)
    holder_id = models.CharField('identificación del titular', max_length=30, blank=True)
    account_masked = models.CharField('cuenta de origen', max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return ' · '.join(x for x in (self.name, self.account_masked) if x) or self.key


class PayerLink(models.Model):
    STATUS = [('suggested', 'Sugerido'), ('confirmed', 'Confirmado'), ('rejected', 'Descartado')]
    payer = models.ForeignKey(Payer, on_delete=models.CASCADE, related_name='links')
    party = models.ForeignKey(Party, on_delete=models.CASCADE, related_name='payer_links')
    status = models.CharField(max_length=10, choices=STATUS, default='confirmed')
    relation = models.CharField('relación', max_length=80, blank=True)
    confirmed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['payer', 'party'], name='unique_payer_party')]


class BankTransaction(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Por identificar'
        IDENTIFIED = 'identified', 'Identificado, con saldo sin aplicar'
        APPLIED = 'applied', 'Aplicado'
        OTHER = 'other', 'No es cartera'
    account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name='transactions')
    statement = models.ForeignKey(StatementImport, on_delete=models.PROTECT, related_name='transactions')
    date = models.DateField('fecha')
    amount = models.DecimalField('valor', **MONEY)
    description = models.CharField('descripción', max_length=300, blank=True)
    reference = models.CharField('referencia', max_length=120, blank=True)
    origin_account = models.CharField('cuenta de origen', max_length=20, blank=True)
    holder_name = models.CharField('titular', max_length=160, blank=True)
    holder_id = models.CharField('identificación del titular', max_length=30, blank=True)
    balance_after = models.DecimalField(**MONEY, null=True, blank=True)
    bank_ref = models.CharField(max_length=80, blank=True)
    fingerprint = models.CharField(max_length=64)
    occurrence = models.PositiveIntegerField(default=1)
    payer = models.ForeignKey(Payer, null=True, blank=True, on_delete=models.PROTECT, related_name='transactions')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    party = models.ForeignKey(Party, null=True, blank=True, on_delete=models.PROTECT, related_name='bank_transactions')
    supplier_payment = models.OneToOneField('inventory.SupplierPayment', null=True, blank=True, on_delete=models.PROTECT, related_name='bank_transaction')
    match_reasons = models.JSONField(default=list, blank=True)
    note = models.CharField(max_length=300, blank=True)
    identified_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    identified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-pk']
        constraints = [models.UniqueConstraint(fields=['account', 'fingerprint', 'occurrence'], name='unique_bank_movement'),
                       models.CheckConstraint(condition=~Q(amount=0), name='nonzero_bank_movement')]

    def __str__(self):
        return f'{self.date:%d/%m/%Y} · {self.amount}'


class Collection(models.Model):
    """Recaudo: dinero efectivamente recibido (de contado en la venta o identificado en el banco)."""
    SOURCES = [('counter', 'Recaudo de contado en la venta'), ('bank', 'Movimiento bancario'), ('manual', 'Registro manual')]
    party = models.ForeignKey(Party, on_delete=models.PROTECT, related_name='collections')
    date = models.DateField()
    amount = models.DecimalField(**MONEY)
    method = models.CharField(max_length=10, choices=Sale.CASH_METHODS)
    reference = models.CharField(max_length=80, blank=True)
    source = models.CharField(max_length=8, choices=SOURCES)
    sale = models.ForeignKey(Sale, null=True, blank=True, on_delete=models.PROTECT, related_name='collections')
    transaction = models.ForeignKey(BankTransaction, null=True, blank=True, on_delete=models.PROTECT, related_name='collections')
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-pk']
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name='positive_collection')]


class Voucher(models.Model):
    """Recibo de caja en el sistema contable para un grupo de aplicaciones confirmadas en una misma operación."""
    STATUS = [('pending', 'Pendiente en el sistema contable'), ('accepted', 'Registrado en el sistema contable'), ('rejected', 'Rechazado por el sistema contable'),
              ('uncertain', 'Envío incierto'), ('review', 'Requiere revisión'), ('linked', 'Vinculado a recibo existente')]
    collection = models.ForeignKey(Collection, on_delete=models.PROTECT, related_name='vouchers')
    status = models.CharField(max_length=10, choices=STATUS, default='pending')
    contable_id = models.CharField(max_length=60, blank=True)
    contable_name = models.CharField(max_length=40, blank=True)
    detail = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)


class PaymentApplication(models.Model):
    PROPOSALS = [('cash_sale', 'Pago de contado de la factura'), ('instruction', 'Facturas indicadas por el cliente'),
                 ('oldest', 'Propuesta por antigüedad'), ('manual', 'Reparto manual')]
    collection = models.ForeignKey(Collection, on_delete=models.PROTECT, related_name='applications')
    receivable = models.ForeignKey(Receivable, on_delete=models.PROTECT, related_name='applications')
    amount = models.DecimalField(**MONEY)
    proposal = models.CharField(max_length=12, choices=PROPOSALS)
    voucher = models.ForeignKey(Voucher, null=True, blank=True, on_delete=models.PROTECT, related_name='applications')
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    reversed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversal_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['pk']
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name='positive_application')]

    @property
    def active(self):
        return self.reversed_at is None


# ------------------------------------------------------- descuentos de venta
# Regla: las ventas se registran a precio de lista; la administración concede descuentos con
# permisos mensuales que asigna la dirección (1 permiso = 1 venta con descuento en el mes). Sin permisos, el
# descuento espera la confirmación de la dirección. La dirección concede descuentos sin consumir permisos.

class DiscountGrant(models.Model):
    """Permisos adicionales del mes concedidos por la dirección a una cuenta de administración."""
    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='discount_grants')
    month = models.DateField('mes')
    quantity = models.PositiveSmallIntegerField('permisos adicionales')
    reason = models.CharField('motivo', max_length=300, blank=True)
    granted_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name='positive_discount_grant')]


class DiscountRequest(models.Model):
    class Kind(models.TextChoices):
        SALE = 'sale', 'Descuento fuera del cupo'
        QUOTA = 'quota', 'Más permisos del mes'

    class Status(models.TextChoices):
        PENDING = 'pending', 'En espera de la dirección'
        APPROVED = 'approved', 'Aprobada'
        REJECTED = 'rejected', 'Rechazada'
        WITHDRAWN = 'withdrawn', 'Retirada'
        VOID = 'void', 'Sin efecto'

    kind = models.CharField(max_length=6, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='discount_requests')
    month = models.DateField('mes')
    sale = models.ForeignKey(Sale, null=True, blank=True, on_delete=models.PROTECT, related_name='discount_requests')
    lines = models.JSONField('descuento propuesto por línea', default=list, blank=True)
    amount = models.DecimalField('valor del descuento', **MONEY, default=0)
    gross = models.DecimalField('valor antes del descuento', **MONEY, default=0)
    quantity = models.PositiveSmallIntegerField('permisos solicitados', default=0)
    reason = models.CharField('motivo', max_length=300)
    decided_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField('nota de la decisión', max_length=300, blank=True)
    granted = models.PositiveSmallIntegerField('permisos concedidos', default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['sale'], condition=Q(status='pending'), name='one_pending_discount_per_sale')]

    @property
    def percent(self):
        return (self.amount * 100 / self.gross).quantize(Decimal('0.1')) if self.gross else Decimal(0)


class SaleDiscount(models.Model):
    """Descuento concedido sobre una venta, con quién lo concedió y si consumió un permiso del mes."""
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Vigente'
        REPLACED = 'replaced', 'Reemplazado'
        REMOVED = 'removed', 'Retirado'

    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name='discounts')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    applied_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='sale_discounts')
    approved_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    request = models.ForeignKey(DiscountRequest, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    month = models.DateField('mes')
    consumed = models.BooleanField('consumió un permiso', default=False)
    lines = models.JSONField(default=list)
    amount = models.DecimalField(**MONEY)
    gross = models.DecimalField(**MONEY)
    reason = models.CharField('motivo', max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['sale'], condition=Q(status='active'), name='one_active_discount_per_sale'),
                       models.CheckConstraint(condition=Q(amount__gt=0), name='positive_sale_discount')]

    @property
    def percent(self):
        return (self.amount * 100 / self.gross).quantize(Decimal('0.1')) if self.gross else Decimal(0)
