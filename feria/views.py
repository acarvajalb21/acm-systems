"""Vistas de la app de demostración que enruta `feria/urls.py`.

El cambio de perfil y los documentos de prueba viven en `demo_views.py`; el mapa de flujo y el recorrido, en
`experiencia_views.py`. `provisional` queda para las secciones que todavía muestran una página mínima.
"""
from django.shortcuts import render
from .demo_views import demo_document, demo_documents, demo_profile  # noqa: F401


def provisional(request, title, text):
    return render(request, 'feria/provisional.html', {'title': title, 'text': text})
