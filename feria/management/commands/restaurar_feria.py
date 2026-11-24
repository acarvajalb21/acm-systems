"""Devuelve la demostración al escenario inicial en segundos, sin generar todo de nuevo.

    manage.py restaurar_feria            pide confirmación y restaura la copia guardada al final del último reinicio
    manage.py restaurar_feria --si       sin preguntar
    manage.py restaurar_feria --guardar-copia
                                         guarda la copia a partir de la base actual, solo si su huella sigue siendo
                                         la de la generación (para una base generada antes de existir este comando)

Usa las mismas guardas que el reinicio: solo actúa sobre `feria.sqlite3` dentro de la carpeta de datos. Se puede
ejecutar con el servidor encendido. Los adjuntos creados después de la copia se borran; las sesiones abiertas se
cierran (se vuelve a entrar con un clic en el perfil). Si la copia se preparó para otra fecha, lo avisa: los casos
dependen de la fecha y conviene `reiniciar_feria`.
"""
import time
from datetime import date
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from feria import reinicio
from feria.huella import table_summary
from feria.models import DemoState


class Command(BaseCommand):
    help = 'Restaura la copia del escenario inicial sobre la base local de la demostración.'

    def add_arguments(self, parser):
        parser.add_argument('--si', action='store_true', help='No pedir confirmación.')
        parser.add_argument('--guardar-copia', action='store_true', help='Guardar la copia inicial desde la base actual, si no ha cambiado desde la generación.')

    def handle(self, *args, **options):
        try:
            path = reinicio.check_target()
        except reinicio.Unsafe as e:
            raise CommandError(f'No se restaura nada. {e}')
        if options['guardar_copia']:
            return self.save(path)
        try:
            snapshot, meta = reinicio.read_snapshot(path)
        except reinicio.NoSnapshot as e:
            raise CommandError(str(e))
        reference = date.fromisoformat(meta['reference_date']) if meta.get('reference_date') else None
        if not options['si']:
            self.stdout.write(f'Se descartará lo hecho en la demostración y la base volverá al escenario inicial'
                              + (f' del {reference:%d/%m/%Y}.' if reference else '.'))
            if input('Escriba SI para continuar: ').strip().upper() not in ('SI', 'SÍ'):
                raise CommandError('Restauración cancelada: no se cambió nada.')
        started = time.monotonic()
        result = reinicio.restore(path)
        elapsed = time.monotonic() - started
        if not result['matches']:
            raise CommandError('La base restaurada no coincide con la huella guardada de la copia. Genere de nuevo el escenario con «manage.py reiniciar_feria».')
        self.stdout.write(f'Escenario inicial restaurado en {elapsed:.1f} s · {meta.get("filas", 0)} filas · huella {result["huella"][:16]} (coincide con la copia).')
        if result['removed']:
            self.stdout.write(f'Se borraron {result["removed"]} adjunto(s) creados después de la copia.')
        self.stdout.write('Las sesiones abiertas se cerraron: vuelva a entrar con el perfil de demostración. No hace falta reiniciar el servidor.')
        today = timezone.localdate()
        if reference and reference != today:
            self.stdout.write(self.style.WARNING(
                f'Atención: la copia es del escenario preparado para el {reference:%d/%m/%Y} y hoy es {today:%d/%m/%Y}. Los casos (vencimientos, pronto pago, '
                'cartera, turnos de los agentes) dependen de la fecha: para prepararlos para hoy ejecute «manage.py reiniciar_feria».'))
        else:
            self.stdout.write(self.style.SUCCESS('Listo para la siguiente demostración.'))

    def save(self, path):
        state = DemoState.current()
        if not state.ready or not state.summary.get('huella'):
            raise CommandError('La base no tiene un escenario generado: ejecute «manage.py reiniciar_feria».')
        live = table_summary()['huella']
        if live != state.summary['huella']:
            raise CommandError('La base ya cambió desde que se generó el escenario (su huella no es la de la generación): no sirve como copia inicial. '
                               'Ejecute «manage.py reiniciar_feria», que genera el escenario y guarda la copia.')
        meta = reinicio.save_snapshot(path, state)
        self.stdout.write(self.style.SUCCESS(f'Copia del escenario inicial guardada ({reinicio.snapshot_path(path).name}) · {meta["filas"]} filas · huella {meta["huella"][:16]}.'))
