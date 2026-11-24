"""Recepción con lector de códigos: GS1 y QR de factura electrónica, conteo por lote, reglas de la recepción y perfil Recepción."""
import uuid
from datetime import date, timedelta
from decimal import Decimal
from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from . import codes, recepcion as R, services as S
from .models import User, Product, Supplier, Invoice, InvoiceLine, Batch, Movement, ProductBarcode, ReceivingSession, ReceivingScan

FAST = {'PASSWORD_HASHERS': ['django.contrib.auth.hashers.MD5PasswordHasher']}
GS = '\x1d'
TODAY = timezone.localdate
CUFE = 'a' * 32 + 'b' * 32 + 'c' * 32


def gs1_date(d):
    return d.strftime('%y%m%d')


class CodesTests(TestCase):
    def test_gtin_normalization_and_check_digit(self):
        self.assertEqual(codes.normalize_gtin('7702057012345'), None)          # dígito verificador incorrecto
        self.assertEqual(codes.normalize_gtin('4006381333931'), '04006381333931')
        self.assertEqual(codes.normalize_gtin('04006381333931'), '04006381333931')
        self.assertIsNone(codes.normalize_gtin('123'))

    def test_gs1_datamatrix_with_separator_parentheses_link_and_pipe(self):
        exp = TODAY() + timedelta(days=400)
        raw = f']d201040063813339311 7{gs1_date(exp)}10AB12-3{GS}21SER0001'.replace(' ', '')
        s = codes.parse(raw)
        self.assertEqual((s.kind, s.gtin, s.lot, s.expiry, s.serial), ('gs1', '04006381333931', 'AB12-3', exp, 'SER0001'))
        p = codes.parse(f'(01)04006381333931(17){gs1_date(exp)}(10)AB12-3(21)SER0001')
        self.assertEqual((p.gtin, p.lot, p.expiry, p.serial), (s.gtin, s.lot, s.expiry, s.serial))
        link = codes.parse(f'https://id.proveedor.example/01/04006381333931/10/AB12-3?17={gs1_date(exp)}')
        self.assertEqual((link.gtin, link.lot, link.expiry), (s.gtin, 'AB12-3', exp))
        piped = codes.parse(f'0104006381333931|10AB12-3|17{gs1_date(exp)}')
        self.assertEqual((piped.lot, piped.expiry), ('AB12-3', exp))

    def test_day_zero_is_end_of_month_and_missing_separator_heuristic(self):
        s = codes.parse(f'0104006381333931{GS}17{date.today().year % 100 + 1:02d}0200{GS}10X1')
        self.assertEqual(s.expiry.day, 28 if s.expiry.year % 4 else 29)
        exp = TODAY() + timedelta(days=200)
        glued = codes.parse(f'010400638133393110LOTE77X17{gs1_date(exp)}')
        self.assertEqual((glued.lot, glued.expiry), ('LOTE77X', exp))
        self.assertTrue(glued.warnings)

    def test_plain_barcodes_and_invoice_qr(self):
        self.assertEqual(codes.parse('4006381333931').kind, 'gtin')
        qr = f'NumFac: FE-1234\nFecFac: 2026-11-07\nNitFac: 900123456\nDocAdq: 800999111\nValTolFac: 1190000.00\nCUFE: {CUFE.upper()}\nQRCode: https://facturas.proveedor.example/document/searchqr?documentkey={CUFE}'
        s = codes.parse(qr)
        self.assertEqual(s.kind, 'invoice')
        self.assertEqual((s.invoice['number'], s.invoice['nit'], s.invoice['cufe']), ('FE-1234', '900123456', CUFE))
        url_only = codes.parse(f'https://facturas.proveedor.example/document/searchqr?documentkey={CUFE}')
        self.assertEqual(url_only.invoice['cufe'], CUFE)
        self.assertEqual(codes.parse('hola').kind, 'unknown')


@override_settings(**FAST)
class ReceptionFlowTests(TestCase):
    def setUp(self):
        mk = lambda name, role: User.objects.create_user(name, password='x' * 14, role=role, must_change_password=False)
        self.reception, self.manager, self.finance, self.requester = mk('recepcion', 'reception'), mk('inventario', 'inventory'), mk('conta', 'finance'), mk('solicitante', 'requester')
        self.vendor = Supplier.objects.create(name='Proveedor Andino S.A.S.', nit='900123456')
        self.prod = Product.objects.create(code='REF-A', name='Referencia A')
        self.prod_b = Product.objects.create(code='REF-B', name='Referencia B')
        self.exp = TODAY() + timedelta(days=500)
        self.inv = Invoice.objects.create(supplier=self.vendor, number='AND-77', issue_date=TODAY(), due_date=TODAY() + timedelta(days=60), cufe=CUFE,
                                          review=Invoice.Review.VALIDATED, total=Decimal('1000'), created_by=self.finance, validated_by=self.finance,
                                          validated_at=timezone.now())
        self.line = InvoiceLine.objects.create(invoice=self.inv, product=self.prod, description='Referencia A', quantity=20, unit_price=Decimal('10'),
                                               expected_lot='L-77', expected_expiry=self.exp)
        self.line_b = InvoiceLine.objects.create(invoice=self.inv, product=self.prod_b, description='Referencia B', quantity=4, unit_price=Decimal('10'))

    def box(self, serial, lot='L-77', exp=None, gtin='04006381333931'):
        return f'01{gtin}17{gs1_date(exp or self.exp)}10{lot}{GS}21{serial}'

    def test_find_invoice_by_qr_number_and_unregistered(self):
        self.assertEqual(R.find_invoice(f'NumFac: AND-77\nNitFac: 900123456\nCUFE: {CUFE}')[0], self.inv)
        self.assertEqual(R.find_invoice('and-77')[0], self.inv)
        inv, info, msg = R.find_invoice('NumFac: XX-9\nNitFac: 900123456\nCUFE: ' + 'd' * 96)
        self.assertIsNone(inv)
        self.assertIn('Proveedor Andino', msg)
        self.assertIn('todavía no está registrada', msg)

    def test_full_reception_with_unknown_code_serials_and_rules(self):
        s = R.open_session(self.reception, self.inv)
        self.assertEqual(R.open_session(self.reception, self.inv), s)                     # una sola sesión abierta por factura
        r = R.scan(self.reception, s.pk, self.box('S1'))
        self.assertEqual(r['kind'], 'unknown')
        R.link_code(self.reception, s.pk, r['gtin'], self.prod.pk, 10)                     # caja de 10 unidades
        self.assertEqual(R.scan(self.reception, s.pk, self.box('S1'))['kind'], 'added')
        self.assertEqual(R.scan(self.reception, s.pk, self.box('S1'))['kind'], 'duplicate')  # misma caja (serial)
        r = R.scan(self.reception, s.pk, self.box('S2', lot='L-78'))
        self.assertTrue(r['ok'] and r['warn'])
        self.assertIn('no es el facturado', r['message'])
        self.assertEqual(R.scan(self.reception, s.pk, self.box('S3'))['kind'], 'error')     # excede lo facturado (20)
        expired = R.scan(self.reception, s.pk, self.box('S4', exp=TODAY() - timedelta(days=1)))
        self.assertIn('vencido', expired['message'])
        # Referencia B: código EAN sin lote ni vencimiento → se piden una vez.
        ean = '5901234123457'
        ProductBarcode.objects.create(product=self.prod_b, gtin=codes.normalize_gtin(ean), units=1)
        need = R.scan(self.reception, s.pk, ean, boxes=2)
        self.assertEqual(need['kind'], 'needs_details')
        self.assertEqual(need['boxes'], 2)
        done = R.manual(self.reception, s.pk, self.line_b.pk, 'B-1', self.exp, 2, gtin=ean)
        self.assertEqual(done['kind'], 'added')
        summary = R.summary(s)
        self.assertEqual(summary['rows'][0]['counted'], 20)
        self.assertTrue(summary['different'])
        with self.assertRaisesMessage(Exception, 'Explique'):
            R.confirm(self.reception, s.pk, TODAY(), 'Bodega 1', '', uuid.uuid4())         # parcial y con lote distinto: exige nota
        R.confirm(self.reception, s.pk, TODAY(), 'Bodega 1', 'Llega el lote L-78 en una caja; faltan 2 de B', uuid.uuid4())
        s.refresh_from_db()
        self.assertEqual(s.status, 'confirmed')
        self.assertEqual(Batch.objects.get(product=self.prod, lot='L-77').physical, 10)
        self.assertEqual(Batch.objects.get(product=self.prod, lot='L-78').physical, 10)
        self.line.refresh_from_db()
        self.assertEqual(self.line.received, 20)
        self.assertEqual(Movement.objects.filter(kind='receipt', receipt=s.receipt).count(), 3)
        # Faltan 2 de B: la factura sigue por recibir, y la misma caja no puede volver a contarse en otra recepción.
        s2 = R.open_session(self.reception, self.inv)
        self.assertNotEqual(s2.pk, s.pk)
        self.assertEqual(R.scan(self.reception, s2.pk, self.box('S1'))['kind'], 'duplicate')

    def test_invoice_must_be_validated_and_permissions(self):
        self.inv.review = Invoice.Review.PENDING
        self.inv.save()
        with self.assertRaisesMessage(Exception, 'validada'):
            R.open_session(self.reception, self.inv)
        self.inv.review = Invoice.Review.VALIDATED
        self.inv.save()
        for user in (self.requester, self.finance):
            with self.assertRaises(PermissionDenied):
                R.open_session(user, self.inv)
        self.assertTrue(R.open_session(self.manager, self.inv))

    def login(self, user):
        c = Client()
        c.force_login(user)
        s = c.session
        s['user_version'] = user.session_version
        s.save()
        return c

    def test_pages_json_scan_and_reception_profile(self):
        c = self.login(self.reception)
        home = c.get(reverse('home'))
        self.assertTemplateUsed(home, 'home.html')
        self.assertEqual((home.context['k_pending'], home.context['k_units']), (1, 24))
        self.assertContains(home, 'AND-77')
        self.assertEqual(c.get(reverse('reception')).status_code, 200)
        for name in ('stock', 'invoices', 'payables', 'portfolio', 'replenishment'):
            self.assertEqual(c.get(reverse(name)).status_code, 403, name)
        self.assertEqual(self.login(self.requester).get(reverse('reception')).status_code, 403)
        resp = c.post(reverse('reception_find'), {'codigo': f'NumFac: AND-77\nNitFac: 900123456\nCUFE: {CUFE}'})
        self.assertRedirects(resp, reverse('reception_invoice', args=[self.inv.pk]))
        page = c.get(reverse('reception_invoice', args=[self.inv.pk]))
        self.assertEqual(page.status_code, 200)
        s = ReceivingSession.objects.get(invoice=self.inv, status='open')
        ProductBarcode.objects.create(product=self.prod, gtin='04006381333931', units=5)
        r = c.post(reverse('reception_scan', args=[s.pk]), {'codigo': self.box('Z1')}, HTTP_X_REQUESTED_WITH='fetch')
        data = r.json()
        self.assertEqual((data['kind'], data['ok']), ('added', True))
        self.assertEqual(sum(ReceivingScan.objects.filter(session=s).values_list('quantity', flat=True)), 5)
        alerts = [a['title'] for a in S.alerts(self.reception)]
        self.assertIn('Mercancía por recibir', alerts)
        resp = c.post(reverse('reception_confirm', args=[s.pk]), {'token': str(uuid.uuid4()), 'ubicacion': 'Bodega 2', 'fecha': TODAY().isoformat(),
                                                                  'nota': 'Llegan 5 de 20; el resto la próxima semana'})
        self.assertRedirects(resp, reverse('reception'))
        self.assertEqual(Batch.objects.get(product=self.prod, lot='L-77', location='Bodega 2').physical, 5)
