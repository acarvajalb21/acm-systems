"""Aislamiento de la instalación y comunicaciones simuladas: todo vive en `datos/` y nada sale de la plataforma."""
import json
import os
import re
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from django.conf import settings
from django.test import Client, SimpleTestCase
from django.urls import NoReverseMatch, reverse
from agentes import runner
from agentes.models import AgentAction
from cartera import services as C
from cartera.models import PartyContact, Receivable
from cartera.tests import Base, PASSWORD, TODAY, tok
from inventory import compras
from inventory.models import PurchaseOrder, Supplier, User

BASE = Path(settings.BASE_DIR)
CONFIG_FILES = ['config/settings.py', 'config/urls.py', 'config/wsgi.py', 'serve.py', 'manage.py']
PROBE = '''
import json, django
from django.conf import settings
django.setup()
from inventory.models import InvoiceFile
print(json.dumps({'private': str(settings.PRIVATE_DIR), 'db': str(settings.DATABASES['default']['NAME']),
                  'test_db': str(settings.DATABASES['default']['TEST']['NAME']), 'engine': settings.DATABASES['default']['ENGINE'],
                  'files': str(InvoiceFile._meta.get_field('file').storage.location), 'hosts': settings.ALLOWED_HOSTS,
                  'secret': bool(settings.SECRET_KEY), 'debug': settings.DEBUG}))
'''


def probe(**extra_env):
    """Carga la configuración en un proceso limpio (sin variables FERIA_ ni de Django heredadas)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(('FERIA_', 'DJANGO_'))}
    env.update({'DJANGO_SETTINGS_MODULE': 'config.settings', 'PYTHONIOENCODING': 'utf-8', **extra_env})
    out = subprocess.run([sys.executable, '-c', PROBE], cwd=BASE, env=env, capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


class IsolationTests(SimpleTestCase):
    def test_database_secret_and_attachments_live_under_the_local_data_folder(self):
        data = probe()
        folder = BASE / 'datos'
        self.assertEqual(Path(data['private']), folder)
        self.assertEqual(Path(data['db']), folder / 'feria.sqlite3')
        self.assertEqual(Path(data['test_db']), folder / 'test.sqlite3')
        self.assertEqual(Path(data['files']), folder / 'adjuntos')
        self.assertEqual(data['engine'], 'django.db.backends.sqlite3')
        self.assertTrue((folder / 'secret.key').is_file())
        self.assertTrue(data['secret'])
        self.assertFalse(data['debug'])
        self.assertEqual(data['hosts'], ['127.0.0.1', 'localhost', 'testserver'])

    def test_only_feria_variables_change_the_configuration(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            data = probe(FERIA_PRIVATE_DIR=tmp, FERIA_HOSTS='192.168.0.10, equipo.test', PGHOST='db.invalid', DJANGO_ALLOWED_HOSTS='*',
                         DJANGO_SECRET_KEY='no-se-usa')
            self.assertEqual(Path(data['private']), Path(tmp))
            self.assertEqual(Path(data['db']).parent, Path(tmp))
            self.assertEqual(data['engine'], 'django.db.backends.sqlite3')              # una variable de base externa no cambia nada
            self.assertEqual(data['hosts'], ['127.0.0.1', 'localhost', 'testserver', '192.168.0.10', 'equipo.test'])
            self.assertNotIn('*', data['hosts'])
            self.assertNotEqual((Path(tmp) / 'secret.key').read_text(encoding='utf-8').strip(), 'no-se-usa')     # el secreto es el del archivo local

    def test_configuration_files_reference_nothing_outside_this_installation(self):
        for rel in CONFIG_FILES:
            text = (BASE / rel).read_text(encoding='utf-8')
            with self.subTest(file=rel):
                read = set(re.findall(r"os\.environ(?:\.get\(|\[|\.setdefault\()\s*'([A-Z_0-9]+)'", text))
                self.assertTrue(all(name.startswith('FERIA_') or name == 'DJANGO_SETTINGS_MODULE' for name in read), read)
                self.assertNotRegex(text, r'[A-Za-z]:[\\/]')                          # sin rutas absolutas del equipo
                self.assertNotRegex(text, r'(?i)https?://|postgres|pghost|trust_proxy')
        self.assertEqual((settings.SESSION_COOKIE_NAME, settings.CSRF_COOKIE_NAME), ('feria_sesion', 'feria_csrf'))
        self.assertEqual(settings.CONTABLE, {'mode': 'simulator', 'stamp_send': True, 'mail_send': False, 'timeout': 120})
        self.assertEqual((settings.COMPANY_NAME, settings.APP_NAME), ('Distribuidora Ejemplo S.A.S.', 'ACM Systems'))
        self.assertIn('profile_switch', settings.FERIA)
        self.assertEqual(set(settings.DATABASES), {'default'})

    def test_accounting_connector_is_always_the_local_simulator(self):
        from contable import client
        from contable.simulator import Simulator
        self.assertIsInstance(client.get_client(), Simulator)
        source = (BASE / 'contable' / 'client.py').read_text(encoding='utf-8')
        for gone in ('urllib', 'socket', 'access_key', 'Bearer', 'http'):
            self.assertNotIn(gone, source)

    def test_retired_routes_are_gone_and_new_ones_exist(self):
        for name in ('ledger_import', 'ledger_preview', 'legacy_rows', 'legacy_open'):
            with self.assertRaises(NoReverseMatch):
                reverse(name, args=[1] if name in ('ledger_preview', 'legacy_open') else [])
        expected = {'flow_map': '/flujo/', 'methodology': '/inteligencia/metodologia/', 'tour': '/recorrido/', 'demo_profile': '/feria/perfil/',
                    'copilot': '/copiloto/', 'copilot_ask': '/copiloto/preguntar/', 'results': '/finanzas/resultados/',
                    'contable_status': '/integracion/contable/', 'ledger_export': '/cuentas-por-pagar/exportar.xlsx'}
        for name, path in expected.items():
            self.assertEqual(reverse(name), path)
        self.assertEqual(reverse('forecast', args=[7]), '/inventario/reposicion/7/')


class NoExternalCommunicationTests(Base):
    """Recordatorios de cobro y pedidos a proveedores quedan como envíos simulados; ninguna pantalla enlaza hacia fuera."""

    def login(self, user):
        c = Client()
        c.force_login(user)
        s = c.session
        s['user_version'] = user.session_version
        s.save()
        return c

    def test_main_pages_have_no_mail_or_messaging_links(self):
        owner = User.objects.create_user('direccion', password=PASSWORD, role='owner', must_change_password=False, first_name='Dirección')
        manager = self.users['manager']
        supplier = Supplier.objects.create(name='Proveedor Andino S.A.S.', nit='900123456', email='pedidos@proveedor.example', phone='300 555 0101')
        order = compras.create_order(manager, supplier, [{'product': self.prod_a, 'quantity': 12}], '', tok())
        sent = compras.create_order(manager, supplier, [{'product': self.prod_b, 'quantity': 6}], '', tok())
        compras.mark_sent(manager, sent.pk, tok())
        sale = self.sale(self.ferreteria, [(self.prod_a, 1, None, '')])
        C.send_invoice_mail(self.finance, sale.pk, 'compras@cliente.example', tok())
        PartyContact.objects.create(party=self.rojas, name='Camilo Rojas', role='collection', channel='chat', value='300 555 0142', preferred=True)
        PartyContact.objects.create(party=self.ferreteria, name='Tesorería', role='collection', channel='email', value='tesoreria@cliente.example', preferred=True)
        for party, number in ((self.rojas, 900), (self.ferreteria, 901)):
            Receivable.objects.create(party=party, source='opening', prefix='FE', number=number, issue_date=TODAY() - timedelta(days=70),
                                      due_date=TODAY() - timedelta(days=40), amount=Decimal('350000'), created_by=self.finance)
        self.assertEqual(runner.run_agent('cobranza', 'manual').status, 'succeeded')
        for action in AgentAction.objects.filter(agent='cobranza', status='proposed'):
            done = runner.decide(self.finance, action.pk, True)
            self.assertEqual((done.status, done.result['state']), ('executed', 'simulated'), done.result)
        self.assertEqual(AgentAction.objects.filter(agent='cobranza', status='executed').count(), 2)
        sale.refresh_from_db()
        self.assertTrue(sale.mail_status.startswith('Envío simulado registrado'))
        self.assertEqual(PurchaseOrder.objects.get(pk=sent.pk).get_status_display(), 'Envío simulado registrado')
        self.assertEqual(PurchaseOrder.objects.get(pk=order.pk).get_status_display(), 'Mensaje preparado')
        pages = [reverse('home'), reverse('operations'), reverse('alerts'), reverse('agents'), reverse('learning'), reverse('replenishment'),
                 reverse('stock'), reverse('products'), reverse('movements'), reverse('requests'), reverse('reservations'),
                 reverse('purchase_orders'), reverse('purchase_order', args=[order.pk]), reverse('purchase_order', args=[sent.pk]),
                 reverse('invoices'), reverse('suppliers'), reverse('supplier_account', args=[supplier.pk]), reverse('payables'),
                 reverse('sales'), reverse('sale', args=[sale.pk]), reverse('parties'), reverse('party', args=[self.rojas.pk]),
                 reverse('party', args=[self.ferreteria.pk]), reverse('prices'), reverse('portfolio'), reverse('bank'), reverse('discounts'),
                 reverse('contable_status'), reverse('users'), reverse('audit'), reverse('reception'), reverse('flow_map'), reverse('methodology'),
                 reverse('tour'), reverse('copilot'), reverse('results'), reverse('forecast', args=[self.prod_a.pk])]
        client = self.login(owner)
        for url in pages:
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertNotIn('mailto:', html)
                self.assertNotIn('wa.me', html)
        for url in (reverse('login'), reverse('forgot'), reverse('offline')):
            html = Client().get(url).content.decode()
            self.assertNotIn('mailto:', html)
            self.assertNotIn('wa.me', html)

    def test_python_sources_build_no_mail_or_messaging_links(self):
        for path in BASE.rglob('*.py'):
            parts = path.relative_to(BASE).parts
            if parts[0] in ('.venv', 'staticfiles', 'datos') or 'migrations' in parts or path.name.startswith('test') or path.name == 'tests.py':
                continue
            text = path.read_text(encoding='utf-8')
            with self.subTest(file='/'.join(parts)):
                self.assertNotIn('mailto:', text)
                self.assertNotIn('wa.me', text)


# ------------------------------------------------------------------------------------------------------------------
# Escenario sintético: seguridad del reinicio, perfiles de demostración, verificación y documentos para importar.

import shutil
import tempfile
import uuid
from unittest import skipUnless
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings, tag
from django.utils import timezone
from cartera.models import BankTransaction, Sale, StatementImport
from cartera.tests import FAST
from inventory.models import Invoice, InvoiceFile, PaymentFile
from feria import documentos, reinicio, verificacion
from feria.escenario import generate
from feria.huella import differences
from feria.models import DemoState

SQLITE = 'django.db.backends.sqlite3'


class ResetSafetyTests(SimpleTestCase):
    """El reinicio solo puede borrar `feria.sqlite3` (o la base de pruebas) dentro de la carpeta de datos."""

    def test_only_the_local_database_inside_the_data_folder_is_accepted(self):
        data = BASE / 'datos'
        for name in ('feria.sqlite3', 'test.sqlite3'):
            self.assertEqual(reinicio.check_target({'ENGINE': SQLITE, 'NAME': data / name}, data, BASE, {}), (data / name).resolve())
        refused = [
            ({'ENGINE': 'django.db.backends.postgresql', 'NAME': 'feria'}, data),                     # otra base de datos
            ({'ENGINE': SQLITE, 'NAME': data / 'otra.sqlite3'}, data),                                 # otro nombre
            ({'ENGINE': SQLITE, 'NAME': BASE / 'feria.sqlite3'}, data),                                # fuera de la carpeta de datos
            ({'ENGINE': SQLITE, 'NAME': data / 'copias' / 'feria.sqlite3'}, data),                     # en una subcarpeta
            ({'ENGINE': SQLITE, 'NAME': ':memory:'}, data),
            ({'ENGINE': SQLITE, 'NAME': BASE.parent / 'feria.sqlite3'}, BASE.parent),                  # la carpeta de datos no es «datos»
        ]
        for database, private in refused:
            with self.subTest(database=str(database['NAME'])), self.assertRaises(reinicio.Unsafe):
                reinicio.check_target(database, private, BASE, {})

    def test_another_data_folder_is_accepted_only_when_the_environment_names_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = {'ENGINE': SQLITE, 'NAME': Path(tmp) / 'feria.sqlite3'}
            with self.assertRaises(reinicio.Unsafe):
                reinicio.check_target(database, tmp, BASE, {})
            self.assertEqual(reinicio.check_target(database, tmp, BASE, {'FERIA_PRIVATE_DIR': tmp}), (Path(tmp) / 'feria.sqlite3').resolve())
            with self.assertRaises(reinicio.Unsafe):                                                   # la variable apunta a otra carpeta
                reinicio.check_target(database, tmp, BASE, {'FERIA_PRIVATE_DIR': str(BASE / 'datos')})

    def test_command_refuses_and_deletes_nothing_outside_the_data_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            keep = Path(tmp) / 'adjuntos' / 'soporte.txt'
            keep.parent.mkdir()
            keep.write_text('no se toca', encoding='utf-8')
            with override_settings(PRIVATE_DIR=Path(tmp)), self.assertRaisesMessage(CommandError, 'No se reinicia nada'):
                call_command('reiniciar_feria', '--si')
            self.assertTrue(keep.is_file())

    def test_scripts_use_no_absolute_paths_and_call_the_expected_commands(self):
        # INSTALAR.ps1 prueba varios intérpretes y pasa los argumentos del entorno como lista: @('-m', 'venv', '.venv').
        expected = {'INSTALAR.ps1': ('requirements-lock.txt', 'migrate', 'reiniciar_feria --si', "'-m', 'venv', '.venv'"), 'INICIAR_FERIA.ps1': ('serve.py', '127.0.0.1:8790'),
                    'REINICIAR_FERIA.ps1': ('reiniciar_feria', 'verificar_escenario')}
        for name, parts in expected.items():
            text = (BASE / name).read_text(encoding='utf-8-sig')
            with self.subTest(script=name):
                self.assertNotRegex(text, r'(?<![A-Za-z])[A-Za-z]:[\\/]')                    # sin unidad de disco (la dirección local no cuenta)
                self.assertIn('$PSScriptRoot', text)
                for part in parts:
                    self.assertIn(part, text)

    def test_barcode_table_is_a_complete_code_128_set(self):
        self.assertEqual(len(documentos.C128), 106)
        self.assertEqual(len(set(documentos.C128)), 106)
        self.assertTrue(all(len(p) == 6 and sum(map(int, p)) == 11 for p in documentos.C128))
        self.assertEqual(sum(map(int, documentos.C128_STOP)), 13)
        values = documentos.gs1_128_values([('01', '02995550000019', True), ('17', '271130', True), ('10', 'L2611-01001', False), ('21', '261118001', False)])
        self.assertEqual(values[:2], [documentos.START_C, documentos.FNC1])
        self.assertEqual(values[2:14], [1, 2, 99, 55, 50, 0, 0, 19, 17, 27, 11, 30])                   # tramo numérico en el juego C
        self.assertEqual(values[-1], (values[0] + sum(i * v for i, v in enumerate(values[1:-1], 1))) % 103)
        self.assertIn('<svg', documentos.barcode_svg(values))


@override_settings(**FAST)
class DemoProfileTests(TestCase):
    """El cambio rápido de perfil: solo local, solo cuentas de demostración, sin elevar permisos y desactivable."""
    ACCOUNTS = (('owner', 'direccion.demo'), ('admin', 'administracion.demo'), ('inventory', 'inventario.demo'), ('reception', 'recepcion.demo'),
                ('finance', 'contabilidad.demo'), ('billing', 'facturacion.demo'), ('requester', 'solicitante.demo'), ('requester', 'proyectos.demo'))

    def setUp(self):
        self.accounts = {name: User.objects.create_user(name, password=PASSWORD, role=role, must_change_password=False, first_name=name.split('.')[0].title())
                         for role, name in self.ACCOUNTS}
        self.outsider = User.objects.create_user('gerencia', password=PASSWORD, role='owner', must_change_password=False)
        roles = {role: name for role, name in self.ACCOUNTS[:7]}
        roles['requester_2'] = 'proyectos.demo'
        DemoState.objects.create(reference_date=timezone.localdate(), seed=1, generated_at=timezone.now(), cases={'accounts': roles})
        self.url = reverse('demo_profile')

    def user_of(self, client):
        return client.session.get('_auth_user_id')

    def test_local_request_signs_in_as_the_demo_account_of_the_profile(self):
        c = Client()
        response = c.post(self.url, {'perfil': 'finance'})
        self.assertRedirects(response, reverse('home'), fetch_redirect_response=False)
        self.assertEqual(self.user_of(c), str(self.accounts['contabilidad.demo'].pk))
        self.assertEqual(c.get(reverse('bank')).status_code, 200)                                    # la sesión es válida para el control de acceso
        self.assertRedirects(c.post(self.url, {'perfil': 'proyectos.demo', 'next': reverse('requests')}), reverse('requests'), fetch_redirect_response=False)
        self.assertEqual(self.user_of(c), str(self.accounts['proyectos.demo'].pk))
        self.assertRedirects(c.post(self.url, {'perfil': 'owner', 'next': 'https://otro.example/'}), reverse('home'), fetch_redirect_response=False)

    def test_remote_requests_are_refused(self):
        for address in ('192.168.1.20', '10.0.0.7', '203.0.113.9', '', 'desconocida'):
            c = Client(REMOTE_ADDR=address)
            with self.subTest(address=address):
                self.assertEqual(c.post(self.url, {'perfil': 'owner'}).status_code, 403)
                self.assertIsNone(self.user_of(c))
        forwarded = Client(REMOTE_ADDR='192.168.1.20', HTTP_X_FORWARDED_FOR='127.0.0.1')              # una cabecera no convierte la petición en local
        self.assertEqual(forwarded.post(self.url, {'perfil': 'owner'}).status_code, 403)
        self.assertEqual(Client(REMOTE_ADDR='::1').post(self.url, {'perfil': 'owner'}).status_code, 302)

    def test_only_registered_active_demo_accounts_can_be_chosen(self):
        c = Client()
        for key in ('gerencia', 'superusuario', '', 'admin; drop'):
            with self.subTest(key=key):
                self.assertEqual(c.post(self.url, {'perfil': key}).status_code, 404)
        User.objects.filter(username='contabilidad.demo').update(is_active=False)
        self.assertEqual(c.post(self.url, {'perfil': 'finance'}).status_code, 404)
        DemoState.objects.all().delete()                                                             # sin escenario no hay cuentas de demostración
        self.assertEqual(c.post(self.url, {'perfil': 'owner'}).status_code, 404)
        self.assertIsNone(self.user_of(c))

    def test_it_can_be_switched_off_and_needs_post_with_csrf(self):
        c = Client()
        with override_settings(FERIA={'profile_switch': False}):
            self.assertEqual(c.post(self.url, {'perfil': 'owner'}).status_code, 404)
            self.assertNotIn('demo_profiles', c.get(reverse('login')).context)
        self.assertEqual(c.get(self.url).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(strict.post(self.url, {'perfil': 'owner'}).status_code, 403)
        self.assertIsNone(self.user_of(strict))

    def test_switching_never_elevates_permissions(self):
        c = Client()
        c.post(self.url, {'perfil': 'requester'})
        for name in ('users', 'audit', 'bank', 'payables', 'stock', 'results'):
            with self.subTest(page=name):
                self.assertEqual(c.get(reverse(name)).status_code, 403)
        before = {u.pk: (u.role, u.is_superuser, u.is_staff, u.discount_quota) for u in User.objects.all()}
        c.post(self.url, {'perfil': 'owner'})
        self.assertEqual(c.get(reverse('users')).status_code, 200)
        self.assertEqual(before, {u.pk: (u.role, u.is_superuser, u.is_staff, u.discount_quota) for u in User.objects.all()})

    def test_login_page_offers_the_profiles_only_to_the_local_machine(self):
        page = Client().get(reverse('login'))
        self.assertEqual([p['username'] for p in page.context['demo_profiles']], [name for _, name in self.ACCOUNTS])
        html = page.content.decode()
        self.assertIn('Entrar con un perfil de demostración', html)
        self.assertEqual(html.count('name="perfil"'), 8)
        self.assertNotIn(PASSWORD, html)
        remote = Client(REMOTE_ADDR='192.168.1.20').get(reverse('login'))
        self.assertNotIn('demo_profiles', remote.context)
        self.assertNotIn('name="perfil"', remote.content.decode())
        c = Client()
        c.post(self.url, {'perfil': 'inventory'})
        page = c.get(reverse('demo_documents'))
        self.assertEqual(page.status_code, 200)
        self.assertTrue(any(p['current'] and p['username'] == 'inventario.demo' for p in page.context['demo_profiles']))


@tag('escenario')
class ScenarioTests(TestCase):
    """Una generación corta (5 meses) con los servicios reales: todo cuadra, los casos existen y los documentos se importan."""
    FILES = [(InvoiceFile, 'file'), (PaymentFile, 'file'), (Sale, 'pdf'), (StatementImport, 'file')]

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls._storages = []
        for model, name in cls.FILES:
            field = model._meta.get_field(name)
            cls._storages.append((field, field.storage))
            field.storage = FileSystemStorage(location=cls.tmp / 'adjuntos')
        cls._settings = override_settings(PRIVATE_DIR=cls.tmp, **FAST)
        cls._settings.enable()
        try:
            super().setUpClass()
        except Exception:
            cls._restore()
            raise

    @classmethod
    def _restore(cls):
        cls._settings.disable()
        for field, storage in cls._storages:
            field.storage = storage
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._restore()

    @classmethod
    def setUpTestData(cls):
        cls.state, cls.passwords = generate(timezone.localdate(), seed=4711, months=5)

    def client_for(self, role):
        c = Client()
        self.assertEqual(c.post(reverse('demo_profile'), {'perfil': role}).status_code, 302)
        return c

    def file(self, kind, index=0):
        doc = [f for f in self.state.cases['documentos']['files'] if f['kind'] == kind][index]
        return doc, (documentos.folder() / doc['name']).read_bytes()

    def test_everything_adds_up_and_the_prepared_cases_exist(self):
        results = verificacion.run(self.state)
        failed = [f'{r.group} · {r.name} — {r.detail}' for r in results if not r.ok]
        self.assertEqual(failed, [])
        self.assertGreater(len(results), 40)
        self.assertEqual(self.state.summary['anomalias'], [])
        self.assertEqual(set(self.passwords), set(self.state.cases['accounts'].values()))
        self.assertTrue(all(len(p) >= 12 for p in self.passwords.values()))
        self.assertEqual(differences(self.state.summary, self.state.summary), [])
        self.assertEqual(documentos.folder(), self.tmp / 'documentos_demo')                           # nunca la carpeta del proyecto en pruebas

    def test_documents_page_lists_and_serves_only_the_scenario_files(self):
        self.assertEqual(Client().get(reverse('demo_documents')).status_code, 302)                   # exige sesión
        c = self.client_for('inventory')
        page = c.get(reverse('demo_documents'))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(page.context['files']), 7)
        for f in page.context['files']:
            response = c.get(reverse('demo_document', args=[f['name']]))
            self.assertEqual(response.status_code, 200)
            self.assertIn('attachment', response['Content-Disposition'])
            self.assertEqual(b''.join(response.streaming_content), (documentos.folder() / f['name']).read_bytes())
        for name in ('secret.key', '..%2Fsecret.key', 'feria.sqlite3'):
            self.assertEqual(c.get(f'/feria/documentos/{name}').status_code, 404)
        sheet = (documentos.folder() / 'codigos_recepcion.html').read_text(encoding='utf-8')
        self.assertEqual(sheet.count('<svg'), len(self.state.cases['recepcion']['codes']))
        self.assertNotRegex(sheet.replace('http://www.w3.org/2000/svg', ''), r'https?://|src=|href=')   # sin recursos externos
        for code in self.state.cases['recepcion']['codes']:
            self.assertIn(code, sheet)
            self.assertContains(page, code)

    def lines_post(self, lines):
        post = {'lineas-TOTAL_FORMS': len(lines), 'lineas-INITIAL_FORMS': len(lines), 'lineas-MIN_NUM_FORMS': 0, 'lineas-MAX_NUM_FORMS': 60}
        for i, line in enumerate(lines):
            self.assertIsNotNone(line['product'], line)                                              # la referencia se reconoce sola
            post.update({f'lineas-{i}-{k}': line[k] if line[k] is not None else '' for k in
                         ('product', 'supplier_code', 'description', 'quantity', 'unit_price', 'discount', 'tax_rate', 'expected_lot', 'expected_expiry')})
        return post

    def import_invoice(self, kind, index):
        doc, raw = self.file(kind, index)
        c = self.client_for('inventory')
        mime = 'application/pdf' if kind == 'factura_pdf' else 'application/xml'
        response = c.post(reverse('invoice_import'), {'file': SimpleUploadedFile(doc['name'], raw, content_type=mime)})
        self.assertEqual(response.status_code, 302, response.content[:500])
        page = c.get(response.url)
        self.assertEqual(page.status_code, 200)
        form, lines = page.context['form'], page.context['lines']
        post = {'token': str(uuid.uuid4()), **{k: form.initial.get(k) or '' for k in ('supplier', 'number', 'issue_date', 'due_date', 'ordered_on', 'order_number', 'cufe')},
                'global_discount': '0', 'retention': '0', 'notes': '', **self.lines_post(lines.initial)}
        if kind == 'factura_pdf':
            terms = page.context['payment_terms'].initial
            post.update({'supplier_name': form.initial['supplier_name'], 'supplier_nit': form.initial['supplier_nit'], 'pdf_currency': 'COP',
                         'pdf_total': form.initial['pdf_total'], 'pdf_reviewed': 'on', **{f'pago-{k}': v for k, v in terms.items() if v not in (None, '')}})
        saved = c.post(response.url, post)
        invoice = Invoice.objects.filter(number=doc['number']).first()
        self.assertIsNotNone(invoice, saved.content.decode()[:1500] if saved.status_code == 200 else saved.status_code)
        self.assertRedirects(saved, reverse('invoice', args=[invoice.pk]), fetch_redirect_response=False)
        order = PurchaseOrder.objects.get(number=doc['order_number'])
        self.assertEqual((invoice.source, invoice.review, invoice.files.count(), order.invoice_id), (kind[-3:], 'pending', 1, invoice.pk))
        self.assertEqual({l.product_id: l.quantity for l in invoice.lines.all()}, {l.product_id: l.quantity for l in order.lines.all()})
        return invoice

    def test_demo_invoices_in_pdf_are_imported_through_the_screen(self):
        for index in (0, 1):
            invoice = self.import_invoice('factura_pdf', index)
            self.assertTrue(invoice.document_terms)

    def test_demo_invoices_in_xml_are_imported_through_the_screen(self):
        for index in (0, 1):
            self.import_invoice('factura_xml', index)

    def test_demo_statement_is_imported_through_the_screen(self):
        from inventory.payables import outflow_candidates
        doc, raw = self.file('extracto')
        c = self.client_for('finance')
        before = BankTransaction.objects.count()
        upload = lambda: {'token': str(uuid.uuid4()), 'account': self.state.cases['banco']['account_id'],
                          'file': SimpleUploadedFile(doc['name'], raw, content_type='text/csv')}
        response = c.post(reverse('bank_import'), upload())
        self.assertEqual(response.status_code, 302, response.content[:500])
        imp = StatementImport.objects.order_by('-pk').first()
        self.assertEqual((imp.status, imp.known_rows, imp.warnings), ('preview', 0, []))
        self.assertGreaterEqual(imp.new_rows, 4)
        self.assertRedirects(c.post(response.url, {'token': str(uuid.uuid4()), 'action': 'confirm'}), reverse('bank'), fetch_redirect_response=False)
        new = BankTransaction.objects.filter(statement=imp)
        self.assertEqual((new.count(), BankTransaction.objects.count() - before), (imp.total_rows, imp.total_rows))
        mention = new.filter(description__startswith='PAGO FE ').first()
        self.assertEqual(C.suggestions(mention)['decision'], 'suggested')                             # un ingreso nuevo con sugerencia clara
        outflow = new.filter(description__startswith='PAGO PROVEEDORES').first()
        self.assertEqual(len(outflow_candidates(outflow)), 1)                                         # y un egreso que cruza con un pago confirmado
        self.assertEqual(c.post(reverse('bank_import'), upload()).status_code, 302)
        self.assertEqual(StatementImport.objects.order_by('-pk').first().new_rows, 0)                 # repetirlo no duplica nada

    def test_reception_codes_work_typed_into_the_reader_field(self):
        case = self.state.cases['recepcion']
        c = self.client_for('reception')
        self.assertEqual(c.get(case['url']).status_code, 200)
        invoice = Invoice.objects.get(pk=case['invoice_id'])
        session = invoice.receiving_sessions.get(status='open')
        scan = reverse('reception_scan', args=[session.pk])
        for code in case['codes']:
            data = c.post(scan, {'codigo': code}, HTTP_X_REQUESTED_WITH='fetch').json()
            self.assertEqual((data['kind'], data['ok']), ('added', True), data['message'])
        repeated = c.post(scan, {'codigo': case['codes'][0]}, HTTP_X_REQUESTED_WITH='fetch').json()
        self.assertEqual(repeated['kind'], 'duplicate')                                               # cada caja entra una sola vez
        done = c.post(reverse('reception_confirm', args=[session.pk]), {'token': str(uuid.uuid4()), 'fecha': timezone.localdate().isoformat(),
                                                                         'ubicacion': 'Bodega principal', 'nota': ''})
        self.assertRedirects(done, reverse('reception'), fetch_redirect_response=False)
        invoice = Invoice.objects.get(pk=invoice.pk)
        self.assertEqual((invoice.reception, invoice.received_units), ('complete', invoice.expected_units))
        self.assertEqual(PurchaseOrder.objects.get(pk=case['order_id']).status, 'received')

    def test_generation_refuses_a_database_that_already_has_data(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            generate(timezone.localdate(), seed=4711, months=5)


@tag('lenta')
@skipUnless(os.environ.get('FERIA_PRUEBAS_LENTAS') == '1', 'Prueba lenta: active FERIA_PRUEBAS_LENTAS=1 o use «verificar_escenario --reproducibilidad».')
class ReproducibilityTests(SimpleTestCase):
    def test_two_generations_with_the_same_seed_and_date_match(self):
        summaries = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                env = {k: v for k, v in os.environ.items() if not k.startswith('FERIA_')}
                env.update({'FERIA_PRIVATE_DIR': tmp, 'PYTHONIOENCODING': 'utf-8'})
                out = Path(tmp) / 'huella.json'
                done = subprocess.run([sys.executable, 'manage.py', 'reiniciar_feria', '--si', '--fecha', '2026-11-18', '--meses', '5', '--resumen', str(out)],
                                      cwd=BASE, env=env, capture_output=True, text=True, encoding='utf-8', timeout=900)
                self.assertEqual(done.returncode, 0, done.stdout[-800:] + done.stderr[-800:])
                summaries.append(json.loads(out.read_text(encoding='utf-8')))
        self.assertEqual(differences(*summaries), [])


# ------------------------------------------------------------------------------------------------------------------
# Restauración rápida del escenario inicial.

import sqlite3
from django.db import connection
from django.test import TransactionTestCase
from cartera.models import BankAccount, Party
from contable import services as contable_services
from feria.huella import table_summary
from inventory import services as S
from inventory.models import Batch, Product

CONSUME = '''
import uuid
from django.utils import timezone
from agentes import runner
from feria.models import DemoState
from inventory import recepcion
from inventory.models import Invoice, User
cases = DemoState.current().cases
users = {u.username: u for u in User.objects.all()}
who = lambda role: users[cases['accounts'][role]]
assert runner.decide(who('inventory'), cases['reposicion']['action_id'], True).status == 'executed'
invoice = Invoice.objects.get(pk=cases['recepcion']['invoice_id'])
session = recepcion.open_session(who('reception'), invoice)
for code in cases['recepcion']['codes']:
    assert recepcion.scan(who('reception'), session.pk, code)['ok']
recepcion.confirm(who('reception'), session.pk, timezone.localdate(), 'Bodega principal', '', uuid.uuid4())
assert runner.decide(who('finance'), cases['banco']['claro']['action_id'], True).status == 'executed'
print('casos consumidos')
'''


class RestoreGuardTests(SimpleTestCase):
    """`restaurar_feria` usa las guardas del reinicio y no hace nada si falta la copia."""

    def test_restore_refuses_outside_the_data_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            keep = Path(tmp) / 'adjuntos' / 'soporte.txt'
            keep.parent.mkdir()
            keep.write_text('no se toca', encoding='utf-8')
            for args in (('--si',), ('--guardar-copia',)):
                with self.subTest(args=args), override_settings(PRIVATE_DIR=Path(tmp)), self.assertRaisesMessage(CommandError, 'No se restaura nada'):
                    call_command('restaurar_feria', *args)
            self.assertTrue(keep.is_file())
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ['adjuntos'])

    def test_restore_fails_clearly_when_there_is_no_copy(self):
        expected = reinicio.snapshot_path(reinicio.check_target())
        self.assertEqual(expected.name, 'escenario_inicial.test.sqlite3')              # la copia de pruebas nunca es la de la demostración
        if expected.exists():
            self.skipTest('Hay una copia de pruebas en la carpeta de datos.')
        with self.assertRaisesMessage(CommandError, 'No hay copia del escenario inicial'):
            call_command('restaurar_feria', '--si')

    def test_each_database_has_its_own_copy_next_to_it(self):
        data = BASE / 'datos'
        self.assertEqual(reinicio.snapshot_path(data / 'feria.sqlite3'), data / 'escenario_inicial.sqlite3')
        self.assertEqual(reinicio.snapshot_path(data / 'test.sqlite3'), data / 'escenario_inicial.test.sqlite3')
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesMessage(reinicio.NoSnapshot, 'No hay copia'):
            reinicio.read_snapshot(Path(tmp) / 'feria.sqlite3')
        script = (BASE / 'RESTAURAR_FERIA.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('restaurar_feria --si', script)
        self.assertIn('$PSScriptRoot', script)
        self.assertNotRegex(script, r'(?<![A-Za-z])[A-Za-z]:[\\/]')


@override_settings(**FAST)
class RestoreTests(TransactionTestCase):
    """Tras preparar un pedido, recibir mercancía y aplicar un ingreso, restaurar devuelve exactamente la huella inicial."""
    FILES = [(InvoiceFile, 'file'), (PaymentFile, 'file'), (Sale, 'pdf'), (StatementImport, 'file')]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._storages = []
        for model, name in self.FILES:
            field = model._meta.get_field(name)
            self._storages.append((field, field.storage))
            field.storage = FileSystemStorage(location=self.tmp / 'adjuntos')
        self.db = Path(connection.settings_dict['NAME'])
        self.snapshot = self.tmp / 'copia_inicial.sqlite3'
        contable_services.sync_catalogs()
        self.finance = User.objects.create_user('contabilidad', password=PASSWORD, role='finance', must_change_password=False)
        self.manager = User.objects.create_user('inventario', password=PASSWORD, role='inventory', must_change_password=False)
        self.supplier = Supplier.objects.create(name='Suministros de Prueba S.A.S.', nit='900555091', payment_days=30)
        self.product = Product.objects.create(code='SEL-900', name='Sellante de prueba 300 ml', pack_size=12)
        S.adjust_in(self.manager, self.product, 'L-INI', TODAY() + timedelta(days=300), 'Bodega principal', 40, 'initial', 'Saldo inicial', tok())
        self.party = C.save_party(self.finance, None, {'id_type': '31', 'identification': '900555092', 'name': 'Taller de Prueba S.A.S.', 'kind': 'company',
                                                      'default_context': 'company', 'price_list': None, 'credit_days': 30, 'requires_po': False, 'active': True,
                                                      'notes': '', 'einvoice_email': 'facturacion@cliente.example'}, tok())
        self.receivable = C.add_opening(self.finance, {'party': self.party, 'prefix': 'FE', 'number': 9001, 'quote': 1, 'issue_date': TODAY() - timedelta(days=20),
                                                       'due_date': TODAY() + timedelta(days=10), 'balance': Decimal('850000'), 'note': ''}, tok())
        self.account = BankAccount.objects.create(name='Cuenta principal', bank='Banco Ejemplo', last4='4821', contable_payment_id='301')
        self.invoice('FP-1', [('L-A', 12)], upload=('factura_FP-1.xml', b'<Invoice>antes de la copia</Invoice>'))

    def tearDown(self):
        connection.close()
        for field, storage in self._storages:
            field.storage = storage
        shutil.rmtree(self.tmp, ignore_errors=True)

    def invoice(self, number, lots, upload=None, order_number=''):
        header = {'supplier': self.supplier, 'number': number, 'issue_date': TODAY(), 'due_date': TODAY() + timedelta(days=30), 'ordered_on': TODAY(),
                  'order_number': order_number, 'cufe': '', 'global_discount': Decimal(0), 'retention': Decimal(0), 'notes': ''}
        lines = [{'product': self.product, 'supplier_code': 'SP-900', 'description': self.product.name, 'quantity': sum(q for _, q in lots),
                  'unit_price': Decimal('9000'), 'discount': Decimal(0), 'tax_rate': Decimal('19'), 'expected_lot': '', 'expected_expiry': None}]
        inv = S.create_invoice(self.manager, header, lines, [upload] if upload else [], tok(), source='xml')
        S.validate_invoice(self.finance, inv.pk, tok())
        line = inv.lines.get()
        S.receive_invoice(self.manager, inv.pk, [{'line': line.pk, 'quantity': q, 'lot': lot, 'expiry': TODAY() + timedelta(days=200), 'location': 'Bodega principal'}
                                                 for lot, q in lots], TODAY(), '', tok())
        return inv

    def consume(self):
        """Lo que hacen los pasos del recorrido: preparar y enviar un pedido, recibir su mercancía y aplicar un ingreso."""
        order = compras.create_order(self.manager, self.supplier, [{'product': self.product, 'quantity': 24}], '', tok())
        compras.mark_sent(self.manager, order.pk, tok())
        self.invoice('FP-2', [('L-B', 24)], upload=('factura_FP-2.xml', b'<Invoice>despues de la copia</Invoice>'), order_number=order.number)
        raw = f'Fecha;Descripción;Referencia;Titular;Valor\n{TODAY():%d/%m/%Y};PAGO FACTURA FE 9001;TRX1;TALLER DE PRUEBA SAS;850000.00\n'.encode()
        imp = C.preview_statement(self.finance, self.account, 'extracto.csv', raw, tok())
        C.confirm_statement(self.finance, imp.pk, tok())
        txn = BankTransaction.objects.get()
        C.apply_transaction(self.finance, txn.pk, self.party.pk, [(self.receivable.pk, Decimal('850000'))], tok())

    def files(self):
        return sorted(p.relative_to(self.tmp / 'adjuntos').as_posix() for p in (self.tmp / 'adjuntos').rglob('*') if p.is_file())

    def test_restore_returns_exactly_the_initial_fingerprint_after_consuming_cases(self):
        initial = table_summary()
        before_files = self.files()
        meta = reinicio.save_snapshot(self.db, snapshot=self.snapshot)
        self.assertEqual((meta['huella'], meta['filas'], meta['database']), (initial['huella'], initial['filas'], self.db.name))
        self.assertTrue(self.snapshot.is_file() and self.snapshot.with_suffix('.json').is_file())
        other = sqlite3.connect(str(self.db))                                            # otra conexión abierta, como la del servidor encendido
        count = lambda table: other.execute(f'select count(*) from {table}').fetchone()[0]
        self.assertEqual((count('inventory_purchaseorder'), count('cartera_banktransaction'), count('inventory_invoice')), (0, 0, 1))
        self.consume()
        changed = table_summary()
        self.assertNotEqual(changed['huella'], initial['huella'])
        self.assertEqual((count('inventory_purchaseorder'), count('cartera_banktransaction'), count('inventory_invoice')), (1, 1, 2))
        self.assertEqual((PurchaseOrder.objects.get().status, Batch.objects.get(lot='L-B').physical, Receivable.objects.get().applications.count()), ('received', 24, 1))
        self.assertGreater(len(self.files()), len(before_files))                         # la factura nueva y el extracto dejaron adjuntos
        result = reinicio.restore(self.db, snapshot=self.snapshot, attachments=self.tmp / 'adjuntos')
        self.assertTrue(result['matches'])
        self.assertEqual((result['huella'], result['expected']), (initial['huella'], initial['huella']))
        self.assertEqual(table_summary(), initial)                                       # conteos, sumas y huella, tabla por tabla
        self.assertEqual((count('inventory_purchaseorder'), count('cartera_banktransaction'), count('inventory_invoice')), (0, 0, 1))   # la otra conexión lo ve
        other.close()
        self.assertEqual(self.files(), before_files)                                     # sin adjuntos huérfanos; los anteriores a la copia siguen
        self.assertEqual(result['removed'], 2)
        self.assertEqual((PurchaseOrder.objects.count(), Batch.objects.filter(lot='L-B').count(), Receivable.objects.get().applications.count()), (0, 0, 0))
        self.consume()                                                                   # y se puede repetir la demostración y volver otra vez
        self.assertTrue(reinicio.restore(self.db, snapshot=self.snapshot, attachments=self.tmp / 'adjuntos')['matches'])

    def test_restore_reports_a_copy_that_does_not_match_and_keeps_unrelated_folders(self):
        reinicio.save_snapshot(self.db, snapshot=self.snapshot)
        meta_file = self.snapshot.with_suffix('.json')
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        meta_file.write_text(json.dumps({**meta, 'huella': '0' * 64}), encoding='utf-8')
        outside = self.tmp / 'otros'
        outside.mkdir()
        (outside / 'nota.txt').write_text('fuera de adjuntos', encoding='utf-8')
        result = reinicio.restore(self.db, snapshot=self.snapshot, attachments=outside)   # solo se limpia una carpeta llamada «adjuntos»
        self.assertFalse(result['matches'])
        self.assertEqual(result['removed'], 0)
        self.assertTrue((outside / 'nota.txt').is_file())
        meta_file.unlink()
        with self.assertRaisesMessage(reinicio.NoSnapshot, 'No hay copia'):
            reinicio.restore(self.db, snapshot=self.snapshot, attachments=self.tmp / 'adjuntos')


@tag('lenta')
@skipUnless(os.environ.get('FERIA_PRUEBAS_LENTAS') == '1', 'Prueba lenta: active FERIA_PRUEBAS_LENTAS=1.')
class RestoreScenarioTests(SimpleTestCase):
    """De punta a punta en una carpeta temporal: generar, consumir los casos de los pasos 3 a 5, restaurar y verificar."""

    def run_manage(self, tmp, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith('FERIA_')}
        env.update({'FERIA_PRIVATE_DIR': tmp, 'PYTHONIOENCODING': 'utf-8'})
        done = subprocess.run([sys.executable, 'manage.py', *args], cwd=BASE, env=env, capture_output=True, text=True, encoding='utf-8', timeout=900)
        self.assertEqual(done.returncode, 0, done.stdout[-1200:] + done.stderr[-1200:])
        return done.stdout

    def test_generate_consume_restore_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.run_manage(tmp, 'reiniciar_feria', '--si', '--meses', '5')
            self.assertIn('Copia del escenario inicial: escenario_inicial.sqlite3', out)
            meta = json.loads((Path(tmp) / 'escenario_inicial.json').read_text(encoding='utf-8'))
            self.assertIn('casos consumidos', self.run_manage(tmp, 'shell', '-c', CONSUME))
            out = self.run_manage(tmp, 'restaurar_feria', '--si')
            self.assertIn(f'huella {meta["huella"][:16]} (coincide con la copia)', out)
            self.assertIn('comprobaciones cuadran', self.run_manage(tmp, 'verificar_escenario'))
