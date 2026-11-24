"""Asigna el perfil Dirección a una cuenta existente.

Solo una cuenta de dirección puede crear cuentas de dirección o de administración desde la plataforma; este comando
resuelve la primera cuenta de dirección de una base nueva. Uso: manage.py asignar_direccion <usuario>
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from inventory.models import User, Audit


class Command(BaseCommand):
    help = 'Asigna el perfil Dirección (acceso total y permisos de descuento) a una cuenta existente.'

    def add_arguments(self, parser):
        parser.add_argument('usuario')

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            user = User.objects.select_for_update().get(username=options['usuario'])
        except User.DoesNotExist:
            raise CommandError('No existe esa cuenta. Créela primero en Usuarios.')
        before = user.get_role_display()
        user.role, user.is_active = User.Role.OWNER, True
        user.session_version += 1
        user.save(update_fields=['role', 'is_active', 'session_version'])
        Audit.objects.create(actor=user, event='Perfil de dirección asignado desde la consola', target=str(user.pk), detail=f'Antes: {before}')
        self.stdout.write(self.style.SUCCESS(f'{user.username} ahora es Dirección (antes: {before}). Debe volver a ingresar.'))
