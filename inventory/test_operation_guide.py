"""El registro guiado mejora la presentación sin cambiar el contrato de los formularios."""
import uuid
from html.parser import HTMLParser

from django.test import Client, override_settings

from .models import Movement, SupplyRequest
from .tests import Base


class FormControls(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.fields = []
        self.submits = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'data-field-name' in attrs:
            self.fields.append(attrs)
        if tag == 'button' and 'data-guide-submit' in attrs:
            self.submits.append(attrs)


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class OperationGuideTests(Base):
    def test_operations_render_complete_accessible_form_without_javascript(self):
        routes = [
            ('requester', '/solicitudes/nueva/', 'new_request', ['product', 'quantity', 'needed_by', 'purpose']),
            ('manager', '/inventario/salida/', 'exit_stock', ['batch', 'quantity', 'reference', 'note']),
            ('manager', '/inventario/reserva/', 'reserve', ['batch', 'quantity', 'note']),
            ('manager', '/inventario/entrada/', 'adjust_in', ['kind', 'product', 'lot', 'expiry', 'location', 'quantity', 'note']),
            ('manager', '/inventario/baja/', 'adjust_out', ['kind', 'batch', 'quantity', 'loss_reason', 'note']),
        ]
        for role, url, operation, names in routes:
            with self.subTest(operation=operation):
                self.login_as(self.u[role])
                response = self.client.get(url)
                self.assertContains(response, f'data-operation-guide="{operation}"')
                controls = FormControls(response.content.decode())
                self.assertEqual([field['data-field-name'] for field in controls.fields], names)
                self.assertTrue(all('hidden' not in field for field in controls.fields))
                self.assertEqual(len(controls.submits), 1)
                self.assertNotIn('hidden', controls.submits[0])
                self.assertEqual(controls.submits[0]['type'], 'submit')
                self.assertContains(response, 'name="csrfmiddlewaretoken"')
                self.assertContains(response, 'name="token"')
                self.assertContains(response, 'data-guide-header hidden')

    def test_server_errors_link_to_fields_and_preserve_entered_values(self):
        self.login_as(self.u['requester'])
        response = self.client.post('/solicitudes/nueva/', {
            'token': uuid.uuid4(), 'product': self.product.pk, 'quantity': 0,
            'purpose': 'Despacho programado', 'needed_by': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-guide-server-errors role="alert" tabindex="-1"')
        self.assertContains(response, '<a href="#id_quantity">')
        self.assertContains(response, 'id="id_quantity_error"')
        self.assertContains(response, 'data-field-name="quantity" data-field-errors')
        self.assertEqual(response.context['form']['purpose'].value(), 'Despacho programado')
        self.assertEqual(response.context['form']['quantity'].value(), '0')
        self.assertFalse(SupplyRequest.objects.exists())

    def test_hidden_token_errors_are_visible_in_the_error_summary(self):
        self.login_as(self.u['requester'])
        response = self.client.post('/solicitudes/nueva/', {
            'token': 'token-invalido', 'product': self.product.pk, 'quantity': 3,
            'purpose': 'Obra norte', 'needed_by': '',
        })
        self.assertContains(response, 'data-guide-server-errors')
        self.assertContains(response, response.context['form']['token'].errors[0])
        self.assertFalse(SupplyRequest.objects.exists())

    def test_original_request_post_still_works_without_javascript(self):
        self.login_as(self.u['requester'])
        response = self.client.post('/solicitudes/nueva/', {
            'token': uuid.uuid4(), 'product': self.product.pk, 'quantity': 3,
            'purpose': 'Obra norte', 'needed_by': '',
        })
        self.assertRedirects(response, '/solicitudes/')
        request = SupplyRequest.objects.get()
        self.assertEqual(request.quantity, 3)
        self.assertEqual(request.purpose, 'Obra norte')
        self.assertEqual(request.status, 'pending')
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.physical, 100)

    def test_global_stock_error_remains_visible_without_changing_inventory(self):
        self.login_as(self.u['manager'])
        response = self.client.post('/inventario/salida/', {
            'token': uuid.uuid4(), 'batch': self.batch.pk, 'quantity': 101,
            'reference': 'REM-123', 'note': 'Despacho norte',
        })
        self.assertContains(response, 'data-guide-server-errors')
        self.assertContains(response, 'La cantidad supera las unidades disponibles del lote.')
        self.assertEqual(response.context['form']['reference'].value(), 'REM-123')
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.physical, 100)
        self.assertFalse(Movement.objects.exists())

    def test_help_text_has_the_id_referenced_by_the_widget(self):
        self.login_as(self.u['requester'])
        response = self.client.get('/solicitudes/nueva/')
        self.assertContains(response, 'aria-describedby="id_purpose_helptext"')
        self.assertContains(response, 'id="id_purpose_helptext"')

    def test_direct_post_still_requires_csrf(self):
        client = self.login_as(self.u['requester'], Client(enforce_csrf_checks=True))
        response = client.post('/solicitudes/nueva/', {
            'token': uuid.uuid4(), 'product': self.product.pk, 'quantity': 3,
            'purpose': 'Obra norte',
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SupplyRequest.objects.exists())
