import tempfile
from datetime import timedelta
from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Batch, Product, SupplyRequest, User
from .testing import new_password

PASSWORD = new_password()


TEST_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    STORAGES=TEST_STORAGES,
)
class RequesterWorkspaceHomeTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        private = tempfile.TemporaryDirectory(prefix='operaciones-home-tests-')
        cls.addClassCleanup(private.cleanup)
        private_settings = override_settings(PRIVATE_DIR=Path(private.name))
        private_settings.enable()
        cls.addClassCleanup(private_settings.disable)

    @classmethod
    def setUpTestData(cls):
        cls.requester = User.objects.create_user(
            username='solicitante-principal', first_name='Ximena', role='requester',
            password=PASSWORD, must_change_password=False)
        cls.other_requester = User.objects.create_user(
            username='usuario-ajeno-privado', first_name='NombreAjenoPrivado',
            last_name='ApellidoAjenoPrivado', email='correo-ajeno@example.invalid',
            role='requester', password=PASSWORD, must_change_password=False)
        cls.product = Product.objects.create(code='REF-A', name='Referencia del catálogo')
        second_product = Product.objects.create(code='REF-B', name='Otra referencia activa')
        inactive = Product.objects.create(code='INACTIVA-PRIVADA', name='Referencia inactiva', active=False)
        today = timezone.localdate()
        for product, lot, days, physical, reserved in (
            (cls.product, 'VIGENTE-A', 60, 30, 7),
            (second_product, 'VIGENTE-B', 90, 14, 4),
            (cls.product, 'VENCIDO', -1, 500, 0),
            (inactive, 'NO-PUBLICADO', 60, 900, 0),
        ):
            Batch.objects.create(product=product, lot=lot, expiry=today + timedelta(days=days),
                                 location='Bodega principal', physical=physical, reserved=reserved)

        # Las solicitudes abiertas son anteriores a las seis entregas recientes.
        for index in range(7):
            SupplyRequest.objects.create(requester=cls.requester, product=cls.product, quantity=1,
                                         purpose=f'Destino propio pendiente {index}', status='pending')
        SupplyRequest.objects.create(requester=cls.requester, product=cls.product, quantity=2,
                                     purpose='Destino propio reservado', status='approved')
        SupplyRequest.objects.filter(requester=cls.requester).update(created_at=timezone.now() - timedelta(days=5))
        for index in range(6):
            SupplyRequest.objects.create(requester=cls.requester, product=cls.product, quantity=1,
                                         purpose=f'Destino propio entregado {index}', status='delivered')
        cls.foreign_requests = []
        for status in ('pending', 'approved'):
            cls.foreign_requests.append(SupplyRequest.objects.create(
                requester=cls.other_requester, product=cls.product, quantity=3,
                purpose=f'DESTINO-AJENO-PRIVADO-{status}', status=status))

    def login_as(self, user):
        self.client.force_login(user)
        session = self.client.session
        session['user_version'] = user.session_version
        session.save()

    def home_as(self, user):
        self.login_as(user)
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'home.html')
        return response

    def test_own_open_counts_include_requests_outside_recent_six(self):
        response = self.home_as(self.requester)
        recent = list(response.context['requests'])
        self.assertEqual(len(recent), 6)
        self.assertTrue(all(request.status == 'delivered' for request in recent))
        self.assertEqual(response.context['k_pending'], 7)
        self.assertEqual(response.context['k_ready'], 1)
        shown = {k['key']: k['value'] for k in response.context['indicators']}
        self.assertEqual((shown['pendientes'], shown['reservadas']), ('7', '1'))

    def test_other_requester_profile_and_requests_do_not_appear_in_html(self):
        response = self.home_as(self.requester)
        self.assertTrue(all(request.requester_id == self.requester.pk for request in response.context['requests']))
        for private_value in (self.other_requester.username, self.other_requester.first_name,
                              self.other_requester.last_name, self.other_requester.email):
            with self.subTest(private_value=private_value):
                self.assertNotContains(response, private_value)
        for request in self.foreign_requests:
            self.assertNotContains(response, request.purpose)
            self.assertNotContains(response, str(request.pk))

    def test_each_requester_gets_their_own_summary_after_account_switch(self):
        self.home_as(self.requester)
        response = self.home_as(self.other_requester)
        self.assertEqual(response.context['k_pending'], 1)
        self.assertEqual(response.context['k_ready'], 1)
        self.assertEqual({request.pk for request in response.context['requests']},
                         {request.pk for request in self.foreign_requests})
        self.assertContains(response, self.other_requester.first_name)
        self.assertNotContains(response, self.requester.first_name)
        self.assertNotContains(response, 'Destino propio')

    def test_available_units_use_active_products_and_usable_unreserved_stock(self):
        response = self.home_as(self.requester)
        # 30 - 7 + 14 - 4; el lote vencido y el producto inactivo no aportan unidades.
        self.assertEqual(response.context['k_available'], 33)
        self.assertEqual({product.code for product in response.context['products']}, {'REF-A', 'REF-B'})
        self.assertNotContains(response, 'INACTIVA-PRIVADA')
        self.assertNotContains(response, 'Referencia inactiva')
