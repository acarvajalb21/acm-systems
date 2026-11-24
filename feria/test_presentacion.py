"""Página de presentación pública: abre sin sesión, la raíz lleva a ella y su contenido editable es tolerante."""
import json
import re
import tempfile
from pathlib import Path
from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from inventory.models import User
from inventory.testing import new_password
from . import presentacion_views as P

FAST = {'PASSWORD_HASHERS': ['django.contrib.auth.hashers.MD5PasswordHasher']}
LABELS = ('Institución', 'Programa', 'Asignatura', 'Tutor', 'Año', 'Contacto')
STATEMENT = ('Desarrollamos una aplicación a partir del trabajo con una empresa para integrar información operativa, anticipar necesidades de '
             'abastecimiento y asistir la ejecución de procesos mediante modelos predictivos y agentes supervisados. Esta demostración utiliza un '
             'caso empresarial y datos sintéticos.')


@override_settings(**FAST)
class PresentacionTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='presentacion-')
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / 'presentacion.json'
        override = override_settings(PRESENTACION_FILE=self.file)
        override.enable()
        self.addCleanup(override.disable)

    def write(self, data):
        self.file.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def page(self, client=None):
        response = (client or Client()).get(reverse('landing'))
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def login(self):
        user = User.objects.create_user('direccion', password=new_password(), role='owner', must_change_password=False, first_name='Dirección')
        client = Client()
        client.force_login(user)
        session = client.session
        session['user_version'] = user.session_version
        session.save()
        return client

    def test_landing_is_public_and_root_leads_to_it_without_session(self):
        self.assertEqual(reverse('landing'), '/presentacion/')
        response, html = self.page()
        self.assertTemplateUsed(response, 'feria/presentacion.html')
        self.assertTemplateNotUsed(response, 'base.html')
        self.assertContains(response, STATEMENT)
        self.assertContains(response, 'Demostración con datos sintéticos')
        self.assertIn(f'href="{reverse("login")}"', html)
        self.assertIn('Entrar a la demostración', html)
        self.assertIn("default-src 'self'", response['Content-Security-Policy'])
        # La raíz sin sesión lleva a la presentación; el resto de la aplicación sigue pidiendo sesión.
        self.assertRedirects(Client().get('/'), reverse('landing'), fetch_redirect_response=False)
        self.assertRedirects(Client().get(reverse('operations')), '/ingresar/?next=/operaciones/', fetch_redirect_response=False)
        # El acceso enlaza de vuelta a la presentación.
        self.assertContains(Client().get(reverse('login')), f'href="{reverse("landing")}"')

    def test_root_with_session_is_the_summary_and_the_app_links_back(self):
        client = self.login()
        home = client.get('/')
        self.assertEqual(home.status_code, 200)
        self.assertTemplateUsed(home, 'home.html')
        self.assertContains(home, f'href="{reverse("landing")}"')
        response, html = self.page(client)
        self.assertIn('Volver a la demostración', html)
        self.assertNotIn('Dirección', html)                      # la presentación no muestra datos de la sesión

    def test_page_has_no_inline_styles_scripts_or_external_urls_by_default(self):
        for content in (None, {'texto_equipo': '', 'equipo': []}, json.loads((Path(settings.BASE_DIR) / 'config' / 'presentacion.json').read_text(encoding='utf-8'))):
            if content is None:
                self.file.unlink(missing_ok=True)
            else:
                self.write(content)
            _, html = self.page()
            with self.subTest(content=content is not None):
                self.assertNotIn('style=', html)
                self.assertNotIn('<style', html)
                self.assertNotRegex(html, r'\son[a-z]+\s*=')
                scripts = re.findall(r'<script\b[^>]*>(.*?)</script>', html, flags=re.S)
                self.assertTrue(scripts)
                self.assertTrue(all(not body.strip() for body in scripts))
                self.assertEqual(len(re.findall(r'<script\b[^>]*\bsrc="/static/', html)), len(scripts))
                self.assertNotRegex(html, r'https?://')
                self.assertNotIn('mailto:', html)
                self.assertNotIn('//', re.sub(r'<!doctype html>', '', html).replace('</', ''))
                for attr in re.findall(r'(?:href|src)="([^"]*)"', html):
                    self.assertTrue(attr.startswith(('/', '#')), attr)

    def test_headings_are_in_order_and_the_flow_is_the_only_drawing(self):
        _, html = self.page()
        levels = [int(n) for n in re.findall(r'<h([1-6])\b', html)]
        self.assertEqual(levels.count(1), 1)
        self.assertEqual(levels[0], 1)
        self.assertTrue(all(b - a <= 1 for a, b in zip(levels, levels[1:])), levels)
        self.assertEqual(html.count('<svg class="flujo-escena"'), 6)
        self.assertIn('<title>Presentación · ', html)
        self.assertRegex(html, r'<meta name="description" content="[^"]{60,}">')
        for word in ('Solicitud', 'Inventario', 'Compra', 'Recepción', 'Venta', 'Cobro', 'Lo que es simulación', 'Quiénes somos'):
            self.assertIn(word, html)

    def test_the_3d_scene_is_local_lazy_and_has_a_static_alternative(self):
        """La maqueta 3D se sirve desde el proyecto, se carga después del primer pintado y no reemplaza al contenido."""
        _, html = self.page()
        static = Path(settings.BASE_DIR) / 'static'
        # El lienzo es decorativo y el motor no está en el HTML: lo importa presentacion.js cuando la página ya cargó.
        self.assertIn('<div class="acm-stage-view" aria-hidden="true"><canvas class="acm-stage-canvas"></canvas></div>', html)
        self.assertIn('data-scene="/static/modulos/escena3d.js"', html)
        self.assertNotIn('three', html.lower().replace('three.js · mit', ''))
        self.assertEqual(len(re.findall(r'<script\b', html)), 1)
        # Alternativa sin WebGL: póster de la maqueta y el selector con los seis dibujos y sus seis fichas.
        self.assertIn('src="/static/presentacion/maqueta.webp"', html)
        self.assertTrue((static / 'presentacion' / 'maqueta.webp').is_file())
        self.assertEqual(len(re.findall(r'<button type="button" data-step="', html)), 6)
        self.assertEqual(len(re.findall(r'<article class="acm-flow-panel ', html)), 6)
        # Motor y escena locales: ninguna dirección remota, evaluación dinámica ni trabajador en segundo plano.
        vendor = static / 'vendor' / 'three-0.187.0'
        for name in ('three.module.min.js', 'three.core.min.js', 'LICENSE'):
            self.assertTrue((vendor / name).is_file(), name)
        scene = (static / 'modulos' / 'escena3d.js').read_text(encoding='utf-8')
        self.assertIn("from '../vendor/three-0.187.0/three.module.min.js'", scene)
        self.assertEqual(len(re.findall(r'^import\b', scene, flags=re.M)), 1)
        page_script = (static / 'modulos' / 'presentacion.js').read_text(encoding='utf-8')
        self.assertIn('import(stage.dataset.scene)', page_script)
        for source in (scene, page_script):
            self.assertNotRegex(source, r'https?://')
            self.assertNotRegex(source, r'\beval\(|new Function\(|new Worker\(|fetch\(|XMLHttpRequest|localStorage|sessionStorage|document\.cookie')

    def test_page_examples_are_marked_as_examples_and_post_nothing(self):
        """Los ejemplos de la portada se calculan en el navegador: sin formularios y rotulados como simulación."""
        _, html = self.page()
        self.assertNotIn('<form', html)
        self.assertNotIn('csrfmiddlewaretoken', html)
        for text in ('Ejemplo interactivo · datos sintéticos', 'Ejemplo local: no mueve inventario en la demo.', 'Esta prueba no crea pedidos en la demo.',
                     'Simulación local · No genera pedidos', 'Resultado simulado'):
            self.assertIn(text, html)

    def test_empty_content_shows_only_the_generic_team_text(self):
        self.write({'texto_equipo': '', 'equipo': [], 'institucion': '', 'programa': ' ', 'asignatura': None, 'tutor': '', 'contacto': '', 'anio': ''})
        response, html = self.page()
        self.assertEqual(response.context['about'], {'text': 'Equipo de desarrollo del proyecto', 'team': [], 'facts': []})
        self.assertIn('Equipo de desarrollo del proyecto', html)
        self.assertNotIn('pres-ficha', html)
        self.assertNotIn('pres-equipo"', html)
        for label in LABELS:
            self.assertNotIn(f'<dt>{label}</dt>', html)
        # El archivo que se entrega está vacío: sin personas, instituciones ni contactos.
        shipped = P.load_content(Path(settings.BASE_DIR) / 'config' / 'presentacion.json')
        self.assertEqual(shipped, {'text': 'Equipo de desarrollo del proyecto', 'team': [], 'facts': []})

    def test_filled_content_appears_and_unsafe_values_are_dropped(self):
        self.write({
            'texto_equipo': 'Equipo de prueba de la presentación',
            'equipo': [
                {'nombre': 'Persona Uno de Prueba', 'rol': 'Modelos', 'descripcion': 'Pronósticos y evaluación.', 'enlace': 'https://perfil.example/uno', 'imagen': 'no-existe.png'},
                {'nombre': 'Persona Dos de Prueba', 'rol': 'Interfaz', 'enlace': 'http://sin-cifrar.example/dos'},
                {'nombre': 'Persona Tres de Prueba', 'enlace': 'javascript:alert(1)', 'imagen': '../secret.key'},
                {'nombre': '', 'rol': 'Sin nombre: no se muestra'},
                'no es un objeto',
            ],
            'institucion': 'Institución de prueba', 'programa': 'Programa de prueba', 'asignatura': '', 'tutor': 'Tutor de prueba',
            'contacto': 'equipo@ejemplo.example', 'anio': 2026, '_ayuda': 'se ignora'})
        response, html = self.page()
        about = response.context['about']
        self.assertEqual([m['name'] for m in about['team']], ['Persona Uno de Prueba', 'Persona Dos de Prueba', 'Persona Tres de Prueba'])
        self.assertEqual(about['team'][0]['link'], {'url': 'https://perfil.example/uno', 'text': 'perfil.example/uno'})
        self.assertEqual([m['link'] for m in about['team'][1:]], [None, None])
        self.assertTrue(all(m['image'] == '' for m in about['team']))
        self.assertEqual([f['label'] for f in about['facts']], ['Institución', 'Programa', 'Tutor', 'Año', 'Contacto'])
        for text in ('Equipo de prueba de la presentación', 'Persona Uno de Prueba', 'Pronósticos y evaluación.', 'Institución de prueba', 'Tutor de prueba', '2026',
                     'equipo@ejemplo.example'):
            self.assertIn(text, html)
        self.assertIn('<a href="https://perfil.example/uno" rel="noopener noreferrer">perfil.example/uno</a>', html)
        self.assertEqual(re.findall(r'https?://[^"<\s]+', html), ['https://perfil.example/uno'])
        for gone in ('sin-cifrar', 'javascript:', 'secret.key', 'no-existe.png', 'Sin nombre', 'mailto:', '<dt>Asignatura</dt>', 'se ignora', '<img class="pres-foto"'):
            self.assertNotIn(gone, html)

    def test_link_and_image_validation(self):
        for bad in ('', None, 12, 'http://a.example', 'https://', 'https://localhost', 'https://a.example/x y', 'https://usuario:clave@a.example', 'https://a.example/"><script>',
                    '//a.example', 'ftp://a.example', 'https://a.example/' + 'x' * 300):
            with self.subTest(link=bad):
                self.assertIsNone(P.safe_link(bad))
        self.assertEqual(P.safe_link('https://a.example/ruta/'), {'url': 'https://a.example/ruta/', 'text': 'a.example/ruta'})
        self.assertEqual(P.safe_link('  https://a.example  ')['url'], 'https://a.example')      # los espacios de los extremos se quitan
        for bad in ('', None, '../x.png', 'a/b.png', 'x.svg', 'x.png.exe', '.oculto.png', 'no-existe.png'):
            with self.subTest(image=bad):
                self.assertEqual(P.safe_image(bad), '')

    def test_broken_or_missing_file_does_not_break_the_page(self):
        for content in ('{ esto no es JSON', '', '[1, 2, 3]', '"texto"', 'null', '{"equipo": "no es lista", "institucion": ["x"], "anio": {"a": 1}, "texto_equipo": 7}'):
            self.write(content)
            with self.subTest(content=content):
                response, html = self.page()
                self.assertEqual(response.context['about']['team'], [])
                self.assertEqual(response.context['about']['facts'], [])
                self.assertIn('Quiénes somos', html)
        self.assertEqual(P.load_content(self.file)['text'], '7')
        self.file.unlink()
        response, html = self.page()
        self.assertEqual(response.context['about'], {'text': 'Equipo de desarrollo del proyecto', 'team': [], 'facts': []})
        self.file.write_bytes(b'\xff\xfe\x00 no es utf-8')
        self.assertEqual(self.page()[0].context['about']['team'], [])
