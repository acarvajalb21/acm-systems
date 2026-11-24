"""Configuración de la plataforma de demostración.

Todo corre en este equipo: una base SQLite, un secreto y los adjuntos viven en la carpeta `datos/` del proyecto.
No hay modo de despliegue, base de datos externa ni servicios remotos; el sistema contable, el banco y las
comunicaciones son simulados. Las únicas variables de entorno que se leen empiezan por `FERIA_`.
"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

COMPANY_NAME = 'Distribuidora Ejemplo S.A.S.'
APP_NAME = 'ACM Systems'
# Cambio rápido de perfil en la demostración (lo usa la app `feria`); FERIA_PERFILES=0 lo apaga.
FERIA = {'profile_switch': os.environ.get('FERIA_PERFILES', '1') == '1'}

# Datos locales: base, secreto y adjuntos. FERIA_PRIVATE_DIR permite usar otra carpeta (por ejemplo, una temporal).
PRIVATE_DIR = Path(os.environ.get('FERIA_PRIVATE_DIR') or BASE_DIR / 'datos')
PRIVATE_DIR.mkdir(parents=True, exist_ok=True)

_key_file = PRIVATE_DIR / 'secret.key'
if not _key_file.exists():
    try:
        with _key_file.open('x', encoding='utf-8') as f:
            f.write(secrets.token_urlsafe(64))
    except FileExistsError:
        pass
SECRET_KEY = _key_file.read_text(encoding='utf-8').strip()

DEBUG = False
# FERIA_HOSTS (opcional, separados por coma) admite otra dirección de la misma red, por ejemplo para abrir la
# demostración desde un celular.
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', 'testserver'] + [h.strip() for h in os.environ.get('FERIA_HOSTS', '').split(',') if h.strip()]

INSTALLED_APPS = [
    'django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles',
    'inventory', 'contable', 'cartera', 'agentes', 'aprendizaje', 'notificaciones', 'feria', 'copiloto',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'inventory.middleware.AccessMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'
TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'],
    'APP_DIRS': True,
    'OPTIONS': {
        'context_processors': ['django.template.context_processors.request', 'django.contrib.auth.context_processors.auth',
                               'django.contrib.messages.context_processors.messages', 'inventory.context.navigation', 'feria.context.demo'],
        'builtins': ['inventory.templatetags.ui'],
    },
}]

DATABASES = {'default': {
    'ENGINE': 'django.db.backends.sqlite3',
    'NAME': PRIVATE_DIR / 'feria.sqlite3',
    'TEST': {'NAME': PRIVATE_DIR / 'test.sqlite3'},
    'OPTIONS': {'timeout': 20, 'transaction_mode': 'IMMEDIATE'},
}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
AUTH_USER_MODEL = 'inventory.User'

# Conector contable: siempre el simulador local (ver contable/client.py). Sus documentos no tienen validez fiscal.
CONTABLE = {'mode': 'simulator', 'stamp_send': True, 'mail_send': False, 'timeout': 120}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
PASSWORD_HASHERS = ['django.contrib.auth.hashers.Argon2PasswordHasher', 'django.contrib.auth.hashers.PBKDF2PasswordHasher']
LOGIN_URL = '/ingresar/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/ingresar/'

LANGUAGE_CODE = 'es-co'
TIME_ZONE = 'America/Bogota'
USE_I18N = True
USE_TZ = True

# Estáticos: WhiteNoise los sirve directamente desde `static/`; no hay paso de `collectstatic` ni carpeta de copia.
# Tras agregar o cambiar un archivo estático basta reiniciar el servidor; el navegador revalida en cada carga.
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
WHITENOISE_USE_FINDERS = True
WHITENOISE_MAX_AGE = 0

FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

SESSION_COOKIE_NAME = 'feria_sesion'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
CSRF_COOKIE_NAME = 'feria_csrf'
CSRF_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'
