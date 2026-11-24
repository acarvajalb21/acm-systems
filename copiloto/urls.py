from django.urls import path
from . import views

urlpatterns = [
    path('copiloto/', views.copilot, name='copilot'),
    path('copiloto/preguntar/', views.copilot_ask, name='copilot_ask'),
]
