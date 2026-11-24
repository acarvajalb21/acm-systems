from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def operations(request):
    """Punto de entrada a las tareas disponibles para el perfil de la persona."""
    return render(request, 'operations.html', {'section': 'operations'})
