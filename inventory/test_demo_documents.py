"""Factura de demostración: generar → leer → mismos datos (PDF y XML), carga con revisión de campos y pedidos sin doble conteo."""
import io
from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from aprendizaje import reposicion as R
from . import compras, demo_documents as demo, services as S
from .einvoice import parse_invoice_xml, read_upload
from .models import Invoice, Supplier
from .pdf_invoice import parse_invoice_pdf, parse_invoice_text
from .tests import Base, tok

TODAY = timezone.localdate


def sample(**extra):
    data = {
        'supplier_name': 'Suministros Andinos S.A.S.', 'supplier_nit': '900555020', 'number': 'FD-1001', 'cufe': 'SIM' + 'a1b2c3d4' * 11,
        'issue_date': TODAY() - timedelta(days=3), 'due_date': TODAY() + timedelta(days=27), 'order_number': 'PED-2026-0007',
        'ordered_on': TODAY() - timedelta(days=9), 'payment_days': 30, 'discount_base': 'subtotal',
        'discount_tiers': [{'days': 20, 'percent': '1.5'}, {'days': 10, 'percent': '3'}], 'global_discount': '1000', 'retention': '2500.50',
        'lines': [
            {'supplier_code': 'ADH-400', 'description': 'Adhesivo epóxico bicomponente (cartucho de 400 ml), uso estructural en concreto y acero', 'quantity': 24,
             'unit_price': '18500', 'discount': '4400', 'tax_rate': '19', 'expected_lot': 'l2411a', 'expected_expiry': TODAY() + timedelta(days=380)},
            {'supplier_code': 'SEL-310', 'description': 'Sellante de poliuretano gris 310 ml', 'quantity': 60, 'unit_price': '12900.50', 'tax_rate': '19',
             'expected_lot': 'S-7781', 'expected_expiry': TODAY() + timedelta(days=300)},
            {'supplier_code': 'MAN-001', 'description': 'Paño de limpieza industrial: paquete × 50', 'quantity': 5, 'unit_price': '0', 'tax_rate': '0'},
        ],
    }
    data.update(extra)
    return data


class RoundTripTests(Base):
    def test_pdf_round_trip_returns_the_same_data(self):
        data = sample()
        raw = demo.invoice_pdf(data)
        self.assertTrue(raw.startswith(b'%PDF'))
        read = parse_invoice_pdf(raw)
        self.assertEqual(read, demo.expected_reading(data, 'pdf'))
        self.assertEqual(read['warnings'], [])
        self.assertEqual((read['payable'], read['payment_days'], read['discount_tiers']),
                         ('1440719.20', 30, [{'days': 10, 'percent': '3'}, {'days': 20, 'percent': '1.5'}]))
        self.assertEqual(read['lines'][0]['expected_lot'], 'L2411A')
        self.assertIn('Documento de demostración sin validez fiscal', '\n'.join(demo.invoice_lines(data)))

    def test_xml_round_trip_returns_the_same_data(self):
        data = sample()
        raw = demo.invoice_xml(data)
        self.assertEqual(parse_invoice_xml(raw), demo.expected_reading(data, 'xml'))
        read, files = read_upload('factura-demo.xml', raw)
        self.assertEqual((read['number'], read['retention'], files[0][0]), ('FD-1001', '2500.50', 'factura-demo.xml'))
        self.assertIn('sin validez fiscal', raw.decode('utf-8'))

    def test_long_invoice_spans_pages_and_still_round_trips(self):
        lines = [{'supplier_code': f'REC-{i:03d}', 'description': f'Recubrimiento acrílico color {i} ' + 'para fachadas y cubiertas expuestas ' * 3,
                  'quantity': i + 1, 'unit_price': str(1000 + i), 'tax_rate': '19', 'expected_lot': f'R{i}', 'expected_expiry': TODAY() + timedelta(days=200 + i)}
                 for i in range(30)]
        data = sample(lines=lines, cufe='', order_number='', ordered_on=None, discount_tiers=[], global_discount=0, retention=0)
        raw = demo.invoice_pdf(data)
        import pdfplumber
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            self.assertGreater(len(pdf.pages), 3)
        read = parse_invoice_pdf(raw)
        self.assertEqual(read, demo.expected_reading(data, 'pdf'))
        self.assertEqual(len(read['lines']), 30)

    def test_other_pdfs_and_damaged_fields_are_not_guessed(self):
        from contable.simulator import tiny_pdf
        with self.assertRaisesMessage(ValidationError, 'Factura de demostración'):
            parse_invoice_pdf(tiny_pdf(['FACTURA DE VENTA', 'Proveedor: Otro formato S.A.S.', 'Total: 100']))
        with self.assertRaisesMessage(ValidationError, 'PDF'):
            parse_invoice_pdf(b'esto no es un PDF')
        text = '\n'.join(demo.invoice_lines(sample()))
        broken = text.replace('Total a pagar: ', 'Total a pagar: 9').replace('Fecha de vencimiento: ', 'Fecha de vencimiento: pronto ')
        read = parse_invoice_text([broken])
        self.assertEqual(read['due_date'], '')
        self.assertTrue(any('vencimiento' in w.lower() for w in read['warnings']))
        self.assertTrue(any('total impreso no coincide' in w for w in read['warnings']))


class ImportReviewTests(Base):
    def test_pdf_is_read_reviewed_and_only_saved_after_confirmation(self):
        self.login_as(self.u['manager'])
        data = sample(lines=sample()['lines'][:2])
        raw = demo.invoice_pdf(data)
        response = self.client.post('/facturas/importar/', {'file': SimpleUploadedFile('factura-demo.pdf', raw, content_type='application/pdf')})
        self.assertEqual(response.status_code, 302, response.content[:600])
        self.assertFalse(Invoice.objects.exists())                                   # leer no registra la compra
        self.assertFalse(Supplier.objects.filter(nit='900555020').exists())          # ni crea el proveedor
        token = response.url.split('importacion=')[1]
        page = self.client.get(response.url)
        self.assertEqual(page.status_code, 200)
        form, lines, terms = page.context['form'], page.context['lines'], page.context['payment_terms']
        self.assertTrue(page.context['is_pdf'])
        self.assertEqual((form.initial['number'], form.initial['supplier_name'], form.initial['supplier_nit']), ('FD-1001', 'Suministros Andinos S.A.S.', '900555020'))
        self.assertEqual((lines.initial[0]['supplier_code'], lines.initial[0]['quantity'], lines.initial[0]['tax_rate']), ('ADH-400', 24, '19'))
        self.assertEqual((terms.initial['payment_days'], terms.initial['days_1'], terms.initial['percent_1']), (30, 10, '3'))
        expected = demo.expected_reading(data, 'pdf')
        post = {'token': tok(), 'supplier': '', 'supplier_name': 'Suministros Andinos S.A.S.', 'supplier_nit': '900555020', 'number': 'FD-1001',
                'issue_date': expected['issue_date'], 'due_date': expected['due_date'], 'ordered_on': expected['ordered_on'], 'order_number': expected['order_number'],
                'cufe': expected['cufe'], 'global_discount': '1000', 'retention': '2500.50', 'notes': '', 'pdf_currency': 'COP', 'pdf_total': expected['payable'],
                'pago-payment_days': '30', 'pago-discount_base': 'subtotal', 'pago-days_1': '10', 'pago-percent_1': '3', 'pago-days_2': '20', 'pago-percent_2': '1.5',
                'lineas-TOTAL_FORMS': 2, 'lineas-INITIAL_FORMS': 2, 'lineas-MIN_NUM_FORMS': 0, 'lineas-MAX_NUM_FORMS': 60}
        for i, line in enumerate(expected['lines']):
            post.update({f'lineas-{i}-product': (self.product, self.other)[i].pk, f'lineas-{i}-supplier_code': line['supplier_code'],
                         f'lineas-{i}-description': line['description'], f'lineas-{i}-quantity': line['quantity'], f'lineas-{i}-unit_price': line['unit_price'],
                         f'lineas-{i}-discount': line['discount'], f'lineas-{i}-tax_rate': '19', f'lineas-{i}-expected_lot': line['expected_lot'],
                         f'lineas-{i}-expected_expiry': line['expected_expiry']})
        # Sin la confirmación expresa de la revisión, nada se guarda.
        rejected = self.client.post(f'/facturas/nueva/?importacion={token}', post)
        self.assertEqual(rejected.status_code, 200)
        self.assertFalse(Invoice.objects.exists())
        post.update({'token': tok(), 'pdf_reviewed': 'on'})
        response = self.client.post(f'/facturas/nueva/?importacion={token}', post)
        inv = Invoice.objects.get(number='FD-1001')
        self.assertRedirects(response, f'/facturas/{inv.pk}/', fetch_redirect_response=False)
        self.assertEqual((inv.source, inv.review, inv.total, inv.files.count()), ('pdf', 'pending', Decimal(expected['payable']), 1))
        self.assertEqual(inv.supplier.nit, '900555020')
        self.assertEqual(inv.document_terms['tiers'], [[10, '3'], [20, '1.5']])
        S.validate_invoice(self.u['finance'], inv.pk, tok())
        self.assertEqual(Invoice.objects.get(pk=inv.pk).terms['tiers'], [[10, '3'], [20, '1.5']])   # condiciones de esa factura, congeladas al validar


class OnOrderTests(Base):
    """Cada unidad pendiente cuenta una sola vez en la posición de reposición: pedida o facturada, nunca las dos."""

    def order(self, qty=40):
        return compras.create_order(self.u['manager'], self.supplier, [{'product': self.product, 'quantity': qty}], '', tok())

    def position(self):
        [plan] = R.plans(products=[self.product])
        return plan.available, plan.in_transit, plan.on_order, plan.position

    def test_linked_invoice_does_not_count_the_order_units_twice(self):
        order = self.order(40)
        self.assertEqual(compras.on_order(), {self.product.pk: 40})
        self.assertEqual(self.position(), (100, 0, 40, 140))
        # Llega la factura que cita el pedido: las 40 unidades pasan a «en tránsito» y dejan de contarse como pedidas.
        inv = self.invoice(order_number=order.number, lines=[self.line(qty=40)])
        order.refresh_from_db()
        self.assertEqual((order.invoice, order.status), (inv, 'draft'))               # el pedido sigue abierto hasta recibir
        self.assertEqual(compras.on_order().get(self.product.pk, 0), 0)
        self.assertEqual(self.position(), (100, 40, 0, 140))                         # antes del arreglo: 180
        # Recepción parcial: lo recibido pasa a disponible y lo pendiente sigue una sola vez.
        line = inv.lines.get()
        S.receive_invoice(self.u['manager'], inv.pk, [{'line': line.pk, 'quantity': 15, 'lot': 'N1', 'expiry': TODAY() + timedelta(days=300),
                                                       'location': 'Bodega principal'}], TODAY(), 'Llegan 15; faltan 25', tok())
        self.assertEqual(self.position(), (115, 25, 0, 140))

    def test_partial_invoice_leaves_only_the_uninvoiced_units_on_order_and_void_restores_them(self):
        order = self.order(40)
        inv = self.invoice(validated=False, order_number=order.number, lines=[self.line(qty=30)])
        self.assertEqual(compras.on_order(), {self.product.pk: 10})
        self.assertEqual(self.position(), (100, 30, 10, 140))
        other = self.order(5)                                                        # otro pedido abierto, sin factura: suma completo
        self.assertEqual(compras.on_order([self.product]), {self.product.pk: 15})
        compras.close_order(self.u['manager'], other.pk, tok(), received=False, note='Duplicado')
        S.void_invoice(self.u['finance'], inv.pk, 'Factura con error', tok())
        self.assertEqual(compras.on_order(), {self.product.pk: 40})                  # una factura anulada no cubre nada
        self.assertEqual(self.position(), (100, 0, 40, 140))
        # La factura corregida vuelve a vincularse al pedido: sigue contando una sola vez.
        order.refresh_from_db()
        self.assertIsNone(order.invoice)
        fixed = self.invoice(number='FE-2', order_number=order.number, lines=[self.line(qty=40)])
        order.refresh_from_db()
        self.assertEqual(order.invoice, fixed)
        self.assertEqual(self.position(), (100, 40, 0, 140))
