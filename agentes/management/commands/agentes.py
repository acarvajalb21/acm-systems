"""Ejecuta los agentes de la plataforma.

    manage.py agentes                 corre los agentes a los que les toca turno (seguro de repetir)
    manage.py agentes --agente cobranza
    manage.py agentes --lista

`serve.py` ya revisa los turnos cada minuto mientras el servidor está encendido. Este comando sirve para
correrlos a mano.
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from agentes import runner
from agentes.agents import REGISTRY


class Command(BaseCommand):
    help = 'Corre los agentes con turno pendiente, o solo el que se indique.'

    def add_arguments(self, parser):
        parser.add_argument('--agente', choices=sorted(REGISTRY), help='Corre solo este agente, ahora.')
        parser.add_argument('--lista', action='store_true', help='Muestra los agentes, su estado y su próxima revisión.')

    def handle(self, *args, **options):
        if options['lista']:
            for a in REGISTRY.values():
                cfg = runner.config(a.key)
                state = 'activo' if cfg.enabled else 'en pausa'
                self.stdout.write(f'{a.key:13} {state:9} {a.schedule_label:32} próxima {timezone.localtime(runner.next_run(a)):%d/%m %H:%M} · {a.name}')
            return
        if options['agente']:
            run = runner.run_agent(options['agente'], 'manual')
            if run is None or run.status != 'succeeded':
                raise CommandError(run.error if run else 'No se pudo ejecutar.')
            self.stdout.write(self.style.SUCCESS(f'{REGISTRY[run.agent].name}: {run.summary}'))
            return
        runs = runner.run_due()
        for run in runs:
            line = f'{REGISTRY[run.agent].name}: {run.summary or run.error}'
            self.stdout.write(self.style.SUCCESS(line) if run.status == 'succeeded' else self.style.ERROR(line))
        self.stdout.write(f'{len(runs)} agente(s) con turno revisado(s).')
