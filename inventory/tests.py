import io
import shutil
import tempfile
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from .einvoice import parse_invoice_xml, read_upload
from .models import (User, Product, Batch, Supplier, DiscountTier, SupplierCode, Invoice, InvoiceFile, PaymentAllocation, SupplyRequest, Movement, Operation,
                     Audit, nit_check_digit, plural_unit)
from . import services as S
from .testing import new_password

PASSWORD = new_password()
NEW_PASSWORD = new_password()
FAST = {'PASSWORD_HASHERS': ['django.contrib.auth.hashers.MD5PasswordHasher']}
TODAY = timezone.localdate


def tok():
    return uuid.uuid4()


def fixture():
    users = {name: User.objects.create_user(name, password=PASSWORD, role=role, must_change_password=False, first_name=name.title())
             for name, role in [('requester', 'requester'), ('second', 'requester'), ('manager', 'inventory'), ('finance', 'finance'), ('admin', 'admin')]}
    product = Product.objects.create(code='REF-A', name='Referencia A', minimum=20)
    other = Product.objects.create(code='REF-B', name='Referencia B')
    batch = Batch.objects.create(product=product, lot='L01', expiry=TODAY() + timedelta(days=200), location='Bodega principal', physical=100)
    supplier = Supplier.objects.create(name='Proveedor Uno S.A.S.', nit='900123456', payment_days=60)
    DiscountTier.objects.bulk_create([DiscountTier(supplier=supplier, days=15, percent=Decimal('4')), DiscountTier(supplier=supplier, days=30, percent=Decimal('2'))])
    return users, product, other, batch, supplier


def ubl_invoice(number='FE-1001', nit='900123456', name='Proveedor Uno S.A.S.', cufe='abc123', lines=(('COD-A', 'REFERENCIA A BROCHA', '10', '1000.00', '0', '0'),),
                lot=True, issue=None):
    issue = issue or TODAY().isoformat()
    body = ''.join(f'''<cac:InvoiceLine><cbc:ID>{i + 1}</cbc:ID><cbc:InvoicedQuantity unitCode="94">{q}</cbc:InvoicedQuantity>
      <cbc:LineExtensionAmount currencyID="COP">0</cbc:LineExtensionAmount>
      {f'<cac:AllowanceCharge><cbc:ChargeIndicator>false</cbc:ChargeIndicator><cbc:Amount currencyID="COP">{d}</cbc:Amount></cac:AllowanceCharge>' if d != '0' else ''}
      <cac:TaxTotal><cbc:TaxAmount currencyID="COP">0</cbc:TaxAmount><cac:TaxSubtotal><cac:TaxCategory><cbc:Percent>{t}</cbc:Percent></cac:TaxCategory></cac:TaxSubtotal></cac:TaxTotal>
      <cac:Item><cbc:Description>{desc}</cbc:Description><cac:SellersItemIdentification><cbc:ID>{code}</cbc:ID></cac:SellersItemIdentification>
      {'<cac:ItemInstance><cac:LotIdentification><cbc:LotNumberID>lx9</cbc:LotNumberID><cbc:ExpiryDate>2028-05-31</cbc:ExpiryDate></cac:LotIdentification></cac:ItemInstance>' if lot else ''}</cac:Item>
      <cac:Price><cbc:PriceAmount currencyID="COP">{p}</cbc:PriceAmount><cbc:BaseQuantity unitCode="94">1</cbc:BaseQuantity></cac:Price></cac:InvoiceLine>'''
                   for i, (code, desc, q, p, d, t) in enumerate(lines))
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
<cbc:ID>{number}</cbc:ID><cbc:UUID schemeName="CUFE-SHA384">{cufe}</cbc:UUID><cbc:IssueDate>{issue}</cbc:IssueDate><cbc:DueDate>{(TODAY() + timedelta(days=30)).isoformat()}</cbc:DueDate>
<cbc:DocumentCurrencyCode>COP</cbc:DocumentCurrencyCode><cac:OrderReference><cbc:ID>OC-77</cbc:ID></cac:OrderReference>
<cac:AccountingSupplierParty><cac:Party><cac:PartyTaxScheme><cbc:RegistrationName>{name}</cbc:RegistrationName><cbc:CompanyID schemeID="{nit_check_digit(nit)}" schemeName="31">{nit}</cbc:CompanyID></cac:PartyTaxScheme></cac:Party></cac:AccountingSupplierParty>
<cac:WithholdingTaxTotal><cbc:TaxAmount currencyID="COP">25.00</cbc:TaxAmount></cac:WithholdingTaxTotal>
<cac:LegalMonetaryTotal><cbc:PayableAmount currencyID="COP">9975.00</cbc:PayableAmount></cac:LegalMonetaryTotal>
{body}</Invoice>'''.encode('utf-8')


def attached(inner):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<AttachedDocument xmlns="urn:oasis:names:specification:ubl:schema:xsd:AttachedDocument-2" xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
<cac:Attachment><cac:ExternalReference><cbc:MimeCode>text/xml</cbc:MimeCode><cbc:Description><![CDATA[{inner.decode('utf-8')}]]></cbc:Description></cac:ExternalReference></cac:Attachment></AttachedDocument>'''.encode('utf-8')


class TempStorage:
    """Redirige adjuntos y archivos privados a una carpeta temporal durante la prueba."""
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        field = InvoiceFile._meta.get_field('file')
        self._storage = field.storage
        field.storage = FileSystemStorage(location=self.tmp / 'adjuntos')
        self._override = override_settings(PRIVATE_DIR=self.tmp)
        self._override.enable()

    def tearDown(self):
        self._override.disable()
        InvoiceFile._meta.get_field('file').storage = self._storage
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()


@override_settings(**FAST)
class Base(TempStorage, TestCase):
    def setUp(self):
        super().setUp()
        self.u, self.product, self.other, self.batch, self.supplier = fixture()

    def login_as(self, user, client=None):
        client = client or self.client
        client.force_login(user)
        session = client.session
        session['user_version'] = user.session_version
        session.save()
        return client

    def header(self, **kw):
        data = {'supplier': self.supplier, 'number': 'FE-1', 'issue_date': TODAY() - timedelta(days=5), 'due_date': None, 'ordered_on': None,
                'order_number': '', 'cufe': '', 'global_discount': Decimal(0), 'retention': Decimal(0), 'notes': ''}
        data.update(kw)
        return data

    def line(self, product=None, qty=50, price='1000', **kw):
        data = {'product': product or self.product, 'supplier_code': 'C-1', 'description': '', 'quantity': qty, 'unit_price': Decimal(price),
                'discount': Decimal(0), 'tax_rate': Decimal(0), 'expected_lot': '', 'expected_expiry': None}
        data.update(kw)
        return data

    def invoice(self, validated=True, **kw):
        lines = kw.pop('lines', None) or [self.line()]
        inv = S.create_invoice(self.u['manager'], self.header(**kw), lines, [], tok())
        if validated:
            S.validate_invoice(self.u['finance'], inv.pk, tok())
        inv.refresh_from_db()
        return inv


class AccessTests(Base):
    def test_all_pages_require_authentication(self):
        # La raíz sin sesión lleva a la presentación pública (feria/test_presentacion.py); todo lo demás pide sesión.
        for url in ['/operaciones/', '/inventario/', '/solicitudes/', '/alertas/', '/usuarios/', '/facturas/', '/cuentas-por-pagar/', '/proveedores/', '/auditoria/', '/productos/']:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith('/ingresar/'))

    def test_requester_cannot_reach_stock_purchase_or_admin(self):
        self.login_as(self.u['requester'])
        for url in ['/inventario/', '/inventario/salida/', '/inventario/reserva/', '/inventario/baja/', '/inventario/entrada/', '/inventario/movimientos/',
                    '/facturas/', '/facturas/nueva/', '/facturas/importar/', '/cuentas-por-pagar/', '/proveedores/', '/productos/nuevo/',
                    '/reservas/', '/usuarios/', '/auditoria/', '/alertas/configuracion/']:
            for method in ('get', 'post'):
                with self.subTest(url=url, method=method):
                    self.assertEqual(getattr(self.client, method)(url).status_code, 403)

    def test_separation_between_inventory_and_finance(self):
        inv = self.invoice()
        self.login_as(self.u['manager'])
        for url in [f'/facturas/{inv.pk}/pagar/', '/cuentas-por-pagar/', f'/proveedores/{self.supplier.pk}/', '/usuarios/', '/auditoria/']:
            with self.subTest(role='inventory', url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
        self.login_as(self.u['finance'])
        for url in [f'/facturas/{inv.pk}/recibir/', '/inventario/', '/inventario/salida/', '/reservas/', '/productos/nuevo/', '/usuarios/']:
            with self.subTest(role='finance', url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_every_page_renders_for_its_roles(self):
        inv = self.invoice()
        pending = self.invoice(validated=False, number='FE-2')
        r = S.request_stock(self.u['requester'], self.product, 5, 'Obra norte', tok())
        res = S.reserve(self.u['manager'], self.batch.pk, 3, 'Despacho', tok())
        pages = {
            'requester': ['/', '/solicitudes/', '/solicitudes/nueva/', '/alertas/', '/cuenta/clave/', f'/solicitudes/{r.pk}/cancel/'],
            'manager': ['/', '/inventario/', '/inventario/?ver=todos', '/inventario/movimientos/', '/inventario/entrada/', '/inventario/baja/', '/inventario/salida/',
                        '/inventario/reserva/', f'/inventario/lote/{self.batch.pk}/', '/productos/', '/productos/nuevo/', f'/productos/{self.product.pk}/',
                        '/solicitudes/', f'/solicitudes/{r.pk}/aprobar/', f'/solicitudes/{r.pk}/reject/', '/reservas/', '/reservas/?ver=todas',
                        f'/reservas/{res.pk}/dispatch/', f'/reservas/{res.pk}/release/', '/facturas/', '/facturas/?vista=revisar', '/facturas/?vista=recibir',
                        '/facturas/?vista=pagar', '/facturas/nueva/', '/facturas/importar/', f'/facturas/{inv.pk}/', f'/facturas/{inv.pk}/recibir/',
                        f'/facturas/{pending.pk}/corregir/', f'/facturas/{pending.pk}/anular/', '/proveedores/', '/proveedores/nuevo/', '/alertas/', '/alertas/configuracion/'],
            'finance': ['/', '/facturas/', f'/facturas/{inv.pk}/', f'/facturas/{inv.pk}/pagar/', '/cuentas-por-pagar/', '/proveedores/',
                        f'/proveedores/{self.supplier.pk}/', '/productos/', '/alertas/'],
            'admin': ['/', '/usuarios/', '/usuarios/nuevo/', f'/usuarios/{self.u["requester"].pk}/clave/', f'/usuarios/{self.u["requester"].pk}/deactivate/',
                      '/auditoria/', '/cuentas-por-pagar/', '/inventario/'],
        }
        for role, urls in pages.items():
            self.login_as(self.u[role])
            for url in urls:
                with self.subTest(role=role, url=url):
                    self.assertEqual(self.client.get(url).status_code, 200)

    def test_real_login_logout_and_error_pages(self):
        self.assertEqual(self.client.get('/ingresar/').status_code, 200)
        self.assertEqual(self.client.get('/ingresar/recuperar/').status_code, 200)
        self.assertEqual(self.client.post('/ingresar/', {'username': 'requester', 'password': PASSWORD}).status_code, 302)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/salir/').status_code, 405)
        self.assertEqual(self.client.post('/salir/').status_code, 302)
        self.assertEqual(self.client.get('/').status_code, 302)
        bad = self.client.post('/ingresar/', {'username': 'requester', 'password': 'equivocada'})
        self.assertEqual(bad.status_code, 200)
        self.assertTrue(bad.context['form'].errors)
        self.assertEqual(self.client.get('/').status_code, 302)
        self.login_as(self.u['requester'])
        self.assertEqual(self.client.get('/no-existe/').status_code, 404)
        self.assertEqual(self.client.get('/usuarios/').status_code, 403)

    def test_password_change_required_and_other_session_invalidated(self):
        requester = self.u['requester']
        requester.must_change_password = True
        requester.save()
        other = self.login_as(requester, Client())
        self.login_as(requester)
        self.assertRedirects(self.client.get('/'), '/cuenta/clave/', fetch_redirect_response=False)
        response = self.client.post('/cuenta/clave/', {'old_password': PASSWORD, 'new_password1': NEW_PASSWORD, 'new_password2': NEW_PASSWORD})
        self.assertEqual(response.status_code, 302)
        requester.refresh_from_db()
        self.assertFalse(requester.must_change_password)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertTrue(other.get('/alertas/').url.startswith('/ingresar/'))

    def test_weak_password_rejected(self):
        self.login_as(self.u['requester'])
        response = self.client.post('/cuenta/clave/', {'old_password': PASSWORD, 'new_password1': '123456', 'new_password2': '123456'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.get(username='requester').check_password(PASSWORD))

    def test_login_throttle_by_account(self):
        for _ in range(5):
            self.client.post('/ingresar/', {'username': 'requester', 'password': 'mala'})
        response = self.client.post('/ingresar/', {'username': 'requester', 'password': PASSWORD})
        self.assertEqual(response.status_code, 429)
        self.assertTrue(response.context['limited'])
        self.assertEqual(response['Retry-After'], '900')

    def test_csrf_is_enforced(self):
        client = self.login_as(self.u['requester'], Client(enforce_csrf_checks=True))
        response = client.post('/solicitudes/nueva/', {'product': self.product.pk, 'quantity': 1, 'purpose': 'x', 'token': tok()})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SupplyRequest.objects.exists())

    def test_user_content_is_escaped(self):
        S.request_stock(self.u['requester'], self.product, 1, '<script>alert(1)</script>', tok())
        self.login_as(self.u['manager'])
        response = self.client.get('/solicitudes/')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, '&lt;script&gt;')

    def test_security_headers(self):
        self.login_as(self.u['manager'])
        response = self.client.get('/')
        self.assertIn("script-src 'self'", response['Content-Security-Policy'])
        self.assertEqual(response['Cache-Control'], 'no-store, private')
        self.assertEqual(response['X-Frame-Options'], 'DENY')

    def test_admin_deactivation_revokes_sessions(self):
        other = self.login_as(self.u['requester'], Client())
        self.login_as(self.u['admin'])
        self.assertEqual(self.client.post(f'/usuarios/{self.u["requester"].pk}/deactivate/', {'token': tok()}).status_code, 302)
        self.assertTrue(other.get('/alertas/').url.startswith('/ingresar/'))
        self.assertEqual(self.client.post(f'/usuarios/{self.u["admin"].pk}/deactivate/', {'token': tok()}).status_code, 400)
        self.assertEqual(self.client.post(f'/usuarios/{self.u["requester"].pk}/activate/', {'token': tok()}).status_code, 302)
        self.assertTrue(User.objects.get(pk=self.u['requester'].pk).is_active)

    def test_admin_creates_account_with_forced_change(self):
        self.login_as(self.u['admin'])
        response = self.client.post('/usuarios/nuevo/', {'username': 'nueva.persona', 'first_name': 'Nueva', 'last_name': 'Persona', 'email': '', 'role': 'finance',
                                                          'password1': NEW_PASSWORD, 'password2': NEW_PASSWORD, 'token': tok()})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.get(username='nueva.persona').must_change_password)


class InventoryTests(Base):
    def test_request_approve_deliver_flow(self):
        r = S.request_stock(self.u['requester'], self.product, 20, 'Obra norte', tok())
        self.batch.refresh_from_db()
        self.assertEqual((self.batch.physical, self.batch.reserved), (100, 0))
        self.assertEqual(S.suggested_batch(self.product, 20), self.batch)
        res = S.approve(self.u['manager'], r.pk, self.batch.pk, tok())
        self.batch.refresh_from_db()
        self.assertEqual((self.batch.physical, self.batch.reserved, self.batch.available), (100, 20, 80))
        S.finish_reservation(self.u['manager'], res.pk, 'dispatch', tok())
        self.batch.refresh_from_db()
        r.refresh_from_db()
        self.assertEqual((self.batch.physical, self.batch.reserved, r.status), (80, 0, 'delivered'))
        self.assertEqual(Movement.objects.get(kind='dispatch').quantity, -20)

    def test_release_reject_cancel(self):
        r = S.request_stock(self.u['requester'], self.product, 10, 'x', tok())
        res = S.approve(self.u['manager'], r.pk, self.batch.pk, tok())
        S.finish_reservation(self.u['manager'], res.pk, 'release', tok())
        self.batch.refresh_from_db()
        self.assertEqual((self.batch.physical, self.batch.reserved), (100, 0))
        r2 = S.request_stock(self.u['requester'], self.product, 10, 'x', tok())
        with self.assertRaises(ValidationError):
            S.resolve_request(self.u['manager'], r2.pk, 'reject', ' ', tok())
        S.resolve_request(self.u['manager'], r2.pk, 'reject', 'Sin existencias suficientes', tok())
        r3 = S.request_stock(self.u['requester'], self.product, 10, 'x', tok())
        with self.assertRaises(PermissionDenied):
            S.resolve_request(self.u['second'], r3.pk, 'cancel', '', tok())
        S.resolve_request(self.u['requester'], r3.pk, 'cancel', '', tok())
        self.assertEqual(SupplyRequest.objects.get(pk=r3.pk).status, 'cancelled')

    def test_requesters_only_see_their_requests(self):
        mine = S.request_stock(self.u['requester'], self.product, 1, 'propia', tok())
        S.request_stock(self.u['second'], self.product, 1, 'ajena', tok())
        self.login_as(self.u['second'])
        self.assertNotContains(self.client.get('/solicitudes/'), 'propia')
        self.assertEqual(self.client.get(f'/solicitudes/{mine.pk}/cancel/').status_code, 404)

    def test_reservation_cannot_exceed_available(self):
        S.reserve(self.u['manager'], self.batch.pk, 90, 'x', tok())
        with self.assertRaises(ValidationError):
            S.reserve(self.u['manager'], self.batch.pk, 11, 'x', tok())
        with self.assertRaises(ValidationError):
            S.exit_stock(self.u['manager'], self.batch.pk, 11, 'REM-1', '', tok())
        S.exit_stock(self.u['manager'], self.batch.pk, 10, 'REM-1', 'Obra', tok())
        self.batch.refresh_from_db()
        self.assertEqual((self.batch.physical, self.batch.reserved, self.batch.available), (90, 90, 0))

    def test_double_submit_is_rejected_atomically(self):
        token = tok()
        S.exit_stock(self.u['manager'], self.batch.pk, 5, 'REM-1', '', token)
        with self.assertRaises(ValidationError):
            S.exit_stock(self.u['manager'], self.batch.pk, 5, 'REM-1', '', token)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.physical, 95)
        failed = tok()
        with self.assertRaises(ValidationError):
            S.exit_stock(self.u['manager'], self.batch.pk, 500, 'REM-2', '', failed)
        self.assertFalse(Operation.objects.filter(token=failed).exists())

    def test_expired_lot_blocks_exit_but_allows_writeoff(self):
        old = Batch.objects.create(product=self.product, lot='VIEJO', expiry=TODAY() - timedelta(days=1), location='Bodega principal', physical=8)
        self.assertEqual(old.available, 0)
        with self.assertRaises(ValidationError):
            S.exit_stock(self.u['manager'], old.pk, 1, 'REM-9', '', tok())
        with self.assertRaises(ValidationError):
            S.adjust_out(self.u['manager'], old.pk, 8, 'writeoff', '', 'Vencido', tok())
        S.adjust_out(self.u['manager'], old.pk, 8, 'writeoff', 'expiry', 'Vencido según inspección', tok())
        old.refresh_from_db()
        self.assertEqual(old.physical, 0)
        self.assertEqual(Movement.objects.get(kind='writeoff').loss_reason, 'expiry')
        self.assertEqual(sum(p.available for p in S.availability() if p.pk == self.product.pk), 100)

    def test_initial_balance_and_lot_consistency(self):
        S.adjust_in(self.u['manager'], self.other, 'b-7', TODAY() + timedelta(days=90), 'Bodega 1', 12, 'initial', '', tok())
        self.assertEqual(Batch.objects.get(product=self.other).lot, 'B-7')
        with self.assertRaises(ValidationError):
            S.adjust_in(self.u['manager'], self.other, 'B-7', TODAY() + timedelta(days=91), 'Bodega 2', 1, 'initial', '', tok())
        with self.assertRaises(ValidationError):
            S.adjust_in(self.u['manager'], self.other, 'B-8', TODAY() + timedelta(days=91), 'Bodega 2', 1, 'adjust_in', '  ', tok())
        position = {p.code: p for p in S.stock_position()}
        self.assertEqual((position['REF-B'].available, position['REF-A'].available), (12, 100))

    def test_units_agree_with_quantities(self):
        cases = {'unidad': 'unidades', 'galón': 'galones', 'cartucho': 'cartuchos', 'rollo': 'rollos', 'caja de 12': 'cajas de 12', 'par': 'pares',
                 'kg': 'kg', 'unidades': 'unidades', '': 'unidades', 'lápiz': 'lápices'}
        for unit, plural in cases.items():
            self.assertEqual(plural_unit(unit), plural, unit)
        self.assertEqual((self.product.unit, self.product.qty(1), self.product.qty(24)), ('unidad', '1 unidad', '24 unidades'))

    def test_category_filters_catalog_and_stock(self):
        Product.objects.filter(pk=self.product.pk).update(category='adhesivos')
        Batch.objects.create(product=self.other, lot='S1', expiry=TODAY() + timedelta(days=90), location='Bodega principal', physical=4)
        self.login_as(self.u['manager'])
        page = self.client.get('/productos/?categoria=adhesivos')
        self.assertEqual(([p.code for p in page.context['products']], [p.code for p in page.context['rows']], page.context['category']),
                         (['REF-A'], ['REF-A'], 'adhesivos'))
        self.assertEqual(len(self.client.get('/productos/?categoria=no-existe').context['products']), 2)      # un valor inválido no filtra
        stock = self.client.get('/inventario/?categoria=adhesivos')
        self.assertEqual([b.lot for b in stock.context['batches']], ['L01'])
        self.assertEqual({b.lot for b in self.client.get('/inventario/?categoria=otros').context['batches']}, {'S1'})
        self.assertEqual(dict(stock.context['categories'])['sellantes'], 'Sellantes')

    def test_stock_position_counts_transit_and_consumption(self):
        self.invoice(lines=[self.line(qty=30)])
        S.exit_stock(self.u['manager'], self.batch.pk, 9, 'REM-1', '', tok())
        p = {p.code: p for p in S.stock_position()}['REF-A']
        self.assertEqual((p.physical, p.available, p.in_transit, p.position, p.used_90), (91, 91, 30, 121, 9))
        self.assertEqual(p.coverage, round(91 / (9 / 90)))

    def test_forms_through_http(self):
        self.login_as(self.u['manager'])
        response = self.client.post('/inventario/entrada/', {'kind': 'initial', 'product': self.other.pk, 'lot': 'N1', 'expiry': (TODAY() + timedelta(days=60)).isoformat(),
                                                             'location': 'Bodega 1', 'quantity': 7, 'note': '', 'token': tok()})
        self.assertRedirects(response, '/inventario/', fetch_redirect_response=False)
        response = self.client.post('/inventario/salida/', {'batch': self.batch.pk, 'quantity': 3, 'reference': 'REM-55', 'note': '', 'token': tok()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Movement.objects.get(kind='exit').reference, 'REM-55')
        response = self.client.post('/productos/nuevo/', {'code': 'REF-C', 'name': 'Referencia C', 'category': 'adhesivos', 'unit': 'cartucho', 'controlled_storage': 'on', 'active': 'on', 'token': tok()})
        self.assertEqual(response.status_code, 302)
        created = Product.objects.get(code='REF-C')
        self.assertEqual((created.category, created.units, created.qty(1), created.qty(3)), ('adhesivos', 'cartuchos', '1 cartucho', '3 cartuchos'))
        response = self.client.post('/alertas/configuracion/', {'expiry_days': 30, 'discount_alert_days': 3, 'due_alert_days': 5, 'token': tok()})
        self.assertEqual(response.status_code, 302)
        csv = self.client.get('/inventario/movimientos.csv')
        self.assertEqual(csv['Content-Type'], 'text/csv; charset=utf-8')
        self.assertTrue(csv.content.decode('utf-8').startswith('﻿Fecha;Tipo;Código'))


class InvoiceTests(Base):
    def test_totals_with_discount_tax_and_retention(self):
        inv = S.create_invoice(self.u['manager'], self.header(global_discount=Decimal('500'), retention=Decimal('250')),
                               [self.line(qty=10, price='1000', discount=Decimal('1000'), tax_rate=Decimal('19')), self.line(self.other, qty=5, price='0')], [], tok())
        self.assertEqual((inv.subtotal, inv.tax, inv.total), (Decimal('9000.00'), Decimal('1710.00'), Decimal('9960.00')))
        self.assertEqual(inv.due_date, inv.issue_date + timedelta(days=60))
        self.assertEqual(inv.review, 'pending')
        self.assertEqual(SupplierCode.objects.get(supplier=self.supplier, code='C-1').product, self.other)

    def test_duplicates_and_dates_are_rejected(self):
        self.invoice(validated=False, cufe='CUFE-1')
        with self.assertRaisesMessage(ValidationError, 'Ya existe la factura'):
            self.invoice(validated=False, number='fe-1')
        with self.assertRaisesMessage(ValidationError, 'CUFE'):
            self.invoice(validated=False, number='FE-9', cufe='cufe-1')
        with self.assertRaises(ValidationError):
            self.invoice(validated=False, number='FE-10', issue_date=TODAY() + timedelta(days=1))
        with self.assertRaises(ValidationError):
            self.invoice(validated=False, number='FE-11', lines=[self.line(qty=1, price='10', discount=Decimal('11'))])
        self.assertEqual(Invoice.objects.count(), 1)

    def test_receipt_requires_validation_then_multilot_partial(self):
        inv = self.invoice(validated=False, lines=[self.line(qty=50, expected_lot='X1'), self.line(self.other, qty=20)])
        a, b = list(inv.lines.all())
        rows = [{'line': a.pk, 'quantity': 30, 'lot': 'x1', 'expiry': TODAY() + timedelta(days=300), 'location': 'Bodega principal'}]
        with self.assertRaisesMessage(ValidationError, 'Valide la factura'):
            S.receive_invoice(self.u['manager'], inv.pk, rows, TODAY(), '', tok())
        S.validate_invoice(self.u['finance'], inv.pk, tok())
        with self.assertRaisesMessage(ValidationError, 'Explique'):
            S.receive_invoice(self.u['manager'], inv.pk, rows, TODAY(), '', tok())
        rows.append({'line': a.pk, 'quantity': 10, 'lot': 'X2', 'expiry': TODAY() + timedelta(days=320), 'location': 'Bodega 1'})
        S.receive_invoice(self.u['manager'], inv.pk, rows, TODAY(), 'Faltan 10 de A y todo B', tok())
        a.refresh_from_db()
        self.assertEqual((a.received, a.pending), (40, 10))
        self.assertEqual(Invoice.objects.get(pk=inv.pk).reception, 'partial')
        excess = [{'line': a.pk, 'quantity': 11, 'lot': 'X1', 'expiry': TODAY() + timedelta(days=300), 'location': 'Bodega principal'}]
        with self.assertRaisesMessage(ValidationError, 'excedentes'):
            S.receive_invoice(self.u['manager'], inv.pk, excess, TODAY(), 'x', tok())
        expired = [{'line': b.pk, 'quantity': 20, 'lot': 'Z', 'expiry': TODAY() - timedelta(days=1), 'location': 'Bodega principal'}]
        with self.assertRaisesMessage(ValidationError, 'vencido'):
            S.receive_invoice(self.u['manager'], inv.pk, expired, TODAY(), 'x', tok())
        final = [{'line': a.pk, 'quantity': 10, 'lot': 'X1', 'expiry': TODAY() + timedelta(days=300), 'location': 'Bodega principal'},
                 {'line': b.pk, 'quantity': 20, 'lot': 'B1', 'expiry': TODAY() + timedelta(days=200), 'location': 'Bodega principal'}]
        S.receive_invoice(self.u['manager'], inv.pk, final, TODAY(), '', tok())
        self.assertEqual(Invoice.objects.get(pk=inv.pk).reception, 'complete')
        self.assertEqual(Batch.objects.get(product=self.product, lot='X1').physical, 40)
        self.assertEqual(Movement.objects.filter(kind='receipt').count(), 4)
        with self.assertRaises(ValidationError):
            S.void_invoice(self.u['manager'], inv.pk, 'error', tok())

    def test_discount_tiers_and_terms_frozen_at_validation(self):
        inv = self.invoice(lines=[self.line(qty=10, price='1000', tax_rate=Decimal('19'))], issue_date=TODAY() - timedelta(days=20))
        self.assertEqual(inv.terms['tiers'], [[15, '4.00'], [30, '2.00']])
        self.supplier.tiers.all().delete()
        inv.refresh_from_db()
        day = lambda n: inv.issue_date + timedelta(days=n)
        self.assertEqual(S.discount_for(inv, day(15))['percent'], Decimal('4.00'))
        self.assertEqual(S.discount_for(inv, day(16))['percent'], Decimal('2.00'))
        self.assertEqual(S.discount_for(inv, day(31))['amount'], Decimal(0))
        d = S.discount_for(inv, day(20))
        self.assertEqual((d['base'], d['amount'], d['potential'], d['lost'], d['due']), (Decimal('10000.00'), Decimal('200.00'), Decimal('400.00'), Decimal('200.00'), Decimal('11700.00')))
        self.assertEqual(d['deadline'], day(30))

    def test_payment_rules(self):
        pending = self.invoice(validated=False, number='FE-P')
        with self.assertRaises(ValidationError):
            S.pay_invoice(self.u['finance'], pending.pk, TODAY(), 'transfer', '', tok())
        inv = self.invoice(issue_date=TODAY() - timedelta(days=10))
        with self.assertRaises(PermissionDenied):
            S.pay_invoice(self.u['manager'], inv.pk, TODAY(), 'transfer', '', tok())
        with self.assertRaises(ValidationError):
            S.pay_invoice(self.u['finance'], inv.pk, TODAY() + timedelta(days=1), 'transfer', '', tok())
        p = S.pay_invoice(self.u['finance'], inv.pk, TODAY(), 'transfer', 'TRF-1', tok())
        a = p.allocations.get()
        self.assertEqual((a.discount_percent, a.discount, p.amount, p.status), (Decimal('4.00'), Decimal('2000.00'), Decimal('48000.00'), 'confirmed'))
        with self.assertRaisesMessage(ValidationError, 'ya tiene un pago'):
            S.pay_invoice(self.u['finance'], inv.pk, TODAY(), 'transfer', '', tok())
        inv = Invoice.objects.get(pk=inv.pk)
        self.assertEqual(inv.payment_state, 'paid')

    def test_edit_and_void_only_while_allowed(self):
        inv = self.invoice(validated=False)
        S.update_invoice(self.u['finance'], inv.pk, self.header(number='FE-1B'), [self.line(qty=7)], [], tok())
        inv.refresh_from_db()
        self.assertEqual((inv.number, inv.expected_units), ('FE-1B', 7))
        S.validate_invoice(self.u['finance'], inv.pk, tok())
        with self.assertRaises(ValidationError):
            S.update_invoice(self.u['finance'], inv.pk, self.header(number='FE-1C'), [self.line(qty=8)], [], tok())
        with self.assertRaises(ValidationError):
            S.void_invoice(self.u['finance'], inv.pk, '', tok())
        S.void_invoice(self.u['finance'], inv.pk, 'Proveedor emitió nota crédito total', tok())
        self.assertEqual(Invoice.objects.get(pk=inv.pk).reception, 'void')
        self.assertEqual({p.code: p.in_transit for p in S.stock_position()}['REF-A'], 0)

    def test_invoice_form_via_http(self):
        self.login_as(self.u['manager'])
        data = {'token': tok(), 'supplier': self.supplier.pk, 'number': 'FE-HTTP', 'issue_date': TODAY().isoformat(), 'due_date': '', 'ordered_on': '',
                'order_number': 'OC-1', 'cufe': '', 'global_discount': '', 'retention': '', 'notes': '',
                'lineas-TOTAL_FORMS': 2, 'lineas-INITIAL_FORMS': 0, 'lineas-MIN_NUM_FORMS': 0, 'lineas-MAX_NUM_FORMS': 60,
                'lineas-0-product': self.product.pk, 'lineas-0-quantity': 12, 'lineas-0-unit_price': '1500', 'lineas-0-discount': '', 'lineas-0-tax_rate': '0',
                'lineas-1-product': '', 'lineas-1-quantity': '', 'lineas-1-unit_price': '', 'lineas-1-tax_rate': '0',
                'files': SimpleUploadedFile('soporte.pdf', b'%PDF-1.4 prueba', content_type='application/pdf')}
        response = self.client.post('/facturas/nueva/', data)
        inv = Invoice.objects.get(number='FE-HTTP')
        self.assertRedirects(response, f'/facturas/{inv.pk}/', fetch_redirect_response=False)
        self.assertEqual((inv.total, inv.files.count()), (Decimal('18000.00'), 1))
        self.assertContains(self.client.get(f'/facturas/{inv.pk}/'), f'action="/facturas/{inv.pk}/validar/"')
        self.client.post(f'/facturas/{inv.pk}/validar/', {'token': tok()})
        self.assertEqual(Invoice.objects.get(pk=inv.pk).review, 'validated')
        line = inv.lines.get()
        response = self.client.post(f'/facturas/{inv.pk}/recibir/', {'token': tok(), 'received_on': TODAY().isoformat(), 'note': '',
                                                                     'lotes-TOTAL_FORMS': 1, 'lotes-INITIAL_FORMS': 1, 'lotes-MIN_NUM_FORMS': 0, 'lotes-MAX_NUM_FORMS': 40,
                                                                     'lotes-0-line': line.pk, 'lotes-0-quantity': 12, 'lotes-0-lot': 'H1', 'lotes-0-expiry': (TODAY() + timedelta(days=99)).isoformat(),
                                                                     'lotes-0-location': 'Bodega principal'})
        self.assertRedirects(response, f'/facturas/{inv.pk}/', fetch_redirect_response=False)
        self.login_as(self.u['finance'])
        response = self.client.post(f'/facturas/{inv.pk}/pagar/', {'token': tok(), 'paid_on': TODAY().isoformat(), 'method': 'transfer', 'reference': 'TRF-9'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PaymentAllocation.objects.get(invoice=inv).discount, Decimal('720.00'))
        csv = self.client.get('/facturas/exportar.csv').content.decode('utf-8')
        self.assertIn('FE-HTTP', csv)

    def test_attachments_are_validated_and_protected(self):
        inv = self.invoice(validated=False)
        with self.assertRaisesMessage(ValidationError, 'no corresponde'):
            S.attach_files(self.u['manager'], inv.pk, [('falso.pdf', b'MZ ejecutable')], tok())
        with self.assertRaisesMessage(ValidationError, 'formato no admitido'):
            S.attach_files(self.u['manager'], inv.pk, [('macro.xlsm', b'PK\x03\x04')], tok())
        S.attach_files(self.u['manager'], inv.pk, [('factura.pdf', b'%PDF-1.7 contenido')], tok())
        with self.assertRaisesMessage(ValidationError, 'ya estaban'):
            S.attach_files(self.u['manager'], inv.pk, [('copia.pdf', b'%PDF-1.7 contenido')], tok())
        f = InvoiceFile.objects.get()
        self.assertTrue((self.tmp / 'adjuntos' / f.file.name).exists())
        self.login_as(self.u['requester'])
        self.assertEqual(self.client.get(f'/facturas/soportes/{f.pk}/').status_code, 403)
        self.login_as(self.u['finance'])
        response = self.client.get(f'/facturas/soportes/{f.pk}/')
        self.assertEqual((response.status_code, response['Content-Type']), (200, 'application/pdf'))
        self.assertEqual(b''.join(response.streaming_content), b'%PDF-1.7 contenido')
        self.assertEqual(self.client.get(f'/static/../datos/{f.file.name}').status_code, 404)

    def test_failed_invoice_does_not_leave_files(self):
        with self.assertRaises(ValidationError):
            S.create_invoice(self.u['manager'], self.header(), [self.line()], [('ok.pdf', b'%PDF-1.4'), ('malo.pdf', b'nada')], tok())
        self.assertFalse(any((self.tmp / 'adjuntos').rglob('*.pdf')))
        self.assertEqual(Invoice.objects.count(), 0)

    def test_suppliers_and_nit(self):
        self.assertEqual(S.normalize_nit('900.123.456-' + nit_check_digit('900123456')), '900123456')
        self.assertEqual(S.normalize_nit('900123456' + nit_check_digit('900123456')), '900123456')
        with self.assertRaisesMessage(ValidationError, 'dígito de verificación'):
            S.normalize_nit('900123456-' + str((int(nit_check_digit('900123456')) + 1) % 10))
        self.login_as(self.u['finance'])
        data = {'token': tok(), 'name': 'Distribuidora Dos S.A.S.', 'nit': '800197268', 'email': '', 'phone': '', 'payment_days': 30,
                'discount_base': 'total', 'active': 'on', 'tramos-TOTAL_FORMS': 4, 'tramos-INITIAL_FORMS': 0, 'tramos-MIN_NUM_FORMS': 0,
                'tramos-MAX_NUM_FORMS': 4, 'tramos-0-days': 10, 'tramos-0-percent': '2.5'}
        self.assertRedirects(self.client.post('/proveedores/nuevo/', data), '/proveedores/', fetch_redirect_response=False)
        s = Supplier.objects.get(nit='800197268')
        self.assertEqual(s.terms(), {'payment_days': 30, 'base': 'total', 'tiers': [[10, '2.50']]})
        self.assertEqual(s.nit_display, '800.197.268-' + nit_check_digit('800197268'))
        data.update({'token': tok(), 'tramos-0-days': 40})
        rejected = self.client.post(f'/proveedores/{s.pk}/', data)
        self.assertIn('no pueden superar el plazo', ' '.join(rejected.context['form'].non_field_errors()))
        self.assertEqual(Supplier.objects.get(pk=s.pk).tiers.get().days, 10)

    def test_alerts_by_role(self):
        overdue = self.invoice(number='FE-V', issue_date=TODAY() - timedelta(days=70))
        soon = self.invoice(number='FE-D', issue_date=TODAY() - timedelta(days=12))
        self.invoice(validated=False, number='FE-R')
        finance = {a['key']: a for a in S.alerts(self.u['finance'])}
        self.assertEqual(finance[f'overdue-{overdue.pk}']['level'], 'critical')
        self.assertIn(f'discount-{soon.pk}-4.00', finance)
        self.assertIn('Descuento de 4 %', finance[f'discount-{soon.pk}-4.00']['title'])
        self.assertTrue(any(k.startswith('review-') for k in finance))
        stock = {a['key'] for a in S.alerts(self.u['manager'])}
        self.assertIn(f'receipt-{overdue.pk}', stock)
        self.assertIn(f'stock-{self.other.pk}', stock)
        self.assertFalse(any(k.startswith('overdue-') for k in stock))
        self.assertEqual(S.alerts(self.u['requester']), [])
        self.login_as(self.u['finance'])
        self.client.post('/alertas/leida/', {'key': '__todas__'})
        self.assertTrue(all(a['read'] for a in S.alerts(self.u['finance'])))


class ImportTests(Base):
    def test_parse_plain_attached_and_zip(self):
        raw = ubl_invoice(lines=(('COD-A', 'REFERENCIA A', '10', '1000.00', '500.00', '19'), ('COD-B', 'REFERENCIA B', '4', '0', '0', '0')))
        data = parse_invoice_xml(raw)
        self.assertEqual((data['number'], data['supplier_nit'], data['cufe'], data['order_number'], data['retention']), ('FE-1001', '900123456', 'abc123', 'OC-77', '25.00'))
        self.assertEqual(data['lines'][0], {'supplier_code': 'COD-A', 'description': 'REFERENCIA A', 'quantity': 10, 'unit_price': '1000.00', 'discount': '500.00',
                                            'tax_rate': '19.00', 'expected_lot': 'lx9', 'expected_expiry': '2028-05-31'})
        self.assertEqual(parse_invoice_xml(attached(raw))['number'], 'FE-1001')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            z.writestr('ad0900123456.xml', attached(raw))
            z.writestr('fv0900123456.pdf', b'%PDF-1.4 representacion grafica')
            z.writestr('leame.txt', b'ignorar')
        data, files = read_upload('factura.zip', buffer.getvalue())
        self.assertEqual((data['number'], sorted(n for n, _ in files)), ('FE-1001', ['ad0900123456.xml', 'fv0900123456.pdf']))

    def test_rejects_unsafe_or_invalid_documents(self):
        evil = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><Invoice/>'
        for name, raw, message in [('a.xml', evil, 'no admitidas'), ('a.xml', b'<x>', 'no es un XML'), ('a.xml', b'<Otro/>', 'no corresponde'),
                                   ('a.pdf', b'%PDF', 'PDF'), ('a.zip', b'PK\x03\x04roto', 'ZIP'),
                                   ('a.xml', ubl_invoice(lines=(('A', 'B', '1.5', '10', '0', '0'),)), 'cantidades enteras')]:
            with self.subTest(message=message):
                with self.assertRaisesMessage(ValidationError, message):
                    read_upload(name, raw)

    def test_import_flow_creates_supplier_maps_products_and_attaches(self):
        self.login_as(self.u['manager'])
        raw = ubl_invoice(number='FE-77', nit='860512330', name='Suministros Nuevos S.A.S.', cufe='cufe-77', lot=False,
                          lines=(('BN-1', 'Referencia A brocha', '6', '2000.00', '0', '0'),))
        response = self.client.post('/facturas/importar/', {'file': SimpleUploadedFile('fe77.xml', raw, content_type='text/xml')})
        self.assertEqual(response.status_code, 302)
        token = response.url.split('importacion=')[1]
        supplier = Supplier.objects.get(nit='860512330')
        page = self.client.get(response.url)
        self.assertEqual((page.context['imported']['data']['number'], page.context['imported']['data']['supplier_name'], page.context['is_pdf']),
                         ('FE-77', 'Suministros Nuevos S.A.S.', False))
        form, lines = page.context['form'], page.context['lines']
        self.assertEqual(form.initial['number'], 'FE-77')
        self.assertEqual(lines.initial[0]['product'], self.product.pk)
        data = {'token': tok(), 'supplier': supplier.pk, 'number': 'FE-77', 'issue_date': TODAY().isoformat(), 'due_date': '', 'ordered_on': '', 'order_number': '',
                'cufe': 'cufe-77', 'global_discount': '0', 'retention': '0', 'notes': '', 'lineas-TOTAL_FORMS': 1, 'lineas-INITIAL_FORMS': 1,
                'lineas-MIN_NUM_FORMS': 0, 'lineas-MAX_NUM_FORMS': 60, 'lineas-0-product': self.product.pk, 'lineas-0-supplier_code': 'BN-1',
                'lineas-0-description': 'Referencia A brocha', 'lineas-0-quantity': 6, 'lineas-0-unit_price': '2000', 'lineas-0-discount': '0', 'lineas-0-tax_rate': '0'}
        response = self.client.post(f'/facturas/nueva/?importacion={token}', data)
        inv = Invoice.objects.get(number='FE-77')
        self.assertRedirects(response, f'/facturas/{inv.pk}/', fetch_redirect_response=False)
        self.assertEqual((inv.source, inv.files.count(), inv.total), ('xml', 1, Decimal('12000.00')))
        self.assertFalse((self.tmp / 'importaciones' / token).exists())
        self.assertNotIn('import:' + token, self.client.session)
        self.assertEqual(SupplierCode.objects.get(supplier=supplier, code='BN-1').product, self.product)
        self.assertTrue(Audit.objects.filter(event='Proveedor creado desde factura electrónica').exists())


@override_settings(**FAST)
class ConcurrencyTests(TransactionTestCase):
    def test_two_approvals_cannot_overbook(self):
        users, product, _, batch, _ = fixture()
        Batch.objects.filter(pk=batch.pk).update(physical=100)
        r1 = S.request_stock(users['requester'], product, 60, 'a', tok())
        r2 = S.request_stock(users['second'], product, 60, 'b', tok())
        barrier = Barrier(2)

        def run(request_id):
            close_old_connections()
            barrier.wait()
            try:
                S.approve(User.objects.get(username='manager'), request_id, batch.pk, tok())
                return 'ok'
            except Exception as err:
                return type(err).__name__
            finally:
                close_old_connections()

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, [r1.pk, r2.pk]))
        batch.refresh_from_db()
        self.assertEqual(results.count('ok'), 1)
        self.assertEqual((batch.physical, batch.reserved), (100, 60))


class PasswordHashTests(TestCase):
    def test_argon2_is_used(self):
        user = User.objects.create_user('real', password=PASSWORD)
        self.assertTrue(user.password.startswith('argon2'))


class MoneyFilterTests(TestCase):
    """`cop` conserva los centavos de un documento; `pesos` redondea al peso para indicadores y tableros."""

    def test_pesos_rounds_to_whole_pesos_and_cop_keeps_cents(self):
        from .templatetags.ui import cop, pesos
        self.assertEqual(cop(Decimal('81002307.75')), '$81.002.307,75')
        self.assertEqual(pesos(Decimal('81002307.75')), '$81.002.308')
        self.assertEqual(pesos(Decimal('147318366.60')), '$147.318.367')
        self.assertEqual(pesos(Decimal('0.50')), '$1')
        self.assertEqual(pesos(Decimal('-26040.40')), '−$26.040')
        self.assertEqual(pesos(None), '$0')
        self.assertEqual(pesos('sin dato'), 'sin dato')

    def test_results_dashboard_shows_no_cents(self):
        import re
        from django.template.loader import get_template
        source = get_template('cartera/results.html').template.source
        self.assertNotIn('|cop', source)                       # el tablero de resultados no muestra centavos
        for name in ('payables.html', 'cartera/portfolio.html', 'cartera/bank.html', 'cartera/sales.html', 'agentes/agents.html'):
            values = re.findall(r'kpi-value">(.*?)</span>', get_template(name).template.source, re.S)
            self.assertFalse([v for v in values if '|cop' in v], name)

    def test_connector_states_are_shown_in_spanish(self):
        from .templatetags.ui import estado_conector
        self.assertEqual([estado_conector(v) for v in ('Accepted', 'NotSent', 'Draft', '', None)], ['Aceptada', 'Sin envío registrado', 'Sin validar', '', ''])
        self.assertEqual(estado_conector('Envío simulado registrado · compras@cliente.example'), 'Envío simulado registrado · compras@cliente.example')

    def test_iso_dates_are_shown_day_first(self):
        from datetime import date
        from .templatetags.ui import fecha
        self.assertEqual([fecha('2026-11-19'), fecha(date(2026, 11, 19)), fecha('2026-11-19T08:30:00'), fecha('sin fecha'), fecha('')],
                         ['19/11/2026', '19/11/2026', '19/11/2026', 'sin fecha', ''])
