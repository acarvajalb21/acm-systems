from html.parser import HTMLParser

from django.test import TestCase, override_settings
from django.urls import reverse

from .models import User
from .testing import new_password

PASSWORD = new_password()


class OperationChoices(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and 'operation-choice' in attrs.get('class', '').split():
            self.links.append(attrs['href'])


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class OperationsHubTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.users = {
            role: User.objects.create_user(
                username=role, role=role, password=PASSWORD, must_change_password=False)
            for role in ('requester', 'inventory', 'finance', 'billing', 'reception', 'admin')
        }

    def login_as(self, role):
        user = self.users[role]
        self.client.force_login(user)
        session = self.client.session
        session['user_version'] = user.session_version
        session.save()

    def test_requires_authentication(self):
        response = self.client.get(reverse('operations'))
        self.assertRedirects(response, '/ingresar/?next=/operaciones/', fetch_redirect_response=False)

    def test_each_profile_gets_accessible_tasks_only(self):
        inventory = {
            reverse('exit_stock'), reverse('requests') + '?estado=pending',
            reverse('reserve'), reverse('adjust_in'), reverse('adjust_out'),
            reverse('invoices') + '?vista=recibir',
        }
        documents = {
            reverse('invoice_import'), reverse('invoice_new'),
            reverse('invoices') + '?vista=revisar',
        }
        sales = {reverse('sale_new'), reverse('bank_import'), reverse('bank') + '?estado=pending'}
        reception = {reverse('reception')}
        expected = {
            'requester': {reverse('new_request'), reverse('requests')},
            'inventory': inventory | documents | reception,
            'finance': documents | sales | {reverse('payables')},
            'billing': {reverse('sale_new')},
            'reception': reception,
            'admin': inventory | documents | sales | reception | {reverse('payables'), reverse('new_user')},
        }
        for role, tasks in expected.items():
            with self.subTest(role=role):
                self.login_as(role)
                response = self.client.get(reverse('operations'))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['section'], 'operations')
                self.assertEqual(set(OperationChoices(response.content.decode()).links), tasks)
                for url in tasks:
                    with self.subTest(role=role, destination=url):
                        self.assertEqual(self.client.get(url).status_code, 200)

    def test_requester_does_not_get_stock_or_purchase_actions(self):
        self.login_as('requester')
        html = self.client.get(reverse('operations')).content.decode()
        self.assertIn(f'href="{reverse("new_request")}"', html)
        for name in ('exit_stock', 'adjust_out', 'invoice_import', 'invoice_new', 'payables', 'sale_new', 'new_user'):
            with self.subTest(action=name):
                self.assertNotIn(f'href="{reverse(name)}"', html)

    def test_inventory_profile_registers_stock_but_not_payments_or_sales(self):
        self.login_as('inventory')
        html = self.client.get(reverse('operations')).content.decode()
        self.assertIn(f'href="{reverse("exit_stock")}"', html)
        for name in ('payables', 'sale_new', 'bank_import', 'new_user'):
            with self.subTest(action=name):
                self.assertNotIn(f'href="{reverse(name)}"', html)
