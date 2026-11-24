"""Página de presentación pública del proyecto (sin sesión) y lectura de su contenido editable.

La sección «Quiénes somos» sale de `config/presentacion.json`. La lectura es tolerante: un archivo ausente o
dañado, un campo vacío o de otro tipo, un enlace que no use el esquema https o una imagen que no exista en
`static/equipo/` simplemente no se muestran; la página nunca falla por su contenido.
"""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from django.conf import settings
from django.shortcuts import render
from . import flow

DEFAULT_TEAM_TEXT = 'Equipo de desarrollo del proyecto'
MAX_MEMBERS = 24
IMAGE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.(?:png|jpe?g|webp)')
FACTS = (('institucion', 'Institución'), ('programa', 'Programa'), ('asignatura', 'Asignatura'), ('tutor', 'Tutor'), ('anio', 'Año'), ('contacto', 'Contacto'))

# Lo que ocurre en cada estación del flujo, en una frase (el dibujo es el del mapa de flujo de la aplicación).
STEPS = (
    ('solicitud', 'Solicitud', 'Un área pide unidades de una referencia y dice para qué las necesita.'),
    ('inventario', 'Inventario', 'Las existencias por lote se comparan con el consumo esperado de cada referencia.'),
    ('compra', 'Compra', 'Un agente propone el pedido al proveedor; al aprobarlo queda preparado y su envío es simulado.'),
    ('recepcion', 'Recepción', 'La mercancía entra contra la factura, caja por caja, y cada unidad queda en su lote.'),
    ('venta', 'Venta', 'Sale primero el lote que vence primero y la venta va al sistema contable simulado.'),
    ('cobro', 'Cobro', 'El ingreso del extracto bancario se cruza con las facturas abiertas del cliente.'),
)


def content_file():
    return Path(getattr(settings, 'PRESENTACION_FILE', None) or Path(settings.BASE_DIR) / 'config' / 'presentacion.json')


def _text(value, limit=200):
    """Texto corto y limpio; cualquier otra cosa (listas, objetos, nulos) queda vacía."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ''
    return ' '.join(str(value).split())[:limit]


def safe_link(value):
    """Solo enlaces con esquema https, bien formados y sin credenciales. Devuelve {'url', 'text'} o None."""
    url = value.strip() if isinstance(value, str) else ''
    if not url.startswith('https://') or len(url) > 200 or re.search(r'[\s"\'<>\\^`{|}]', url):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname or ''
    except ValueError:
        return None
    if parts.scheme != 'https' or not host or '.' not in host or parts.username or parts.password:
        return None
    return {'url': url, 'text': (parts.netloc + parts.path).rstrip('/')}


def safe_image(value):
    """Nombre de un archivo de imagen que existe en `static/equipo/`; devuelve su ruta estática o ''."""
    name = value.strip() if isinstance(value, str) else ''
    if not IMAGE_NAME.fullmatch(name) or not (Path(settings.BASE_DIR) / 'static' / 'equipo' / name).is_file():
        return ''
    return f'equipo/{name}'


def load_content(path=None):
    """Contenido de «Quiénes somos» ya validado: solo lo que tiene valor."""
    try:
        raw = json.loads(Path(path or content_file()).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    team = []
    members = raw.get('equipo')
    for m in members[:MAX_MEMBERS] if isinstance(members, list) else []:
        if not isinstance(m, dict) or not _text(m.get('nombre'), 120):
            continue
        team.append({'name': _text(m.get('nombre'), 120), 'role': _text(m.get('rol'), 120), 'about': _text(m.get('descripcion'), 400),
                     'link': safe_link(m.get('enlace')), 'image': safe_image(m.get('imagen'))})
    facts = [{'label': label, 'value': _text(raw.get(key))} for key, label in FACTS if _text(raw.get(key))]
    return {'text': _text(raw.get('texto_equipo'), 300) or DEFAULT_TEAM_TEXT, 'team': team, 'facts': facts}


def landing(request):
    """Presentación del proyecto. Es pública: no muestra ningún dato de la base, solo texto fijo y el archivo de contenido."""
    steps = [{'key': key, 'order': i, 'label': label, 'text': text, 'scene': flow.SCENES[key], 'clip': flow.CLIP}
             for i, (key, label, text) in enumerate(STEPS, start=1)]
    # `crates`: las 35 casillas del laboratorio de abastecimiento (7 × 5); su estado lo calcula el navegador.
    return render(request, 'feria/presentacion.html', {'steps': steps, 'about': load_content(), 'crates': range(35)})
