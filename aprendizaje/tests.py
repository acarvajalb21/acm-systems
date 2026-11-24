"""Modelos que aprenden: reposición, pagos y cobranza. Datos sintéticos; cada prueba va del dato al efecto visible.

Las pruebas de `series` son puras (sin base): fuga temporal, separación entre selección y prueba, métricas e
intervalos. Las demás recorren los servicios de la plataforma.
"""
import math
import random
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from django.conf import settings
from django.db.models import F
from django.test import Client, SimpleTestCase
from django.urls import reverse
from django.utils import timezone
from inventory import services as S, compras
from inventory.models import (Audit, User, Product, Batch, Movement, Supplier, Invoice, InvoiceLine, Receipt, PurchaseOrder, PurchaseOrderLine,
                              SupplyRequest)
from cartera import services as C
from cartera.models import Receivable, BankTransaction, Collection, PaymentApplication, PartyContact, with_balance
from cartera.tests import Base, tok, TODAY, PASSWORD
from agentes import runner
from agentes.models import AgentAction, AgentConfig
from . import series, reposicion as R, pagos, cobranza
from .models import ForecastRecord, MatchFeedback, CollectionAttempt

WEEK = series.WEEK


def noisy(n, level=20.0, season=0.0, trend=0.0, noise=3.0, seed=1):
    rng = random.Random(seed)
    return [max(0, round(level + trend * i + season * math.sin(2 * math.pi * i / 52) + rng.gauss(0, noise))) for i in range(n)]


class SeriesTests(SimpleTestCase):
    def test_steady_series_uses_simple_method_and_floor_sigma(self):
        fit = series.choose([10] * 12)
        self.assertTrue(fit['enough'])
        self.assertAlmostEqual(fit['forecast'], 10.0)
        self.assertAlmostEqual(fit['sigma'], 10 ** 0.5, places=3)   # error cero → piso √media (solo para la aproximación normal)
        self.assertEqual(fit['method'], 'promedio')                  # empate entre todos → el más simple
        self.assertEqual(fit['design'], {'train': 4, 'selection': 4, 'test': 4, 'reduced': True, 'weeks': 12})

    def test_intermittent_series_considers_sba_and_selection_never_loses_to_the_average(self):
        h = [0, 0, 6, 0, 0, 5, 0, 0, 6, 0, 0, 5, 0, 0, 6, 0, 0, 5, 0, 0, 6, 0, 0, 5]
        fit = series.choose(h)
        self.assertTrue(fit['intermittent'])
        self.assertIn('sba_0.1', [e['method'] for e in fit['evaluations']])
        self.assertLessEqual(fit['mae'], fit['mae_base'] + 1e-9)     # error de selección: por construcción no pierde frente al promedio

    def test_trend_series_prefers_a_reactive_method_and_the_gain_is_measured_on_the_test_window(self):
        fit = series.choose(list(range(2, 26)))
        self.assertNotEqual(fit['method'], 'promedio')
        self.assertGreater(fit['improvement'], 20)                   # medido en la ventana de prueba, no en la de selección

    def test_short_history_uses_the_fallback_and_says_so(self):
        fit = series.fit([3, 4, 5, 3, 4, 5, 3, 4, 5, 3, 4], horizon=2)    # 11 semanas: una menos que el mínimo
        self.assertFalse(fit['enough'])
        self.assertEqual((fit['method'], fit['mae'], fit['design'], fit['evaluations']), ('promedio', None, None, []))
        self.assertIn('Historial insuficiente', fit['note'])
        self.assertIsNone(fit['interval_h'])                          # sin evaluación no hay intervalo medido
        self.assertAlmostEqual(fit['forecast_h'], 2 * sum([3, 4, 5, 3, 4, 5, 3, 4, 5, 3, 4]) / 11)

    def test_windows_shrink_explicitly_with_short_history(self):
        self.assertIsNone(series.design(11))
        self.assertEqual(series.design(12), {'train': 4, 'selection': 4, 'test': 4, 'reduced': True, 'weeks': 12})
        self.assertEqual(series.design(19), {'train': 4, 'selection': 11, 'test': 4, 'reduced': True, 'weeks': 19})
        self.assertEqual(series.design(20), {'train': 4, 'selection': 8, 'test': 8, 'reduced': False, 'weeks': 20})
        self.assertEqual(series.design(104), {'train': 70, 'selection': 26, 'test': 8, 'reduced': False, 'weeks': 104})

    def test_no_temporal_leak_changing_the_future_does_not_change_a_forecast(self):
        y, other = noisy(80, season=8, seed=3), noisy(80, level=12, season=5, seed=4)
        dates = series._synthetic_dates(80)
        for t in (12, 40, 71):
            changed = y[:t] + [v * 7 + 100 for v in y[t:]]
            changed_other = other[:t] + [v * 3 + 55 for v in other[t:]]
            ridge = series.GlobalRidge({'a': (y, dates), 'b': (other, dates)})
            ridge_changed = series.GlobalRidge({'a': (changed, dates), 'b': (changed_other, dates)})
            for cand in series.CANDIDATES:
                before = series._forecaster(cand, y, dates, 'a', ridge)
                after = series._forecaster(cand, changed, dates, 'a', ridge_changed)
                for h in (1, 3):
                    args = (t, h, dates[t:t + h], dates[t - 1] + WEEK)
                    self.assertEqual(before(*args), after(*args), f'{cand.name} en el origen {t} a {h} semana(s) usó datos posteriores')
            # Los coeficientes del modelo entrenado en ese origen tampoco dependen de lo que pasa después.
            self.assertEqual(ridge.beta(2, dates[t - 1] + WEEK), ridge_changed.beta(2, dates[t - 1] + WEEK))

    def test_ridge_needs_training_rows_that_ended_before_the_origin(self):
        y, dates = noisy(60, seed=5), series._synthetic_dates(60)
        ridge = series.GlobalRidge({'a': (y, dates)})
        self.assertIsNone(ridge.predict('a', 6, 1, dates[6], dates[5] + WEEK))        # 2 filas anteriores: no alcanza para ajustar
        self.assertIsNone(ridge.predict('a', 43, 1, dates[43], dates[42] + WEEK))      # 39 filas: una menos que el mínimo
        self.assertIsNotNone(ridge.predict('a', 44, 1, dates[44], dates[43] + WEEK))   # 40 filas terminadas antes del origen
        fit = series.fit(y, dates, 1, ridge, 'a')
        row = next(e for e in fit['evaluations'] if e['method'] == 'ridge_global')
        self.assertFalse(row['available'])                                              # y por eso no compite: no hay elección a medias
        self.assertIn('no compitió', fit['ridge_note'])
        self.assertNotEqual(fit['method'], 'ridge_global')

    def test_selection_never_looks_at_the_test_window(self):
        y = noisy(60, season=6, seed=7)
        shaken = y[:-8] + [v * 5 + 40 for v in y[-8:]]
        a, b = series.fit(y, horizon=2), series.fit(shaken, horizon=2)
        self.assertEqual(a['method'], b['method'])
        self.assertEqual([(e['method'], e['sel_h'].mae, e['sel_week'].mae) for e in a['evaluations']],
                         [(e['method'], e['sel_h'].mae, e['sel_week'].mae) for e in b['evaluations']])
        self.assertEqual((a['interval_h']['q_low'], a['interval_h']['q_high']), (b['interval_h']['q_low'], b['interval_h']['q_high']))
        self.assertNotEqual(a['test']['h'].mae, b['test']['h'].mae)                    # lo único que cambia es el error de prueba
        self.assertEqual(a['design'], {'train': 26, 'selection': 26, 'test': 8, 'reduced': False, 'weeks': 60})
        self.assertEqual((a['selection']['h'].n, a['test']['h'].n, a['test']['week'].n), (25, 7, 8))   # orígenes con ventana objetivo completa

    def test_wape_is_undefined_without_outflows_and_never_averaged(self):
        m = series.metrics([(1.0, 0.0), (2.0, 0.0)])
        self.assertEqual((m.n, m.mae, m.bias), (2, 1.5, -1.5))
        self.assertIsNone(m.wape)
        self.assertFalse(m.wape_defined)
        self.assertIsNone(m.wape_pct)
        self.assertAlmostEqual(series.metrics([(8.0, 10.0), (12.0, 10.0)]).wape, 0.2)
        fit = series.fit([0] * 30)
        self.assertEqual((fit['forecast'], fit['test']['week'].mae), (0.0, 0.0))
        self.assertIsNone(fit['test']['week'].wape)

    def test_interval_comes_from_selection_errors_and_its_coverage_is_measured_on_the_test_window(self):
        y = noisy(48, season=5, seed=11)
        fit = series.fit(y, horizon=2)
        errors = fit['errors_h']
        low, high = min(series.quantile(errors, 0.10), 0.0), max(series.quantile(errors, 0.90), 0.0)
        self.assertEqual(fit['selection']['h'].errors, errors)
        self.assertEqual((fit['interval_h']['q_low'], fit['interval_h']['q_high']), (low, high))
        self.assertAlmostEqual(fit['interval_h']['low'], max(fit['forecast_h'] + low, 0))
        self.assertAlmostEqual(fit['interval_h']['high'], fit['forecast_h'] + high)
        pairs = fit['test']['h'].pairs
        inside = sum(1 for f, a in pairs if f + low - 1e-9 <= a <= f + high + 1e-9)
        self.assertEqual((fit['coverage_h']['inside'], fit['coverage_h']['n']), (inside, 7))
        self.assertAlmostEqual(fit['coverage_h']['share'], inside / 7 * 100)
        self.assertTrue(fit['coverage_h']['few'])                                      # 7 observaciones: se dice que son pocas
        # Un método sesgado (todos los errores de un lado) no deja al pronóstico fuera de su propio intervalo.
        biased = series.fit([10 + i for i in range(40)])
        self.assertLessEqual(biased['interval_week']['low'], biased['forecast_week'])
        self.assertGreaterEqual(biased['interval_week']['high'], biased['forecast_week'])
        self.assertEqual(series.quantile([1, 2, 3, 4, 5], 0.5), 3)
        self.assertEqual(series.coverage([(10, 9), (10, 12), (10, 20)], -1, 2), (2, 3))

    def test_global_ridge_competes_on_equal_terms_and_ties_go_to_the_simplest(self):
        panel = {k: (noisy(104, level=18 + 3 * k, season=9, seed=20 + k), series._synthetic_dates(104)) for k in range(5)}
        ridge = series.GlobalRidge(panel)
        for k, (y, dates) in panel.items():
            fit = series.fit(y, dates, 2, ridge, k)
            rows = {e['method']: e for e in fit['evaluations']}
            self.assertTrue(rows['ridge_global']['available'])
            self.assertEqual({e['sel_h'].n for e in fit['evaluations']}, {25})         # todos los candidatos, los mismos orígenes
            best = min(fit['evaluations'], key=lambda e: (round(e['sel_h'].mae, 6), series.ORDER.index(e['method'])))
            self.assertEqual(fit['method'], best['method'])
            self.assertIn(fit['kind'], ('estadistico', 'entrenado'))
            self.assertTrue(fit['ridge_note'])
        self.assertEqual(ridge.rows_used[2], 5 * (104 - 2 + 1 - series.WARMUP))


class LearningBase(Base):
    def setUp(self):
        super().setUp()
        self.manager, self.admin, self.requester = self.users['manager'], self.users['admin'], self.users['requester']
        self.owner = User.objects.create_user('owner', password=PASSWORD, role='owner', must_change_password=False)
        self.vendor = Supplier.objects.create(name='Proveedor Andino S.A.S.', nit='900123456', payment_days=60, email='pedidos@proveedor.example')

    def backdate(self, movement, days_ago, hour=10):
        when = timezone.make_aware(timezone.datetime.combine(TODAY() - timedelta(days=days_ago), timezone.datetime.min.time()) + timedelta(hours=hour))
        Movement.objects.filter(pk=movement.pk).update(created_at=when)

    def weekly_product(self, code='REF-C', weeks=10, per_week=10, initial=115, expiry_days=300, pack=10, manufacturer='Andino', values=None):
        """Una referencia con semanas completas de consumo (constante, o `values` de la más antigua a la más reciente) y un lote vigente."""
        values = list(values) if values is not None else [per_week] * weeks
        weeks = len(values)
        p = Product.objects.create(code=code, name=f'Referencia {code[-1]}', manufacturer=manufacturer, pack_size=pack)
        monday = R.week_start(TODAY())
        first = S.adjust_in(self.manager, p, 'LC1', TODAY() + timedelta(days=expiry_days), 'Bodega 1', initial, 'initial', '', tok())
        self.backdate(first, (TODAY() - (monday - timedelta(days=7 * weeks))).days)
        batch = Batch.objects.get(product=p, lot='LC1')
        for i, qty in enumerate(values):
            w = weeks - i
            if qty <= 0:
                continue
            day = monday - timedelta(days=7 * w - 2)   # miércoles de cada semana cerrada
            m = S.exit_stock(self.manager, batch.pk, qty, f'REM-{w}', '', tok())
            self.backdate(m, (TODAY() - day).days)
        return p, batch

    def lead_time_invoice(self, product, lead=7, qty=20, cost='1000', number='AND-1'):
        ordered = TODAY() - timedelta(days=40)
        inv = Invoice.objects.create(supplier=self.vendor, number=number, issue_date=ordered + timedelta(days=3), due_date=ordered + timedelta(days=63), ordered_on=ordered,
                                     review=Invoice.Review.VALIDATED, subtotal=Decimal(cost) * qty, total=Decimal(cost) * qty, created_by=self.manager,
                                     validated_by=self.finance, validated_at=timezone.now())
        InvoiceLine.objects.create(invoice=inv, product=product, description=product.name, quantity=qty, unit_price=Decimal(cost), received=qty)
        Receipt.objects.create(invoice=inv, received_on=ordered + timedelta(days=lead), actor=self.manager)
        return inv

    def login(self, user):
        c = Client()
        c.force_login(user)
        s = c.session
        s['user_version'] = user.session_version
        s.save()
        return c


class ReplenishmentTests(LearningBase):
    def test_reorder_point_quantity_and_lead_time_from_platform_data(self):
        p, batch = self.weekly_product()
        self.lead_time_invoice(p, lead=7)
        [plan] = R.plans(products=[p])
        self.assertEqual(plan.supplier, self.vendor)
        self.assertEqual((plan.lead_days, plan.lead_n), (7, 1))
        self.assertEqual(plan.weeks, 10)
        self.assertAlmostEqual(plan.rate, 10.0, places=3)
        self.assertEqual(plan.position, 15)                       # 115 − 100 salidas; nada en tránsito ni pedido
        self.assertEqual((plan.protection_days, plan.horizon_weeks), (14, 2))
        self.assertAlmostEqual(plan.forecast_h, 20.0)             # consumo acumulado del periodo de protección
        self.assertGreater(plan.reorder_point, 20)                # 20 de consumo + seguridad
        self.assertEqual(plan.status, 'pedir_ya')
        self.assertEqual(plan.quantity, 40)                       # (47,4 − 15) redondeado a empaques de 10
        self.assertEqual(plan.estimated_cost, Decimal('40000.00'))
        self.assertEqual((plan.runout_on, plan.urgency_days), (TODAY() + timedelta(days=10), 3))
        self.assertTrue(any('Punto de pedido' in r for r in plan.reasons))
        self.assertTrue(any('mediana de 1 pedido' in r for r in plan.reasons))
        # Diez semanas no alcanzan para elegir y probar: respaldo declarado, intervalo aproximado y seguridad por aproximación normal.
        self.assertEqual((plan.enough, plan.method, plan.safety_method, plan.interval_week['kind']), (False, 'promedio', 'normal', 'aproximado'))
        self.assertTrue(any('Historial insuficiente' in r for r in plan.reasons))
        self.assertIsNone(plan.test)
        # Evidencia en lenguaje llano para la pantalla: sin fórmulas, con los hechos observados.
        e = plan.evidence
        self.assertEqual((e['typical'], e['last4'], e['prev4'], e['change_label']), (10.0, 40, 40, '+0 %'))
        self.assertEqual(e['runout'], TODAY() + timedelta(days=plan.coverage_days))
        self.assertTrue(plan.summary.startswith('Pedir 40 unidades hoy: lo que hay alcanza unos 10 días y el proveedor tarda 7 días'))
        self.assertNotIn('nivel de servicio', plan.summary.lower())
        chart = R.chart(plan)
        self.assertEqual((len(chart['cols']), len(chart['future_cols']), len(chart['points']), chart['outs']), (10, 4, 10, []))

    def test_long_history_chooses_with_selection_reports_the_test_error_and_uses_empirical_quantiles(self):
        values = noisy(44, level=20, noise=4, seed=2)
        p, batch = self.weekly_product(values=values, initial=sum(values) + 30)
        self.lead_time_invoice(p)
        [plan] = R.plans(products=[p])
        fit = plan.fit
        self.assertTrue(plan.enough)
        self.assertEqual(fit['design'], {'train': 10, 'selection': 26, 'test': 8, 'reduced': False, 'weeks': 44})
        self.assertEqual((fit['horizon'], fit['selection_horizon']), (2, 2))
        self.assertEqual((plan.test['n'], plan.test_h['n']), (8, 7))                    # el desempeño que se muestra es el de prueba, con su n
        self.assertEqual(plan.safety_method, 'empirico')                                # 25 errores acumulados ≥ 12
        self.assertAlmostEqual(plan.safety, max(series.quantile(fit['errors_h'], 0.95), 0.0))
        self.assertAlmostEqual(plan.reorder_point, plan.forecast_h + plan.safety)
        self.assertEqual(plan.interval_h['kind'], 'empirico')
        self.assertEqual(fit['coverage_h']['n'], 7)
        self.assertTrue(any('cuantil 95 %' in r for r in plan.reasons))
        # El pronóstico guardado lleva su intervalo.
        R.record_forecasts([plan], TODAY())
        rec = ForecastRecord.objects.get(product=p)
        self.assertEqual((rec.interval_kind, rec.forecast), ('empirico', plan.forecast_week))
        self.assertAlmostEqual(rec.low, plan.interval_week['low'])
        self.assertAlmostEqual(rec.high, plan.interval_week['high'])
        self.assertIsNone(rec.covered)                                                   # la semana no ha cerrado
        summary = R.comparison([plan])
        self.assertEqual((summary['refs'], summary['chosen']['refs'], summary['chosen']['chosen']), (1, 1, 1))
        self.assertIsNone(summary['ridge'])                                              # una sola referencia: el modelo global no tenía con qué entrenar
        self.assertEqual(R.coverage_summary([plan])['h']['n'], 7)

    def test_movements_of_the_week_in_progress_do_not_change_the_forecast(self):
        p, batch = self.weekly_product(values=noisy(30, seed=6), initial=900)
        [before] = R.plans(products=[p])
        m = S.exit_stock(self.manager, batch.pk, 77, 'REM-0', '', tok())                 # semana en curso: aún no es historia
        self.backdate(m, 0)
        [after] = R.plans(products=[p])
        self.assertEqual((before.rate, before.forecast_week, before.method), (after.rate, after.forecast_week, after.method))
        self.assertEqual(before.fit['errors_h'], after.fit['errors_h'])
        self.assertEqual(before.available - 77, after.available)                          # la posición sí refleja lo que ya salió

    def test_position_counts_each_unit_once_and_does_not_subtract_reservations_twice(self):
        p = Product.objects.create(code='REF-P', name='Referencia P', pack_size=5)
        Batch.objects.create(product=p, lot='LP1', expiry=TODAY() + timedelta(days=200), location='Bodega 1', physical=100, reserved=30)
        Batch.objects.create(product=p, lot='LP0', expiry=TODAY() - timedelta(days=5), location='Bodega 1', physical=500)      # vencido: no cuenta
        inv = Invoice.objects.create(supplier=self.vendor, number='AND-9', issue_date=TODAY(), due_date=TODAY() + timedelta(days=60), ordered_on=TODAY() - timedelta(days=3),
                                     review=Invoice.Review.VALIDATED, subtotal=Decimal('50000'), total=Decimal('50000'), created_by=self.manager,
                                     validated_by=self.finance, validated_at=timezone.now())
        InvoiceLine.objects.create(invoice=inv, product=p, description=p.name, quantity=50, unit_price=Decimal('1000'), received=20)
        void = Invoice.objects.create(supplier=self.vendor, number='AND-10', issue_date=TODAY(), due_date=TODAY() + timedelta(days=60), review=Invoice.Review.VOID,
                                      subtotal=Decimal('9000'), total=Decimal('9000'), created_by=self.manager)
        InvoiceLine.objects.create(invoice=void, product=p, description=p.name, quantity=9, unit_price=Decimal('1000'))          # anulada: no cuenta
        free = PurchaseOrder.objects.create(number='PED-T-1', supplier=self.vendor, created_by=self.manager)
        PurchaseOrderLine.objects.create(order=free, product=p, quantity=40)
        linked = PurchaseOrder.objects.create(number='PED-T-2', supplier=self.vendor, created_by=self.manager, invoice=inv, status='sent')
        PurchaseOrderLine.objects.create(order=linked, product=p, quantity=25)                                                   # ya está en la factura
        closed = PurchaseOrder.objects.create(number='PED-T-3', supplier=self.vendor, created_by=self.manager, status='received')
        PurchaseOrderLine.objects.create(order=closed, product=p, quantity=60)
        SupplyRequest.objects.create(requester=self.requester, product=p, quantity=10, purpose='Obra sur')                       # pendiente, sin reservar
        [plan] = R.plans(products=[p])
        self.assertEqual((plan.physical, plan.reserved, plan.available), (100, 30, 70))
        self.assertEqual((plan.in_transit, plan.on_order), (30, 40))
        self.assertEqual(plan.position, 140)                       # 70 + 30 + 40: cada unidad una vez, sin restar otra vez las reservas
        self.assertEqual(plan.pending_requests, 10)                # se muestra como dato y no se resta

    def test_expiry_caps_the_quantity(self):
        p, batch = self.weekly_product(expiry_days=45)
        [plan] = R.plans(products=[p])
        self.assertTrue(plan.capped)
        self.assertEqual(plan.quantity, 0)                        # con 45 días de vida útil y 30 de margen no cabe otro empaque
        self.assertEqual(plan.status, 'ok')
        params = dict(R.DEFAULTS, margen_vencimiento_dias=0)
        [plan2] = R.plans(params, products=[p])
        self.assertEqual((plan2.status, plan2.quantity, plan2.capped), ('pedir_ya', 40, False))

    def test_stockout_weeks_do_not_count_as_zero_demand(self):
        p = Product.objects.create(code='REF-D', name='Referencia D')
        monday = R.week_start(TODAY())
        m0 = S.adjust_in(self.manager, p, 'LD1', TODAY() + timedelta(days=200), 'Bodega 1', 20, 'initial', '', tok())
        self.backdate(m0, (TODAY() - (monday - timedelta(days=42))).days)
        b = Batch.objects.get(product=p, lot='LD1')
        for w in (6, 5):
            m = S.exit_stock(self.manager, b.pk, 10, f'REM-{w}', '', tok())
            self.backdate(m, (TODAY() - (monday - timedelta(days=7 * w - 2))).days)
        m1 = S.adjust_in(self.manager, p, 'LD1', TODAY() + timedelta(days=200), 'Bodega 1', 50, 'adjust_in', 'Reposición', tok())
        self.backdate(m1, (TODAY() - (monday - timedelta(days=6))).days)
        m2 = S.exit_stock(self.manager, b.pk, 10, 'REM-1', '', tok())
        self.backdate(m2, (TODAY() - (monday - timedelta(days=5))).days)
        s = R.history([p], TODAY())[p.pk]
        self.assertEqual(len(s.weeks), 6)
        self.assertEqual(sum(s.stockout), 3)
        self.assertEqual(s.usable, [10, 10, 10])
        self.assertEqual(len(s.usable_weeks), 3)
        [plan] = R.plans(products=[p])
        self.assertEqual((plan.stockout_weeks, plan.total_weeks, plan.observed_units, plan.unmet_units), (3, 6, 30, 0))
        self.assertTrue(any('sin existencias' in r for r in plan.reasons))
        self.assertEqual(len(R.chart(plan)['outs']), 3)            # las semanas censuradas se marcan en el gráfico

    def test_rejected_requests_are_shown_apart_as_unmet_demand(self):
        p, batch = self.weekly_product()
        req = SupplyRequest.objects.create(requester=self.requester, product=p, quantity=6, purpose='Obra norte', status='rejected')
        SupplyRequest.objects.filter(pk=req.pk).update(created_at=timezone.now() - timedelta(days=9))
        [plan] = R.plans(products=[p])
        self.assertEqual((plan.observed_units, plan.unmet_units), (100, 6))    # salidas y demanda no atendida, por separado
        self.assertTrue(any('Demanda no atendida registrada: 6' in r for r in plan.reasons))
        self.assertEqual(len(R.chart(plan)['unmet']), 1)
        [without] = R.plans(dict(R.DEFAULTS, incluir_rechazadas=0), products=[p])
        self.assertEqual(without.unmet_units, 0)

    def test_series_ending_in_many_zero_weeks_with_stock_is_no_recent_consumption_not_a_tiny_forecast(self):
        values = [12] * 16 + [0] * 22
        residue = series.suavizado(0.5)(values)
        self.assertTrue(0 < residue < R.MIN_RATE)                  # el suavizado deja un residuo positivo ínfimo: no es un pronóstico útil
        p, batch = self.weekly_product(code='REF-Z', values=values, initial=400)
        [plan] = R.plans(products=[p])
        self.assertEqual((plan.status, plan.status_label, plan.rate, plan.quantity), ('sin_consumo', 'Sin consumo reciente', 0.0, 0))
        self.assertEqual((plan.coverage_days, plan.runout_on, plan.order_on, plan.coverage_label), (None, None, None, ''))
        self.assertEqual((plan.idle_weeks, plan.position, plan.at_risk, plan.excess), (22, 208, 208, 208))
        self.assertEqual(plan.summary, 'Sin salidas en las últimas 22 semanas: no se sugiere pedir. Hay 208 unidades que no están rotando: conviene revisarlas.')
        self.assertTrue(any('Sin consumo reciente: 22 semanas seguidas sin salidas' in r for r in plan.reasons))
        # La regla, directamente: una tasa positiva por debajo del umbral nunca se convierte en cobertura ni en fechas.
        tiny = R.Plan(p, rate=1e-5, forecast_h=2e-5, weeks=38, enough=True, position=208, lead_days=7, protection_days=14, horizon_weeks=2, shelf_days=300)
        R._decide(tiny, R.DEFAULTS, R.Z[95], TODAY(), [batch])
        self.assertEqual((tiny.status, tiny.rate, tiny.coverage_days, tiny.runout_on), ('sin_consumo', 0.0, None, None))
        # Justo por encima del umbral y con muchísimas existencias: la cobertura se acota antes de derivar fechas.
        slow = R.Plan(p, rate=0.06, forecast_h=0.12, sigma=0.5, weeks=38, enough=True, position=10 ** 7, lead_days=7, protection_days=14, horizon_weeks=2,
                      shelf_days=300, fit={'errors_h': []})
        R._decide(slow, R.DEFAULTS, R.Z[95], TODAY(), [])
        self.assertEqual((slow.coverage_days, slow.coverage_capped, slow.runout_on, slow.order_on), (R.MAX_COVERAGE_DAYS, True, None, None))
        self.assertEqual((slow.coverage_label, slow.covers, slow.status, slow.days_until), ('más de 10 años', 'para más de 10 años', 'exceso', R.MAX_COVERAGE_DAYS))
        slow.evidence = {'incoming': 0}
        self.assertTrue(R._summary(slow, {'incoming': 0, 'weeks_after': None}).startswith('No pedir: hay existencias para más de 10 años'))
        # El agente, la lista y el pronóstico de la referencia siguen funcionando con ella.
        run = runner.run_agent('reposicion', 'manual')
        self.assertEqual(run.status, 'succeeded', run.error)
        c = self.login(self.manager)
        page = c.get(reverse('replenishment'))
        self.assertContains(page, 'Sin consumo reciente')
        self.assertEqual(page.context['risk'], 208)
        page = c.get(reverse('forecast', args=[p.pk]))
        self.assertContains(page, 'Lleva 22 semanas seguidas sin salidas')
        self.assertContains(page, 'vencerían sin usarse si siguen sin salir')

    def test_a_faulty_reference_is_isolated_and_marked_not_computable(self):
        good, _ = self.weekly_product()
        self.lead_time_invoice(good)
        bad, _ = self.weekly_product(code='REF-X')
        real = series.fit

        def flaky(y, dates=None, horizon=1, ridge=None, key=None, now_date=None):
            if key == bad.pk:
                raise OverflowError('date value out of range')
            return real(y, dates, horizon, ridge, key, now_date)

        with mock.patch.object(series, 'fit', flaky), self.assertLogs('aprendizaje.reposicion', level='ERROR') as logs:
            plans = {pl.product.pk: pl for pl in R.plans()}
            broken, fine = plans[bad.pk], plans[good.pk]
            self.assertEqual((fine.status, fine.quantity), ('pedir_ya', 40))                    # las demás se calculan igual
            self.assertEqual((broken.status, broken.status_label, broken.quantity, broken.needs_order), ('no_calculable', 'No calculable', 0, False))
            self.assertEqual(broken.error, 'OverflowError: date value out of range')
            self.assertIn('No se pudo calcular', broken.summary)
            self.assertEqual(broken.position, 15)                                                 # lo que sí se sabe se conserva
            self.assertEqual(R.budget_plan(plans.values(), 0)['by_product'], {good.pk: 40})
            run = runner.run_agent('reposicion', 'manual')                                       # el agente no se cae
            self.assertEqual((run.status, run.proposed), ('succeeded', 1))
            c = self.login(self.manager)
            page = c.get(reverse('replenishment'))
            self.assertEqual((page.status_code, page.context['counts']['no_calculable']), (200, 1))
            self.assertContains(page, 'No calculable')
            page = c.get(reverse('forecast', args=[bad.pk]))
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, 'No se pudo calcular esta referencia')
            self.assertEqual(self.login(self.owner).get(reverse('learning')).status_code, 200)
            self.assertEqual(c.get(reverse('methodology')).status_code, 200)
        self.assertTrue(any('REF-X' in line for line in logs.output))
        # El fallo no queda memorizado: cuando la causa desaparece, la referencia vuelve a calcularse sin tocar los datos.
        self.assertEqual({pl.product.pk: pl.status for pl in R.plans()}[bad.pk], 'pedir_ya')

    def test_supplier_fallback_by_manufacturer_and_minimum_rule_without_history(self):
        p = Product.objects.create(code='REF-E', name='Referencia E', manufacturer='Andino', minimum=10)
        Batch.objects.create(product=p, lot='LE1', expiry=TODAY() + timedelta(days=100), location='Bodega 1', physical=4)
        [plan] = R.plans(products=[p])
        self.assertEqual(plan.supplier, self.vendor)
        self.assertEqual((plan.status, plan.quantity), ('pedir_ya', 16))   # llevar al doble del mínimo
        self.assertTrue(any('regla del mínimo' in r for r in plan.reasons))

    def test_forecast_records_are_scored_against_reality_with_their_interval(self):
        p, batch = self.weekly_product()
        R.record_forecasts(R.plans(products=[p]), TODAY())
        rec = ForecastRecord.objects.get(product=p)
        self.assertEqual((rec.week_start, rec.actual, rec.interval_kind), (R.week_start(TODAY()), None, 'aproximado'))
        self.assertAlmostEqual(rec.high, 10 + 1.2816 * 10 ** 0.5, places=3)
        # Esta semana salen 12; al evaluar la semana siguiente se completa lo real y se mide el error y la cobertura.
        m = S.exit_stock(self.manager, batch.pk, 12, 'REM-0', '', tok())
        self.backdate(m, 0)
        later = R.week_start(TODAY()) + timedelta(days=7)
        R.record_forecasts([], later)
        rec.refresh_from_db()
        self.assertEqual((rec.actual, rec.covered), (12, True))    # 12 ≤ 10 + 1,28·√10
        acc = R.accuracy(today=later)
        self.assertEqual((acc['n'], acc['inside'], acc['interval_n']), (1, 1, 1))
        self.assertAlmostEqual(acc['mae'], 2.0)
        self.assertAlmostEqual(acc['bias'], 2.0)                    # observado − pronóstico: se pronosticó de menos
        self.assertAlmostEqual(acc['wape'], 2 / 12 * 100)
        self.assertNotIn('hits', acc)                               # ya no existe una «precisión» inventada

    def test_plans_are_memoized_until_the_data_changes(self):
        p, batch = self.weekly_product()
        R.invalidate()
        base = dict(R.stats)
        [first] = R.plans(products=[p])
        [second] = R.plans(products=[p])
        self.assertEqual((R.stats['misses'] - base['misses'], R.stats['hits'] - base['hits']), (1, 1))
        self.assertIsNot(first, second)                             # cada llamada recibe su copia: anotar un plan no altera lo memorizado
        self.assertEqual((first.quantity, first.rate), (second.quantity, second.rate))
        self.assertEqual(len(R.plans()), 3)                         # la lista completa sale del mismo cálculo memorizado
        self.assertEqual(R.stats['misses'] - base['misses'], 1)
        # Un cambio hecho sin señales (update directo) cambia la huella: se recalcula.
        Batch.objects.filter(pk=batch.pk).update(physical=F('physical') + 500)
        [third] = R.plans(products=[p])
        self.assertEqual(R.stats['misses'] - base['misses'], 2)
        self.assertEqual(third.available, first.available + 500)
        # Un cambio por los servicios (con señales) también.
        S.adjust_in(self.manager, p, 'LC7', TODAY() + timedelta(days=300), 'Bodega 1', 7, 'adjust_in', 'Conteo', tok())
        [fourth] = R.plans(products=[p])
        self.assertEqual(fourth.available, third.available + 7)
        [uncached] = R.plans(products=[p], use_cache=False)
        self.assertEqual(uncached.available, fourth.available)

    def budget_case(self):
        def plan(pk, name, qty, cost, urgency, pack, status='pedir_ya'):
            prod = Product(pk=pk, code=f'B-{pk}', name=name, pack_size=pack)
            return R.Plan(prod, self.vendor, status=status, quantity=qty, unit_cost=Decimal(cost) if cost else None, urgency_days=urgency, coverage_days=urgency + 7,
                          lead_days=7)
        return [plan(901, 'Holgada', 40, '1000', 9, 10, 'pedir_pronto'), plan(902, 'Urgente', 30, '2000', -4, 10), plan(903, 'Media', 24, '500', 2, 12),
                plan(904, 'Sin costo', 10, None, 1, 5), plan(905, 'Cubierta', 0, '100', 30, 1, 'ok')]

    def test_budget_prioritizes_by_urgency_cuts_by_pack_and_lists_what_is_left_out(self):
        plans = self.budget_case()
        free = R.budget_plan(plans, 0)
        self.assertTrue(free['unlimited'])
        self.assertEqual([r['plan'].product.name for r in free['lines']], ['Urgente', 'Sin costo', 'Media', 'Holgada'])    # por urgencia
        self.assertEqual((free['total'], free['excluded'], free['restrictive']), (Decimal('112000.00'), [], False))
        tight = R.budget_plan(plans, 85000)
        # Urgente completo (60.000); «Sin costo» no se puede presupuestar; Media completa (12.000); a Holgada le quedan 13.000 → 1 empaque de 10.
        self.assertEqual([(r['plan'].product.name, r['quantity'], r['cost'], r['cut']) for r in tight['lines']],
                         [('Urgente', 30, Decimal('60000.00'), False), ('Media', 24, Decimal('12000.00'), False), ('Holgada', 10, Decimal('10000.00'), True)])
        self.assertEqual((tight['spent'], tight['left'], tight['restrictive']), (Decimal('82000.00'), Decimal('3000.00'), True))
        self.assertEqual([(x['plan'].product.name, x['quantity']) for x in tight['excluded']], [('Sin costo', 10), ('Holgada', 30)])
        self.assertIn('Recortada a 10 de 40', tight['excluded'][1]['reason'])
        self.assertIn('Sin costo conocido', tight['excluded'][0]['reason'])
        self.assertEqual(tight['by_product'], {902: 30, 903: 24, 901: 10})
        self.assertEqual(tight['included'], tight['lines'])
        self.assertTrue(all(r['quantity'] % r['plan'].product.pack_size == 0 for r in tight['lines']))
        # Lo ya comprometido en pedidos de la revisión consume presupuesto: quedan 15.000. A Urgente no le alcanza ni un empaque de 10
        # (7 unidades); Media cabe completa; con los 3.000 restantes Holgada tampoco completa un empaque.
        used = R.budget_plan(plans, 85000, committed=Decimal('70000'))
        self.assertEqual((used['available'], [(r['plan'].product.name, r['quantity']) for r in used['lines']]), (Decimal('15000.00'), [('Media', 24)]))
        self.assertEqual([(x['plan'].product.name, x['quantity']) for x in used['excluded']], [('Urgente', 30), ('Sin costo', 10), ('Holgada', 40)])
        self.assertIn('no alcanza para un empaque de 10', used['excluded'][0]['reason'])
        self.assertEqual((used['spent'], used['left']), (Decimal('12000.00'), Decimal('3000.00')))
        self.assertEqual([pl.quantity for pl in plans], [40, 30, 24, 10, 0])            # no modifica los planes

    def test_committed_budget_counts_open_orders_of_the_current_review(self):
        p, batch = self.weekly_product()
        self.assertEqual(R.committed_cost(), Decimal('0.00'))
        order = compras.create_order(self.manager, self.vendor, [{'product': p, 'quantity': 20, 'unit_cost': Decimal('1000')}], '', tok())
        self.assertEqual(R.committed_cost(), Decimal('20000.00'))
        PurchaseOrder.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(days=8))    # de una revisión anterior
        self.assertEqual(R.committed_cost(), Decimal('0.00'))

    def test_agent_proposes_grouped_order_and_approval_creates_a_draft_once(self):
        p, batch = self.weekly_product()
        self.lead_time_invoice(p)
        run = runner.run_agent('reposicion', 'manual')
        self.assertEqual(run.status, 'succeeded', run.error)
        [prop] = AgentAction.objects.filter(agent='reposicion', status='proposed')
        self.assertIn('Preparar pedido a Proveedor Andino', prop.title)
        self.assertEqual(prop.amount, Decimal('40000.00'))
        self.assertEqual(prop.payload['quantities'], {str(p.pk): 40})
        self.assertIn('El pronóstico semanal se evaluará', run.summary)
        with self.assertRaises(Exception):
            runner.decide(self.finance, prop.pk, True)             # contabilidad no pide inventario
        a = runner.decide(self.manager, prop.pk, True)
        self.assertEqual(a.status, 'executed', a.result)
        self.assertFalse(a.result['recalculated'])
        order = PurchaseOrder.objects.get()
        self.assertEqual((order.status, order.source, order.supplier, order.units), ('draft', 'agent', self.vendor, 40))
        self.assertEqual(order.expected_on, TODAY() + timedelta(days=7))
        self.assertEqual(compras.on_order()[p.pk], 40)
        # Con el pedido abierto la posición ya cubre el objetivo: no se vuelve a proponer.
        runner.run_agent('reposicion', 'manual')
        self.assertFalse(AgentAction.objects.filter(agent='reposicion', status='proposed').exists())
        [plan] = R.plans(products=[p])
        self.assertEqual((plan.on_order, plan.status), (40, 'ok'))
        # Enviar y cerrar con la factura que cita el número del pedido.
        compras.mark_sent(self.manager, order.pk, tok())
        header = {'supplier': self.vendor, 'number': 'AND-2', 'issue_date': TODAY(), 'due_date': TODAY() + timedelta(days=60), 'ordered_on': TODAY() - timedelta(days=2),
                  'order_number': order.number.lower(), 'cufe': '', 'global_discount': Decimal(0), 'retention': Decimal(0), 'notes': ''}
        lines = [{'product': p, 'supplier_code': '', 'description': p.name, 'quantity': 40, 'unit_price': Decimal('1000'), 'discount': Decimal(0), 'tax_rate': Decimal(0),
                  'expected_lot': 'LC2', 'expected_expiry': TODAY() + timedelta(days=300)}]
        inv = S.create_invoice(self.manager, header, lines, [], tok())
        order.refresh_from_db()
        self.assertEqual(order.invoice, inv)
        [plan] = R.plans(products=[p])
        self.assertEqual((plan.in_transit, plan.on_order, plan.position), (40, 0, 55))   # pasó de «pedido» a «por recibir»: una sola vez
        S.validate_invoice(self.finance, inv.pk, tok())
        line = inv.lines.get()
        S.receive_invoice(self.manager, inv.pk, [{'line': line.pk, 'quantity': 40, 'lot': 'LC2', 'expiry': TODAY() + timedelta(days=300), 'location': 'Bodega 1'}], TODAY(), '', tok())
        order.refresh_from_db()
        self.assertEqual(order.status, 'received')
        self.assertEqual(compras.on_order().get(p.pk, 0), 0)

    def test_stale_order_proposal_when_stock_arrived(self):
        p, batch = self.weekly_product()
        runner.run_agent('reposicion', 'manual')
        [prop] = AgentAction.objects.filter(agent='reposicion', status='proposed')
        S.adjust_in(self.manager, p, 'LC9', TODAY() + timedelta(days=300), 'Bodega 1', 200, 'adjust_in', 'Llegó mercancía', tok())
        a = runner.decide(self.manager, prop.pk, True)
        self.assertEqual(a.status, 'superseded')
        self.assertIn('ya no necesitan pedido', a.result['message'])
        self.assertFalse(PurchaseOrder.objects.exists())

    def test_replenishment_page_assumptions_budget_and_manual_order(self):
        p, batch = self.weekly_product()
        self.lead_time_invoice(p)
        self.assertEqual(self.login(self.requester).get(reverse('replenishment')).status_code, 403)
        page = self.login(self.finance).get(reverse('replenishment'))
        self.assertEqual((page.status_code, page.context['can_order']), (200, False))        # contabilidad consulta; no prepara pedidos
        self.assertEqual(page.context['counts']['pedir_ya'], 1)
        self.assertNotContains(page, 'style=')
        c = self.login(self.manager)
        page = c.get(reverse('replenishment'))
        self.assertTrue(page.context['can_order'])
        [plan] = [pl for pl in page.context['plans'] if pl.product == p]
        self.assertEqual((plan.status_label, plan.quantity, len(plan.spark)), ('Pedir ya', 40, 10))
        self.assertContains(page, p.name)
        self.assertContains(page, reverse('forecast', args=[p.pk]))
        self.assertContains(page, 'modulos/pronosticos.css')
        self.assertContains(page, 'Sin tope')                                                # el presupuesto se muestra donde afecta
        self.assertNotContains(page, 'Guardar supuestos')                                    # inventario no edita los supuestos
        # Simulación con ?presupuesto=: recorta en empaques, lista lo que queda por fuera y no guarda nada.
        page = c.get(reverse('replenishment'), {'presupuesto': '25.000'})
        budget = page.context['budget']
        self.assertEqual((budget['what_if'], budget['budget'], budget['by_product'][p.pk], budget['restrictive']), (True, Decimal('25000.00'), 20, True))
        self.assertContains(page, 'Quedó por fuera del presupuesto')
        self.assertContains(page, 'Recortada a 20 de 40')
        self.assertEqual(R.current_params()['presupuesto'], 0)
        self.assertEqual(c.get(reverse('replenishment'), {'presupuesto': 'mucho'}).context['budget']['what_if'], False)
        # Si los pedidos abiertos del periodo ya ocupan el presupuesto, la pantalla lo dice en vez de mostrar solo un cero.
        with mock.patch.object(R, 'committed_cost', return_value=Decimal('40000.00')):
            page = c.get(reverse('replenishment'), {'presupuesto': '25.000'})
            self.assertEqual((page.context['budget']['available'], page.context['budget']['lines']), (Decimal('0.00'), []))
            self.assertContains(page, 'El presupuesto ya está comprometido en pedidos abiertos.')
            self.assertContains(page, 'ya ocupan todo el presupuesto')
            self.assertNotContains(page, 'El presupuesto no alcanza para todo.')
        # Editar supuestos: solo administración y dirección; queda en auditoría y cambia la sugerencia.
        self.assertEqual(c.post(reverse('replenishment'), {'accion': 'supuestos', 'nivel_servicio': '99', 'presupuesto': '30000'}).status_code, 403)
        resp = self.login(self.owner).post(reverse('replenishment'), {'accion': 'supuestos', 'nivel_servicio': '99', 'presupuesto': '30.000'})
        self.assertRedirects(resp, reverse('replenishment'))
        self.assertEqual((R.current_params()['nivel_servicio'], R.current_params()['presupuesto']), (99, 30000))
        audit = Audit.objects.get(event='Supuestos de reposición ajustados')
        self.assertEqual((audit.actor, audit.detail), (self.owner, 'nivel_servicio: 95→99; presupuesto: 0→30000'))
        page = self.login(self.owner).get(reverse('replenishment'))
        self.assertContains(page, 'Guardar supuestos')
        self.assertEqual((page.context['budget']['what_if'], page.context['budget']['by_product'][p.pk]), (False, 30))
        R.save_assumptions(self.owner, {'presupuesto': 0, 'nivel_servicio': 95})
        # Pedido manual desde la lista.
        resp = c.post(reverse('replenishment'), {'token': str(uuid.uuid4()), 'producto': [str(p.pk)], f'cantidad_{p.pk}': '50', 'nota': 'Urgente'})
        order = PurchaseOrder.objects.get()
        self.assertRedirects(resp, reverse('purchase_order', args=[order.pk]))
        self.assertEqual((order.units, order.note, order.source), (50, 'Urgente', 'manual'))
        detail = c.get(reverse('purchase_order', args=[order.pk]))
        self.assertNotContains(detail, 'mailto:')
        self.assertEqual(detail.context['delivery']['state'], 'prepared')
        self.assertIn('• 50 unidades de Referencia C', detail.context['text'])
        self.assertIn(settings.COMPANY_NAME, detail.context['text'])
        # «Enviar» registra un envío simulado en la auditoría y cambia el estado; no sale ningún mensaje.
        c.post(reverse('purchase_order_action', args=[order.pk, 'enviar']), {'token': str(uuid.uuid4())})
        order.refresh_from_db()
        self.assertEqual((order.status, order.get_status_display(), compras.delivery(order)['state']), ('sent', 'Envío simulado registrado', 'simulated'))
        self.assertTrue(Audit.objects.filter(event='Envío simulado de pedido al proveedor', target=f'po-{order.pk}').exists())
        self.assertNotContains(c.get(reverse('purchase_order', args=[order.pk])), 'mailto:')
        self.assertEqual(c.get(reverse('purchase_orders')).status_code, 200)

    def test_forecast_page_shows_the_series_the_evaluation_and_the_order_form(self):
        values = noisy(30, level=20, noise=4, seed=9)
        p, batch = self.weekly_product(values=values, initial=sum(values) + 10)
        self.lead_time_invoice(p)
        quiet, _ = self.weekly_product(code='REF-Q', values=[0] * 14, initial=40)             # con existencias y sin salidas
        self.assertEqual(self.login(self.requester).get(reverse('forecast', args=[p.pk])).status_code, 403)
        c = self.login(self.manager)
        page = c.get(reverse('forecast', args=[p.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'style=')
        for text in ('class="series-chart"', 'Salidas observadas', 'Semana sin existencias', 'Demanda no atendida registrada', 'Intervalo del 80 %',
                     'Error en la prueba', 'Elegido', 'Preparar el pedido', 'Cada unidad cuenta una sola vez', 'no se usaron para elegir'):
            self.assertContains(page, text)
        ctx = page.context
        self.assertEqual((ctx['design']['test'], ctx['test'].n, ctx['coverage']['n']), (8, 7, 7))
        self.assertEqual(sum(1 for r in ctx['candidates'] if r['chosen']), 1)
        self.assertEqual(len(ctx['chart']['cols']), 26)
        # Preparar el pedido desde el pronóstico.
        resp = c.post(reverse('replenishment'), {'token': str(uuid.uuid4()), 'producto': [str(p.pk)], f'cantidad_{p.pk}': str(ctx['quantity']), 'volver': str(p.pk)})
        self.assertRedirects(resp, reverse('purchase_order', args=[PurchaseOrder.objects.get().pk]))
        # Sin salidas en la ventana, el error relativo no está definido y así se dice.
        page = c.get(reverse('forecast', args=[quiet.pk]))
        self.assertEqual(page.context['pl'].status, 'sin_consumo')
        self.assertContains(page, 'no definido (sin salidas en la ventana)')
        self.assertContains(page, 'No definido')
        # Historial insuficiente: se declara y no se muestra una evaluación que no existe.
        short, _ = self.weekly_product(code='REF-S', weeks=5)
        page = c.get(reverse('forecast', args=[short.pk]))
        self.assertContains(page, 'Historial insuficiente')
        self.assertEqual(page.context['candidates'], [])


class PaymentsLearningTests(LearningBase):
    def pay_by_bank(self, party, holder, description='TRANSF DESDE CTA 1234', amount='200.000,00', day=None, balance='1.000.000,00'):
        row = [(day or TODAY()).strftime('%d/%m/%Y'), description, '', '', holder, amount, balance]
        self.statement([row], name=f'ext-{uuid.uuid4().hex[:6]}.csv')
        return BankTransaction.objects.order_by('-pk').first()

    def apply(self, txn, party, **kw):
        plan, _ = C.propose_allocation(txn, party)
        with self.captureOnCommitCallbacks(execute=True):
            C.apply_transaction(self.finance, txn.pk, party.pk, [(r.pk, a) for r, a, _ in plan], tok(), **kw)
        return BankTransaction.objects.get(pk=txn.pk)

    def test_model_learns_an_alias_and_breaks_a_tie_the_rules_cannot(self):
        # Diez veces: ambos clientes deben $200.000 y paga una tercera persona, que no es ninguno de los dos. Las reglas empatan.
        for i in range(10):
            self.sale(self.ferreteria, [(self.prod_a, 2, None, '')])
            self.sale(self.rojas, [(self.prod_b, 2, None, '')])
            txn = self.pay_by_bank(self.ferreteria, 'SANDRA PINEDA', balance=f'{1 + i}.000.000,00')   # saldos distintos: huellas distintas
            before = C.suggestions(txn)
            if not pagos.load().trained:
                self.assertEqual(before['decision'], 'ambiguous')
            plan, _ = C.propose_allocation(txn, self.ferreteria)
            C.apply_transaction(self.finance, txn.pk, self.ferreteria.pk, [(r.pk, a) for r, a, _ in plan], tok(), remember_payer=False)
            pagos.learn_from(BankTransaction.objects.get(pk=txn.pk), self.ferreteria)
        st = pagos.status()
        self.assertEqual(st['examples'], 10)
        self.assertTrue(st['trained'])
        self.assertTrue(any(r['party'] == self.ferreteria.name and any(w in ('sandra', 'pineda') for w, _ in r['words']) for r in st['relations']))
        # Evaluación visible: aciertos del primer candidato ya entrenado, con su n, frente a las reglas solas en esos mismos casos.
        self.assertEqual((st['eval_n'], st['rules_hits'], st['rules_none']), (2, 0, 2))      # las reglas empataban: sin sugerencia única
        self.assertGreaterEqual(st['eval_hits'], 1)
        self.assertEqual(sum(r['n'] for r in st['reliability']), st['eval_n'])
        # Un pago nuevo con el mismo patrón: ahora hay un sugerido claro, con la razón del modelo y sin «% de confianza».
        self.sale(self.ferreteria, [(self.prod_a, 2, None, '')])
        self.sale(self.rojas, [(self.prod_b, 2, None, '')])
        txn = self.pay_by_bank(self.ferreteria, 'SANDRA PINEDA', balance='20.000.000,00')
        hint = C.suggestions(txn)
        self.assertEqual(hint['decision'], 'suggested')
        top = hint['candidates'][0]
        self.assertEqual(top['party'], self.ferreteria)
        reason = next(r for r in top['reasons'] if 'modelo de pagos' in r)
        self.assertRegex(reason, r'puntaje \d+ de 100; en la evaluación acertó \d+ de \d+ veces con puntajes de')
        self.assertIn('(pocos casos)', reason)
        self.assertNotIn('confianza', reason)
        self.assertNotIn('%', reason)
        self.assertEqual(top['score_100'], round(top['score'] * 100))
        # Y el agente de conciliación lo convierte en propuesta.
        run = runner.run_agent('conciliacion', 'manual')
        self.assertEqual(run.status, 'succeeded', run.error)
        self.assertTrue(AgentAction.objects.filter(agent='conciliacion', status='proposed', payload__party_id=self.ferreteria.pk).exists())

    def test_learning_runs_after_commit_and_retrain_is_consistent(self):
        self.sale(self.ferreteria, [(self.prod_a, 1, None, '')])
        txn = self.pay_by_bank(self.ferreteria, 'FERRETERIA EL TORNILLO', description='PAGO FACTURA', amount='100.000,00')
        self.apply(txn, self.ferreteria)
        self.assertEqual(MatchFeedback.objects.count(), 1)
        self.assertEqual(pagos.load().meta['examples'], 1)
        online = dict(pagos.load().w)
        n = pagos.retrain()
        self.assertEqual((n, MatchFeedback.objects.count(), pagos.load().meta['examples']), (1, 1, 1))
        self.assertEqual(pagos.load().w, online)                   # reconstruir da el mismo modelo que aprender en línea

    def test_the_sequential_prediction_does_not_see_the_link_created_by_the_identification_itself(self):
        self.sale(self.ferreteria, [(self.prod_a, 1, None, '')])
        txn = self.pay_by_bank(self.ferreteria, 'SANDRA PINEDA', amount='100.000,00')
        txn = self.apply(txn, self.ferreteria)                      # recuerda al pagador: crea el vínculo pagador–cliente
        self.assertTrue(txn.payer.links.filter(party=self.ferreteria, status='confirmed').exists())
        self.assertEqual(pagos.context(txn, learning=True)['links'], {})                     # al aprender, ese vínculo aún no existía
        self.assertEqual(pagos.context(txn)['links'], {self.ferreteria.pk: 'confirmed'})     # al sugerir sobre un ingreso nuevo, sí
        self.assertFalse(any(k == 'payer_confirmed' for k in pagos.load().w))
        # El segundo pago del mismo pagador sí lo encuentra, porque el primero ya estaba identificado.
        self.sale(self.ferreteria, [(self.prod_a, 1, None, '')])
        second = self.apply(self.pay_by_bank(self.ferreteria, 'SANDRA PINEDA', amount='100.000,00', balance='2.000.000,00'), self.ferreteria)
        self.assertEqual(pagos.context(second, learning=True)['links'], {self.ferreteria.pk: 'confirmed'})
        self.assertIn('payer_confirmed', pagos.load().w)

    def test_reverting_an_application_voids_the_example_and_rebuilds_the_model_without_it(self):
        for _ in range(3):
            self.sale(self.ferreteria, [(self.prod_a, 1, None, '')])
        first = self.pay_by_bank(self.ferreteria, 'FERRETERIA EL TORNILLO', description='PAGO FACTURA', amount='100.000,00', balance='1.000.000,00')
        wrong = self.pay_by_bank(self.ferreteria, 'SANDRA PINEDA', amount='100.000,00', balance='2.000.000,00')
        third = self.pay_by_bank(self.ferreteria, 'FERRETERIA EL TORNILLO', description='ABONO FACTURA', amount='100.000,00', balance='3.000.000,00')
        self.apply(first, self.ferreteria)
        self.apply(third, self.ferreteria)
        pagos.retrain()
        never_saw = dict(pagos.load().w)                            # un modelo que nunca vio el ingreso de Sandra
        self.assertFalse(any('sandra' in k for k in never_saw))
        # Se identifica el tercer ingreso: el modelo lo aprende.
        self.apply(wrong, self.ferreteria)
        learned = pagos.load()
        self.assertEqual(learned.meta['examples'], 3)
        self.assertTrue(any('sandra' in k for k in learned.w))
        self.assertNotEqual(learned.w, never_saw)
        # Contabilidad revierte la aplicación: el ejemplo deja de ser verdadero.
        app = PaymentApplication.objects.get(collection__transaction=wrong, reversed_at__isnull=True)
        with self.captureOnCommitCallbacks(execute=True):
            C.reverse_application(self.finance, app.pk, 'Era de otro cliente', tok())
        self.assertEqual(BankTransaction.objects.get(pk=wrong.pk).status, 'pending')
        fb = MatchFeedback.objects.get(transaction=wrong)
        self.assertIsNotNone(fb.voided_at)
        self.assertIn('se revirtió', fb.void_reason)
        rebuilt = pagos.load()
        self.assertEqual(rebuilt.w, never_saw)                      # pesos iguales a los del modelo que nunca vio ese ejemplo
        self.assertEqual(rebuilt.meta['examples'], 2)
        self.assertEqual(MatchFeedback.objects.filter(voided_at__isnull=True).count(), 2)
        st = pagos.status()
        self.assertEqual((st['examples'], st['voided'], len(st['corrections'])), (2, 1, 1))
        self.assertNotIn(wrong.pk, [f.transaction_id for f in st['feedback']])
        self.assertFalse(pagos.reconcile(wrong.pk))                 # ya corregido: no reconstruye de nuevo
        # El ingreso se identifica ahora con el cliente correcto: vuelve a aprenderse, como ejemplo nuevo.
        self.sale(self.rojas, [(self.prod_b, 1, None, '')])
        self.apply(BankTransaction.objects.get(pk=wrong.pk), self.rojas, remember_payer=False)
        self.assertEqual(MatchFeedback.objects.get(transaction=wrong, voided_at__isnull=True).chosen_party, self.rojas)
        self.assertEqual(pagos.load().meta['examples'], 3)

    def test_reliability_table_reports_measured_hit_rate_by_score_band(self):
        self.sale(self.ferreteria, [(self.prod_a, 1, None, '')])
        txn = self.pay_by_bank(self.ferreteria, 'FERRETERIA EL TORNILLO', amount='100.000,00')

        def fb(score, correct, trained=True, voided=False):
            MatchFeedback.objects.create(transaction=txn, predicted_party=self.ferreteria, score=score, chosen_party=self.ferreteria, correct=correct,
                                         candidates=2, trained=trained, voided_at=timezone.now() if voided else None)
        for score, correct in [(0.95, True), (0.90, True), (0.86, False), (0.75, True), (0.55, False), (0.2, False)]:
            fb(score, correct)
        fb(0.99, True, trained=False)                               # antes de entrenado: no cuenta
        fb(0.99, True, voided=True)                                 # ejemplo anulado: no cuenta
        table = {r['label']: (r['n'], r['hits'], r['rate'], r['few']) for r in pagos.reliability()}
        self.assertEqual(table['85 a 100'][:2], (3, 2))
        self.assertAlmostEqual(table['85 a 100'][2], 200 / 3)
        self.assertEqual((table['70 a 84'], table['50 a 69'], table['0 a 49']), ((1, 1, 100.0, True), (1, 0, 0.0, True), (1, 0, 0.0, True)))
        self.assertTrue(table['85 a 100'][3])                       # 3 casos: se advierte que son pocos
        self.assertEqual(pagos.score_label(0.9), 'puntaje 90 de 100; en la evaluación acertó 2 de 3 veces con puntajes de 85 a 100 (pocos casos)')
        MatchFeedback.objects.filter(score__gte=0.85).delete()
        self.assertEqual(pagos.score_label(0.9), 'puntaje 90 de 100; aún sin casos evaluados con puntajes similares')
        for _ in range(pagos.FEW):
            fb(0.6, True)
        self.assertFalse({r['label']: r for r in pagos.reliability()}['50 a 69']['few'])

    def test_behaviour_and_expected_payment(self):
        sale = self.sale(self.rojas, [(self.prod_b, 1, None, '')])
        rec = sale.receivable
        col = Collection.objects.create(party=self.rojas, date=rec.due_date - timedelta(days=5), amount=rec.amount, method='transfer', source='manual', created_by=self.finance)
        PaymentApplication.objects.create(collection=col, receivable=rec, amount=rec.amount, proposal='instruction', created_by=self.finance)
        sale2 = self.sale(self.rojas, [(self.prod_b, 1, None, '')])
        rec2 = sale2.receivable
        col2 = Collection.objects.create(party=self.rojas, date=rec2.due_date - timedelta(days=9), amount=rec2.amount, method='transfer', source='manual', created_by=self.finance)
        PaymentApplication.objects.create(collection=col2, receivable=rec2, amount=rec2.amount, proposal='instruction', created_by=self.finance)
        beh = pagos.behaviour()[self.rojas.pk]
        self.assertEqual((beh['n'], beh['median'], beh['late_share']), (2, -7, 0.0))
        self.assertEqual(pagos.history_label(beh), 'puntual')
        sale3 = self.sale(self.rojas, [(self.prod_b, 1, None, '')])
        self.assertEqual(pagos.expected_payment(sale3.receivable, beh), sale3.receivable.due_date - timedelta(days=7))
        # El comportamiento «hasta una fecha» no ve los pagos posteriores.
        self.assertEqual(pagos.behaviour(until=rec.due_date - timedelta(days=8))[self.rojas.pk]['n'], 1)
        self.assertNotIn(self.rojas.pk, pagos.behaviour(until=rec.due_date - timedelta(days=30)))


class CollectionsLearningTests(LearningBase):
    def overdue(self, party, days, amount='350000', number=900):
        return Receivable.objects.create(party=party, source='opening', prefix='FE', number=number, issue_date=TODAY() - timedelta(days=days + 30),
                                         due_date=TODAY() - timedelta(days=days), amount=Decimal(amount), created_by=self.finance)

    def pay(self, rec, amount, day):
        col = Collection.objects.create(party=rec.party, date=day, amount=amount, method='transfer', source='manual', created_by=self.finance)
        return PaymentApplication.objects.create(collection=col, receivable=rec, amount=amount, proposal='instruction', created_by=self.finance)

    def remind(self, party):
        runner.run_agent('cobranza', 'manual')
        prop = AgentAction.objects.get(agent='cobranza', status='proposed', payload__party_id=party.pk)
        a = runner.decide(self.finance, prop.pk, True)
        self.assertEqual(a.status, 'executed', a.result)
        return a, CollectionAttempt.objects.get(action=a)

    def test_strategy_is_chosen_recorded_and_evaluated_only_when_the_window_is_complete(self):
        rec = self.overdue(self.rojas, 30)
        run = runner.run_agent('cobranza', 'manual')
        self.assertEqual(run.status, 'succeeded', run.error)
        [prop] = AgentAction.objects.filter(agent='cobranza', status='proposed')
        strategy = prop.payload['strategy']
        self.assertEqual(strategy['segment'], 'person:nuevo')
        self.assertIn(strategy['tone'], cobranza.TONES)
        self.assertIn('Estrategia', prop.detail)
        self.assertNotIn('funcion', prop.detail)                    # sin lenguaje causal
        again = cobranza.choose(self.rojas, None, TODAY())
        self.assertEqual((again['tone'], again['timing']), (strategy['tone'], strategy['timing']))   # estable dentro de la semana
        a = runner.decide(self.finance, prop.pk, True)
        self.assertEqual((a.status, a.result['state']), ('executed', 'simulated'))
        attempt = CollectionAttempt.objects.get()
        self.assertEqual((attempt.party, attempt.action, attempt.outcome, attempt.tone, attempt.sent), (self.rojas, a, 'pending', strategy['tone'], True))
        self.assertIn(self.rojas.pk, cobranza.waiting_parties())
        # El cliente paga dos días después. La ventana sigue abierta: todavía no se evalúa ni se aprende.
        self.pay(rec, rec.amount, TODAY() + timedelta(days=2))
        self.assertEqual(cobranza.evaluate(TODAY() + timedelta(days=3)), {'paid': 0, 'partial': 0, 'unpaid': 0})
        self.assertEqual(cobranza.evaluate(TODAY() + timedelta(days=cobranza.WINDOW_DAYS)), {'paid': 0, 'partial': 0, 'unpaid': 0})
        attempt.refresh_from_db()
        self.assertEqual(attempt.outcome, 'pending')
        self.assertEqual(cobranza.counts(), {})
        self.assertEqual(cobranza.overview()['pending'], 1)
        # Al cumplirse la ventana se cierra: hubo pago dentro de ella.
        self.assertEqual(cobranza.evaluate(TODAY() + timedelta(days=cobranza.WINDOW_DAYS + 1))['paid'], 1)
        attempt.refresh_from_db()
        self.assertEqual((attempt.outcome, attempt.days_to_pay), ('paid', 2))
        self.assertEqual(cobranza.counts()[('person:nuevo', strategy['tone'], strategy['timing'])], [1, 0])
        view = cobranza.overview()
        self.assertEqual(view['segments'][0]['label'], 'Persona natural sin historial de pagos')
        self.assertEqual((view['segments'][0]['successes'], view['evaluated'], view['paid']), (1, 1, 1))
        self.assertIn('no prueba', view['caveat'])
        # La evidencia de la propuesta siguiente describe lo observado, sin decir que el recordatorio «funcionó».
        table = {('person:nuevo', tone, days): [1, 0] for tone, days in cobranza.ARMS}
        evidence = cobranza.choose(self.rojas, None, TODAY(), table)['evidence']
        self.assertIn('hubo pago en la ventana en 1 de 1 recordatorios evaluados', evidence)
        self.assertNotIn('funcion', evidence)

    def test_a_prepared_message_without_a_registered_delivery_is_not_learned_from(self):
        rec = self.overdue(self.rojas, 30)
        self.rojas.branches.update(einvoice_email='')               # sin canal: el mensaje solo queda preparado
        a, attempt = self.remind(self.rojas)
        self.assertEqual((a.result['state'], a.result['state_label']), ('prepared', 'Mensaje preparado'))
        self.assertIn('no se evalúa', a.result['message'])
        self.assertEqual((attempt.sent, attempt.outcome), (False, 'skipped'))
        self.assertFalse(Audit.objects.filter(event='Envío simulado de recordatorio de cobro').exists())
        self.assertNotIn(self.rojas.pk, cobranza.waiting_parties())
        self.pay(rec, rec.amount, TODAY() + timedelta(days=1))
        self.assertEqual(cobranza.evaluate(TODAY() + timedelta(days=30)), {'paid': 0, 'partial': 0, 'unpaid': 0})
        attempt.refresh_from_db()
        self.assertEqual(attempt.outcome, 'skipped')
        self.assertEqual(cobranza.counts(), {})                     # no entra al aprendizaje
        view = cobranza.overview()
        self.assertEqual((view['prepared_only'], view['evaluated'], view['pending'], view['segments']), (1, 0, 0, []))

    def test_partial_and_late_payments_and_waiting_for_the_strategy_moment(self):
        self.overdue(self.rojas, 2)                       # cualquier estrategia espera al menos 3 días de mora
        run = runner.run_agent('cobranza', 'manual')
        self.assertEqual(run.metrics['esperan_estrategia'], 1)
        self.assertFalse(AgentAction.objects.filter(agent='cobranza', status='proposed').exists())
        rec = self.overdue(self.ferreteria, 40, number=901)
        a, attempt = self.remind(self.ferreteria)
        self.assertTrue(attempt.sent)                                # correo de facturación registrado: envío simulado
        self.pay(rec, Decimal('500'), TODAY() + timedelta(days=cobranza.WINDOW_DAYS + 2))        # después de la ventana: no cuenta
        changed = cobranza.evaluate(TODAY() + timedelta(days=cobranza.WINDOW_DAYS + 3))
        self.assertEqual(changed, {'paid': 0, 'partial': 0, 'unpaid': 1})
        attempt.refresh_from_db()
        self.assertEqual((attempt.outcome, attempt.days_to_pay), ('unpaid', None))
        self.assertEqual(cobranza.counts()[(attempt.segment, attempt.tone, attempt.timing_days)], [0, 1])
        # Un abono dentro de la ventana cuenta como «abonó».
        other = self.overdue(self.rojas, 40, number=902)
        late = cobranza.record(None, self.finance, {'party_id': self.rojas.pk, 'receivables': [other.pk], 'amount': '350000', 'channel': 'email',
                                                    'strategy': {'segment': 'person:nuevo', 'tone': 'formal', 'timing': 10}})
        self.pay(other, Decimal('100000'), TODAY() + timedelta(days=cobranza.WINDOW_DAYS))        # último día de la ventana
        self.assertEqual(cobranza.evaluate(TODAY() + timedelta(days=cobranza.WINDOW_DAYS + 1))['partial'], 1)
        late.refresh_from_db()
        self.assertEqual((late.outcome, late.days_to_pay), ('partial', cobranza.WINDOW_DAYS))

    def test_reference_rate_of_comparable_receivables_without_a_reminder(self):
        paid = self.overdue(self.rojas, 40, number=910)             # llegó a 10 días de mora hace 30
        self.pay(paid, paid.amount, paid.due_date + timedelta(days=15))              # pagó dentro de la ventana [10, 20]
        self.overdue(self.rojas, 45, number=911)                    # sigue sin pagar
        early = self.overdue(self.rojas, 50, number=912)
        self.pay(early, early.amount, early.due_date + timedelta(days=4))             # pagada antes de los 10 días: no es comparable
        self.overdue(self.rojas, 12, number=913)                    # su ventana aún no se cumple
        reminded = self.overdue(self.rojas, 60, number=914)
        cobranza.record(None, self.finance, {'party_id': self.rojas.pk, 'receivables': [reminded.pk], 'amount': '350000', 'channel': 'email'},
                        now=timezone.now() - timedelta(days=45))    # tuvo recordatorio: no es «sin recordatorio»
        ref = cobranza.reference(TODAY())
        self.assertEqual(sum(r['n'] for r in ref.values()), 2)
        self.assertEqual(sum(r['paid'] for r in ref.values()), 1)
        self.assertEqual(cobranza.overview()['reference_total'], {'n': 2, 'paid': 1})

    def test_messages_by_tone(self):
        rec = self.overdue(self.ferreteria, 25)
        docs = [(r, 25) for r in with_balance(Receivable.objects.filter(pk=rec.pk))]
        firm = cobranza.message(self.ferreteria, docs, 'firme', 'Sandra')
        self.assertIn('25 días de mora', firm)
        self.assertIn('FE-900', firm)
        self.assertIn('$350.000', firm)
        self.assertTrue(cobranza.message(self.ferreteria, docs, 'formal', 'Sandra').startswith('Estimados señores de'))
        self.assertTrue(cobranza.message(self.rojas, docs, 'cordial', 'Camilo').startswith('Hola Camilo'))


class PagesTests(LearningBase):
    def test_learning_page_by_role_and_retrain(self):
        self.weekly_product()
        self.assertEqual(self.login(self.requester).get(reverse('learning')).status_code, 403)
        retrain = reverse('learning_retrain')
        page = self.login(self.manager).get(reverse('learning'))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(page.context['plans']), 1)                         # inventario ve cómo se pronostica cada referencia
        self.assertEqual((page.context['pagos'], page.context['cobranza']), (None, None))   # y no los modelos de pagos ni de cobranza
        self.assertContains(page, 'Historial insuficiente')
        self.assertNotContains(page, 'style=')
        page = self.login(self.finance).get(reverse('learning'))
        self.assertIsNotNone(page.context['pagos'])
        self.assertIsNotNone(page.context['cobranza'])
        self.assertNotContains(page, retrain)                                   # reconstruir es de administración y dirección
        self.assertContains(page, 'no es un porcentaje de confianza')
        self.assertContains(page, 'no prueba que el recordatorio causara el pago')
        self.assertNotContains(page, 'de probabilidad')
        page = self.login(self.owner).get(reverse('learning'))
        self.assertContains(page, retrain)
        self.assertEqual(self.login(self.finance).post(reverse('learning_retrain'), {'token': str(uuid.uuid4())}).status_code, 403)
        resp = self.login(self.admin).post(reverse('learning_retrain'), {'token': str(uuid.uuid4())})
        self.assertRedirects(resp, reverse('learning'))

    def test_methodology_page_is_fed_with_computed_figures(self):
        values = noisy(30, level=20, noise=4, seed=9)
        self.weekly_product(values=values, initial=sum(values) + 10)
        self.assertEqual(Client().get(reverse('methodology')).status_code, 302)              # exige sesión
        page = self.login(self.requester).get(reverse('methodology'))                        # cualquier perfil puede consultarla
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'style=')
        for text in ('Regla automática', 'Método estadístico', 'Modelo entrenado', 'IA generativa', 'no está definido', 'Salidas no es demanda',
                     'no prueba que el recordatorio causara el pago', 'Datos sintéticos'):
            self.assertContains(page, text)
        rep = page.context['rep']
        self.assertEqual((rep['comparison']['refs'], rep['coverage']['h']['n'], rep['total_weeks'], rep['observed_units']), (1, 7, 30, sum(values)))
        self.assertEqual(page.context['k']['test'], series.TEST_WEEKS)
        chosen = rep['comparison']['chosen']
        self.assertAlmostEqual(chosen['test_rel'], chosen['test'][0] / chosen['test'][1])
