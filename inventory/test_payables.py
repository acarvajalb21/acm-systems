"""Cuentas por pagar con abonos, pagos agrupados, notas crédito, conciliación y exportación a Excel (datos sintéticos)."""
import io
import shutil
import tempfile
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .models import User, Supplier, DiscountTier, Invoice, SupplierPayment, PaymentAllocation, PaymentFile, SupplierCreditNote, with_payable_balance
from . import payables as P, services as S
from .testing import new_password

PASSWORD = new_password()
FAST = {'PASSWORD_HASHERS': ['django.contrib.auth.hashers.MD5PasswordHasher']}
TODAY = timezone.localdate
TERMS = {'payment_days': 120, 'base': 'subtotal', 'tiers': [[30, '5'], [60, '3']]}


def tok():
    return uuid.uuid4()


def fresh(inv):
    return with_payable_balance(Invoice.objects.filter(pk=inv.pk)).get()


@override_settings(**FAST)
class Base(TestCase):
    FILES = [(PaymentFile, 'file')]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._storages = []
        for model, name in self.FILES:
            field = model._meta.get_field(name)
            self._storages.append((field, field.storage))
            field.storage = FileSystemStorage(location=self.tmp / 'adjuntos')
        self._override = override_settings(PRIVATE_DIR=self.tmp)
        self._override.enable()
        self.finance = User.objects.create_user('finance', password=PASSWORD, role='finance', must_change_password=False)
        self.manager = User.objects.create_user('manager', password=PASSWORD, role='inventory', must_change_password=False)
        self.vendor = Supplier.objects.create(name='Proveedor Andino S.A.S.', nit='900123456', payment_days=120)
        DiscountTier.objects.bulk_create([DiscountTier(supplier=self.vendor, days=30, percent=Decimal('5')), DiscountTier(supplier=self.vendor, days=60, percent=Decimal('3'))])

    def tearDown(self):
        self._override.disable()
        for field, storage in self._storages:
            field.storage = storage
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def invoice(self, number='LA-7781', total='10000000', days_ago=10, supplier=None):
        issue = TODAY() - timedelta(days=days_ago)
        return Invoice.objects.create(supplier=supplier or self.vendor, number=number, issue_date=issue, due_date=issue + timedelta(days=120),
                                      subtotal=Decimal(total), total=Decimal(total), review=Invoice.Review.VALIDATED, terms=TERMS,
                                      validated_by=self.finance, validated_at=timezone.now(), created_by=self.finance)

    def pay(self, rows, on=None, reference='TRF', supplier=None):
        return P.register_payment(self.finance, supplier or self.vendor, rows, on or TODAY(), 'transfer', reference, [], '', tok())


class Payments(Base):
    def test_partial_then_settlement_with_discount(self):
        inv = self.invoice(total='100000')
        self.pay([{'invoice': inv, 'amount': Decimal('40000')}])
        inv = fresh(inv)
        self.assertEqual((inv.balance, inv.payment_state), (Decimal('60000.00'), 'partial'))
        s = P.settlement(inv, TODAY())
        self.assertEqual((s['discount'], s['cash']), (Decimal('5000.00'), Decimal('55000.00')))
        self.pay([{'invoice': inv, 'amount': s['cash'], 'discount': s['discount']}])
        inv = fresh(inv)
        self.assertEqual((inv.balance, inv.payment_state, inv.discount_taken), (Decimal('0.00'), 'paid', Decimal('5000.00')))
        with self.assertRaisesMessage(ValidationError, 'superan el saldo'):
            self.pay([{'invoice': inv, 'amount': Decimal('1')}])

    def test_one_transfer_for_several_invoices(self):
        a, b = self.invoice('LA-1', '300000'), self.invoice('LA-2', '200000')
        other = Supplier.objects.create(name='Otro proveedor', nit='800111222')
        c = self.invoice('OT-1', '50000', supplier=other)
        with self.assertRaisesMessage(ValidationError, 'no es de'):
            self.pay([{'invoice': a, 'amount': Decimal('285000'), 'discount': Decimal('15000')}, {'invoice': c, 'amount': Decimal('50000')}])
        p = self.pay([{'invoice': a, 'amount': Decimal('285000'), 'discount': Decimal('15000')}, {'invoice': b, 'amount': Decimal('190000'), 'discount': Decimal('10000')}])
        self.assertEqual((p.amount, p.allocations.count()), (Decimal('475000.00'), 2))
        self.assertEqual((fresh(a).balance, fresh(b).balance), (Decimal('0.00'), Decimal('0.00')))

    def test_scheduled_payment_confirmed_late_leaves_balance(self):
        """Se programó con 5 %, el banco pagó el día 35 y corresponde 3 %."""
        inv = self.invoice(days_ago=35)
        p = P.schedule_payment(self.finance, self.vendor, [{'invoice': inv, 'amount': Decimal('9500000'), 'discount': Decimal('500000')}], TODAY(), 'transfer', '', tok())
        self.assertEqual((p.status, fresh(inv).balance), ('scheduled', Decimal('10000000.00')))
        with self.assertRaisesMessage(ValidationError, 'supera el que corresponde'):
            P.confirm_payment(self.finance, p.pk, [{'invoice': inv, 'amount': Decimal('9500000'), 'discount': Decimal('500000')}], TODAY(), 'transfer', 'TRF-9', [], '', tok())
        P.confirm_payment(self.finance, p.pk, [{'invoice': inv, 'amount': Decimal('9500000'), 'discount': Decimal('300000')}], TODAY(), 'transfer', 'TRF-9', [], '', tok())
        a = PaymentAllocation.objects.get(payment=p)
        self.assertEqual((a.discount_percent, a.lost_discount, fresh(inv).balance), (Decimal('3.00'), Decimal('200000.00'), Decimal('200000.00')))
        self.pay([{'invoice': inv, 'amount': Decimal('200000')}], reference='TRF-10')
        self.assertTrue(fresh(inv).paid)

    def test_discount_accepted_then_claimed_by_supplier(self):
        inv = self.invoice(days_ago=35)
        self.pay([{'invoice': inv, 'amount': Decimal('9500000'), 'discount': Decimal('500000'), 'discount_note': 'Aceptación escrita del proveedor'}])
        self.assertTrue(fresh(inv).paid)
        P.add_adjustment(self.finance, inv.pk, 'discount_claim', Decimal('200000'), 'Cobro del excedente al 3 %', tok())
        inv = fresh(inv)
        self.assertEqual((inv.balance, inv.payment_state), (Decimal('200000.00'), 'partial'))
        self.assertEqual(P.settlement(inv, TODAY())['discount'], Decimal('0.00'))

    def test_credit_note_and_void_protection(self):
        inv = self.invoice(total='1000000')
        P.add_credit_note(self.finance, self.vendor, 'NC-55', TODAY(), Decimal('300000'), 'Devolución de 5 unidades', inv, Decimal('250000'), tok())
        cn = SupplierCreditNote.objects.get()
        self.assertEqual((fresh(inv).balance, cn.available), (Decimal('750000.00'), Decimal('50000.00')))
        with self.assertRaisesMessage(ValidationError, 'Ya existe la nota crédito'):
            P.add_credit_note(self.finance, self.vendor, 'nc-55', None, Decimal('1'), '', None, None, tok())
        with self.assertRaisesMessage(ValidationError, 'no se puede anular'):
            S.void_invoice(self.finance, inv.pk, 'Error', tok())

    def test_cancel_payment_restores_balance_and_permissions(self):
        inv = self.invoice(total='100000')
        p = self.pay([{'invoice': inv, 'amount': Decimal('100000')}])
        self.assertTrue(fresh(inv).paid)
        P.cancel_payment(self.finance, p.pk, 'Transferencia devuelta por el banco', tok())
        self.assertEqual(fresh(inv).balance, Decimal('100000.00'))
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            P.register_payment(self.manager, self.vendor, [{'invoice': inv, 'amount': Decimal('1')}], TODAY(), 'transfer', 'x', [], '', tok())

    def test_confirmation_requires_reference_or_support(self):
        inv = self.invoice(total='100000')
        with self.assertRaisesMessage(ValidationError, 'referencia del pago'):
            P.register_payment(self.finance, self.vendor, [{'invoice': inv, 'amount': Decimal('100000')}], TODAY(), 'transfer', '', [], '', tok())
        p = P.register_payment(self.finance, self.vendor, [{'invoice': inv, 'amount': Decimal('95000'), 'discount': Decimal('5000')}], TODAY(), 'transfer', '',
                               [('soporte.pdf', b'%PDF-1.4 prueba')], '', tok())
        self.assertEqual(p.files.count(), 1)

    def test_reconcile_with_bank_outflow(self):
        from cartera import services as C
        from cartera.models import BankAccount, BankTransaction
        inv = self.invoice(total='100000')
        p = self.pay([{'invoice': inv, 'amount': Decimal('95000'), 'discount': Decimal('5000')}])
        account = BankAccount.objects.create(name='Principal', bank='Banco', last4='1234', contable_payment_id='301')
        text = f'Fecha;Descripción;Valor\n{TODAY():%d/%m/%Y};PAGO PROVEEDOR PROV ANDINO;-95.000\n'
        C.confirm_statement(self.finance, C.preview_statement(self.finance, account, 'ext.csv', text.encode(), tok()).pk, tok())
        txn = BankTransaction.objects.get()
        self.assertEqual([c.pk for c in P.outflow_candidates(txn)], [p.pk])
        P.reconcile_payment(self.finance, p.pk, txn.pk, tok())
        p.refresh_from_db()
        self.assertEqual((p.status, BankTransaction.objects.get().supplier_payment_id), ('reconciled', p.pk))
        with self.assertRaisesMessage(ValidationError, 'conciliado con el banco'):
            P.cancel_payment(self.finance, p.pk, 'x', tok())


class Export(Base):
    def test_excel_export_has_own_neutral_columns(self):
        import openpyxl
        paid, part, pending = self.invoice('LA-1', '100000'), self.invoice('LA-2', '200000'), self.invoice('LA-3', '300000')
        self.pay([{'invoice': paid, 'amount': Decimal('95000'), 'discount': Decimal('5000')}])
        self.pay([{'invoice': part, 'amount': Decimal('50000')}])
        P.add_credit_note(self.finance, self.vendor, 'NC-9', TODAY(), Decimal('20000'), 'Devolución', part, Decimal('20000'), tok())
        P.add_adjustment(self.finance, part.pk, 'charge', Decimal('1000'), 'Nota débito 12', tok())
        when = TODAY() + timedelta(days=3)
        P.schedule_payment(self.finance, self.vendor, [{'invoice': pending, 'amount': Decimal('300000')}], when, 'transfer', '', tok())
        Invoice.objects.create(supplier=self.vendor, number='LA-ANULADA', issue_date=TODAY(), due_date=TODAY(), total=Decimal('1'), review=Invoice.Review.VOID,
                               created_by=self.finance)
        wb = openpyxl.load_workbook(io.BytesIO(P.export_payables()))
        self.assertEqual(wb.sheetnames, ['Cuentas por pagar'])
        ws = wb.active
        self.assertIn('Demostración con datos sintéticos', ws['A2'].value)
        self.assertEqual([c.value for c in ws[4]], ['Proveedor', 'NIT', 'Factura', 'Emisión', 'Vencimiento', 'Total', 'Pagado', 'Descuento tomado',
                                                     'Notas crédito', 'Ajustes', 'Saldo', 'Estado', 'Pago programado'])
        rows = {r[2]: r for r in ws.iter_rows(min_row=5, values_only=True)}
        self.assertEqual(set(rows), {'LA-1', 'LA-2', 'LA-3'})                       # la anulada no se exporta
        self.assertEqual(rows['LA-1'][5:12], (100000, 95000, 5000, 0, 0, 0, 'Pagada'))
        self.assertEqual(rows['LA-2'][5:12], (200000, 50000, 0, 20000, 1000, 131000, 'Abono parcial'))
        self.assertEqual(rows['LA-3'][10:12], (300000, 'Por pagar'))
        self.assertEqual(rows['LA-3'][12].date(), when)
        self.assertEqual(rows['LA-1'][0:2], ('Proveedor Andino S.A.S.', self.vendor.nit_display))


@override_settings(**FAST)
class Screens(Base):
    def login(self):
        c = Client()
        c.force_login(self.finance)
        s = c.session
        s['user_version'] = self.finance.session_version
        s.save()
        return c

    def test_pages_and_grouped_payment_post(self):
        a, b = self.invoice('LA-1', '300000'), self.invoice('LA-2', '200000')
        c = self.login()
        for url in [reverse('payables'), reverse('payables') + '?vista=programados', reverse('payables') + '?vista=pagos', reverse('supplier_account', args=[self.vendor.pk]),
                    reverse('credit_note_new', args=[self.vendor.pk]), reverse('adjustment_new', args=[a.pk]),
                    reverse('payment_new') + f'?facturas={a.pk},{b.pk}&modo=pagar', reverse('payment_new') + f'?facturas={a.pk}&fecha=no-es-fecha',
                    reverse('invoice', args=[a.pk]), reverse('invoice_pay', args=[a.pk]), reverse('home')]:
            self.assertEqual(c.get(url).status_code, 200, url)
        data = {'token': str(tok()), 'mode': 'pagar', 'factura': [a.pk, b.pk], 'on': TODAY().isoformat(), 'method': 'transfer', 'reference': 'TRF-55', 'note': '',
                f'amount_{a.pk}': '285000', f'discount_{a.pk}': '15000', f'amount_{b.pk}': '190.000', f'discount_{b.pk}': '10.000'}
        r = c.post(reverse('payment_new'), data)
        p = SupplierPayment.objects.get()
        self.assertRedirects(r, reverse('supplier_payment', args=[p.pk]), fetch_redirect_response=False)
        self.assertEqual((p.amount, fresh(a).balance, fresh(b).balance), (Decimal('475000.00'), Decimal('0.00'), Decimal('0.00')))
        self.assertEqual(c.get(reverse('supplier_payment', args=[p.pk])).status_code, 200)
        r = c.get(reverse('ledger_export'))
        self.assertEqual((r.status_code, r['Content-Type'][:24]), (200, 'application/vnd.openxmlf'))
        self.assertEqual(Client().get(reverse('payables')).status_code, 302)

    def test_partial_payment_keeps_what_was_typed_when_the_reference_is_missing(self):
        a = self.invoice('LA-3', '300000')
        c = self.login()
        data = {'token': str(tok()), 'mode': 'pagar', 'factura': [a.pk], 'on': TODAY().isoformat(), 'method': 'transfer', 'reference': '', 'note': 'Abono acordado',
                f'amount_{a.pk}': '120000', f'discount_{a.pk}': '0', f'note_{a.pk}': 'sin descuento'}
        r = c.post(reverse('payment_new'), data)
        self.assertEqual((r.status_code, SupplierPayment.objects.count()), (200, 0))
        html = r.content.decode()
        self.assertIn('Indique la referencia del pago o adjunte el soporte.', html)
        self.assertIn(f'name="amount_{a.pk}" value="120000"', html)                  # el abono escrito no vuelve al saldo completo
        self.assertIn(f'name="note_{a.pk}" value="sin descuento"', html)
        self.assertIn('Abono acordado', html)
        r = c.post(reverse('payment_new'), {**data, 'token': str(tok()), 'reference': 'TRF-56'})
        p = SupplierPayment.objects.get()
        self.assertRedirects(r, reverse('supplier_payment', args=[p.pk]), fetch_redirect_response=False)
        self.assertEqual((p.amount, fresh(a).balance), (Decimal('120000.00'), Decimal('180000.00')))


class RoundedBalances(Base):
    """El saldo anotado se redondea a centavos: una factura saldada no vuelve a aparecer por un residuo de coma flotante."""

    def test_settled_invoice_with_cents_is_not_listed_as_payable(self):
        inv = self.invoice('LA-9001', '1487404.80')
        self.pay([{'invoice': inv, 'amount': Decimal('1462406.40'), 'discount': Decimal('24998.40')}])
        self.assertGreater(1487404.80 - 1462406.40 - 24998.40, 0)                    # en coma flotante queda un residuo positivo
        inv = fresh(inv)
        self.assertEqual((inv.balance_due, inv.balance, inv.payment_state), (Decimal('0.00'), Decimal('0.00'), 'paid'))
        self.assertFalse(with_payable_balance(Invoice.objects.all()).filter(balance_due__gt=0).exists())
        self.assertEqual(S.payables(), [])
        other = self.invoice('LA-9002', '250000.10')
        self.pay([{'invoice': other, 'amount': Decimal('100000.05')}])
        self.assertEqual([(i.number, i.balance_due) for i in S.payables()], [('LA-9002', Decimal('150000.05'))])
