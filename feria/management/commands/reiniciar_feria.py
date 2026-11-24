"""Reinicia la demostración: borra la base local y genera de nuevo el escenario sintético.

    manage.py reiniciar_feria                       pide confirmación; fecha de referencia = hoy; semilla 4711
    manage.py reiniciar_feria --si                  sin preguntar
    manage.py reiniciar_feria --fecha 2026-11-18 --semilla 4711

Solo actúa sobre `datos/feria.sqlite3` y `datos/adjuntos/` (o la carpeta indicada en FERIA_PRIVATE_DIR). Al
terminar deja las cuentas de demostración en `datos/ACCESOS_DEMO.md`, los documentos de prueba en
`documentos_demo/` y una copia del escenario recién generado en `datos/escenario_inicial.sqlite3`, que
`manage.py restaurar_feria` devuelve en segundos. Misma fecha y misma semilla producen los mismos datos.
"""
import json
import time
from datetime import date
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from feria import documentos, reinicio
from feria.escenario import DEFAULT_MONTHS, DEFAULT_SEED, fast_sqlite, generate


class Command(BaseCommand):
    help = 'Borra la base local de la demostración y genera de nuevo el escenario sintético.'

    def add_arguments(self, parser):
        parser.add_argument('--fecha', help='Fecha de referencia (AAAA-MM-DD). Por defecto, hoy.')
        parser.add_argument('--semilla', type=int, default=DEFAULT_SEED, help=f'Semilla del escenario (por defecto {DEFAULT_SEED}).')
        parser.add_argument('--si', action='store_true', help='No pedir confirmación.')
        parser.add_argument('--meses', type=int, default=DEFAULT_MONTHS, help='Meses de historia simulados (uso interno: pruebas más cortas).')
        parser.add_argument('--resumen', help='Guarda en este archivo la huella de conteos y sumas (uso interno).')

    def handle(self, *args, **options):
        try:
            path = reinicio.check_target()
        except reinicio.Unsafe as e:
            raise CommandError(f'No se reinicia nada. {e}')
        try:
            reference = date.fromisoformat(options['fecha']) if options['fecha'] else timezone.localdate()
        except ValueError:
            raise CommandError('La fecha debe tener la forma AAAA-MM-DD.')
        if not 5 <= options['meses'] <= 36:
            raise CommandError('Los meses simulados deben estar entre 5 y 36.')
        if not options['si']:
            self.stdout.write(f'Se borrará la base de la demostración ({path.name}) y sus adjuntos, y se generará de nuevo el escenario.')
            if input('Escriba SI para continuar: ').strip().upper() not in ('SI', 'SÍ'):
                raise CommandError('Reinicio cancelado: no se cambió nada.')
        started = time.monotonic()
        try:
            reinicio.reset(path)
        except PermissionError:
            raise CommandError('No se pudo borrar la base: está en uso. Detenga el servidor de la demostración y vuelva a intentarlo.')
        fast_sqlite()
        self.stdout.write(f'Generando el escenario del {reference:%d/%m/%Y} (semilla {options["semilla"]}, {options["meses"]} meses)…')
        state, passwords = generate(reference, options['semilla'], options['meses'], out=self.stdout.write)
        access = reinicio.write_access(state, passwords)
        reinicio.save_snapshot(path, state)
        summary = state.summary
        if options['resumen']:
            Path(options['resumen']).write_text(json.dumps({k: summary[k] for k in ('tablas', 'huella', 'filas')}, ensure_ascii=False, sort_keys=True), encoding='utf-8')
        self.stdout.write('')
        for key, value in summary.get('conteos', {}).items():
            self.stdout.write(f'  {key}: {value}')
        tables = summary['tablas']
        self.stdout.write(f'  filas en total: {summary["filas"]} · movimientos de inventario: {tables["inventory.movement"]["filas"]} · '
                          f'auditoría: {tables["inventory.audit"]["filas"]} · huella {summary["huella"][:16]}')
        if options['verbosity'] > 1:
            self.stdout.write('  tiempos (s): ' + ', '.join(f'{k} {v}' for k, v in summary.get('tiempos', {}).items()))
        anomalies = summary.get('anomalias', [])
        if anomalies:
            self.stdout.write(self.style.WARNING(f'  {len(anomalies)} operación(es) no se pudieron registrar durante la simulación:'))
            for line in anomalies[:10]:
                self.stdout.write(f'    {line}')
        files = state.cases.get('documentos', {}).get('files', [])
        self.stdout.write(f'Cuentas de demostración: {access.name} en la carpeta de datos. Documentos de prueba: {len(files)} en {documentos.FOLDER}/.')
        self.stdout.write(f'Copia del escenario inicial: {reinicio.snapshot_path(path).name} (se restaura en segundos con: manage.py restaurar_feria).')
        self.stdout.write(self.style.SUCCESS(f'Escenario listo en {time.monotonic() - started:.0f} s. Compruébelo con: manage.py verificar_escenario'))
