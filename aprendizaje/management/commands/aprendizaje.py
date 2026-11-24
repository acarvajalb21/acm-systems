"""Estado y mantenimiento de los modelos que aprenden.

    manage.py aprendizaje                 estado de los tres modelos
    manage.py aprendizaje --reentrenar    reconstruye el modelo de pagos con el historial identificado vigente
    manage.py aprendizaje --evaluar       cierra recordatorios de cobro y completa los pronósticos semanales con lo real
    manage.py aprendizaje --reposicion    imprime la sugerencia de reposición por referencia
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from aprendizaje import reposicion as R, pagos, cobranza


class Command(BaseCommand):
    help = 'Estado y mantenimiento de los modelos que aprenden (reposición, pagos, cobranza).'

    def add_arguments(self, parser):
        parser.add_argument('--reentrenar', action='store_true')
        parser.add_argument('--evaluar', action='store_true')
        parser.add_argument('--reposicion', action='store_true')

    def handle(self, *args, **o):
        today = timezone.localdate()
        if o['reentrenar']:
            n = pagos.retrain()
            self.stdout.write(self.style.SUCCESS(f'Modelo de pagos reconstruido con {n} ingresos identificados vigentes.'))
        if o['evaluar']:
            closed = cobranza.evaluate(today)
            weeks = R.record_forecasts(R.plans(today=today), today)
            self.stdout.write(self.style.SUCCESS(f'Cobranza: {closed}. Pronósticos: {weeks} semana(s) completadas con lo real.'))
        if o['reposicion']:
            for pl in R.plans(today=today):
                test = f'prueba MAE {pl.test["mae"]:.1f} (n={pl.test["n"]})' if pl.test else 'sin prueba'
                self.stdout.write(f'{pl.status_label:22} {pl.product.name[:26]:26} tasa {pl.rate_label:>6}/sem  H={pl.horizon_weeks}  posición {pl.position:4}  '
                                  f's={pl.reorder_point:5.0f} S={pl.target:5.0f}  pedir {pl.quantity:4}  [{pl.label}; {test}]')
        st, acc, cb = pagos.status(), R.accuracy(today=today), cobranza.overview()
        self.stdout.write(f"Pagos: {st['examples']} ejemplos vigentes ({st['voided']} anulados), {st['features']} rasgos; primer candidato acertado "
                          f"{st['eval_hits']} de {st['eval_n']} ya entrenado (reglas solas: {st['rules_hits']}); {len(st['relations'])} relaciones.")
        mae = f"{acc['mae']:.2f}" if acc['n'] else 'sin medir'
        self.stdout.write(f"Reposición: pronóstico semanal guardado, error medio {mae} en {acc['n']} semana(s) cerrada(s); "
                          f"{acc['inside']} de {acc['interval_n']} dentro del intervalo del 80 %.")
        self.stdout.write(f"Cobranza: {cb['evaluated']} recordatorios evaluados al cumplirse su ventana ({cb['paid']} con pago en ella), {cb['pending']} con la "
                          f"ventana abierta, {cb['prepared_only']} solo preparados, {len(cb['segments'])} tipos de cliente con datos.")
