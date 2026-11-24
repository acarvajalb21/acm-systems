import hashlib
import re
import uuid
from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F, Q, Sum
from django.urls import reverse
from django.utils import timezone
from .models import (Product, Batch, SupplyRequest, Reservation, Supplier, DiscountTier, SupplierCode, Invoice, InvoiceLine,
                     InvoiceFile, Receipt, Movement, Operation, Audit, AlertRead, Settings, SupplierPayment, PaymentAllocation, money, nit_check_digit,
                     with_payable_balance)

# Jerarquía: la dirección tiene todos los accesos; la administración opera todo salvo lo reservado a la dirección.
OWNER = ('owner',)
ADMINS = ('admin', 'owner')
REQUESTERS = ('requester', 'owner')
MANAGERS = ('inventory', 'admin', 'owner')
FINANCE = ('finance', 'admin', 'owner')
DOCUMENTS = ('inventory', 'finance', 'admin', 'owner')
SALES = ('billing', 'finance', 'admin', 'owner')
RECEIVERS = ('inventory', 'reception', 'admin', 'owner')   # reciben la mercancía de los proveedores
EVERYONE = ('requester', 'inventory', 'finance', 'billing', 'reception', 'admin', 'owner')
OUTFLOWS = ('exit', 'dispatch')
MAX_FILE = 20 * 1024 * 1024
FILE_TYPES = {'.pdf': ('application/pdf', (b'%PDF',)), '.xml': ('application/xml', (b'<',)), '.zip': ('application/zip', (b'PK\x03\x04',)),
              '.png': ('image/png', (b'\x89PNG',)), '.jpg': ('image/jpeg', (b'\xff\xd8\xff',)), '.jpeg': ('image/jpeg', (b'\xff\xd8\xff',))}


def require(user, roles):
    if not user.is_authenticated or not user.is_active or user.role not in roles:
        raise PermissionDenied('Su cuenta no tiene permiso para esta operación.')


def event(user, name, target='', detail=''):
    Audit.objects.create(actor=user, event=name, target=str(target)[:100], detail=(detail or '')[:300])


def operation(user, token, kind):
    # Se llama dentro de la transacción de negocio: si la operación falla, el token también se revierte.
    _, created = Operation.objects.get_or_create(token=token, defaults={'actor': user, 'kind': kind})
    if not created:
        raise ValidationError('Esta operación ya se registró. Revise el resultado antes de repetirla.')


def normalize_nit(value):
    raw = (value or '').strip()
    digits = re.sub(r'\D', '', raw.split('-')[0])
    if '-' not in raw and len(digits) == 10 and digits[0] in '89' and nit_check_digit(digits[:9]) == digits[9]:
        digits = digits[:9]  # NIT empresarial escrito con su dígito de verificación pero sin guion
    if not 6 <= len(digits) <= 12:
        raise ValidationError('Escriba un NIT válido, con o sin dígito de verificación.')
    if '-' in raw:
        dv = re.sub(r'\D', '', raw.split('-', 1)[1])
        if dv and dv != nit_check_digit(digits):
            raise ValidationError(f'El dígito de verificación no corresponde. Para {digits} es {nit_check_digit(digits)}.')
    return digits


def pct(value):
    return format(Decimal(value).quantize(Decimal('0.01')), 'f').rstrip('0').rstrip('.').replace('.', ',')


def cop(value):
    return '$' + f'{Decimal(value):,.0f}'.replace(',', '.')


# ---------------------------------------------------------------- inventario

def availability(active_only=True):
    today = timezone.localdate()
    totals = {r['product_id']: r['units'] for r in Batch.objects.filter(expiry__gte=today).values('product_id').annotate(units=Sum(F('physical') - F('reserved')))}
    products = list(Product.objects.filter(active=True) if active_only else Product.objects.all())
    for p in products:
        p.available = totals.get(p.pk, 0) or 0
    return products


def stock_position():
    """Posición por referencia con los datos registrados en la plataforma; la sugerencia de compra vive en reposición."""
    today = timezone.localdate()
    usable = Q(expiry__gte=today)
    lots = {r['product_id']: r for r in Batch.objects.values('product_id').annotate(
        usable_units=Sum('physical', filter=usable), reserved_units=Sum('reserved', filter=usable), expired_units=Sum('physical', filter=~usable))}
    transit = {r['product_id']: r['units'] for r in InvoiceLine.objects.exclude(invoice__review=Invoice.Review.VOID)
               .values('product_id').annotate(units=Sum(F('quantity') - F('received')))}
    since = timezone.now() - timedelta(days=90)
    used = {r['batch__product_id']: -r['units'] for r in Movement.objects.filter(kind__in=OUTFLOWS, created_at__gte=since)
            .values('batch__product_id').annotate(units=Sum('quantity'))}
    rows = []
    for p in Product.objects.filter(active=True):
        lot = lots.get(p.pk, {})
        p.physical, p.reserved, p.expired_units = lot.get('usable_units') or 0, lot.get('reserved_units') or 0, lot.get('expired_units') or 0
        p.available = p.physical - p.reserved
        p.in_transit = transit.get(p.pk, 0) or 0
        p.position = p.available + p.in_transit
        p.used_90 = used.get(p.pk, 0) or 0
        p.coverage = round(p.available / (p.used_90 / 90)) if p.used_90 else None
        p.state = 'out' if p.available == 0 else 'low' if p.minimum is not None and p.available <= p.minimum else 'ok'
        rows.append(p)
    return rows


def _batch_for_entry(product, lot, expiry, location):
    lot, location = lot.strip().upper(), location.strip()
    if not lot or not location:
        raise ValidationError('Lote y ubicación son obligatorios.')
    if Batch.objects.filter(product=product, lot__iexact=lot).exclude(expiry=expiry).exists():
        raise ValidationError(f'El lote {lot} de {product.name} ya está registrado con otra fecha de vencimiento.')
    batch, _ = Batch.objects.get_or_create(product=product, lot=lot, location=location, defaults={'expiry': expiry})
    return batch


@transaction.atomic
def adjust_in(user, product, lot, expiry, location, quantity, kind, note, token):
    require(user, MANAGERS)
    operation(user, token, kind)
    if kind not in ('initial', 'adjust_in') or quantity < 1:
        raise ValidationError('Tipo de ajuste o cantidad inválidos.')
    if kind == 'adjust_in' and not note.strip():
        raise ValidationError('Explique el motivo del ajuste.')
    Product.objects.select_for_update().get(pk=product.pk)
    batch = _batch_for_entry(product, lot, expiry, location)
    Batch.objects.filter(pk=batch.pk).update(physical=F('physical') + quantity)
    m = Movement.objects.create(batch=batch, quantity=quantity, kind=kind, note=note.strip(), actor=user)
    event(user, m.get_kind_display(), batch.pk, f'{product.name} · lote {batch.lot} · {product.qty(quantity)}')
    return m


@transaction.atomic
def adjust_out(user, batch_id, quantity, kind, loss_reason, note, token):
    require(user, MANAGERS)
    operation(user, token, kind)
    b = Batch.objects.select_for_update().select_related('product').get(pk=batch_id)
    if kind not in ('adjust_out', 'writeoff') or quantity < 1 or not note.strip():
        raise ValidationError('Indique tipo, cantidad y justificación.')
    if kind == 'writeoff' and not loss_reason:
        raise ValidationError('Seleccione la causa de la baja.')
    if not Batch.objects.filter(pk=b.pk, physical__gte=F('reserved') + quantity).update(physical=F('physical') - quantity):
        raise ValidationError('La cantidad supera las unidades libres del lote. Libere primero las reservas activas.')
    m = Movement.objects.create(batch=b, quantity=-quantity, kind=kind, loss_reason=loss_reason if kind == 'writeoff' else '', note=note.strip(), actor=user)
    event(user, m.get_kind_display(), b.pk, f'{b.product.name} · lote {b.lot} · {quantity} · {note}')
    return m


@transaction.atomic
def exit_stock(user, batch_id, quantity, reference, note, token):
    require(user, MANAGERS)
    operation(user, token, 'exit')
    b = Batch.objects.select_for_update().select_related('product').get(pk=batch_id)
    if b.expired:
        raise ValidationError('Lote vencido: registre una baja en lugar de una salida.')
    if quantity < 1 or not (reference.strip() or note.strip()):
        raise ValidationError('Indique la cantidad y el documento soporte o destino.')
    if not Batch.objects.filter(pk=b.pk, physical__gte=F('reserved') + quantity).update(physical=F('physical') - quantity):
        raise ValidationError('La cantidad supera las unidades disponibles del lote.')
    m = Movement.objects.create(batch=b, quantity=-quantity, kind='exit', reference=reference.strip(), note=note.strip(), actor=user)
    event(user, 'Salida registrada', b.pk, f'{b.product.name} · {quantity} · {reference or note}')
    return m


# ------------------------------------------------------- solicitudes y reservas

@transaction.atomic
def request_stock(user, product, quantity, purpose, token, needed_by=None):
    require(user, REQUESTERS)
    operation(user, token, 'request')
    if quantity < 1 or not purpose.strip():
        raise ValidationError('Indique la cantidad y el destino.')
    if needed_by and needed_by < timezone.localdate():
        raise ValidationError('La fecha requerida no puede estar en el pasado.')
    r = SupplyRequest.objects.create(requester=user, product=product, quantity=quantity, purpose=purpose.strip(), needed_by=needed_by)
    event(user, 'Solicitud creada', r.pk, f'{product.name} · {quantity}')
    return r


def suggested_batch(product, quantity):
    # Primero vence, primero sale: lote vigente más próximo a vencer que cubra la cantidad.
    for b in Batch.objects.filter(product=product, expiry__gte=timezone.localdate()).order_by('expiry', 'pk'):
        if b.available >= quantity:
            return b
    return None


@transaction.atomic
def approve(user, request_id, batch_id, token):
    require(user, MANAGERS)
    operation(user, token, 'approve')
    r = SupplyRequest.objects.select_for_update().get(pk=request_id)
    if r.status != 'pending':
        raise ValidationError('La solicitud ya no está pendiente.')
    b = Batch.objects.select_for_update().get(pk=batch_id)
    if b.product_id != r.product_id or b.expired:
        raise ValidationError('Seleccione un lote vigente del producto solicitado.')
    if not Batch.objects.filter(pk=b.pk, physical__gte=F('reserved') + r.quantity).update(reserved=F('reserved') + r.quantity):
        raise ValidationError('Disponibilidad insuficiente en el lote; otra reserva pudo consumirla.')
    res = Reservation.objects.create(request=r, batch=b, quantity=r.quantity, purpose=r.purpose, created_by=user)
    r.status = 'approved'
    r.save(update_fields=['status', 'updated_at'])
    event(user, 'Solicitud aprobada', r.pk, f'Lote {b.lot}')
    return res


@transaction.atomic
def resolve_request(user, request_id, action, reason, token):
    operation(user, token, action)
    r = SupplyRequest.objects.select_for_update().get(pk=request_id)
    if action == 'cancel':
        require(user, REQUESTERS)
        if r.requester_id != user.pk:
            raise PermissionDenied
        status = 'cancelled'
    elif action == 'reject':
        require(user, MANAGERS)
        if not reason.strip():
            raise ValidationError('Explique el motivo del rechazo.')
        status = 'rejected'
    else:
        raise ValidationError('Acción inválida.')
    if r.status != 'pending':
        raise ValidationError('La solicitud ya no está pendiente.')
    r.status, r.resolution = status, reason.strip()
    r.save(update_fields=['status', 'resolution', 'updated_at'])
    event(user, 'Solicitud ' + r.get_status_display().lower(), r.pk, reason)


@transaction.atomic
def reserve(user, batch_id, quantity, purpose, token):
    require(user, MANAGERS)
    operation(user, token, 'reserve')
    b = Batch.objects.select_for_update().get(pk=batch_id)
    if quantity < 1 or not purpose.strip() or b.expired:
        raise ValidationError('Reserva inválida o lote vencido.')
    if not Batch.objects.filter(pk=b.pk, physical__gte=F('reserved') + quantity).update(reserved=F('reserved') + quantity):
        raise ValidationError('Disponibilidad insuficiente.')
    r = Reservation.objects.create(batch=b, quantity=quantity, purpose=purpose.strip(), created_by=user)
    event(user, 'Reserva creada', r.pk, purpose)
    return r


@transaction.atomic
def finish_reservation(user, reservation_id, action, token):
    require(user, MANAGERS)
    operation(user, token, action)
    r = Reservation.objects.select_for_update().get(pk=reservation_id)
    if r.status != 'active':
        raise ValidationError('La reserva ya está cerrada.')
    b = Batch.objects.select_for_update().get(pk=r.batch_id)
    if action == 'dispatch':
        if b.expired:
            raise ValidationError('Lote vencido: entrega bloqueada.')
        if not Batch.objects.filter(pk=b.pk, reserved__gte=r.quantity, physical__gte=r.quantity).update(physical=F('physical') - r.quantity, reserved=F('reserved') - r.quantity):
            raise ValidationError('Saldo insuficiente para entregar.')
        Movement.objects.create(batch=b, quantity=-r.quantity, kind='dispatch', note=r.purpose, reservation=r, actor=user)
        r.status = 'delivered'
    elif action == 'release':
        if not Batch.objects.filter(pk=b.pk, reserved__gte=r.quantity).update(reserved=F('reserved') - r.quantity):
            raise ValidationError('Reserva inconsistente; revise saldos.')
        r.status = 'released'
    else:
        raise ValidationError('Acción inválida.')
    r.closed_at = timezone.now()
    r.save(update_fields=['status', 'closed_at'])
    if r.request_id:
        req = SupplyRequest.objects.select_for_update().get(pk=r.request_id)
        req.status = r.status
        req.save(update_fields=['status', 'updated_at'])
    event(user, 'Reserva ' + r.get_status_display().lower(), r.pk)


def visible_requests(user):
    require(user, (*REQUESTERS, *MANAGERS))
    qs = SupplyRequest.objects.select_related('requester', 'product', 'reservation__batch').order_by('-created_at')
    return qs.filter(requester=user) if user.role == 'requester' else qs


# ------------------------------------------------------------- proveedores

@transaction.atomic
def save_supplier(user, supplier, data, tiers, token):
    require(user, FINANCE + ('inventory',) if supplier is None else FINANCE)
    operation(user, token, 'supplier')
    nit = normalize_nit(data['nit'])
    if Supplier.objects.filter(nit=nit).exclude(pk=getattr(supplier, 'pk', None)).exists():
        raise ValidationError('Ya existe un proveedor con este NIT.')
    days = [t['days'] for t in tiers]
    if len(days) != len(set(days)):
        raise ValidationError('Cada tramo de descuento debe tener un plazo distinto.')
    if any(d > data['payment_days'] for d in days) and data['payment_days']:
        raise ValidationError('Los tramos de descuento no pueden superar el plazo de pago.')
    created = supplier is None
    supplier = supplier or Supplier()
    for field in ('name', 'email', 'phone', 'payment_days', 'discount_base', 'active'):
        setattr(supplier, field, data[field].strip() if isinstance(data[field], str) else data[field])
    supplier.nit = nit
    supplier.save()
    supplier.tiers.all().delete()
    DiscountTier.objects.bulk_create([DiscountTier(supplier=supplier, days=t['days'], percent=t['percent']) for t in tiers])
    event(user, 'Proveedor creado' if created else 'Condiciones de proveedor actualizadas', supplier.pk, supplier.name)
    return supplier


@transaction.atomic
def supplier_from_import(user, nit, name):
    require(user, DOCUMENTS)
    nit = normalize_nit(nit)
    supplier = Supplier.objects.filter(nit=nit).first()
    if supplier:
        return supplier
    if not name.strip():
        raise ValidationError('El documento no trae la razón social del proveedor.')
    supplier = Supplier.objects.create(name=name.strip()[:160], nit=nit)
    event(user, 'Proveedor creado desde factura electrónica', supplier.pk, supplier.name)
    return supplier


# ---------------------------------------------------------------- facturas

def _store(invoice, user, name, raw, saved):
    ext = ('.' + name.rsplit('.', 1)[-1].lower()) if '.' in name else ''
    if ext not in FILE_TYPES:
        raise ValidationError(f'{name}: formato no admitido. Use PDF, XML, ZIP, PNG o JPG.')
    content_type, magic = FILE_TYPES[ext]
    head = raw.lstrip(b'\xef\xbb\xbf \r\n\t')[:8] if ext == '.xml' else raw[:8]
    if not raw or len(raw) > MAX_FILE or not head.startswith(magic):
        raise ValidationError(f'{name}: el archivo está vacío, supera 20 MB o su contenido no corresponde a la extensión.')
    digest = hashlib.sha256(raw).hexdigest()
    if invoice.files.filter(sha256=digest).exists():
        return None
    f = InvoiceFile(invoice=invoice, name=name[:200], content_type=content_type, size=len(raw), sha256=digest, uploaded_by=user)
    f.file.save(uuid.uuid4().hex + ext, ContentFile(raw), save=False)
    saved.append(f.file.name)
    f.save()
    return f


def _discard(saved):
    storage = InvoiceFile._meta.get_field('file').storage
    for name in saved:
        storage.delete(name)


def _check_header(header, lines):
    today = timezone.localdate()
    if header['issue_date'] > today:
        raise ValidationError('La fecha de emisión no puede estar en el futuro.')
    if header.get('due_date') and header['due_date'] < header['issue_date']:
        raise ValidationError('La fecha de vencimiento no puede ser anterior a la emisión.')
    if header.get('ordered_on') and header['ordered_on'] > header['issue_date']:
        raise ValidationError('La fecha del pedido no puede ser posterior a la factura.')
    if not lines:
        raise ValidationError('Agregue al menos un producto a la factura.')
    for line in lines:
        if line['quantity'] < 1 or line['discount'] > money(line['quantity'] * line['unit_price']):
            raise ValidationError(f'Revise cantidad y descuento de {line["product"].name}.')


def _write_lines(invoice, supplier, lines):
    invoice.lines.all().delete()
    for line in lines:
        product = line['product']
        InvoiceLine.objects.create(invoice=invoice, product=product, supplier_code=line.get('supplier_code', '').strip(),
                                   description=(line.get('description') or product.name).strip()[:200], quantity=line['quantity'],
                                   unit_price=line['unit_price'], discount=line.get('discount') or 0, tax_rate=line.get('tax_rate') or 0,
                                   expected_lot=(line.get('expected_lot') or '').strip().upper(), expected_expiry=line.get('expected_expiry'))
        if line.get('supplier_code', '').strip():
            SupplierCode.objects.update_or_create(supplier=supplier, code=line['supplier_code'].strip(), defaults={'product': product})
    invoice.recalculate()
    if invoice.total < 0:
        raise ValidationError('El total de la factura no puede ser negativo.')
    invoice.save()


def _apply_header(invoice, header):
    supplier = header['supplier']
    invoice.supplier = supplier
    invoice.number = header['number'].strip().upper()
    invoice.issue_date = header['issue_date']
    invoice.due_date = header.get('due_date') or header['issue_date'] + timedelta(days=supplier.payment_days)
    invoice.ordered_on = header.get('ordered_on')
    invoice.order_number = (header.get('order_number') or '').strip()
    invoice.cufe = (header.get('cufe') or '').strip()
    invoice.global_discount = header.get('global_discount') or 0
    invoice.retention = header.get('retention') or 0
    invoice.notes = (header.get('notes') or '').strip()
    if 'document_terms' in header:
        invoice.document_terms = header['document_terms']


def _duplicates(supplier, number, cufe, exclude=None):
    qs = Invoice.objects.exclude(pk=exclude)
    if qs.filter(supplier=supplier, number__iexact=number.strip()).exists():
        raise ValidationError(f'Ya existe la factura {number} de {supplier.name}.')
    if cufe and qs.filter(cufe__iexact=cufe.strip()).exists():
        raise ValidationError('Ya existe una factura registrada con este CUFE.')


def create_invoice(user, header, lines, uploads, token, source='manual'):
    """uploads: lista de (nombre, bytes). Los archivos se eliminan si la transacción falla."""
    require(user, DOCUMENTS)
    saved = []
    try:
        with transaction.atomic():
            operation(user, token, 'invoice')
            supplier = header['supplier']
            _duplicates(supplier, header['number'], header.get('cufe', ''))
            _check_header(header, lines)
            if header.get('currency', 'COP') != 'COP':
                raise ValidationError('Por ahora el sistema registra facturas en pesos colombianos (COP).')
            invoice = Invoice(source=source, created_by=user)
            _apply_header(invoice, header)
            invoice.save()
            _write_lines(invoice, supplier, lines)
            for name, raw in uploads:
                _store(invoice, user, name, raw, saved)
            from .compras import link_invoice
            link_invoice(invoice)
            event(user, 'Factura registrada', invoice.pk, f'{invoice.number} · {supplier.name}')
            return invoice
    except Exception:
        _discard(saved)
        raise


def update_invoice(user, invoice_id, header, lines, uploads, token):
    require(user, DOCUMENTS)
    saved = []
    try:
        with transaction.atomic():
            operation(user, token, 'invoice_edit')
            invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
            if invoice.review != Invoice.Review.PENDING:
                raise ValidationError('Solo se modifican facturas que siguen por revisar.')
            supplier = header['supplier']
            _duplicates(supplier, header['number'], header.get('cufe', ''), exclude=invoice.pk)
            _check_header(header, lines)
            _apply_header(invoice, header)
            _write_lines(invoice, supplier, lines)
            for name, raw in uploads:
                _store(invoice, user, name, raw, saved)
            from .compras import link_invoice
            link_invoice(invoice)
            event(user, 'Factura corregida', invoice.pk, invoice.number)
            return invoice
    except Exception:
        _discard(saved)
        raise


def attach_files(user, invoice_id, uploads, token):
    require(user, DOCUMENTS)
    saved = []
    try:
        with transaction.atomic():
            operation(user, token, 'attach')
            invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
            added = [f for f in (_store(invoice, user, n, raw, saved) for n, raw in uploads) if f]
            if not added:
                raise ValidationError('Los archivos ya estaban adjuntos a esta factura.')
            event(user, 'Soporte adjuntado', invoice.pk, ', '.join(f.name for f in added))
    except Exception:
        _discard(saved)
        raise


@transaction.atomic
def validate_invoice(user, invoice_id, token):
    require(user, DOCUMENTS)
    operation(user, token, 'validate')
    invoice = Invoice.objects.select_for_update().select_related('supplier').get(pk=invoice_id)
    if invoice.review != Invoice.Review.PENDING:
        raise ValidationError('La factura ya fue validada o anulada.')
    if not invoice.lines.exists():
        raise ValidationError('La factura no tiene productos.')
    invoice.review = Invoice.Review.VALIDATED
    invoice.terms = invoice.document_terms if invoice.document_terms is not None else invoice.supplier.terms()
    invoice.validated_by, invoice.validated_at = user, timezone.now()
    invoice.save(update_fields=['review', 'terms', 'validated_by', 'validated_at'])
    event(user, 'Factura validada', invoice.pk, invoice.number)


@transaction.atomic
def void_invoice(user, invoice_id, reason, token):
    require(user, DOCUMENTS)
    operation(user, token, 'void')
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
    if invoice.review == Invoice.Review.VOID:
        raise ValidationError('La factura ya está anulada.')
    if (invoice.receipts.exists() or invoice.allocations.exclude(payment__status=SupplierPayment.Status.CANCELLED).exists()
            or invoice.credit_applications.exists() or invoice.adjustments.exists()):
        raise ValidationError('La factura tiene recepciones, pagos, notas crédito o ajustes registrados; no se puede anular.')
    if not reason.strip():
        raise ValidationError('Explique el motivo de la anulación.')
    invoice.review, invoice.void_reason = Invoice.Review.VOID, reason.strip()
    invoice.save(update_fields=['review', 'void_reason'])
    # El pedido abierto que citaba esta factura queda libre para vincularse a la factura corregida.
    from .models import PurchaseOrder
    PurchaseOrder.objects.filter(invoice=invoice, status__in=PurchaseOrder.OPEN).update(invoice=None)
    event(user, 'Factura anulada', invoice.pk, reason)


@transaction.atomic
def receive_invoice(user, invoice_id, rows, received_on, note, token):
    require(user, RECEIVERS)
    operation(user, token, 'receive')
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
    if invoice.review != Invoice.Review.VALIDATED:
        raise ValidationError('Valide la factura antes de recibir la mercancía.')
    today = timezone.localdate()
    if received_on > today:
        raise ValidationError('La fecha de recepción no puede estar en el futuro.')
    lines = {l.pk: l for l in InvoiceLine.objects.select_for_update().select_related('product').filter(invoice=invoice)}
    if not rows:
        raise ValidationError('Registre al menos un lote recibido.')
    totals, different = {}, False
    for row in rows:
        line = lines.get(row['line'])
        if line is None or row['quantity'] < 1:
            raise ValidationError('Cada lote debe corresponder a un producto de la factura con cantidad positiva.')
        if row['expiry'] < today:
            raise ValidationError(f'El lote {row["lot"]} está vencido. No se reciben unidades vencidas.')
        totals[line.pk] = totals.get(line.pk, 0) + row['quantity']
        if (line.expected_lot and row['lot'].strip().upper() != line.expected_lot) or (line.expected_expiry and row['expiry'] != line.expected_expiry):
            different = True
    for pk, qty in totals.items():
        if qty > lines[pk].pending:
            raise ValidationError(f'{lines[pk].product.name}: llegan {qty} y faltan {lines[pk].pending}. Los excedentes no se reciben sin nota crédito o nueva factura.')
    partial = any(totals.get(pk, 0) < l.pending for pk, l in lines.items() if l.pending)
    if (partial or different) and not note.strip():
        raise ValidationError('Explique la recepción parcial o las diferencias de lote y vencimiento.')
    seen = {}
    for row in rows:
        key = (lines[row['line']].product_id, row['lot'].strip().upper())
        if key in seen and seen[key] != row['expiry']:
            raise ValidationError(f'El lote {row["lot"]} aparece con dos vencimientos distintos.')
        seen[key] = row['expiry']
    receipt = Receipt.objects.create(invoice=invoice, received_on=received_on, note=note.strip(), actor=user)
    for row in rows:
        line = lines[row['line']]
        batch = _batch_for_entry(line.product, row['lot'], row['expiry'], row['location'])
        Batch.objects.filter(pk=batch.pk).update(physical=F('physical') + row['quantity'])
        Movement.objects.create(batch=batch, quantity=row['quantity'], kind='receipt', reference=invoice.number, note=note.strip(),
                                invoice_line=line, receipt=receipt, actor=user)
    for pk, qty in totals.items():
        InvoiceLine.objects.filter(pk=pk).update(received=F('received') + qty)
    if not InvoiceLine.objects.filter(invoice=invoice, received__lt=F('quantity')).exists():
        from .compras import close_with_invoice
        close_with_invoice(invoice, user)
    event(user, 'Recepción confirmada', invoice.pk, f'{invoice.number} · {sum(totals.values())} unidades')
    return receipt


def discount_for(invoice, pay_date):
    """Descuento por pronto pago con las condiciones congeladas al validar (días calendario desde la emisión)."""
    terms = invoice.terms or {'tiers': [], 'base': 'subtotal'}
    base = invoice.total if terms.get('base') == 'total' else invoice.subtotal - invoice.global_discount
    elapsed = (pay_date - invoice.issue_date).days
    tiers = sorted((int(d), Decimal(p)) for d, p in terms.get('tiers', []))
    percent = next((p for d, p in tiers if elapsed <= d), Decimal(0))
    best = max((p for _, p in tiers), default=Decimal(0))
    amount, potential = money(base * percent / 100), money(base * best / 100)
    deadline = next((invoice.issue_date + timedelta(days=d) for d, p in tiers if elapsed <= d), None)
    return {'elapsed': elapsed, 'percent': percent, 'base': money(base), 'amount': amount, 'potential': potential,
            'lost': potential - amount, 'due': money(invoice.total - amount), 'deadline': deadline, 'tiers': tiers}


def pay_invoice(user, invoice_id, paid_on, method, reference, token):
    """Pago completo de una factura con el descuento que corresponde a la fecha (atajo del registro de pagos)."""
    from .payables import register_payment, settlement
    require(user, FINANCE)
    invoice = with_payable_balance(Invoice.objects.filter(pk=invoice_id).select_related('supplier')).get()
    if invoice.review != Invoice.Review.VALIDATED:
        raise ValidationError('Solo se pagan facturas validadas.')
    if invoice.balance <= 0:
        raise ValidationError('Esta factura ya tiene un pago registrado que la salda.')
    if paid_on > timezone.localdate() or paid_on < invoice.issue_date:
        raise ValidationError('La fecha de pago debe estar entre la emisión de la factura y hoy.')
    s = settlement(invoice, paid_on)
    return register_payment(user, invoice.supplier, [{'invoice': invoice, 'amount': s['cash'], 'discount': s['discount']}], paid_on, method,
                            reference or f'Pago de la factura {invoice.number}', [], '', token)


def payables(supplier=None):
    from .payables import settlement
    today = timezone.localdate()
    qs = with_payable_balance(Invoice.objects.filter(review=Invoice.Review.VALIDATED).select_related('supplier')).filter(balance_due__gt=0)
    if supplier is not None:
        qs = qs.filter(supplier=supplier)
    rows = list(qs.order_by('due_date', 'pk'))
    scheduled = dict(PaymentAllocation.objects.filter(payment__status=SupplierPayment.Status.SCHEDULED).values_list('invoice_id', 'payment__scheduled_on'))
    for inv in rows:
        inv.offer = settlement(inv, today)
        inv.days_to_due = (inv.due_date - today).days
        inv.scheduled_on = scheduled.get(inv.pk)
    return rows


# ------------------------------------------------------------------ alertas

def alerts(user):
    require(user, EVERYONE)
    items = []

    def add(key, level, title, detail, url, revision=''):
        fingerprint = hashlib.sha256((detail + revision).encode()).hexdigest()
        items.append({'key': key, 'level': level, 'title': title, 'detail': detail, 'url': url, 'revision': fingerprint})

    today = timezone.localdate()
    cfg = Settings.current()
    if user.role == 'requester':
        for r in visible_requests(user).exclude(status='pending').filter(updated_at__gte=timezone.now() - timedelta(days=14)):
            add(f'request-{r.pk}', 'info', 'Solicitud ' + r.get_status_display().lower(),
                f'{r.product.name} · {r.product.qty(r.quantity)}. {r.resolution}'.strip(), reverse('requests'), str(r.updated_at))
        for r in Reservation.objects.select_related('batch__product').filter(request__requester=user, status='active', batch__expiry__lt=today):
            add(f'blocked-{r.pk}', 'critical', 'Entrega bloqueada por vencimiento', f'{r.batch.product.name} · {r.quantity} unidades. Inventario asignará otro lote.', reverse('requests'))
    if user.role in MANAGERS:
        for b in Batch.objects.select_related('product').filter(physical__gt=0, expiry__lte=today + timedelta(days=cfg.expiry_days)):
            remaining = b.days_left
            add(f'expiry-{b.pk}', 'critical' if remaining < 0 else 'warning', 'Lote vencido: registrar baja' if remaining < 0 else f'Vence en {remaining} días',
                f'{b.product.name} · lote {b.lot} · {b.location} · {b.physical} unidades · vence {b.expiry:%d/%m/%Y}', reverse('trace', args=[b.pk]))
        for p in availability():
            if p.available == 0 or (p.minimum is not None and p.available <= p.minimum):
                add(f'stock-{p.pk}', 'critical' if p.available == 0 else 'warning', 'Referencia agotada' if p.available == 0 else 'Disponible en el mínimo o por debajo',
                    f'{p.name} · disponible {p.available} · mínimo {p.minimum if p.minimum is not None else "sin definir"}', reverse('replenishment'))
        for r in visible_requests(user).filter(status='pending'):
            add(f'pending-{r.pk}', 'warning', 'Solicitud por atender', f'{r.requester.display_name} · {r.product.name} · {r.product.qty(r.quantity)}', reverse('approve', args=[r.pk]))
        for inv in Invoice.objects.filter(review=Invoice.Review.VALIDATED, lines__received__lt=F('lines__quantity')).select_related('supplier').distinct():
            add(f'receipt-{inv.pk}', 'info', 'Mercancía por recibir', f'{inv.supplier.name} · factura {inv.number} · {inv.expected_units - inv.received_units} unidades pendientes', reverse('invoice_receive', args=[inv.pk]))
    if user.role == 'reception':
        for inv in Invoice.objects.filter(review=Invoice.Review.VALIDATED, reception_required=True, lines__received__lt=F('lines__quantity')).select_related('supplier').distinct():
            add(f'receipt-{inv.pk}', 'info', 'Mercancía por recibir', f'{inv.supplier.name} · factura {inv.number} · {inv.expected_units - inv.received_units} unidades pendientes',
                reverse('reception_invoice', args=[inv.pk]))
    if user.role in DOCUMENTS:
        for inv in Invoice.objects.filter(review=Invoice.Review.PENDING).select_related('supplier'):
            add(f'review-{inv.pk}', 'info', 'Factura por revisar', f'{inv.supplier.name} · {inv.number} · emitida {inv.issue_date:%d/%m/%Y}', reverse('invoice', args=[inv.pk]))
    if user.role in FINANCE:
        for inv in payables():
            offer = inv.offer
            if inv.days_to_due < 0:
                add(f'overdue-{inv.pk}', 'critical', 'Factura vencida', f'{inv.supplier.name} · {inv.number} · venció el {inv.due_date:%d/%m/%Y}', reverse('invoice_pay', args=[inv.pk]))
            elif inv.days_to_due <= cfg.due_alert_days:
                add(f'due-{inv.pk}', 'warning', f'Vence en {inv.days_to_due} días', f'{inv.supplier.name} · {inv.number} · vence el {inv.due_date:%d/%m/%Y}', reverse('invoice_pay', args=[inv.pk]))
            if offer['deadline'] and offer['amount'] and (offer['deadline'] - today).days <= cfg.discount_alert_days:
                add(f'discount-{inv.pk}-{offer["percent"]}', 'warning', f'Descuento de {pct(offer["percent"])} % hasta el {offer["deadline"]:%d/%m/%Y}',
                    f'{inv.supplier.name} · {inv.number} · ahorro de {cop(offer["amount"])} si se paga a tiempo', reverse('invoice_pay', args=[inv.pk]))
            if inv.scheduled_on and inv.scheduled_on < today:
                add(f'scheduled-{inv.pk}', 'warning', 'Pago programado sin confirmar', f'{inv.supplier.name} · {inv.number} · programado para el {inv.scheduled_on:%d/%m/%Y}',
                    reverse('payables') + '?vista=programados')
    if user.role in FINANCE:
        from cartera.services import alerts as cartera_alerts
        for key, level, title, detail, url in cartera_alerts(user):
            add(key, level, title, detail, url)
    from agentes.runner import alerts as agent_alerts
    for key, level, title, detail, url in agent_alerts(user):
        add(key, level, title, detail, url)
    read = dict(AlertRead.objects.filter(user=user).values_list('key', 'revision'))
    for a in items:
        a['read'] = read.get(a['key']) == a['revision']
    return sorted(items, key=lambda a: ({'critical': 0, 'warning': 1, 'info': 2}[a['level']], a['read'], a['key']))
