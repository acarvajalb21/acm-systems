from django.contrib.auth import logout
from django.shortcuts import redirect

OPEN_PATHS = ('/cuenta/clave/', '/salir/', '/sw.js', '/manifest.webmanifest', '/sin-conexion/')


class AccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            if request.session.get('user_version') != request.user.session_version:
                logout(request)
                return redirect('login')
            if request.user.must_change_password and request.path not in OPEN_PATHS and not request.path.startswith('/static/'):
                return redirect('password_change')
        response = self.get_response(request)
        if not request.path.startswith('/static/'):
            response['Cache-Control'] = 'no-store, private'
        response['Content-Security-Policy'] = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                                               "base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        return response
