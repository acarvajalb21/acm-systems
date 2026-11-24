from django.urls import path
from . import views

urlpatterns = [
    path('presentacion/', __import__('feria.presentacion_views', fromlist=['landing']).landing, name='landing'),
    path('flujo/', __import__('feria.experiencia_views', fromlist=['flow_map']).flow_map, name='flow_map'),
    path('inteligencia/metodologia/', __import__('aprendizaje.views', fromlist=['methodology']).methodology, name='methodology'),
    path('recorrido/', __import__('feria.experiencia_views', fromlist=['tour']).tour, name='tour'),
    path('feria/perfil/', views.demo_profile, name='demo_profile'),
    path('feria/documentos/', views.demo_documents, name='demo_documents'),
    path('feria/documentos/<str:name>', views.demo_document, name='demo_document'),
]
