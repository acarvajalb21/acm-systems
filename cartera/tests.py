"""Pruebas de ventas, cartera y banco con datos sintéticos y el simulador local del sistema contable."""
import shutil
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from decimal import Decimal
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import FileSystemStorage
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from inventory.models import User, Product, Batch, Movement, Reservation
from contable import services as contable
from contable.models import Submission, SimulatedDocument
from contable.simulator import Simulator
from inventory.testing import new_password
from . import services as C
from .models import (PriceList, PriceItem, Sale, Receivable, BankAccount, BankTransaction, PaymentApplication, Payer, PayerLink, Collection,
                     StatementImport, with_balance)

PASSWORD = new_password()
FAST = {'PASSWORD_HASHERS': ['django.contrib.auth.hashers.MD5PasswordHasher']}
TODAY = timezone.localdate


def tok():
    return uuid.uuid4()


def balance(rec):
    return with_balance(Receivable.objects.filter(pk=rec.pk)).get().balance


@override_settings(**FAST)
class Base(TestCase):
    FILES = [(Sale, 'pdf'), (StatementImport, 'file')]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._storages = []
        for model, name in self.FILES:
            field = model._meta.get_field(name)
            self._storages.append((field, field.storage))
            field.storage = FileSystemStorage(location=self.tmp / 'adjuntos')
        self._override = override_settings(PRIVATE_DIR=self.tmp)
        self._override.enable()
        Simulator.fail_next = None
        self.users = {n: User.objects.create_user(n, password=PASSWORD, role=r, must_change_password=False, first_name=n.title())
                      for n, r in [('finance', 'finance'), ('billing', 'billing'), ('manager', 'inventory'), ('requester', 'requester'), ('admin', 'admin')]}
        self.finance, self.billing = self.users['finance'], self.users['billing']
        self.prod_a = Product.objects.create(code='REF-A', name='Referencia A')
        self.prod_b = Product.objects.create(code='REF-B', name='Referencia B')
        for p in (self.prod_a, self.prod_b):
            Batch.objects.create(product=p, lot='L1', expiry=TODAY() + timedelta(days=60), location='Bodega 1', physical=5)
            Batch.objects.create(product=p, lot='L2', expiry=TODAY() + timedelta(days=200), location='Bodega 1', physical=50)
        contable.sync_catalogs()
        self.lists = {c: PriceList.objects.create(code=c.upper(), name=n, context=c) for c, n in
                      [('counter', 'Mostrador'), ('company', 'Empresa'), ('contractor', 'Contratista')]}
        for pl in self.lists.values():
            PriceItem.objects.create(price_list=pl, product=self.prod_a, price=Decimal('100000'), valid_from=TODAY() - timedelta(days=30), created_by=self.finance)
            PriceItem.objects.create(price_list=pl, product=self.prod_b, price=Decimal('100000'), valid_from=TODAY() - timedelta(days=30), created_by=self.finance)
        self.ferreteria = self.party('Ferretería El Tornillo S.A.S.', '900555011', '31', 'contractor')
        self.rojas = self.party('Camilo Rojas', '52555012', '13', 'contractor')
        self.account = BankAccount.objects.create(name='Cuenta principal', bank='Banco Ejemplo', last4='1234', contable_payment_id='301')

    def tearDown(self):
        self._override.disable()
        for field, storage in self._storages:
            field.storage = storage
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def party(self, name, ident, id_type='13', context='counter', **extra):
        data = {'id_type': id_type, 'identification': ident, 'name': name, 'kind': 'company' if id_type == '31' else 'person', 'default_context': context,
                'price_list': None, 'credit_days': 30, 'requires_po': False, 'active': True, 'notes': '', 'einvoice_email': 'facturacion@cliente.example', **extra}
        return C.save_party(self.finance, None, data, tok())

    def header(self, party, condition='credit', **extra):
        h = {'party': party, 'context': party.default_context, 'price_list': self.lists[party.default_context], 'issue_date': TODAY(),
             'seller_id': '401', 'condition': condition, 'payment_type_id': '204' if condition == 'credit' else '202',
             'due_date': TODAY() + timedelta(days=30) if condition == 'credit' else None, 'cash_method': '' if condition == 'credit' else 'card',
             'cash_reference': '' if condition == 'credit' else 'APROB-778', 'purchase_order': '', 'observations': ''}
        h.update(extra)
        return h

    def sale(self, party, lines, user=None, **extra):
        user = user or self.finance
        s = C.save_sale(user, None, self.header(party, **extra), [{'product': p, 'quantity': q, 'unit_price': price, 'price_reason': reason}
                                                                  for p, q, price, reason in lines], tok())
        with self.captureOnCommitCallbacks(execute=True):
            C.issue_sale(self.finance, s.pk, tok())
        return Sale.objects.get(pk=s.pk)

    def statement(self, rows, name='extracto.csv'):
        text = 'Fecha;Descripción;Referencia;Cuenta origen;Titular;Valor;Saldo\n' + '\n'.join(';'.join(r) for r in rows)
        imp = C.preview_statement(self.finance, self.account, name, text.encode('utf-8'), tok())
        return C.confirm_statement(self.finance, imp.pk, tok())


class AcceptanceExample(Base):
    """Recorrido completo: dos facturas de $300.000 y $200.000 y una transferencia de $350.000."""

    def test_full_example(self):
        fe1 = self.sale(self.ferreteria, [(self.prod_a, 2, None, ''), (self.prod_b, 1, None, '')])
        fe2 = self.sale(self.ferreteria, [(self.prod_a, 2, None, '')])
        self.assertEqual((fe1.status, fe1.total, fe2.total), (Sale.Status.ISSUED, Decimal('300000.00'), Decimal('200000.00')))
        self.assertTrue(fe1.cufe.startswith('SIM') and fe1.pdf_sha256 and fe1.fiscal_name.startswith('FE-'))
        # Inventario: primero vence, primero sale; las salidas quedan vinculadas a la factura, sin reservas activas.
        l1 = Batch.objects.get(product=self.prod_a, lot='L1')
        self.assertEqual((l1.physical, l1.reserved), (1, 0))
        self.assertEqual(Movement.objects.filter(kind='exit', reference=fe1.fiscal_name).count(), 2)
        r1, r2 = fe1.receivable, fe2.receivable
        # Una persona de tesorería ya está confirmada como pagadora de la ferretería.
        payer = Payer.objects.create(key='acct:' + C.account_hash('99998821'), name='SANDRA PINEDA', account_masked='****8821')
        PayerLink.objects.create(payer=payer, party=self.ferreteria, status='confirmed', relation='tesorería', confirmed_by=self.finance)
        row = [TODAY().strftime('%d/%m/%Y'), 'TRANSF DE SANDRA PINEDA', 'PSE 5521', '99998821', 'SANDRA PINEDA', '350.000,00', '9.350.000,00']
        imp = self.statement([row])
        self.assertEqual((imp.new_rows, imp.known_rows), (1, 0))
        txn = BankTransaction.objects.get()
        self.assertEqual((txn.amount, txn.origin_account, txn.payer_id), (Decimal('350000.00'), '****8821', payer.pk))
        result = C.suggestions(txn)
        self.assertEqual(result['decision'], 'suggested')
        self.assertEqual(result['candidates'][0]['party'], self.ferreteria)
        plan, excess = C.propose_allocation(txn, self.ferreteria)
        self.assertEqual([(r.pk, a) for r, a, _ in plan], [(r1.pk, Decimal('300000.00')), (r2.pk, Decimal('50000.00'))])
        self.assertEqual(excess, Decimal('0.00'))
        with self.captureOnCommitCallbacks(execute=True):
            voucher = C.apply_transaction(self.finance, txn.pk, self.ferreteria.pk, [(r.pk, a) for r, a, _ in plan], tok())
        voucher.refresh_from_db()
        txn.refresh_from_db()
        self.assertEqual(voucher.status, 'accepted')
        self.assertTrue(voucher.contable_name.startswith('RC-'))
        self.assertEqual((balance(r1), balance(r2), txn.status), (Decimal('0.00'), Decimal('150000.00'), BankTransaction.Status.APPLIED))
        self.assertEqual(PaymentApplication.objects.filter(voucher=voucher).count(), 2)
        # El saldo en el sistema contable coincide con la cartera local.
        self.assertEqual(C.refresh_sale(self.finance, fe2.pk).contable_balance, Decimal('150000.00'))
        self.assertEqual(C.refresh_sale(self.finance, fe1.pk).contable_balance, Decimal('0.00'))
        # Al cargar de nuevo el extracto no se duplica nada.
        again = self.statement([row])
        self.assertEqual((again.new_rows, again.known_rows, BankTransaction.objects.count()), (0, 1, 1))
        self.assertEqual(balance(r2), Decimal('150000.00'))

    def test_ambiguous_payer_stays_unidentified(self):
        self.sale(self.ferreteria, [(self.prod_a, 3, None, '')])
        self.sale(self.rojas, [(self.prod_b, 2, None, '')])
        payer = Payer.objects.create(key='acct:' + C.account_hash('99998821'), name='SANDRA PINEDA', account_masked='****8821')
        for party in (self.ferreteria, self.rojas):
            PayerLink.objects.create(payer=payer, party=party, status='confirmed', confirmed_by=self.finance)
        self.statement([[TODAY().strftime('%d/%m/%Y'), 'TRANSF DE SANDRA PINEDA', '', '99998821', 'SANDRA PINEDA', '350000', '']])
        txn = BankTransaction.objects.get()
        result = C.suggestions(txn)
        self.assertEqual(result['decision'], 'ambiguous')
        self.assertEqual({c['party'] for c in result['candidates'][:2]}, {self.ferreteria, self.rojas})
        self.assertEqual((txn.status, PaymentApplication.objects.filter(proposal__in=['oldest', 'manual']).count()), (BankTransaction.Status.PENDING, 0))


class Sales(Base):
    def test_cash_sale_requires_reference_and_settles_receivable(self):
        with self.assertRaisesMessage(ValidationError, 'referencia del recaudo'):
            C.save_sale(self.billing, None, self.header(self.ferreteria, condition='cash', cash_reference=''), [{'product': self.prod_a, 'quantity': 1}], tok())
        sale = self.sale(self.ferreteria, [(self.prod_a, 1, None, '')], condition='cash')
        self.assertEqual(balance(sale.receivable), Decimal('0.00'))
        col = Collection.objects.get(sale=sale)
        self.assertEqual((col.source, col.method, col.reference), ('counter', 'card', 'APROB-778'))
        self.assertEqual(sale.contable_balance, Decimal('0.00'))

    def test_credit_requires_due_payment_type(self):
        with self.assertRaisesMessage(ValidationError, 'maneje vencimiento'):
            C.save_sale(self.finance, None, self.header(self.ferreteria, payment_type_id='201'), [{'product': self.prod_a, 'quantity': 1}], tok())

    def test_price_override_needs_reason_and_finance(self):
        # Un precio menor al de la lista es un descuento: lo concede la administración desde la venta (permisos de la dirección).
        with self.assertRaisesMessage(ValidationError, 'menor al de la lista'):
            C.save_sale(self.billing, None, self.header(self.ferreteria), [{'product': self.prod_a, 'quantity': 1, 'unit_price': Decimal('90000'),
                                                                             'price_reason': 'Acuerdo de lanzamiento'}], tok())
        with self.assertRaisesMessage(ValidationError, 'Explique por qué'):
            C.save_sale(self.billing, None, self.header(self.ferreteria), [{'product': self.prod_a, 'quantity': 1, 'unit_price': Decimal('110000')}], tok())
        s = C.save_sale(self.billing, None, self.header(self.ferreteria), [{'product': self.prod_a, 'quantity': 1, 'unit_price': Decimal('110000'),
                                                                             'price_reason': 'Presentación especial'}], tok())
        self.billing.can_issue_invoices = True
        self.billing.save()
        with self.assertRaisesMessage(ValidationError, 'debe emitirla contabilidad'):
            C.issue_sale(self.billing, s.pk, tok())
        with self.captureOnCommitCallbacks(execute=True):
            C.issue_sale(self.finance, s.pk, tok())
        self.assertEqual(Sale.objects.get(pk=s.pk).total, Decimal('110000.00'))

    def test_billing_without_permission_cannot_issue(self):
        s = C.save_sale(self.billing, None, self.header(self.ferreteria), [{'product': self.prod_a, 'quantity': 1}], tok())
        with self.assertRaises(PermissionDenied):
            C.issue_sale(self.billing, s.pk, tok())
        with self.assertRaises(PermissionDenied):
            C.save_sale(self.users['manager'], None, self.header(self.ferreteria), [{'product': self.prod_a, 'quantity': 1}], tok())

    def test_rejection_keeps_reservation_until_edit(self):
        bad = self.party('Cliente sin registro en el sistema contable', '99900011')
        s = C.save_sale(self.finance, None, self.header(bad, condition='cash'), [{'product': self.prod_a, 'quantity': 2}], tok())
        with self.captureOnCommitCallbacks(execute=True):
            C.issue_sale(self.finance, s.pk, tok())
        s.refresh_from_db()
        self.assertEqual(s.status, Sale.Status.REJECTED)
        self.assertIn('no existe', s.status_detail)
        self.assertEqual(Batch.objects.get(product=self.prod_a, lot='L1').reserved, 2)
        self.assertFalse(Receivable.objects.exists())
        C.save_sale(self.finance, s.pk, self.header(self.ferreteria, condition='cash'), [{'product': self.prod_a, 'quantity': 2}], tok())
        self.assertEqual(Batch.objects.get(product=self.prod_a, lot='L1').reserved, 0)
        self.assertEqual(Reservation.objects.get().status, 'released')

    def test_lost_response_is_recovered_with_same_key(self):
        s = C.save_sale(self.finance, None, self.header(self.ferreteria), [{'product': self.prod_a, 'quantity': 1}], tok())
        Simulator.fail_next = 'lost_response'
        with self.captureOnCommitCallbacks(execute=True):
            C.issue_sale(self.finance, s.pk, tok())
        s.refresh_from_db()
        sub = Submission.objects.get()
        self.assertEqual((s.status, sub.status), (Sale.Status.UNCERTAIN, Submission.Status.UNCERTAIN))
        self.assertEqual(SimulatedDocument.objects.filter(kind='invoice').count(), 1)
        C.retry_sale(self.finance, s.pk, tok())
        s.refresh_from_db()
        sub.refresh_from_db()
        self.assertEqual((s.status, sub.status, sub.attempts), (Sale.Status.ISSUED, Submission.Status.ACCEPTED, 2))
        self.assertEqual(SimulatedDocument.objects.filter(kind='invoice').count(), 1)
        self.assertEqual(Receivable.objects.count(), 1)

    def test_stale_sending_becomes_uncertain_after_restart(self):
        s = C.save_sale(self.finance, None, self.header(self.ferreteria), [{'product': self.prod_a, 'quantity': 1}], tok())
        Simulator.fail_next = 'timeout'
        with self.captureOnCommitCallbacks(execute=True):
            C.issue_sale(self.finance, s.pk, tok())
        Submission.objects.update(status='sending', sent_at=timezone.now() - timedelta(hours=1))
        contable.process_pending()
        self.assertEqual(Sale.objects.get(pk=s.pk).status, Sale.Status.ISSUED)
        self.assertEqual(SimulatedDocument.objects.filter(kind='invoice').count(), 1)

    def test_idempotency_key_format(self):
        key = contable.new_key(Submission.Kind.INVOICE)
        self.assertEqual(len(key), 30)
        self.assertTrue(key.isalnum())

    def test_prior_delivery_is_linked_not_discounted_twice(self):
        batch = Batch.objects.get(product=self.prod_b, lot='L2')
        m = Movement.objects.create(batch=batch, quantity=-3, kind='dispatch', note='Entrega previa', actor=self.users['manager'])
        Batch.objects.filter(pk=batch.pk).update(physical=47)
        s = C.save_sale(self.finance, None, self.header(self.rojas), [{'product': self.prod_b, 'quantity': 3, 'prior_movement': m}], tok())
        with self.captureOnCommitCallbacks(execute=True):
            C.issue_sale(self.finance, s.pk, tok())
        self.assertEqual(Batch.objects.get(pk=batch.pk).physical, 47)
        self.assertEqual(Sale.objects.get(pk=s.pk).status, Sale.Status.ISSUED)
        with self.assertRaisesMessage(ValidationError, 'ya está vinculada'):
            C.save_sale(self.finance, None, self.header(self.rojas), [{'product': self.prod_b, 'quantity': 3, 'prior_movement': m}], tok())


class Bank(Base):
    def test_identical_rows_and_overlapping_statements(self):
        row = [TODAY().strftime('%d/%m/%Y'), 'CONSIGNACION', '', '', '', '50.000', '']
        imp = self.statement([row, row])
        self.assertEqual((imp.new_rows, BankTransaction.objects.count()), (2, 2))
        self.assertEqual(self.statement([row, row]).new_rows, 0)
        self.assertEqual(self.statement([row, row, row]).new_rows, 1)
        self.assertEqual(BankTransaction.objects.count(), 3)

    def test_overapply_blocked_excess_and_reversal(self):
        sale = self.sale(self.ferreteria, [(self.prod_a, 1, None, '')])
        self.statement([[TODAY().strftime('%d/%m/%Y'), 'PAGO FACTURA ' + sale.fiscal_name, '', '', '', '150.000', '']])
        txn = BankTransaction.objects.get()
        result = C.suggestions(txn)
        self.assertEqual((result['decision'], result['candidates'][0]['party']), ('suggested', self.ferreteria))
        with self.assertRaisesMessage(ValidationError, 'supera su saldo'):
            C.apply_transaction(self.finance, txn.pk, self.ferreteria.pk, [(sale.receivable.pk, Decimal('120000'))], tok())
        with self.captureOnCommitCallbacks(execute=True):
            C.apply_transaction(self.finance, txn.pk, self.ferreteria.pk, [(sale.receivable.pk, Decimal('100000'))], tok())
        txn.refresh_from_db()
        self.assertEqual((txn.status, C.remaining(txn)), (BankTransaction.Status.IDENTIFIED, Decimal('50000.00')))
        app = PaymentApplication.objects.get(proposal='instruction')
        C.reverse_application(self.finance, app.pk, 'Error de digitación', tok())
        txn.refresh_from_db()
        self.assertEqual((txn.status, balance(sale.receivable)), (BankTransaction.Status.PENDING, Decimal('100000.00')))

    def test_rejected_voucher_reverts_application(self):
        sale = self.sale(self.ferreteria, [(self.prod_a, 1, None, '')])
        self.statement([[TODAY().strftime('%d/%m/%Y'), 'ABONO', '', '', '', '100000', '']])
        txn = BankTransaction.objects.get()
        self.account.contable_payment_id = '999'  # forma de pago inexistente en el sistema contable
        self.account.save()
        with self.captureOnCommitCallbacks(execute=True):
            voucher = C.apply_transaction(self.finance, txn.pk, self.ferreteria.pk, [(sale.receivable.pk, Decimal('100000'))], tok())
        voucher.refresh_from_db()
        txn.refresh_from_db()
        self.assertEqual((voucher.status, txn.status, balance(sale.receivable)), ('rejected', BankTransaction.Status.PENDING, Decimal('100000.00')))

    def test_counter_collection_reconciled_without_double_application(self):
        sale = self.sale(self.ferreteria, [(self.prod_a, 1, None, '')], condition='cash')
        self.statement([[TODAY().strftime('%d/%m/%Y'), 'ABONO DATAFONO', '', '', '', '100000', '']])
        txn = BankTransaction.objects.get()
        result = C.suggestions(txn)
        self.assertEqual(len(result['counter']), 1)
        C.link_counter(self.finance, txn.pk, [result['counter'][0].pk], tok())
        txn.refresh_from_db()
        self.assertEqual((txn.status, PaymentApplication.objects.count(), balance(sale.receivable)), (BankTransaction.Status.APPLIED, 1, Decimal('0.00')))

    def test_permissions_and_mark_other(self):
        self.statement([[TODAY().strftime('%d/%m/%Y'), 'INTERESES', '', '', '', '1.200', '']])
        txn = BankTransaction.objects.get()
        with self.assertRaises(PermissionDenied):
            C.mark_other(self.billing, txn.pk, 'Intereses', tok())
        C.mark_other(self.finance, txn.pk, 'Rendimientos financieros', tok())
        self.assertEqual(BankTransaction.objects.get().status, BankTransaction.Status.OTHER)

    def test_xlsx_with_debit_credit_columns(self):
        import io
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['Banco Ejemplo'])
        ws.append(['Fecha', 'Concepto', 'Débito', 'Crédito', 'Saldo'])
        ws.append([timezone.now().replace(tzinfo=None), 'ABONO TRANSFERENCIA', None, 250000, 1250000])
        ws.append([timezone.now().replace(tzinfo=None), 'PAGO PROVEEDOR', 80000, None, 1170000])
        buf = io.BytesIO()
        wb.save(buf)
        imp = C.confirm_statement(self.finance, C.preview_statement(self.finance, self.account, 'movimientos.xlsx', buf.getvalue(), tok()).pk, tok())
        self.assertEqual(imp.new_rows, 2)
        amounts = sorted(BankTransaction.objects.values_list('amount', 'status'))
        self.assertEqual(amounts, [(Decimal('-80000.00'), 'other'), (Decimal('250000.00'), 'pending')])

    def test_money_parsing(self):
        cases = {'350.000,00': '350000.00', '$ 1.234.567': '1234567.00', '1,234.50': '1234.50', '-50.000': '-50000.00', '(2.500)': '-2500.00', '99.5': '99.50'}
        for raw, expected in cases.items():
            self.assertEqual(C.parse_money(raw), Decimal(expected), raw)

    def test_statement_form_preselects_the_only_account_and_explains_what_is_missing(self):
        from .forms import StatementForm
        form = StatementForm()
        self.assertEqual((form.fields['account'].initial, form.fields['account'].empty_label), (self.account.pk, None))
        missing = StatementForm({'token': str(tok()), 'account': str(self.account.pk)}, {})
        self.assertFalse(missing.is_valid())
        self.assertIn('Elija el archivo del extracto', missing.errors['file'][0])
        BankAccount.objects.create(name='Cuenta de ahorros', bank='Banco Ejemplo', last4='9876', contable_payment_id='302')
        two = StatementForm()
        self.assertIsNone(two.fields['account'].initial)                 # con varias cuentas se elige a mano
        self.assertIsNotNone(two.fields['account'].empty_label)


@override_settings(**FAST)
class Screens(Base):
    def login(self, name):
        c = Client()
        c.force_login(self.users[name])
        s = c.session
        s['user_version'] = self.users[name].session_version
        s.save()
        return c

    def test_pages_by_role(self):
        sale = self.sale(self.ferreteria, [(self.prod_a, 1, None, '')])
        self.statement([[TODAY().strftime('%d/%m/%Y'), 'ABONO', '', '', '', '100000', '']])
        txn = BankTransaction.objects.get()
        finance = self.login('finance')
        for url in [reverse('sales'), reverse('sale_new'), reverse('sale', args=[sale.pk]), reverse('parties'), reverse('party', args=[self.ferreteria.pk]),
                    reverse('party_new'), reverse('prices'), reverse('portfolio'), reverse('bank'), reverse('bank_import'), reverse('bank_txn', args=[txn.pk]),
                    reverse('contable_status'), reverse('opening_new'), reverse('bank_accounts'), reverse('home'), reverse('operations'), reverse('alerts')]:
            self.assertEqual(finance.get(url).status_code, 200, url)
        billing = self.login('billing')
        for url in [reverse('sales'), reverse('sale_new'), reverse('parties'), reverse('prices'), reverse('operations'), reverse('alerts')]:
            self.assertEqual(billing.get(url).status_code, 200, url)
        home = billing.get(reverse('home'))
        self.assertEqual(home.status_code, 200)
        self.assertTemplateUsed(home, 'home.html')
        self.assertEqual(home.context['indicators'][0]['label'], 'Borradores y rechazadas')
        self.assertContains(home, reverse('sale_new'))
        self.assertNotContains(home, reverse('bank'))
        for url in [reverse('bank'), reverse('portfolio'), reverse('contable_status')]:
            self.assertEqual(billing.get(url).status_code, 403, url)
        requester = self.login('requester')
        self.assertEqual(requester.get(reverse('sales')).status_code, 403)
        self.assertEqual(self.login('manager').get(reverse('parties')).status_code, 403)

    def test_sale_form_post_and_issue(self):
        c = self.login('finance')
        data = {'token': str(tok()), 'party': self.ferreteria.pk, 'context': 'contractor', 'price_list': self.lists['contractor'].pk,
                'issue_date': TODAY().isoformat(), 'seller_id': '401', 'condition': 'credit', 'payment_type_id': '204',
                'due_date': (TODAY() + timedelta(days=30)).isoformat(), 'cash_method': '', 'cash_reference': '', 'purchase_order': '', 'observations': '',
                'beneficiary_name': '', 'beneficiary_document': '', 'beneficiary_relation': '', 'internal_note': '',
                'lines-TOTAL_FORMS': '1', 'lines-INITIAL_FORMS': '0', 'lines-MIN_NUM_FORMS': '0', 'lines-MAX_NUM_FORMS': '50',
                'lines-0-product': self.prod_a.pk, 'lines-0-quantity': '2', 'lines-0-unit_price': '', 'lines-0-price_reason': '', 'lines-0-discount': '0',
                'lines-0-tax_rate': '0', 'lines-0-prior_movement': ''}
        r = c.post(reverse('sale_new'), data)
        self.assertEqual(r.status_code, 302, r.content[:2000])
        sale = Sale.objects.get()
        with self.captureOnCommitCallbacks(execute=True):
            r = c.post(reverse('sale_issue', args=[sale.pk]), {'token': str(tok())})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Sale.objects.get().status, Sale.Status.ISSUED)
        self.assertEqual(c.get(reverse('sale_pdf', args=[sale.pk])).status_code, 200)
        # El PDF del simulador se lee con formatos locales: fecha día/mes/año y pesos con punto de miles.
        pdf = b''.join(c.get(reverse('sale_pdf', args=[sale.pk]))).decode('cp1252')
        sale = Sale.objects.get()
        self.assertIn(f'Fecha: {sale.issue_date:%d/%m/%Y}', pdf)
        self.assertIn('Total: $' + f'{sale.total:,.0f}'.replace(',', '.'), pdf)
        self.assertIn('sin validez fiscal', pdf)
        self.assertNotIn(f'{sale.total:,.2f}', pdf)

    def test_simulator_money_and_date_formats(self):
        from contable.simulator import _cop, _day_first
        self.assertEqual([_cop('481236.00'), _cop('1593112.5'), _cop(0)], ['$481.236', '$1.593.112,50', '$0'])
        self.assertEqual([_day_first('2026-11-20'), _day_first('sin fecha'), _day_first('')], ['20/11/2026', 'sin fecha', ''])


class RoundedReceivableBalances(Base):
    """El saldo de cartera se redondea a centavos en la consulta: una cuota saldada con dos abonos no queda abierta."""

    def test_receivable_paid_in_two_parts_with_cents_is_closed(self):
        rec = Receivable.objects.create(party=self.ferreteria, source='opening', prefix='FE', number=7001, issue_date=TODAY() - timedelta(days=40),
                                        due_date=TODAY() - timedelta(days=10), amount=Decimal('17811372.60'), created_by=self.finance)
        col = Collection.objects.create(party=self.ferreteria, date=TODAY(), amount=Decimal('17811372.60'), method='transfer', source='manual', created_by=self.finance)
        for amount in ('17362346.40', '449026.20'):
            PaymentApplication.objects.create(collection=col, receivable=rec, amount=Decimal(amount), proposal='manual', created_by=self.finance)
        self.assertGreater(17811372.60 - (17362346.40 + 449026.20), 0)                # en coma flotante queda un residuo positivo
        self.assertEqual(balance(rec), Decimal('0.00'))
        self.assertEqual(list(C.open_receivables(self.ferreteria)), [])
        self.assertFalse(with_balance(Receivable.objects.all()).filter(balance__gt=0).exists())
