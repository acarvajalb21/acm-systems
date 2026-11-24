"""Reglas de ventas, cartera y banco. Cada operación es transaccional, auditada y verifica permisos en el servidor.

Reglas:
- Pago obligatorio: condición y forma de pago siempre; vencimiento en crédito; en contado, medio y referencia
  del recaudo (la referencia no se exige en efectivo).
- Precios: un precio distinto al de la lista exige motivo y solo lo emite contabilidad.
- Abono global: instrucción del cliente primero; si no hay, propuesta por antigüedad. Nada se aplica sin confirmación.
- Excedentes: quedan como saldo sin aplicar del movimiento para decisión de contabilidad.
"""
import csv
import hashlib
import io
import re
import unicodedata
from base64 import b64decode
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import combinations
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from django.utils.crypto import salted_hmac
from inventory.models import Batch, Reservation, Movement, money
from inventory.services import require, event, operation, normalize_nit, cop, FINANCE, SALES
from contable import services as contable
from contable.client import ConnectorError
from . import discounts
from contable.models import Submission
from .models import (Party, PartyBranch, PartyContact, PriceList, PriceItem, Sale, SaleLine, SaleStock, Receivable, ReceivableAdjustment,
                     BankAccount, StatementImport, Payer, PayerLink, BankTransaction, Collection, Voucher, PaymentApplication, Context,
                     with_balance, ZERO)

HIGH, MEDIUM, LOW = 3, 2, 1
LEVELS = {HIGH: 'Alta', MEDIUM: 'Media', LOW: 'Baja'}
MAX_STATEMENT = 10 * 1024 * 1024


def can_issue(user):
    return user.is_authenticated and user.is_active and (user.role in FINANCE or (user.role == 'billing' and user.can_issue_invoices))


def norm(text):
    text = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', text)).strip()


def sid(value):
    value = str(value)
    return int(value) if value.isdigit() else value


# ------------------------------------------------------------------ terceros

def clean_identification(id_type, raw):
    raw = (raw or '').strip()
    if id_type == '31':
        return normalize_nit(raw)
    if id_type in ('13', '12', '11'):
        digits = re.sub(r'\D', '', raw)
        if not 4 <= len(digits) <= 12:
            raise ValidationError('Escriba un número de documento válido (solo dígitos).')
        return digits
    value = re.sub(r'[^0-9A-Za-z]', '', raw).upper()
    if not 4 <= len(value) <= 20:
        raise ValidationError('Escriba un número de documento válido.')
    return value


@transaction.atomic
def save_party(user, party, data, token):
    require(user, SALES)
    operation(user, token, 'party')
    ident = clean_identification(data['id_type'], data['identification'])
    if Party.objects.filter(id_type=data['id_type'], identification=ident).exclude(pk=getattr(party, 'pk', None)).exists():
        raise ValidationError('Ya existe un tercero con ese tipo y número de identificación.')
    price_list = data.get('price_list')
    if price_list and not price_list.active:
        raise ValidationError('La lista de precios acordada no está activa.')
    created = party is None
    party = party or Party(created_by=user)
    for field in ('id_type', 'name', 'kind', 'default_context', 'price_list', 'credit_days', 'requires_po', 'active', 'notes'):
        value = data.get(field)
        setattr(party, field, value.strip() if isinstance(value, str) else value)
    party.identification = ident
    party.save()
    branch, _ = PartyBranch.objects.get_or_create(party=party, office=0, defaults={'name': 'Principal'})
    branch.einvoice_email = (data.get('einvoice_email') or '').strip()
    branch.address = (data.get('address') or '').strip()
    branch.city = (data.get('city') or '').strip()
    branch.save()
    event(user, 'Tercero creado' if created else 'Tercero actualizado', f'party-{party.pk}', party.name)
    return party


@transaction.atomic
def add_contact(user, party_id, data, token):
    require(user, SALES)
    operation(user, token, 'contact')
    party = Party.objects.get(pk=party_id)
    value = data['value'].strip()
    if data['channel'] == 'email' and '@' not in value:
        raise ValidationError('Escriba un correo válido.')
    if data.get('preferred'):
        party.contacts.update(preferred=False)
    c = PartyContact.objects.create(party=party, name=data['name'].strip(), role=data['role'], channel=data['channel'], value=value,
                                    preferred=bool(data.get('preferred')), notes=(data.get('notes') or '').strip())
    event(user, 'Contacto agregado', f'party-{party.pk}', f'{c.get_channel_display()} · {c.name}')
    return c


@transaction.atomic
def remove_contact(user, contact_id, token):
    require(user, SALES)
    operation(user, token, 'contact_remove')
    c = PartyContact.objects.select_related('party').get(pk=contact_id)
    event(user, 'Contacto eliminado', f'party-{c.party_id}', f'{c.get_channel_display()} · {c.name}')
    c.delete()


# ------------------------------------------------------------------- precios

def price_for(price_list, product, on=None):
    on = on or timezone.localdate()
    return PriceItem.objects.filter(price_list=price_list, product=product, valid_from__lte=on).order_by('-valid_from').first()


@transaction.atomic
def save_price(user, price_list, product, price, valid_from, token):
    require(user, FINANCE)
    operation(user, token, 'price')
    if price is None or price < 0:
        raise ValidationError('Indique un precio válido.')
    item = PriceItem.objects.filter(price_list=price_list, product=product, valid_from=valid_from).first()
    if item and SaleLine.objects.filter(price_item=item).exists():
        raise ValidationError('Ese precio ya se usó en ventas. Registre una nueva vigencia en lugar de modificarlo.')
    if item:
        item.price, item.created_by = price, user
        item.save(update_fields=['price', 'created_by'])
    else:
        item = PriceItem.objects.create(price_list=price_list, product=product, price=price, valid_from=valid_from, created_by=user)
    event(user, 'Precio registrado', f'price-{item.pk}', f'{price_list.name} · {product.name} · {cop(price)} desde {valid_from:%d/%m/%Y}')
    return item


# -------------------------------------------------------------------- ventas

def payment_options(kind='payment_fv'):
    return list(contable.catalog(kind))


def _payment(payment_type_id):
    item = contable.catalog('payment_fv').filter(contable_id=str(payment_type_id)).first()
    if item is None:
        raise ValidationError('La forma de pago no existe o no está activa en el sistema contable. Actualice los catálogos.')
    return item


def _unbilled_outflow(movement, product, quantity, exclude_line=None):
    if movement.kind not in ('exit', 'dispatch') or movement.batch.product_id != product.pk:
        raise ValidationError(f'La entrega elegida no corresponde a {product.name}.')
    if -movement.quantity != quantity:
        raise ValidationError(f'La entrega elegida tiene {-movement.quantity} unidades; la línea indica {quantity}.')
    used = SaleLine.objects.filter(prior_movement=movement).exclude(sale__status=Sale.Status.CANCELLED)
    if exclude_line:
        used = used.exclude(sale=exclude_line)
    if used.exists() or SaleStock.objects.filter(movement=movement).exists():
        raise ValidationError('Esa entrega ya está vinculada a otra venta.')


def _validate_header(sale, header):
    party = header['party']
    today = timezone.localdate()
    if not party.active:
        raise ValidationError('El tercero está inactivo.')
    price_list = header['price_list']
    if not price_list.active or (price_list.context != header['context'] and price_list.pk != party.price_list_id):
        raise ValidationError('La lista de precios no corresponde al contexto elegido ni a la lista acordada con el tercero.')
    if header['issue_date'] < today:
        raise ValidationError('Una factura electrónica no puede tener fecha anterior a hoy.')
    payment = _payment(header['payment_type_id'])
    with_due = bool(payment.extra.get('due_date'))
    if header['condition'] == 'credit':
        if not with_due:
            raise ValidationError('Para una venta a crédito elija una forma de pago del sistema contable que maneje vencimiento.')
        if not header.get('due_date') or header['due_date'] < header['issue_date']:
            raise ValidationError('Indique el vencimiento del crédito (igual o posterior a la fecha de la factura).')
    else:
        if with_due:
            raise ValidationError('Para una venta de contado elija una forma de pago sin vencimiento.')
        if not header.get('cash_method'):
            raise ValidationError('Indique el medio con el que se recibió el pago de contado.')
        if header['cash_method'] != 'cash' and not (header.get('cash_reference') or '').strip():
            raise ValidationError('Indique la referencia del recaudo (aprobación del datáfono o comprobante de la transferencia).')
    if header['context'] == Context.COMPANY and party.requires_po and not (header.get('purchase_order') or '').strip():
        raise ValidationError('Este cliente exige orden de compra en sus facturas.')
    if not contable.catalog('seller').filter(contable_id=str(header['seller_id'])).exists():
        raise ValidationError('El vendedor no existe o no está activo en el sistema contable.')
    return payment


@transaction.atomic
def save_sale(user, sale_id, header, lines, token):
    require(user, SALES)
    operation(user, token, 'sale')
    sale = Sale.objects.select_for_update().get(pk=sale_id) if sale_id else Sale(created_by=user)
    if sale.pk and not sale.editable:
        raise ValidationError('Solo se modifican borradores o ventas rechazadas por el sistema contable.')
    if sale.pk:
        _release(sale, user)
        discounts.on_sale_changed(user, sale, 'La venta se modificó: el descuento debe concederse de nuevo')
    payment = _validate_header(sale, header)
    party = header['party']
    if not lines:
        raise ValidationError('Agregue al menos una referencia.')
    sale.party, sale.branch = party, party.branches.get(office=0)
    for field in ('context', 'price_list', 'issue_date', 'seller_id', 'condition', 'purchase_order', 'observations', 'beneficiary_name',
                  'beneficiary_document', 'beneficiary_relation', 'internal_note'):
        value = header.get(field)
        setattr(sale, field, value.strip() if isinstance(value, str) else (value or '' if field != 'issue_date' else value))
    sale.payment_type_id, sale.payment_name = payment.contable_id, payment.name
    credit = header['condition'] == 'credit'
    sale.due_date = header['due_date'] if credit else None
    sale.cash_method = '' if credit else header['cash_method']
    sale.cash_reference = '' if credit else (header.get('cash_reference') or '').strip()
    sale.status, sale.status_detail, sale.stamp_errors = Sale.Status.DRAFT, '', []
    sale.save()
    sale.lines.all().delete()
    taxes = {Decimal(str(t.extra.get('percentage', 0))) for t in contable.catalog('tax')}
    for line in lines:
        product = line['product']
        item = price_for(sale.price_list, product, sale.issue_date)
        list_price = item.price if item else None
        unit = line.get('unit_price')
        if unit is None:
            if list_price is None:
                raise ValidationError(f'{product.name} no tiene precio en la lista {sale.price_list.name}. Indique el precio y el motivo.')
            unit = list_price
        reason = (line.get('price_reason') or '').strip()
        if list_price is not None and unit < list_price:
            raise ValidationError(f'El precio de {product.name} es menor al de la lista ({cop(list_price)}). Registre la venta a precio de lista; '
                                  'el descuento lo concede la administración desde la venta.')
        if (list_price is None or unit != list_price) and not reason:
            raise ValidationError(f'Explique por qué el precio de {product.name} es distinto al de la lista.')
        rate = Decimal(line.get('tax_rate') or 0)
        if rate and rate not in taxes:
            raise ValidationError(f'No hay un impuesto de {rate} % activo en el sistema contable.')
        prior = line.get('prior_movement')
        if prior:
            _unbilled_outflow(prior, product, line['quantity'], exclude_line=sale)
        # Los descuentos no se digitan en la venta: los concede la administración con permiso de la dirección.
        discount = ZERO
        SaleLine.objects.create(sale=sale, product=product, quantity=line['quantity'], price_item=item, list_price=list_price, unit_price=unit,
                                price_reason=reason, discount=discount, tax_rate=rate, prior_movement=prior)
    lines = list(sale.lines.all())
    sale.subtotal = money(sum((l.base for l in lines), ZERO))
    sale.tax = money(sum((l.tax_value for l in lines), ZERO))
    sale.total = money(sale.subtotal + sale.tax)
    if sale.total <= 0:
        raise ValidationError('El total de la venta debe ser mayor que cero.')
    sale.save(update_fields=['subtotal', 'tax', 'total'])
    event(user, 'Venta guardada como borrador', f'sale-{sale.pk}', f'{party.name} · {cop(sale.total)}')
    return sale


def _release(sale, user):
    for st in SaleStock.objects.select_related('reservation').filter(line__sale=sale, movement__isnull=True):
        r = st.reservation
        if r and r.status == 'active':
            if not Batch.objects.filter(pk=st.batch_id, reserved__gte=st.quantity).update(reserved=F('reserved') - st.quantity):
                raise ValidationError('Reserva inconsistente; revise los saldos del lote.')
            r.status, r.closed_at = 'released', timezone.now()
            r.save(update_fields=['status', 'closed_at'])
        st.delete()
    SaleStock.objects.filter(line__sale=sale, reservation__isnull=True).delete()


def _reserve(sale, user):
    today = timezone.localdate()
    for line in sale.lines.select_related('product', 'prior_movement__batch'):
        if line.prior_movement_id:
            _unbilled_outflow(line.prior_movement, line.product, line.quantity, exclude_line=sale)
            SaleStock.objects.create(line=line, batch=line.prior_movement.batch, quantity=line.quantity, movement=line.prior_movement)
            continue
        need = line.quantity
        for b in Batch.objects.select_for_update().filter(product=line.product, expiry__gte=today).order_by('expiry', 'pk'):
            take = min(b.physical - b.reserved, need)
            if take <= 0 or not Batch.objects.filter(pk=b.pk, physical__gte=F('reserved') + take).update(reserved=F('reserved') + take):
                continue
            r = Reservation.objects.create(batch=b, quantity=take, purpose=f'Venta {sale.pk} · {sale.party.name}'[:300], created_by=user)
            SaleStock.objects.create(line=line, batch=b, quantity=take, reservation=r)
            need -= take
            if not need:
                break
        if need:
            raise ValidationError(f'No hay existencias vigentes suficientes de {line.product.name}: faltan {line.product.qty(need)}.')


def invoice_payload(sale):
    doc = contable.catalog('document_fv').first()
    if doc is None:
        raise ValidationError('No hay un comprobante de factura activo en el sistema contable. Actualice los catálogos.')
    taxes = {Decimal(str(t.extra.get('percentage', 0))): t.contable_id for t in contable.catalog('tax')}
    warehouse = contable.catalog('warehouse').first()
    items = []
    for l in sale.lines.select_related('product'):
        item = {'code': l.product.contable_code or l.product.code, 'description': l.product.name[:100], 'quantity': l.quantity,
                'price': float(l.unit_price), 'discount': float(l.discount)}
        if l.tax_rate:
            item['taxes'] = [{'id': sid(taxes[l.tax_rate])}]
        if warehouse:
            item['warehouse'] = sid(warehouse.contable_id)
        items.append(item)
    payment = {'id': sid(sale.payment_type_id), 'value': float(sale.total)}
    if sale.condition == 'credit':
        payment['due_date'] = sale.due_date.isoformat()
    payload = {'document': {'id': sid(doc.contable_id)}, 'date': sale.issue_date.isoformat(),
               'customer': {'identification': sale.party.identification, 'branch_office': sale.branch.office},
               'seller': sid(sale.seller_id), 'observations': sale.observations, 'items': items, 'payments': [payment],
               'stamp': {'send': bool(settings.CONTABLE['stamp_send'])}, 'mail': {'send': False}}
    if sale.purchase_order:
        payload['additional_fields'] = {'purchase_order': sale.purchase_order[:20]}
    return payload


def issue_sale(user, sale_id, token):
    if not can_issue(user):
        raise PermissionDenied('Su cuenta no tiene permiso para emitir facturas.')
    with transaction.atomic():
        operation(user, token, 'issue')
        sale = Sale.objects.select_for_update().select_related('party', 'branch', 'price_list').get(pk=sale_id)
        if sale.status != Sale.Status.DRAFT:
            raise ValidationError('Solo se envían al sistema contable las ventas en borrador.')
        today = timezone.localdate()
        if sale.issue_date < today:
            shift = today - sale.issue_date
            sale.issue_date = today
            if sale.due_date:
                sale.due_date += shift
            event(user, 'Fecha de venta actualizada a hoy', f'sale-{sale.pk}', 'La factura electrónica no admite fecha anterior')
        header = {f: getattr(sale, f) for f in ('party', 'context', 'price_list', 'issue_date', 'seller_id', 'condition', 'payment_type_id',
                                                 'due_date', 'cash_method', 'cash_reference', 'purchase_order')}
        _validate_header(sale, header)
        if user.role not in FINANCE and any(l.price_changed for l in sale.lines.all()):
            raise ValidationError('La venta tiene precios distintos a la lista: debe emitirla contabilidad.')
        discounts.check_before_issue(sale)
        _reserve(sale, user)
        sub = contable.enqueue(user, Submission.Kind.INVOICE, sale.pk, invoice_payload(sale))
        sale.status, sale.status_detail = Sale.Status.SENDING, ''
        sale.issued_by, sale.issued_at, sale.environment = user, timezone.now(), sub.environment
        sale.save()
        event(user, 'Factura enviada al sistema contable', f'sale-{sale.pk}', f'{sale.party.name} · {cop(sale.total)} · clave {sub.idempotency_key}')
        contable.after_commit(sub)
    return sale


def on_invoice_result(sub):
    accepted = False
    with transaction.atomic():
        sale = Sale.objects.select_for_update().select_related('party', 'branch').get(pk=int(sub.target_id))
        if sale.status in (Sale.Status.ISSUED, Sale.Status.CANCELLED):
            return
        S = Submission.Status
        if sub.status == S.ACCEPTED:
            _accept(sale, sub)
            accepted = True
        elif sub.status == S.REJECTED:
            sale.status, sale.stamp_errors = Sale.Status.REJECTED, sub.errors
            sale.status_detail = sub.last_error or 'El sistema contable rechazó la factura.'
            event(sub.created_by, 'Factura rechazada por el sistema contable', f'sale-{sale.pk}', sale.status_detail)
        elif sub.status in (S.UNCERTAIN, S.REVIEW):
            sale.status, sale.status_detail = Sale.Status.UNCERTAIN, sub.last_error
        else:
            sale.status, sale.status_detail = Sale.Status.SENDING, sub.last_error
        sale.save()
    if accepted:
        try:
            store_pdf(sale)
        except (ConnectorError, ValueError):
            pass


def _accept(sale, sub):
    r = sub.response or {}
    stamp, mail = r.get('stamp') or {}, r.get('mail') or {}
    sale.contable_id, sale.fiscal_prefix, sale.fiscal_number = str(r.get('id', '')), str(r.get('prefix') or ''), r.get('number')
    sale.fiscal_name, sale.cufe = str(r.get('name') or ''), str(stamp.get('cufe') or '')
    sale.stamp_status, sale.stamp_errors, sale.mail_status = str(stamp.get('status') or ''), stamp.get('errors') or [], str(mail.get('status') or '')
    sale.contable_balance = money(r['balance']) if r.get('balance') is not None else None
    sale.balance_checked_at, sale.environment = timezone.now(), sub.environment
    sale.status, sale.status_detail = Sale.Status.ISSUED, ''
    if not sale.fiscal_number:
        raise ValidationError('El sistema contable no devolvió el número de la factura.')
    actor, now = sub.created_by, timezone.now()
    for st in SaleStock.objects.select_for_update().select_related('reservation', 'batch').filter(line__sale=sale, movement__isnull=True):
        res = st.reservation
        if not Batch.objects.filter(pk=st.batch_id, reserved__gte=st.quantity, physical__gte=st.quantity).update(
                physical=F('physical') - st.quantity, reserved=F('reserved') - st.quantity):
            raise ValidationError(f'Saldo inconsistente en el lote {st.batch.lot}.')
        st.movement = Movement.objects.create(batch=st.batch, quantity=-st.quantity, kind='exit', reference=sale.fiscal_name,
                                              note=f'Venta a {sale.party.name}'[:300], reservation=res, actor=actor)
        st.save(update_fields=['movement'])
        res.status, res.closed_at = 'delivered', now
        res.save(update_fields=['status', 'closed_at'])
    rec = Receivable.objects.create(party=sale.party, branch_office=sale.branch.office, sale=sale, source='sale', prefix=sale.fiscal_prefix or 'FE',
                                    number=sale.fiscal_number, issue_date=sale.issue_date,
                                    due_date=sale.due_date if sale.condition == 'credit' else sale.issue_date, amount=sale.total, created_by=actor)
    if sale.condition == 'cash':
        col = Collection.objects.create(party=sale.party, date=sale.issue_date, amount=sale.total, method=sale.cash_method,
                                        reference=sale.cash_reference, source='counter', sale=sale, created_by=actor)
        PaymentApplication.objects.create(collection=col, receivable=rec, amount=sale.total, proposal='cash_sale', created_by=actor)
    event(actor, 'Factura de venta emitida', f'sale-{sale.pk}', f'{sale.fiscal_name} · {sale.party.name} · {cop(sale.total)}')


def store_pdf(sale):
    data = contable.fetch(f'/v1/invoices/{sale.contable_id}/pdf')
    raw = b64decode(data.get('base64') or '')
    if not raw.startswith(b'%PDF'):
        raise ValueError('El sistema contable no devolvió un PDF válido.')
    with transaction.atomic():
        s = Sale.objects.select_for_update().get(pk=sale.pk)
        s.pdf.save(f'{s.fiscal_name or s.pk}.pdf', ContentFile(raw), save=False)
        s.pdf_sha256 = hashlib.sha256(raw).hexdigest()
        s.save(update_fields=['pdf', 'pdf_sha256'])
    return sale


def refresh_sale(user, sale_id):
    require(user, SALES)
    sale = Sale.objects.get(pk=sale_id)
    if not sale.contable_id:
        raise ValidationError('La venta todavía no tiene una factura en el sistema contable.')
    try:
        data = contable.fetch(f'/v1/invoices/{sale.contable_id}')
        if not sale.pdf:
            store_pdf(sale)
    except ConnectorError as e:
        raise ValidationError(f'No fue posible consultar el sistema contable: {e}')
    stamp = data.get('stamp') or {}
    Sale.objects.filter(pk=sale.pk).update(contable_balance=money(data.get('balance') or 0), balance_checked_at=timezone.now(),
                                           stamp_status=str(stamp.get('status') or sale.stamp_status), cufe=str(stamp.get('cufe') or sale.cufe))
    event(user, 'Saldo consultado en el sistema contable', f'sale-{sale.pk}', f'{sale.fiscal_name} · saldo {cop(data.get("balance") or 0)}')
    return Sale.objects.get(pk=sale.pk)


def live_submission(kind, target_id):
    return Submission.objects.filter(kind=kind, target_id=str(target_id)).exclude(status=Submission.Status.REJECTED).first()


def retry_sale(user, sale_id, token):
    if not (can_issue(user) or user.role in FINANCE):
        raise PermissionDenied('Su cuenta no tiene permiso para reintentar envíos.')
    with transaction.atomic():
        operation(user, token, 'issue_retry')
        sale = Sale.objects.get(pk=sale_id)
        sub = live_submission(Submission.Kind.INVOICE, sale.pk)
        if sale.status not in (Sale.Status.UNCERTAIN, Sale.Status.SENDING) or sub is None:
            raise ValidationError('La venta no tiene un envío pendiente o incierto.')
        if sub.status == Submission.Status.REVIEW:
            raise ValidationError('Este envío requiere revisión en la pantalla del sistema contable antes de reintentarlo.')
        event(user, 'Reintento de envío al sistema contable', f'sale-{sale.pk}', f'Misma clave {sub.idempotency_key}')
    contable.process(sub.pk)
    return Sale.objects.get(pk=sale_id)


@transaction.atomic
def cancel_sale(user, sale_id, reason, token):
    require(user, SALES)
    operation(user, token, 'sale_cancel')
    sale = Sale.objects.select_for_update().get(pk=sale_id)
    if not sale.editable:
        raise ValidationError('Solo se cancelan borradores o ventas rechazadas. Una factura emitida se corrige con nota crédito en el sistema contable.')
    if not reason.strip():
        raise ValidationError('Explique el motivo de la cancelación.')
    _release(sale, user)
    discounts.on_sale_changed(user, sale, 'Venta cancelada')
    sale.status, sale.cancel_reason = Sale.Status.CANCELLED, reason.strip()
    sale.save(update_fields=['status', 'cancel_reason', 'updated_at'])
    event(user, 'Venta cancelada', f'sale-{sale.pk}', reason)


def send_invoice_mail(user, sale_id, to, token):
    require(user, SALES)
    with transaction.atomic():
        operation(user, token, 'sale_mail')
        sale = Sale.objects.get(pk=sale_id)
    if sale.status != Sale.Status.ISSUED:
        raise ValidationError('Solo se envían por correo facturas emitidas.')
    # El simulador registra la solicitud y no envía nada: queda como envío simulado en la venta y en la auditoría.
    try:
        contable.get_client().request('POST', f'/v1/invoices/{sale.contable_id}/mail', {'mail_to': to.strip()})
    except ConnectorError as e:
        raise ValidationError(f'El sistema contable no registró el envío: {e}')
    Sale.objects.filter(pk=sale.pk).update(mail_status=f'Envío simulado registrado · {to.strip()} · {timezone.localtime():%d/%m/%Y %H:%M}'[:120])
    event(user, 'Envío simulado de factura por correo', f'sale-{sale.pk}', f'{sale.fiscal_name} · {to.strip()} · no salió de la plataforma')


# ------------------------------------------------------------------- cartera

def open_receivables(party=None):
    qs = with_balance(Receivable.objects.select_related('party', 'sale'))
    if party is not None:
        qs = qs.filter(party=party)
    return qs.filter(balance__gt=0).order_by('due_date', 'number')


def aging(rows, today=None):
    today = today or timezone.localdate()
    buckets = {'current': ZERO, 'd30': ZERO, 'd60': ZERO, 'd90': ZERO, 'over': ZERO}
    for r in rows:
        late = (today - r.due_date).days
        key = 'current' if late <= 0 else 'd30' if late <= 30 else 'd60' if late <= 60 else 'd90' if late <= 90 else 'over'
        buckets[key] += r.balance
        r.days_late = late
    return buckets


@transaction.atomic
def add_opening(user, data, token):
    require(user, FINANCE)
    operation(user, token, 'opening')
    prefix = re.sub(r'[^A-Z0-9]', '', data['prefix'].upper())
    if not prefix:
        raise ValidationError('Indique el prefijo de la factura en el sistema contable.')
    if Receivable.objects.filter(prefix=prefix, number=data['number'], quote=data['quote']).exists():
        raise ValidationError('Esa cuota ya está registrada en la cartera.')
    if data['due_date'] < data['issue_date']:
        raise ValidationError('El vencimiento no puede ser anterior a la emisión.')
    party = data['party']
    rec = Receivable.objects.create(party=party, branch_office=0, source='opening', prefix=prefix, number=data['number'], quote=data['quote'],
                                    issue_date=data['issue_date'], due_date=data['due_date'], amount=data['balance'],
                                    note=(data.get('note') or '').strip(), created_by=user)
    if contable.environment() == 'simulator':
        # La cartera inicial también debe existir en el simulador contable para poder registrar recibos de caja.
        from contable.models import SimulatedDocument
        import uuid
        SimulatedDocument.objects.get_or_create(kind='invoice', prefix=prefix, number=data['number'], defaults={
            'contable_id': str(uuid.uuid4()), 'name': f'{prefix}-{data["number"]}', 'customer': party.identification, 'total': data['balance'],
            'balance': data['balance'], 'payload': {'request': {'date': data['issue_date'].isoformat(), 'customer': {'identification': party.identification}},
                                                    'response': {'prefix': prefix, 'number': data['number'], 'name': f'{prefix}-{data["number"]}', 'stamp': {}}}})
    event(user, 'Saldo inicial de cartera registrado', f'receivable-{rec.pk}', f'{rec.document} · {party.name} · {cop(rec.amount)}')
    return rec


# --------------------------------------------------------------------- banco

HEADERS = {
    'date': ['fecha', 'fecha movimiento', 'fecha de movimiento', 'fecha transaccion', 'fecha de transaccion', 'fecha operacion', 'date'],
    'amount': ['valor', 'monto', 'importe', 'valor transaccion', 'valor movimiento'],
    'credit': ['credito', 'creditos', 'abono', 'abonos', 'entradas', 'ingresos', 'valor credito'],
    'debit': ['debito', 'debitos', 'cargo', 'cargos', 'salidas', 'egresos', 'valor debito'],
    'description': ['descripcion', 'concepto', 'detalle', 'descripcion movimiento', 'transaccion', 'movimiento'],
    'reference': ['referencia', 'ref', 'referencia 1', 'documento', 'numero documento', 'comprobante', 'referencia del pago'],
    'origin_account': ['cuenta origen', 'cuenta de origen', 'producto origen', 'cuenta ordenante', 'numero de cuenta origen'],
    'holder_name': ['titular', 'nombre', 'ordenante', 'nombre ordenante', 'originador', 'nombre del originador', 'nombre titular'],
    'holder_id': ['nit', 'identificacion', 'nit originador', 'documento ordenante', 'cedula', 'identificacion ordenante', 'nit ordenante'],
    'balance': ['saldo', 'saldo final', 'saldo disponible', 'saldo total'],
    'bank_ref': ['codigo', 'id transaccion', 'numero de transaccion', 'secuencia', 'numero de operacion'],
}
LOOKUP = {name: field for field, names in HEADERS.items() for name in names}


def parse_money(value):
    if value is None or value == '':
        return None
    if isinstance(value, (int, float, Decimal)):
        return money(Decimal(str(value)))
    s = str(value).strip().replace('$', '').replace(' ', '').replace(' ', '')
    negative = s.startswith('-') or (s.startswith('(') and s.endswith(')')) or s.endswith('-')
    s = s.strip('-()')
    if ',' in s and '.' in s:
        dec = ',' if s.rfind(',') > s.rfind('.') else '.'
        s = s.replace('.' if dec == ',' else ',', '').replace(dec, '.')
    elif ',' in s:
        s = s.replace(',', '.') if re.search(r',\d{1,2}$', s) and s.count(',') == 1 else s.replace(',', '')
    elif s.count('.') > 1 or re.search(r'\.\d{3}$', s):
        s = s.replace('.', '')
    try:
        amount = Decimal(s)
    except InvalidOperation:
        raise ValidationError(f'Valor no reconocido: {value}')
    return money(-amount if negative else amount)


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value or '').strip()[:10]
    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y', '%Y/%m/%d', '%d/%m/%y', '%d.%m.%Y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValidationError(f'Fecha no reconocida: {value}')


def _table(name, raw):
    ext = name.lower().rsplit('.', 1)[-1] if '.' in name else ''
    if ext == 'xlsx':
        if not raw.startswith(b'PK'):
            raise ValidationError('El archivo no es un XLSX válido.')
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    if ext in ('csv', 'txt'):
        for enc in ('utf-8-sig', 'cp1252'):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=';,\t|') if text.strip() else csv.excel
        return [row for row in csv.reader(io.StringIO(text), dialect)]
    raise ValidationError('Formato no admitido. Use el CSV o XLSX descargado del banco.')


def parse_statement(name, raw):
    table = _table(name, raw)
    header_row, columns = None, {}
    for i, row in enumerate(table[:20]):
        cols = {}
        for j, cell in enumerate(row):
            field = LOOKUP.get(norm(cell))
            if field and field not in cols:
                cols[field] = j
        if 'date' in cols and ('amount' in cols or 'credit' in cols):
            header_row, columns = i, cols
            break
    if header_row is None:
        raise ValidationError('No se encontró una fila de encabezados con fecha y valor (o crédito/débito).')
    rows, warnings = [], []
    for n, row in enumerate(table[header_row + 1:], start=header_row + 2):
        cell = lambda f: row[columns[f]] if f in columns and columns[f] < len(row) else None
        if not any(c not in (None, '') for c in row):
            continue
        try:
            when = parse_date(cell('date'))
            if 'amount' in columns:
                amount = parse_money(cell('amount'))
            else:
                amount = (parse_money(cell('credit')) or ZERO) - abs(parse_money(cell('debit')) or ZERO)
        except ValidationError as e:
            warnings.append(f'Fila {n}: {e.messages[0]}')
            continue
        if not amount:
            warnings.append(f'Fila {n}: valor cero o vacío; se omitió.')
            continue
        balance = cell('balance')
        rows.append({'line': n, 'date': when.isoformat(), 'amount': str(amount), 'description': str(cell('description') or '').strip()[:300],
                     'reference': str(cell('reference') or '').strip()[:120], 'origin_account': re.sub(r'\D', '', str(cell('origin_account') or '')),
                     'holder_name': str(cell('holder_name') or '').strip()[:160], 'holder_id': re.sub(r'[^0-9A-Za-z]', '', str(cell('holder_id') or ''))[:30],
                     'balance_after': str(parse_money(balance)) if balance not in (None, '') else '', 'bank_ref': str(cell('bank_ref') or '').strip()[:80]})
    if not rows:
        raise ValidationError('El archivo no tiene movimientos legibles.')
    return rows, warnings


def mask(digits):
    return f'****{digits[-4:]}' if digits else ''


def account_hash(digits):
    return salted_hmac('cartera-cuenta-origen', digits).hexdigest()[:40]


def fingerprint(account, row):
    parts = [str(account.pk), row['date'], row['amount'], norm(row['description']), norm(row['reference']), row['bank_ref'], row['balance_after'],
             account_hash(row['origin_account']) if row['origin_account'] else '']
    return hashlib.sha256('|'.join(parts).encode()).hexdigest()


def _mark_known(account, rows):
    seen = {}
    for row in rows:
        fp = fingerprint(account, row)
        seen[fp] = seen.get(fp, 0) + 1
        row['fingerprint'], row['occurrence'] = fp, seen[fp]
        row['known'] = BankTransaction.objects.filter(account=account, fingerprint=fp, occurrence=seen[fp]).exists()
    return rows


def preview_statement(user, account, file_name, raw, token):
    require(user, FINANCE)
    if not raw or len(raw) > MAX_STATEMENT:
        raise ValidationError('El archivo está vacío o supera 10 MB.')
    rows, warnings = parse_statement(file_name, raw)
    digest = hashlib.sha256(raw).hexdigest()
    with transaction.atomic():
        operation(user, token, 'statement_preview')
        previous = StatementImport.objects.filter(account=account, sha256=digest, status='confirmed').first()
        if previous:
            warnings.insert(0, f'Este mismo archivo ya se importó el {timezone.localtime(previous.confirmed_at):%d/%m/%Y %H:%M}. Los movimientos repetidos no se duplicarán.')
        _mark_known(account, rows)
        imp = StatementImport(account=account, file_name=file_name[:200], sha256=digest, rows=rows, warnings=warnings, total_rows=len(rows),
                              new_rows=sum(not r['known'] for r in rows), known_rows=sum(r['known'] for r in rows), uploaded_by=user)
        ext = file_name.lower().rsplit('.', 1)[-1]
        imp.file.save(f'{digest[:16]}.{ext}', ContentFile(raw), save=False)
        imp.save()
        event(user, 'Extracto cargado para revisión', f'statement-{imp.pk}', f'{account} · {len(rows)} movimientos')
    return imp


def _payer_for(row):
    digits, hid, name = row['origin_account'], row['holder_id'], row['holder_name']
    if digits:
        key = 'acct:' + account_hash(digits)
    elif hid:
        key = 'id:' + hid.upper()
    elif name:
        key = 'name:' + norm(name)[:70]
    else:
        return None
    payer, _ = Payer.objects.get_or_create(key=key, defaults={'name': name, 'holder_id': hid, 'account_masked': mask(digits)})
    if name and not payer.name:
        payer.name = name
        payer.save(update_fields=['name'])
    return payer


@transaction.atomic
def confirm_statement(user, import_id, token):
    require(user, FINANCE)
    operation(user, token, 'statement_confirm')
    imp = StatementImport.objects.select_for_update().select_related('account').get(pk=import_id)
    if imp.status != 'preview':
        raise ValidationError('Esta importación ya fue confirmada o descartada.')
    rows = _mark_known(imp.account, imp.rows)
    created = 0
    for row in rows:
        if row['known']:
            continue
        amount = Decimal(row['amount'])
        BankTransaction.objects.create(
            account=imp.account, statement=imp, date=date.fromisoformat(row['date']), amount=amount, description=row['description'],
            reference=row['reference'], origin_account=mask(row['origin_account']), holder_name=row['holder_name'], holder_id=row['holder_id'],
            balance_after=Decimal(row['balance_after']) if row['balance_after'] else None, bank_ref=row['bank_ref'], fingerprint=row['fingerprint'],
            occurrence=row['occurrence'], payer=_payer_for(row) if amount > 0 else None,
            status=BankTransaction.Status.PENDING if amount > 0 else BankTransaction.Status.OTHER,
            note='' if amount > 0 else 'Salida de dinero: se conciliará con pagos a proveedores.')
        created += 1
    imp.rows, imp.status, imp.confirmed_by, imp.confirmed_at = rows, 'confirmed', user, timezone.now()
    imp.new_rows, imp.known_rows = created, len(rows) - created
    imp.save()
    event(user, 'Extracto importado', f'statement-{imp.pk}', f'{imp.account} · {created} nuevos · {len(rows) - created} ya importados')
    return imp


@transaction.atomic
def discard_statement(user, import_id, token):
    require(user, FINANCE)
    operation(user, token, 'statement_discard')
    imp = StatementImport.objects.select_for_update().get(pk=import_id)
    if imp.status != 'preview':
        raise ValidationError('Solo se descartan importaciones sin confirmar.')
    imp.status = 'discarded'
    imp.save(update_fields=['status'])
    event(user, 'Extracto descartado', f'statement-{imp.pk}', imp.file_name)


def used_amount(txn):
    applied = PaymentApplication.objects.filter(collection__transaction=txn, collection__source='bank', reversed_at__isnull=True).aggregate(s=Sum('amount'))['s']
    counter = Collection.objects.filter(transaction=txn).exclude(source='bank').aggregate(s=Sum('amount'))['s']
    return money((applied or ZERO) + (counter or ZERO))


def remaining(txn):
    return money(txn.amount - used_amount(txn)) if txn.amount > 0 else ZERO


def _refresh_txn(txn):
    if txn.status == BankTransaction.Status.OTHER:
        return
    used = used_amount(txn)
    if used <= 0:
        txn.status = BankTransaction.Status.PENDING
        txn.party = None
    else:
        txn.status = BankTransaction.Status.APPLIED if used >= txn.amount else BankTransaction.Status.IDENTIFIED
    txn.save(update_fields=['status', 'party'])


def _mentions(text, rec):
    return re.search(rf'\b(?:{re.escape(rec.prefix.lower())}|fact|factura|fe|fv)\s*0*{rec.number}\b', text) is not None


def suggestions(txn):
    """Candidatos con sus razones. Nunca aplica nada: contabilidad confirma."""
    cands = {}

    def add(party, level, reason):
        c = cands.setdefault(party.pk, {'party': party, 'level': 0, 'reasons': []})
        c['level'] = max(c['level'], level)
        if reason not in c['reasons']:
            c['reasons'].append(reason)

    text = norm(f'{txn.description} {txn.reference}')
    open_recs = list(open_receivables())
    for r in open_recs:
        if _mentions(text, r):
            add(r.party, HIGH, f'La descripción menciona la factura {r.document}.')
    if txn.holder_id:
        for p in Party.objects.filter(identification=re.sub(r'\D', '', txn.holder_id) or txn.holder_id, active=True):
            add(p, HIGH, 'La identificación del titular coincide con la del cliente.')
    if txn.payer_id:
        links = list(PayerLink.objects.select_related('party').filter(payer=txn.payer, status='confirmed'))
        for link in links:
            who = f' ({link.relation})' if link.relation else ''
            shared = f' Cuenta compartida: vinculada a {len(links)} clientes.' if len(links) > 1 else ''
            add(link.party, HIGH, f'El titular{who} ya está confirmado como pagador de este cliente.{shared}')
    by_party = {}
    for r in open_recs:
        by_party.setdefault(r.party_id, []).append(r)
    for recs in by_party.values():
        party = recs[0].party
        for r in recs:
            if r.balance == txn.amount:
                add(party, MEDIUM, f'El valor coincide con el saldo de {r.document}.')
        if len(recs) > 1 and sum(r.balance for r in recs) == txn.amount:
            add(party, MEDIUM, 'El valor coincide con el saldo total del cliente.')
        elif 1 < len(recs) <= 8:
            for size in (2, 3):
                for combo in combinations(recs, size):
                    if sum(r.balance for r in combo) == txn.amount:
                        add(party, MEDIUM, 'El valor coincide con la suma de ' + ', '.join(r.document for r in combo) + '.')
                        break
    if txn.holder_name:
        words = {w for w in norm(txn.holder_name).split() if len(w) >= 3}
        for p in Party.objects.filter(active=True):
            shared = words & {w for w in norm(p.name).split() if len(w) >= 3}
            if len(shared) >= 2:
                add(p, LOW, 'El nombre del titular se parece al del cliente.')
    _learned_opinion(txn, cands, add)
    ranked = sorted(cands.values(), key=lambda c: (-c['level'], -c.get('prob', 0), c['party'].name))
    for c in ranked:
        c['label'] = LEVELS[c['level']]
    if not ranked:
        decision = 'none'
    elif ranked[0]['level'] < MEDIUM:
        decision = 'weak'
    elif len([c for c in ranked if c['level'] == ranked[0]['level']]) > 1:
        decision = 'ambiguous'
    else:
        decision = 'suggested'
    counter = Collection.objects.select_related('party', 'sale').filter(source='counter', transaction__isnull=True, method__in=('card', 'transfer'),
                                                                       date__range=(txn.date - timedelta(days=7), txn.date))
    return {'decision': decision, 'candidates': ranked, 'counter': list(counter.filter(amount__lte=remaining(txn))[:10])}


def _learned_opinion(txn, cands, add):
    """El modelo de pagos (aprendizaje/pagos.py) suma su puntaje como una razón más; nunca decide solo ni bloquea."""
    try:
        from aprendizaje import pagos
        trained, scores, extra = pagos.opinion(txn, [c['party'] for c in cands.values()])
    except Exception:
        return
    if not trained or not scores:
        return
    for p in extra:
        cands.setdefault(p.pk, {'party': p, 'level': 0, 'reasons': []})
    # El puntaje del modelo no está calibrado: no es una probabilidad. Se muestra en escala 0–100 junto al acierto
    # medido en la evaluación secuencial para puntajes similares.
    ordered = sorted(scores.items(), key=lambda kv: -kv[1]['prob'])
    top_pid, top = ordered[0]
    second = ordered[1][1]['prob'] if len(ordered) > 1 else 0.0
    table = pagos.reliability()
    for pid, sc in scores.items():
        c = cands.get(pid)
        if c is None:
            continue
        c['prob'] = c['score'] = sc['prob']
        c['score_100'] = round(sc['prob'] * 100)
        c['measured'] = pagos.measured(sc['prob'], table)
        why = pagos.describe(sc['why'])
        shown = pagos.score_label(sc['prob'], table) + (f'. Señales: {why}' if why else '')
        if pid == top_pid and sc['prob'] >= 0.8 and sc['prob'] - second >= 0.25:
            add(c['party'], HIGH, f'El modelo de pagos reconoce el patrón ({shown}).')
        elif pid == top_pid and sc['prob'] >= 0.6 and c['level'] < MEDIUM:
            add(c['party'], MEDIUM, f'El modelo de pagos lo señala como posible ({shown}).')
        elif c['level'] == 0 and sc['prob'] >= 0.5:
            add(c['party'], LOW, f'El modelo de pagos recuerda una relación ({shown}).')
    for pid in [pid for pid, c in cands.items() if c['level'] == 0]:
        del cands[pid]


def propose_allocation(txn, party, amount=None):
    """Facturas mencionadas primero; luego, por antigüedad del vencimiento. Devuelve (reparto, excedente)."""
    left = remaining(txn) if amount is None else amount
    text = norm(f'{txn.description} {txn.reference}')
    recs = list(open_receivables(party))
    mentioned = [r for r in recs if _mentions(text, r)]
    plan = []
    for r in mentioned + [r for r in recs if r not in mentioned]:
        take = min(left, r.balance)
        if take > 0:
            plan.append((r, take, 'instruction' if r in mentioned else 'oldest'))
            left -= take
    return plan, money(left)


def voucher_payload(voucher, applications, account):
    doc = contable.catalog('document_rc').first()
    if doc is None:
        raise ValidationError('No hay un comprobante de recibo de caja activo en el sistema contable. Actualice los catálogos.')
    col = voucher.collection
    total = money(sum((a.amount for a in applications), ZERO))
    return {'document': {'id': sid(doc.contable_id)}, 'date': col.date.isoformat(), 'type': 'DebtPayment',
            'customer': {'identification': col.party.identification, 'branch_office': applications[0].receivable.branch_office},
            'items': [{'due': {'prefix': a.receivable.prefix, 'consecutive': a.receivable.number, 'quote': a.receivable.quote,
                               'date': a.receivable.due_date.isoformat()}, 'value': float(a.amount)} for a in applications],
            'payment': {'id': sid(account.contable_payment_id), 'value': float(total)},
            'observations': f'Recaudo bancario {col.date:%d/%m/%Y} {col.reference}'.strip()[:200]}


def apply_transaction(user, txn_id, party_id, allocations, token, remember_payer=True, relation=''):
    require(user, FINANCE)
    with transaction.atomic():
        operation(user, token, 'apply')
        txn = BankTransaction.objects.select_for_update().select_related('account', 'payer').get(pk=txn_id)
        party = Party.objects.get(pk=party_id)
        if txn.amount <= 0 or txn.status == BankTransaction.Status.OTHER:
            raise ValidationError('Solo se aplican ingresos marcados como cartera.')
        if txn.party_id and txn.party_id != party.pk and used_amount(txn) > 0:
            raise ValidationError('El movimiento ya tiene aplicaciones de otro cliente. Reviértalas antes de cambiar de cliente.')
        allocations = [(int(rid), money(a)) for rid, a in allocations if a and money(a) > 0]
        if not allocations:
            raise ValidationError('Indique al menos un valor a aplicar.')
        if len({rid for rid, _ in allocations}) != len(allocations):
            raise ValidationError('Cada factura debe aparecer una sola vez en el reparto.')
        left = remaining(txn)
        total = money(sum(a for _, a in allocations))
        if total > left:
            raise ValidationError(f'El valor aplicado ({cop(total)}) supera el saldo sin aplicar del movimiento ({cop(left)}).')
        ids = [rid for rid, _ in allocations]
        list(Receivable.objects.select_for_update().filter(pk__in=ids))
        recs = {r.pk: r for r in with_balance(Receivable.objects.filter(pk__in=ids))}
        proposed = {r.pk: (a, kind) for r, a, kind in propose_allocation(txn, party, left)[0]}
        col = txn.collections.filter(source='bank', party=party).first()
        if col is None:
            col = Collection.objects.create(party=party, date=txn.date, amount=left, method='transfer', reference=(txn.reference or txn.description)[:80],
                                            source='bank', transaction=txn, created_by=user)
        voucher = Voucher.objects.create(collection=col, created_by=user)
        apps = []
        for rid, amount in allocations:
            rec = recs.get(rid)
            if rec is None or rec.party_id != party.pk:
                raise ValidationError('Una de las facturas no pertenece al cliente elegido.')
            if amount > rec.balance:
                raise ValidationError(f'El valor aplicado a {rec.document} supera su saldo ({cop(rec.balance)}).')
            kind = proposed[rid][1] if rid in proposed and proposed[rid][0] == amount else 'manual'
            apps.append(PaymentApplication.objects.create(collection=col, receivable=rec, amount=amount, proposal=kind, voucher=voucher, created_by=user))
        reasons = next((c['reasons'] for c in suggestions(txn)['candidates'] if c['party'].pk == party.pk), ['Identificado manualmente por contabilidad.'])
        txn.party, txn.identified_by, txn.identified_at, txn.match_reasons = party, user, timezone.now(), reasons
        txn.save(update_fields=['party', 'identified_by', 'identified_at', 'match_reasons'])
        _refresh_txn(txn)
        if remember_payer and txn.payer_id:
            PayerLink.objects.update_or_create(payer=txn.payer, party=party, defaults={'status': 'confirmed', 'relation': relation.strip()[:80],
                                                                                         'confirmed_by': user, 'confirmed_at': timezone.now()})
        sub = contable.enqueue(user, Submission.Kind.VOUCHER, voucher.pk, voucher_payload(voucher, apps, txn.account))
        event(user, 'Pago aplicado', f'bank-{txn.pk}', f'{party.name} · ' + ', '.join(f'{a.receivable.document} {cop(a.amount)}' for a in apps))
        contable.after_commit(sub)
    return voucher


def on_voucher_result(sub):
    with transaction.atomic():
        voucher = Voucher.objects.select_for_update().select_related('collection__transaction').get(pk=int(sub.target_id))
        if voucher.status in ('accepted', 'linked'):
            return
        S = Submission.Status
        if sub.status == S.ACCEPTED:
            voucher.status, voucher.contable_id, voucher.contable_name, voucher.detail = 'accepted', sub.contable_id, sub.contable_name, ''
            event(sub.created_by, 'Recibo de caja registrado en el sistema contable', f'voucher-{voucher.pk}', sub.contable_name)
        elif sub.status == S.REJECTED:
            voucher.status, voucher.detail = 'rejected', (sub.last_error or 'El sistema contable rechazó el recibo.')[:500]
            # Se revierte en la aplicación para que la cartera local no difiera del sistema contable.
            now = timezone.now()
            for app in voucher.applications.filter(reversed_at__isnull=True):
                app.reversed_at, app.reversed_by, app.reversal_reason = now, sub.created_by, f'El sistema contable rechazó el recibo: {voucher.detail}'[:300]
                app.save(update_fields=['reversed_at', 'reversed_by', 'reversal_reason'])
            if voucher.collection.transaction_id:
                _refresh_txn(BankTransaction.objects.select_for_update().get(pk=voucher.collection.transaction_id))
            event(sub.created_by, 'Recibo rechazado por el sistema contable; aplicación revertida', f'voucher-{voucher.pk}', voucher.detail)
        elif sub.status in (S.UNCERTAIN, S.REVIEW):
            voucher.status, voucher.detail = ('review' if sub.status == S.REVIEW else 'uncertain'), sub.last_error[:500]
        else:
            voucher.status, voucher.detail = 'pending', sub.last_error[:500]
        voucher.save()


def retry_voucher(user, voucher_id, token):
    require(user, FINANCE)
    with transaction.atomic():
        operation(user, token, 'voucher_retry')
        sub = live_submission(Submission.Kind.VOUCHER, voucher_id)
        if sub is None or sub.status not in (Submission.Status.PENDING, Submission.Status.UNCERTAIN):
            raise ValidationError('El recibo no tiene un envío pendiente o incierto.')
    contable.process(sub.pk)


@transaction.atomic
def link_counter(user, txn_id, collection_ids, token):
    require(user, FINANCE)
    operation(user, token, 'link_counter')
    txn = BankTransaction.objects.select_for_update().get(pk=txn_id)
    cols = list(Collection.objects.select_for_update().filter(pk__in=collection_ids, source='counter', transaction__isnull=True))
    if not cols or len(cols) != len(set(collection_ids)):
        raise ValidationError('Elija recaudos de contado que no estén conciliados.')
    total = money(sum((c.amount for c in cols), ZERO))
    if total > remaining(txn):
        raise ValidationError(f'Los recaudos suman {cop(total)} y el movimiento tiene {cop(remaining(txn))} sin aplicar.')
    Collection.objects.filter(pk__in=[c.pk for c in cols]).update(transaction=txn)
    parties = {c.party_id for c in cols}
    if len(parties) == 1:
        txn.party_id = parties.pop()
    txn.identified_by, txn.identified_at = user, timezone.now()
    txn.match_reasons = ['Conciliado con recaudos de contado ya registrados en la venta.']
    txn.save(update_fields=['party', 'identified_by', 'identified_at', 'match_reasons'])
    _refresh_txn(txn)
    event(user, 'Movimiento conciliado con recaudos de contado', f'bank-{txn.pk}', f'{len(cols)} recaudo(s) · {cop(total)}')


@transaction.atomic
def mark_other(user, txn_id, note, token):
    require(user, FINANCE)
    operation(user, token, 'bank_other')
    txn = BankTransaction.objects.select_for_update().get(pk=txn_id)
    if used_amount(txn) > 0:
        raise ValidationError('El movimiento tiene aplicaciones activas. Reviértalas primero.')
    if not note.strip():
        raise ValidationError('Explique por qué no corresponde a cartera.')
    txn.status, txn.note, txn.party = BankTransaction.Status.OTHER, note.strip()[:300], None
    txn.save(update_fields=['status', 'note', 'party'])
    event(user, 'Movimiento marcado como no cartera', f'bank-{txn.pk}', note)


@transaction.atomic
def reopen_transaction(user, txn_id, token):
    require(user, FINANCE)
    operation(user, token, 'bank_reopen')
    txn = BankTransaction.objects.select_for_update().get(pk=txn_id)
    if txn.status != BankTransaction.Status.OTHER or txn.amount <= 0:
        raise ValidationError('Solo se reabren ingresos marcados como no cartera.')
    txn.status, txn.note = BankTransaction.Status.PENDING, ''
    txn.save(update_fields=['status', 'note'])
    event(user, 'Movimiento devuelto a por identificar', f'bank-{txn.pk}')


@transaction.atomic
def reverse_application(user, application_id, reason, token):
    require(user, FINANCE)
    operation(user, token, 'apply_reverse')
    app = PaymentApplication.objects.select_for_update().select_related('voucher', 'collection', 'receivable').get(pk=application_id)
    if app.reversed_at:
        raise ValidationError('La aplicación ya fue revertida.')
    if app.proposal == 'cash_sale':
        raise ValidationError('El pago de contado de una factura se corrige con nota crédito en el sistema contable.')
    if not reason.strip():
        raise ValidationError('Explique el motivo de la reversión.')
    app.reversed_at, app.reversed_by, app.reversal_reason = timezone.now(), user, reason.strip()[:300]
    app.save(update_fields=['reversed_at', 'reversed_by', 'reversal_reason'])
    if app.voucher and app.voucher.status == 'accepted':
        Voucher.objects.filter(pk=app.voucher_id).update(detail=f'Aplicación revertida: anule o ajuste el recibo {app.voucher.contable_name} en el sistema contable.')
    if app.collection.transaction_id:
        _refresh_txn(BankTransaction.objects.select_for_update().get(pk=app.collection.transaction_id))
    event(user, 'Aplicación revertida', f'receivable-{app.receivable_id}', f'{app.receivable.document} · {cop(app.amount)} · {reason}')


@transaction.atomic
def confirm_payer(user, payer_id, party_id, relation, token):
    require(user, FINANCE)
    operation(user, token, 'payer')
    link, _ = PayerLink.objects.update_or_create(payer_id=payer_id, party_id=party_id, defaults={
        'status': 'confirmed', 'relation': relation.strip()[:80], 'confirmed_by': user, 'confirmed_at': timezone.now()})
    event(user, 'Pagador vinculado', f'party-{party_id}', str(link.payer))
    return link


@transaction.atomic
def remove_payer(user, link_id, token):
    require(user, FINANCE)
    operation(user, token, 'payer_remove')
    link = PayerLink.objects.select_related('payer').get(pk=link_id)
    link.status, link.confirmed_by, link.confirmed_at = 'rejected', user, timezone.now()
    link.save(update_fields=['status', 'confirmed_by', 'confirmed_at'])
    event(user, 'Pagador desvinculado', f'party-{link.party_id}', str(link.payer))


# ------------------------------------------------------------------- alertas

def alerts(user):
    """Alertas para contabilidad: movimientos por identificar, envíos inciertos y recibos con problemas."""
    from django.urls import reverse
    items = []
    if user.role not in FINANCE:
        return items
    old = BankTransaction.objects.filter(status=BankTransaction.Status.PENDING, date__lte=timezone.localdate() - timedelta(days=3))
    count = old.count()
    if count:
        total = old.aggregate(s=Sum('amount'))['s'] or ZERO
        items.append(('bank-pending', 'warning', f'{count} {"ingreso" if count == 1 else "ingresos"} por identificar desde hace más de 3 días', f'Suman {cop(total)}.', reverse('bank') + '?estado=pending'))
    for sub in Submission.objects.filter(status__in=(Submission.Status.UNCERTAIN, Submission.Status.REVIEW)):
        items.append((f'contable-{sub.pk}', 'critical', f'{sub.get_kind_display()}: {sub.get_status_display().lower()}', sub.last_error or 'Revise el envío.',
                      reverse('contable_status')))
    for v in Voucher.objects.filter(status='rejected', created_at__gte=timezone.now() - timedelta(days=15)):
        items.append((f'voucher-{v.pk}', 'warning', 'Recibo de caja rechazado por el sistema contable', v.detail, reverse('contable_status')))
    items.extend(discounts.alerts(user))
    return items
