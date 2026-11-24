from django.core.management.base import BaseCommand
from contable import services as Q


class Command(BaseCommand):
    help = 'Retoma envíos al sistema contable pendientes o inciertos con su misma clave de idempotencia.'

    def add_arguments(self, parser):
        parser.add_argument('--catalogos', action='store_true', help='Actualiza también los catálogos activos del sistema contable.')

    def handle(self, *args, **options):
        if options['catalogos']:
            for kind, result in Q.sync_catalogs().items():
                self.stdout.write(f'Catálogo {kind}: {result}')
        done = Q.process_pending()
        for sub in done:
            self.stdout.write(f'{sub.get_kind_display()} {sub.target_id}: {sub.get_status_display()} {sub.last_error}'.strip())
        self.stdout.write(self.style.SUCCESS(f'{len(done)} envío(s) revisado(s).'))
