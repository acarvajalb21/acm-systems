"""Avisos internos (una sola vez por persona y alerta) y app instalable. Nada sale de la plataforma."""
import json
from datetime import timedelta
from django.test import Client, TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from agentes import runner
from inventory.models import AlertRead, User, Product, Batch
from inventory.testing import new_password
from . import notifier
from .models import Notice

FAST = {'PASSWORD_HASHERS': ['django.contrib.auth.hashers.MD5PasswordHasher']}
PASSWORD = new_password()


@override_settings(**FAST)
class NoticeTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user('inventario', password=PASSWORD, role='inventory', must_change_password=False)
        self.requester = User.objects.create_user('solicitante', password=PASSWORD, role='requester', must_change_password=False)
        self.inactive = User.objects.create_user('retirado', password=PASSWORD, role='inventory', must_change_password=False, is_active=False)

    def login(self, user):
        c = Client()
        c.force_login(user)
        s = c.session
        s['user_version'] = user.session_version
        s.save()
        return c

    def test_new_alerts_are_registered_once_per_person(self):
        product = Product.objects.create(code='REF-A', name='Referencia A', minimum=5)        # agotada: alerta crítica para inventario
        r = notifier.dispatch()
        self.assertEqual((r['new'], r['people'], r['users']), (1, 1, 2))                      # la cuenta inactiva no cuenta
        notice = Notice.objects.get()
        self.assertEqual((notice.user, notice.key, notice.level, notice.title), (self.manager, f'stock-{product.pk}', 'critical', 'Referencia agotada'))
        self.assertTrue(notice.url.startswith('/'))                                           # enlaces internos solamente
        self.assertEqual(notifier.dispatch()['new'], 0)                                       # la misma alerta no se repite
        self.assertEqual(Notice.objects.count(), 1)
        # Una alerta distinta sí se registra; la misma alerta con otra situación (nueva revisión), también.
        Batch.objects.create(product=product, lot='L1', expiry=timezone.localdate() - timedelta(days=1), location='Bodega 1', physical=3)
        self.assertEqual(notifier.dispatch()['new'], 1)
        Batch.objects.create(product=product, lot='L2', expiry=timezone.localdate() + timedelta(days=300), location='Bodega 1', physical=2)
        self.assertEqual(notifier.dispatch()['new'], 1)                                       # ahora «en el mínimo o por debajo»: cambió el detalle
        self.assertEqual(Notice.objects.filter(user=self.manager, key=f'stock-{product.pk}').count(), 2)
        self.assertFalse(Notice.objects.exclude(user=self.manager).exists())

    def test_read_alerts_are_not_registered_and_old_notices_are_pruned(self):
        product = Product.objects.create(code='REF-B', name='Referencia B', minimum=2)
        [alert] = [a for a in notifier.S.alerts(self.manager) if a['key'] == f'stock-{product.pk}']
        AlertRead.objects.create(user=self.manager, key=alert['key'], revision=alert['revision'])
        self.assertEqual(notifier.dispatch()['new'], 0)
        old = Notice.objects.create(user=self.manager, key='antiguo', revision='x', level='info', title='Aviso antiguo')
        Notice.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=notifier.KEEP_DAYS + 1))
        notifier.dispatch()
        self.assertFalse(Notice.objects.filter(pk=old.pk).exists())

    def test_agent_runs_alone_and_the_alerts_page_lists_recent_notices(self):
        Product.objects.create(code='REF-C', name='Referencia C', minimum=5)
        run = runner.run_agent('avisos', 'manual')
        self.assertEqual(run.status, 'succeeded', run.error)
        self.assertEqual(run.metrics['new'], 1)
        self.assertIn('Nada salió de la plataforma', run.summary)
        self.assertEqual(runner.run_agent('avisos', 'manual').metrics['new'], 0)
        page = self.login(self.manager).get(reverse('alerts'))
        self.assertContains(page, 'Avisos recientes')
        self.assertEqual([n.title for n in page.context['notices']], ['Referencia agotada'])
        other = self.login(self.requester).get(reverse('alerts'))
        self.assertEqual(other.context['notices'], [])
        self.assertContains(other, 'Sin avisos todavía')


@override_settings(**FAST)
class InstallableAppTests(TestCase):
    def test_manifest_service_worker_and_offline_page(self):
        anon = Client()
        m = anon.get(reverse('manifest'))
        self.assertEqual(m['Content-Type'], 'application/manifest+json; charset=utf-8')
        data = json.loads(m.content)
        self.assertEqual((data['display'], data['start_url'], data['theme_color'], data['background_color']), ('standalone', '/', '#141d20', '#F1F5F0'))
        self.assertEqual(data['name'], 'ACM Systems')
        self.assertTrue(any(i['purpose'] == 'maskable' for i in data['icons']))
        self.assertTrue(all(i['src'].startswith('/static/icono/') for i in data['icons']))
        sw = anon.get(reverse('service_worker'))
        self.assertEqual(sw['Service-Worker-Allowed'], '/')
        body = sw.content.decode()
        self.assertIn('/sin-conexion/', body)
        for gone in ("addEventListener('push'", 'showNotification', 'pushManager', 'http://', 'https://'):
            self.assertNotIn(gone, body)
        offline = anon.get(reverse('offline'))
        self.assertContains(offline, 'Sin conexión')
        self.assertContains(offline, 'Demostración con datos sintéticos')

    def test_service_worker_is_reachable_before_changing_the_password(self):
        user = User.objects.create_user('nueva', password=PASSWORD, role='inventory', must_change_password=True)
        c = Client()
        c.force_login(user)
        s = c.session
        s['user_version'] = user.session_version
        s.save()
        self.assertEqual(c.get(reverse('service_worker')).status_code, 200)
        self.assertEqual(c.get(reverse('home')).status_code, 302)

    def test_external_push_routes_no_longer_exist(self):
        for name in ('push_devices', 'push_subscribe', 'push_unsubscribe', 'push_test', 'push_preferences'):
            with self.assertRaises(NoReverseMatch):
                reverse(name)
        self.assertEqual(Client().get('/cuenta/avisos/').status_code, 404)
