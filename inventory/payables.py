"""Cuentas por pagar: pagos programados, confirmados y conciliados; notas crédito; ajustes; exportación a Excel.

Reglas:
- El descuento por pronto pago se calcula con las condiciones congeladas al validar la factura y la fecha real
  del pago, sobre la base pactada (subtotal o total), antes de notas crédito.
- Un descuento mayor al que corresponde por fecha exige el soporte de aceptación del proveedor.
- Si el proveedor cobra después la diferencia de descuento, se registra como ajuste y se paga aparte.
"""
import hashlib
import io
import re
import uuid
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from .models import (Invoice, SupplierPayment, PaymentAllocation, PaymentFile, SupplierCreditNote, CreditNoteApplication, PayableAdjustment,
                     money, with_payable_balance)
from .services import require, event, operation, discount_for, cop, pct, FINANCE, FILE_TYPES, MAX_FILE

ZERO = Decimal('0.00')


# ---------------------------------------------------------------- liquidación

def settlement(inv, on):
    """Valor a pagar en una fecha: saldo, descuento aplicable (si no se tomó antes) y descuento no aprovechado."""
    balance = inv.balance
    d = discount_for(inv, on)
    taken = inv.discount_taken
    discount = ZERO if taken > 0 or balance <= 0 else min(d['amount'], balance)
    potential = ZERO if taken > 0 else d['potential']
    return {'balance': balance, 'discount': money(discount), 'amount': money(discount), 'cash': money(balance - discount), 'due': money(balance - discount),
            'percent': d['percent'] if discount else ZERO, 'potential': potential, 'lost': money(max(potential - discount, ZERO)), 'elapsed': d['elapsed'],
            'deadline': d['deadline'], 'tiers': d['tiers'], 'base': d['base']}


def _rows(user, supplier, rows, on, scheduled=False):
    if not rows:
        raise ValidationError('Seleccione al menos una factura.')
    ids = [r['invoice'].pk for r in rows]
    if len(set(ids)) != len(ids):
        raise ValidationError('Cada factura debe aparecer una sola vez en el pago.')
    locked = {i.pk: i for i in Invoice.objects.select_for_update().filter(pk__in=ids)}
    fresh = {i.pk: i for i in with_payable_balance(Invoice.objects.filter(pk__in=ids))}
    out = []
    for r in rows:
        inv = fresh[r['invoice'].pk]
        if inv.supplier_id != supplier.pk:
            raise ValidationError(f'La factura {inv.number} no es de {supplier.name}.')
        if inv.review != Invoice.Review.VALIDATED:
            raise ValidationError(f'La factura {inv.number} no está validada.')
        if on < inv.issue_date:
            raise ValidationError(f'La fecha del pago es anterior a la emisión de {inv.number}.')
        amount, discount = money(r.get('amount') or 0), money(r.get('discount') or 0)
        if amount < 0 or discount < 0 or amount + discount <= 0:
            raise ValidationError(f'Indique el valor a pagar de {inv.number}.')
        if amount + discount > inv.balance:
            raise ValidationError(f'{inv.number}: el pago y el descuento ({cop(amount + discount)}) superan el saldo ({cop(inv.balance)}).')
        s = settlement(inv, on)
        note = (r.get('discount_note') or '').strip()
        if discount > s['discount'] and not note and not scheduled:
            raise ValidationError(f'{inv.number}: el descuento supera el que corresponde por la fecha ({cop(s["discount"])}). '
                                  'Indique el soporte de aceptación del proveedor.')
        base = s['base'] or inv.total
        percent = money(discount * 100 / base) if base and discount else ZERO
        # El descuento no aprovechado se registra una sola vez: en el pago que toma descuento o salda la factura.
        settles = discount > 0 or amount + discount == inv.balance
        out.append({'invoice': locked[inv.pk], 'amount': amount, 'discount': discount, 'discount_percent': min(percent, Decimal('100')),
                    'days_elapsed': s['elapsed'], 'potential_discount': s['potential'] if settles else ZERO,
                    'lost_discount': money(max(s['potential'] - discount, ZERO)) if settles else ZERO, 'discount_note': note[:200]})
    return out


def _store_files(payment, user, uploads, saved):
    for name, raw in uploads:
        ext = ('.' + name.rsplit('.', 1)[-1].lower()) if '.' in name else ''
        if ext not in FILE_TYPES or ext in ('.xml', '.zip'):
            raise ValidationError(f'{name}: use PDF, PNG o JPG para el soporte del pago.')
        content_type, magic = FILE_TYPES[ext]
        if not raw or len(raw) > MAX_FILE or not raw[:8].startswith(magic):
            raise ValidationError(f'{name}: el archivo está vacío, supera 20 MB o su contenido no corresponde a la extensión.')
        f = PaymentFile(payment=payment, name=name[:200], content_type=content_type, size=len(raw), sha256=hashlib.sha256(raw).hexdigest(), uploaded_by=user)
        f.file.save(uuid.uuid4().hex + ext, ContentFile(raw), save=False)
        saved.append(f.file.name)
        f.save()


def _discard(saved):
    storage = PaymentFile._meta.get_field('file').storage
    for name in saved:
        storage.delete(name)


def _describe(allocs):
    return ', '.join(f'{a["invoice"].number} {cop(a["amount"])}' + (f' (dcto {cop(a["discount"])})' if a['discount'] else '') for a in allocs)


@transaction.atomic
def schedule_payment(user, supplier, rows, scheduled_on, method, note, token):
    require(user, FINANCE)
    operation(user, token, 'payment_schedule')
    if scheduled_on < timezone.localdate():
        raise ValidationError('La fecha programada no puede estar en el pasado.')
    busy = PaymentAllocation.objects.filter(invoice__in=[r['invoice'] for r in rows], payment__status=SupplierPayment.Status.SCHEDULED)
    if busy.exists():
        raise ValidationError(f'La factura {busy.first().invoice.number} ya está en un pago programado. Confírmelo o anúlelo primero.')
    allocs = _rows(user, supplier, rows, scheduled_on, scheduled=True)
    total = money(sum(a['amount'] for a in allocs))
    if total <= 0:
        raise ValidationError('El valor programado debe ser mayor que cero.')
    p = SupplierPayment.objects.create(supplier=supplier, status=SupplierPayment.Status.SCHEDULED, scheduled_on=scheduled_on, method=method, amount=total,
                                       note=(note or '').strip()[:300], created_by=user)
    for a in allocs:
        PaymentAllocation.objects.create(payment=p, **a)
    event(user, 'Pago programado', f'supplier-payment-{p.pk}', f'{supplier.name} · {scheduled_on:%d/%m/%Y} · {_describe(allocs)}')
    return p


def _confirm(user, p, rows, paid_on, method, reference, uploads, note):
    if paid_on > timezone.localdate():
        raise ValidationError('La fecha del pago no puede estar en el futuro.')
    if not (reference or '').strip() and not uploads:
        raise ValidationError('Indique la referencia del pago o adjunte el soporte.')
    allocs = _rows(user, p.supplier, rows, paid_on)
    total = money(sum(a['amount'] for a in allocs))
    if total <= 0:
        raise ValidationError('El valor pagado debe ser mayor que cero.')
    p.allocations.all().delete()
    for a in allocs:
        PaymentAllocation.objects.create(payment=p, **a)
    p.status, p.paid_on, p.method, p.reference, p.amount = SupplierPayment.Status.CONFIRMED, paid_on, method, (reference or '').strip()[:80], total
    p.note = (note or p.note or '').strip()[:300]
    p.confirmed_by, p.confirmed_at = user, timezone.now()
    p.save()
    saved = []
    try:
        _store_files(p, user, uploads, saved)
    except Exception:
        _discard(saved)
        raise
    event(user, 'Pago registrado', f'supplier-payment-{p.pk}', f'{p.supplier.name} · {cop(total)} · {_describe(allocs)}')
    for a in allocs:
        event(user, 'Pago registrado', a['invoice'].pk, f'{a["invoice"].number} · {cop(a["amount"])} · descuento {pct(a["discount_percent"])} %')
    return p


def confirm_payment(user, payment_id, rows, paid_on, method, reference, uploads, note, token):
    require(user, FINANCE)
    with transaction.atomic():
        operation(user, token, 'payment_confirm')
        p = SupplierPayment.objects.select_for_update().select_related('supplier').get(pk=payment_id)
        if p.status != SupplierPayment.Status.SCHEDULED:
            raise ValidationError('Solo se confirman pagos programados.')
        return _confirm(user, p, rows, paid_on, method, reference, uploads, note)


def register_payment(user, supplier, rows, paid_on, method, reference, uploads, note, token):
    require(user, FINANCE)
    with transaction.atomic():
        operation(user, token, 'payment')
        p = SupplierPayment(supplier=supplier, status=SupplierPayment.Status.SCHEDULED, amount=Decimal('0.01'), created_by=user)
        p.save()
        return _confirm(user, p, rows, paid_on, method, reference, uploads, note)


@transaction.atomic
def cancel_payment(user, payment_id, reason, token):
    require(user, FINANCE)
    operation(user, token, 'payment_cancel')
    p = SupplierPayment.objects.select_for_update().get(pk=payment_id)
    if p.status == SupplierPayment.Status.CANCELLED:
        raise ValidationError('El pago ya está anulado.')
    if p.status == SupplierPayment.Status.RECONCILED:
        raise ValidationError('El pago está conciliado con el banco. Deshaga primero la conciliación.')
    if not reason.strip():
        raise ValidationError('Explique el motivo de la anulación.')
    p.status, p.cancel_reason = SupplierPayment.Status.CANCELLED, reason.strip()[:300]
    p.save(update_fields=['status', 'cancel_reason'])
    event(user, 'Pago a proveedor anulado', f'supplier-payment-{p.pk}', reason)


@transaction.atomic
def set_contable_receipt(user, payment_id, name, token):
    require(user, FINANCE)
    operation(user, token, 'payment_contable')
    p = SupplierPayment.objects.select_for_update().get(pk=payment_id)
    if not p.settled:
        raise ValidationError('Vincule el egreso del sistema contable a un pago confirmado.')
    p.contable_receipt = re.sub(r'\s+', '', name or '').upper()[:40]
    p.save(update_fields=['contable_receipt'])
    event(user, 'Egreso del sistema contable vinculado', f'supplier-payment-{p.pk}', p.contable_receipt)


def outflow_candidates(txn):
    if txn.amount >= 0 or txn.supplier_payment_id:
        return []
    return list(SupplierPayment.objects.select_related('supplier').filter(status=SupplierPayment.Status.CONFIRMED, amount=-txn.amount,
                                                                         paid_on__range=(txn.date - timedelta(days=10), txn.date + timedelta(days=3)),
                                                                         bank_transaction__isnull=True))


@transaction.atomic
def reconcile_payment(user, payment_id, txn_id, token):
    from cartera.models import BankTransaction
    require(user, FINANCE)
    operation(user, token, 'payment_reconcile')
    p = SupplierPayment.objects.select_for_update().get(pk=payment_id)
    txn = BankTransaction.objects.select_for_update().get(pk=txn_id)
    if p.status != SupplierPayment.Status.CONFIRMED:
        raise ValidationError('Solo se concilian pagos confirmados y aún no conciliados.')
    if txn.supplier_payment_id or txn.amount >= 0:
        raise ValidationError('El movimiento no es una salida disponible para conciliar.')
    if -txn.amount != p.amount:
        raise ValidationError(f'El banco muestra {cop(-txn.amount)} y el pago registrado es de {cop(p.amount)}.')
    txn.supplier_payment, txn.note = p, f'Pago a {p.supplier.name}'[:300]
    txn.save(update_fields=['supplier_payment', 'note'])
    p.status = SupplierPayment.Status.RECONCILED
    p.save(update_fields=['status'])
    event(user, 'Pago a proveedor conciliado con el banco', f'supplier-payment-{p.pk}', f'Movimiento del {txn.date:%d/%m/%Y} · {cop(p.amount)}')


# ------------------------------------------------- notas crédito y ajustes

@transaction.atomic
def add_credit_note(user, supplier, number, issue_date, amount, note, invoice, apply_amount, token):
    require(user, FINANCE)
    operation(user, token, 'credit_note')
    number = (number or '').strip().upper()
    if not number or not amount or amount <= 0:
        raise ValidationError('Indique número y valor de la nota crédito.')
    if SupplierCreditNote.objects.filter(supplier=supplier, number__iexact=number).exists():
        raise ValidationError(f'Ya existe la nota crédito {number} de {supplier.name}.')
    cn = SupplierCreditNote.objects.create(supplier=supplier, number=number, issue_date=issue_date, amount=money(amount), note=(note or '').strip()[:300],
                                           created_by=user)
    event(user, 'Nota crédito de proveedor registrada', f'credit-note-{cn.pk}', f'{supplier.name} · {number} · {cop(amount)}')
    if invoice is not None:
        _apply_credit(user, cn, invoice, apply_amount or amount)
    return cn


def _apply_credit(user, cn, invoice, amount):
    amount = money(amount)
    Invoice.objects.select_for_update().filter(pk=invoice.pk).first()
    inv = with_payable_balance(Invoice.objects.filter(pk=invoice.pk)).get()
    if inv.supplier_id != cn.supplier_id or inv.review != Invoice.Review.VALIDATED:
        raise ValidationError('La nota crédito solo se aplica a facturas validadas del mismo proveedor.')
    if amount <= 0 or amount > cn.available or amount > inv.balance:
        raise ValidationError(f'Valor no válido: disponible en la nota {cop(cn.available)}, saldo de la factura {cop(inv.balance)}.')
    CreditNoteApplication.objects.create(credit_note=cn, invoice=invoice, amount=amount, created_by=user)
    event(user, 'Nota crédito aplicada', invoice.pk, f'{cn.number} · {invoice.number} · {cop(amount)}')


@transaction.atomic
def apply_credit_note(user, cn_id, invoice, amount, token):
    require(user, FINANCE)
    operation(user, token, 'credit_note_apply')
    cn = SupplierCreditNote.objects.select_for_update().get(pk=cn_id)
    _apply_credit(user, cn, invoice, amount)


@transaction.atomic
def add_adjustment(user, invoice_id, kind, amount, reference, token):
    require(user, FINANCE)
    operation(user, token, 'payable_adjustment')
    Invoice.objects.select_for_update().get(pk=invoice_id)
    inv = with_payable_balance(Invoice.objects.filter(pk=invoice_id)).get()
    if inv.review != Invoice.Review.VALIDATED:
        raise ValidationError('Solo se ajustan facturas validadas.')
    amount = money(amount)
    if amount <= 0 or not (reference or '').strip():
        raise ValidationError('Indique el valor y el soporte del ajuste.')
    if kind in PayableAdjustment.DECREASE and amount > inv.balance:
        raise ValidationError(f'El ajuste supera el saldo de la factura ({cop(inv.balance)}).')
    adj = PayableAdjustment.objects.create(invoice=inv, kind=kind, amount=amount, reference=reference.strip()[:120], created_by=user)
    event(user, 'Ajuste de cuenta por pagar', inv.pk, f'{inv.number} · {adj.get_kind_display()} · {cop(amount)} · {reference}')
    return adj


# ------------------------------------------------------- exportación a Excel

EXPORT_COLUMNS = [('Proveedor', 34), ('NIT', 16), ('Factura', 16), ('Emisión', 12), ('Vencimiento', 12), ('Total', 16), ('Pagado', 16),
                  ('Descuento tomado', 18), ('Notas crédito', 16), ('Ajustes', 14), ('Saldo', 16), ('Estado', 18), ('Pago programado', 16)]
MONEY_COLUMNS = (6, 7, 8, 9, 10, 11)
DATE_COLUMNS = (4, 5, 13)


def export_payables():
    """Cuentas por pagar en Excel: una fila por factura no anulada, con su saldo y su estado a la fecha."""
    import openpyxl
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Cuentas por pagar'
    ws['A1'] = f'Cuentas por pagar · {settings.COMPANY_NAME}'
    ws['A1'].font = Font(bold=True, size=13)
    ws['A2'] = f'Corte al {timezone.localdate():%d/%m/%Y} · Demostración con datos sintéticos'
    head = 4
    for i, (title, width) in enumerate(EXPORT_COLUMNS, start=1):
        ws.cell(head, i, title).font = Font(bold=True)
        ws.column_dimensions[get_column_letter(i)].width = width
    scheduled = dict(PaymentAllocation.objects.filter(payment__status=SupplierPayment.Status.SCHEDULED).values_list('invoice_id', 'payment__scheduled_on'))
    invoices = with_payable_balance(Invoice.objects.exclude(review=Invoice.Review.VOID).select_related('supplier')).order_by('supplier__name', 'issue_date', 'pk')
    n = head
    for inv in invoices:
        n += 1
        state = inv.payment_label if inv.review == Invoice.Review.VALIDATED else inv.get_review_display()
        row = [inv.supplier.name, inv.supplier.nit_display, inv.number, inv.issue_date, inv.due_date, float(inv.total), float(inv.paid_sum),
               float(inv.discount_sum), float(inv.credit_sum), float(inv.adjust_sum), float(inv.balance_due), state, scheduled.get(inv.pk)]
        for i, value in enumerate(row, start=1):
            cell = ws.cell(n, i, value)
            if i in MONEY_COLUMNS:
                cell.number_format = '"$"#,##0'
            elif i in DATE_COLUMNS and value is not None:
                cell.number_format = 'dd/mm/yyyy'
    ws.freeze_panes = ws.cell(head + 1, 1)
    ws.auto_filter.ref = f'A{head}:{get_column_letter(len(EXPORT_COLUMNS))}{max(n, head)}'
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
