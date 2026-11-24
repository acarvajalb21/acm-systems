"""Comprueba que el escenario cargado cuadra y que los casos preparados existen.

    manage.py verificar_escenario                       revisa la base actual; termina con código 1 si algo falla
    manage.py verificar_escenario --reproducibilidad    además genera dos veces el escenario en carpetas temporales
                                                        (misma semilla y fecha) y compara conteos y sumas por tabla
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from feria import verificacion
from feria.huella import differences
from feria.models import DemoState


class Command(BaseCommand):
    help = 'Verifica saldos, documentos y casos preparados del escenario de la demostración.'

    def add_arguments(self, parser):
        parser.add_argument('--reproducibilidad', action='store_true', help='Genera dos veces el escenario en carpetas temporales y compara.')
        parser.add_argument('--meses', type=int, help='Meses simulados en la prueba de reproducibilidad (por defecto, los del escenario actual).')

    def handle(self, *args, **options):
        state = DemoState.current()
        if not state.ready:
            raise CommandError('Todavía no hay un escenario: ejecute primero manage.py reiniciar_feria.')
        today = timezone.localdate()
        self.stdout.write(f'Escenario del {state.reference_date:%d/%m/%Y} · semilla {state.seed} · {state.months} meses · {state.summary.get("filas", 0)} filas')
        if today != state.reference_date:
            self.stdout.write(self.style.WARNING(f'Hoy es {today:%d/%m/%Y}: los casos se prepararon para el {state.reference_date:%d/%m/%Y} y se comprueban en esa fecha. '
                                                 'Reinicie la demostración para prepararlos para hoy.'))
        results = verificacion.run(state)
        group, failed = None, 0
        for r in results:
            if r.group != group:
                group = r.group
                self.stdout.write(f'\n{group}')
            mark = self.style.SUCCESS('[ bien ]') if r.ok else self.style.ERROR('[ FALLA ]')
            self.stdout.write(f'  {mark} {r.name}' + (f' — {r.detail}' if r.detail else ''))
            failed += not r.ok
        if options['reproducibilidad']:
            self.stdout.write('\nReproducibilidad')
            diffs = self.reproducibility(state, options['meses'] or state.months)
            mark = self.style.SUCCESS('[ bien ]') if not diffs else self.style.ERROR('[ FALLA ]')
            self.stdout.write(f'  {mark} Dos generaciones con la misma semilla y fecha producen los mismos conteos y sumas por tabla'
                              + (' — ' + '; '.join(diffs[:6]) if diffs else ''))
            failed += bool(diffs)
        self.stdout.write('')
        if failed:
            raise CommandError(f'{failed} comprobación(es) no cuadran de {len(results)}.')
        self.stdout.write(self.style.SUCCESS(f'Las {len(results)} comprobaciones cuadran.'))

    def reproducibility(self, state, months):
        """Dos procesos independientes, cada uno con su carpeta de datos temporal."""
        manage = str(Path(settings.BASE_DIR) / 'manage.py')
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            runs = []
            for folder in (a, b):
                env = {k: v for k, v in os.environ.items() if not k.startswith('FERIA_')}
                env.update({'FERIA_PRIVATE_DIR': folder, 'PYTHONIOENCODING': 'utf-8'})
                out = str(Path(folder) / 'huella.json')
                cmd = [sys.executable, manage, 'reiniciar_feria', '--si', '--fecha', state.reference_date.isoformat(), '--semilla', str(state.seed),
                       '--meses', str(months), '--resumen', out]
                runs.append((subprocess.Popen(cmd, cwd=settings.BASE_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT), out))
            summaries = []
            for process, out in runs:
                output = process.communicate()[0].decode('utf-8', 'replace')
                if process.returncode != 0:
                    return ['una de las generaciones falló: ' + output.strip().splitlines()[-1][:200]]
                summaries.append(json.loads(Path(out).read_text(encoding='utf-8')))
        diffs = differences(*summaries)
        if not diffs and months == state.months and state.summary.get('huella') != summaries[0]['huella']:
            diffs = differences({k: state.summary[k] for k in ('tablas', 'huella')}, summaries[0])
            diffs = ['la base actual difiere de una generación nueva: ' + d for d in diffs]
        self.stdout.write(f'  dos generaciones de {months} meses · {summaries[0]["filas"]} filas cada una · huella {summaries[0]["huella"][:16]}')
        return diffs
