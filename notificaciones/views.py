"""App instalable: manifiesto, service worker y página sin conexión. Todo se sirve desde este equipo."""
import hashlib
import json
from django.conf import settings
from django.contrib.staticfiles import finders
from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.templatetags.static import static

THEME_COLOR = '#141d20'
BACKGROUND_COLOR = '#F1F5F0'
ICON_192, ICON_512, ICON_SVG = 'icono/app-192.png', 'icono/app-512.png', 'icono/icono.svg'


def manifest(request):
    data = {
        'name': settings.APP_NAME, 'short_name': 'ACM Systems', 'lang': 'es-CO', 'dir': 'ltr',
        'description': f'Inventario, compras, ventas y alertas de {settings.COMPANY_NAME}. Demostración con datos sintéticos.',
        'id': '/', 'start_url': '/', 'scope': '/', 'display': 'standalone', 'orientation': 'portrait',
        'background_color': BACKGROUND_COLOR, 'theme_color': THEME_COLOR,
        'icons': [{'src': static(ICON_192), 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
                  {'src': static(ICON_512), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
                  {'src': static(ICON_512), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
                  {'src': static(ICON_SVG), 'sizes': 'any', 'type': 'image/svg+xml', 'purpose': 'any'}],
        'shortcuts': [{'name': 'Alertas', 'url': '/alertas/', 'icons': [{'src': static(ICON_192), 'sizes': '192x192'}]},
                      {'name': 'Recibir mercancía', 'url': '/recepcion/', 'icons': [{'src': static(ICON_192), 'sizes': '192x192'}]}],
    }
    response = HttpResponse(json.dumps(data, ensure_ascii=False), content_type='application/manifest+json; charset=utf-8')
    response['Cache-Control'] = 'public, max-age=3600'
    return response


def _version():
    """Huella corta de la hoja de estilos: al cambiar los estáticos, el service worker renueva su caché."""
    path = finders.find('app.css')
    try:
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except (OSError, TypeError):
        return 'base'


def service_worker(request):
    body = render_to_string('notificaciones/sw.js', {'version': _version(), 'icon': static(ICON_192), 'styles': static('app.css')})
    response = HttpResponse(body, content_type='application/javascript; charset=utf-8')
    response['Service-Worker-Allowed'] = '/'
    response['Cache-Control'] = 'no-cache'
    return response


def offline(request):
    return render(request, 'notificaciones/offline.html', {'theme_color': THEME_COLOR, 'icon': ICON_192, 'app_name': settings.APP_NAME})
