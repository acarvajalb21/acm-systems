"""Pantallas de los modelos: reposición, pronóstico de una referencia, «lo aprendido» y metodología.

Las pantallas de trabajo (reposición, pronóstico, lo aprendido) hablan en lenguaje llano, con evidencias y
acciones. El detalle técnico (diseño de validación, métricas, clasificación de cada pieza) vive en Metodología.
Todas las cifras salen de cálculos sobre la base; ninguna está escrita a mano.
"""
import re
import uuid
from collections import Counter, defaultdict
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from inventory import services as S, compras
from inventory.forms import TokenForm
from inventory.models import Product, Supplier, PurchaseOrder
from inventory.views import roles
from . import reposicion as R, pagos, cobranza, series
from .models import ForecastRecord

VIEWERS = ('inventory', 'finance', 'admin', 'owner')
MAX_BUDGET = R.BOUNDS['presupuesto'][1]


def parse_budget(text):
    """«8000000», «8.000.000» o «$ 8.000.000» → 8000000. None si viene vacío; ValueError si no es una cifra."""
    text = (text or '').strip()
    if not text:
        return None
    digits = re.sub(r'[\s.$,]', '', text)
    if not digits.isdigit():
        raise ValueError(text)
    return min(int(digits), MAX_BUDGET)


def _budget(request, p, plan_list, today):
    """Presupuesto vigente: el guardado o, si viene `?presupuesto=`, ese valor como simulación (no se guarda)."""
    what_if = None
    try:
        what_if = parse_budget(request.GET.get('presupuesto'))
    except ValueError:
        messages.warning(request, 'El presupuesto debe ser una cifra en pesos, por ejemplo 8000000. Se usa el guardado.')
    value = p['presupuesto'] if what_if is None else what_if
    bp = R.budget_plan(plan_list, value, R.committed_cost(today, p))
    bp['what_if'] = what_if is not None
    bp['saved'] = p['presupuesto']
    return bp


@roles(*VIEWERS)
def replenishment(request):
    today = timezone.localdate()
    p = R.current_params()
    plan_list = R.with_accuracy(R.plans(p, today), today)
    can_order = request.user.role in S.MANAGERS
    is_admin = request.user.role in S.ADMINS
    bp = _budget(request, p, plan_list, today)
    if request.method == 'POST':
        if request.POST.get('accion') == 'supuestos':
            return _save_assumptions(request)
        if not can_order:
            messages.error(request, 'Solo inventario, administración y la dirección preparan pedidos.')
            return redirect('replenishment')
        return _create_orders(request, plan_list, bp)
    groups, orphans = defaultdict(list), []
    for row in bp['lines']:
        (groups[row['plan'].supplier] if row['plan'].supplier else orphans).append(row)
    counts = {k: sum(1 for pl in plan_list if pl.status == k) for k in R.STATUS}
    to_order = [pl for pl in plan_list if pl.needs_order]
    return render(request, 'aprendizaje/replenishment.html', {
        'title': 'Reposición', 'plans': plan_list, 'groups': sorted(groups.items(), key=lambda kv: kv[0].name), 'orphans': orphans,
        'acc': R.accuracy(today=today), 'params': p, 'counts': counts, 'can_order': can_order, 'suppliers': Supplier.objects.filter(active=True),
        'risk': sum(pl.at_risk for pl in plan_list), 'to_order': len(to_order), 'cost': bp['total'], 'budget': bp,
        'units_to_order': sum(pl.quantity for pl in to_order), 'service_levels': sorted(R.Z),
        'open_orders': PurchaseOrder.objects.filter(status__in=PurchaseOrder.OPEN).select_related('supplier').prefetch_related('lines__product')[:8],
        'token_form': TokenForm(), 'today': today, 'is_admin': is_admin})


def _save_assumptions(request):
    if request.user.role not in S.ADMINS:
        raise PermissionDenied('Solo la administración y la dirección ajustan los supuestos de reposición.')
    changes = {}
    try:
        budget = parse_budget(request.POST.get('presupuesto'))
        changes['presupuesto'] = 0 if budget is None else budget
        changes['nivel_servicio'] = int(request.POST.get('nivel_servicio', ''))
    except ValueError:
        messages.error(request, 'Revise los supuestos: el nivel de servicio va de 90 a 99 y el presupuesto es una cifra en pesos (0 = sin tope).')
        return redirect('replenishment')
    done = R.save_assumptions(request.user, changes)
    if done:
        messages.success(request, 'Supuestos guardados. Las sugerencias se recalcularon con ellos y el cambio quedó en la auditoría.')
    else:
        messages.info(request, 'Los supuestos no cambiaron.')
    return redirect('replenishment')


def _create_orders(request, plan_list, bp):
    back = request.POST.get('volver') or ''
    target = redirect('forecast', pk=int(back)) if back.isdigit() else redirect('replenishment')
    form = TokenForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'La solicitud venció. Vuelva a intentarlo.')
        return target
    by_pk = {str(pl.product.pk): pl for pl in plan_list}
    funded = bp['by_product']
    by_supplier, problems = defaultdict(list), []
    for pk in request.POST.getlist('producto'):
        pl = by_pk.get(pk)
        if pl is None:
            continue
        try:
            qty = int(request.POST.get(f'cantidad_{pk}', funded.get(pl.product.pk, pl.quantity)) or 0)
        except ValueError:
            problems.append(f'{pl.product.name}: la cantidad debe ser un número.')
            continue
        sid = request.POST.get(f'proveedor_{pk}') or (str(pl.supplier.pk) if pl.supplier else '')
        supplier = Supplier.objects.filter(pk=sid, active=True).first() if sid.isdigit() else None
        if supplier is None:
            problems.append(f'{pl.product.name}: elija el proveedor al que se pide.')
            continue
        if qty > 0:
            by_supplier[supplier].append({'product': pl.product, 'quantity': qty, 'unit_cost': pl.unit_cost, 'reason': pl.summary[:300], 'lead': pl.lead_days})
    for text in problems:
        messages.error(request, text)
    if not by_supplier:
        if not problems:
            messages.info(request, 'Seleccione al menos una referencia con cantidad.')
        return target
    base, created = form.cleaned_data['token'], []
    for supplier, lines in by_supplier.items():
        try:
            order = compras.create_order(request.user, supplier, lines, request.POST.get('nota', ''), uuid.uuid5(base, str(supplier.pk)), source='manual',
                                         expected_on=timezone.localdate() + timezone.timedelta(days=lines[0]['lead']))
        except (ValidationError, IntegrityError, OperationalError) as e:
            messages.error(request, f'{supplier.name}: ' + (' '.join(getattr(e, 'messages', [str(e)])) or 'no se pudo preparar el pedido.'))
            continue
        created.append(order)
    if created:
        messages.success(request, ('1 pedido preparado: ' if len(created) == 1 else f'{len(created)} pedidos preparados: ') + ', '.join(o.number for o in created) + '. Revíselos y registre su envío simulado.')
        return redirect('purchase_order', pk=created[0].pk) if len(created) == 1 else redirect('purchase_orders')
    return target


@roles(*VIEWERS)
def forecast(request, pk):
    """Pronóstico de una referencia: lo observado, lo que se espera, con qué error y qué se sugiere hacer."""
    product = get_object_or_404(Product, pk=pk)
    today = timezone.localdate()
    p = R.current_params()
    plan_list = R.with_accuracy(R.plans(p, today), today)
    plan = next((pl for pl in plan_list if pl.product.pk == product.pk), None)
    if plan is None:      # referencia inactiva: se calcula aparte, con el mismo modelo global
        plan = R.with_accuracy(R.plans(p, today, products=[product]), today)[0]
    bp = _budget(request, p, plan_list, today)
    line = next((row for row in bp['lines'] if row['plan'].product.pk == product.pk), None)
    left_out = next((row for row in bp['excluded'] if row['plan'].product.pk == product.pk), None)
    fit = plan.fit
    records = list(ForecastRecord.objects.filter(product=product).order_by('-week_start')[:12])
    closed = [r for r in records if r.actual is not None and not r.stockout]
    can_order = request.user.role in S.MANAGERS
    return render(request, 'aprendizaje/forecast.html', {
        'title': f'Pronóstico · {product.name}', 'product': product, 'pl': plan, 'e': plan.evidence, 'fit': fit, 'chart': R.chart(plan),
        'design': fit.get('design'), **_evaluation(plan), 'params': p, 'budget': bp, 'line': line, 'left_out': left_out,
        'records': records, 'prospective': R._score(closed), 'can_order': can_order, 'is_admin': request.user.role in S.ADMINS,
        'quantity': line['quantity'] if line else plan.quantity, 'today': today, 'token_form': TokenForm(),
        'updated': timezone.localtime(), 'last_week': plan.series.weeks[-1] if plan.series and plan.series.weeks else None,
        'next_week': R.week_start(today), 'horizon_end': R.week_start(today) + timezone.timedelta(days=7 * plan.horizon_weeks - 1),
        'min_weeks': series.MIN_WEEKS, 'min_quantile': series.MIN_QUANTILE, 'suppliers': Supplier.objects.filter(active=True) if can_order and not plan.supplier else []})


def _evaluation(plan):
    """Cómo se eligió el método de una referencia y cómo le fue, listo para mostrar."""
    fit, unit = plan.fit, plan.product.units
    if not fit.get('enough'):
        return {'why': fit.get('note') or 'Sin movimientos suficientes para pronosticar esta referencia.', 'candidates': [], 'windows': [],
                'kind_label': '', 'criterion': None, 'criterion_h': 1, 'test': None, 'coverage': None}
    H, hs = fit['horizon'], fit['selection_horizon']
    key = 'sel_h' if hs == H else 'sel_week'
    rows = [{**r, 'criterion': r[key], 'test': r['test_h']} for r in fit['evaluations']]
    chosen = next(r for r in rows if r['chosen'])
    reference = next((r for r in rows if r['method'] == series.FALLBACK), None)
    why = f'Se usa «{plan.label}»: tuvo el menor error al compararlo con los demás candidatos en {fit["design"]["selection"]} semanas de selección'
    if reference and reference is not chosen and reference['criterion'].mae:
        why += f' ({_dec(chosen["criterion"].mae)} {unit} de error medio frente a {_dec(reference["criterion"].mae)} del promedio simple)'
    elif reference is chosen:
        why += ' (ningún otro candidato lo superó)'
    d = fit['design']
    total, x, windows = d['weeks'], 0.0, []
    for cls, n, label in (('train', d['train'], 'entrenamiento'), ('selection', d['selection'], 'selección'), ('test', d['test'], 'prueba')):
        w = round(n / total * 600, 1)
        last = cls == 'test'       # el rótulo de la última ventana se alinea a la derecha para que no se salga del dibujo
        windows.append({'cls': cls, 'x': round(x, 1), 'w': max(w - 2, 1), 'label': f'{n} sem. {label}', 'tx': 600 if last else round(x, 1),
                        'anchor': 'end' if last else 'start'})
        x += w
    return {'why': why + '.', 'candidates': rows, 'windows': windows, 'kind_label': series.KINDS[plan.kind], 'criterion': chosen['criterion'],
            'criterion_h': hs, 'test': chosen['test'], 'coverage': fit.get('coverage_h')}


def _dec(v):
    return f'{v:.1f}'.replace('.', ',')


def _replenishment_summary(today):
    plan_list = R.with_accuracy(R.plans(today=today), today)
    with_history = [pl for pl in plan_list if pl.weeks]
    enough = [pl for pl in with_history if pl.enough]
    return {'plans': with_history, 'all': plan_list, 'enough': enough, 'methods': Counter(pl.label for pl in enough).most_common(),
            'comparison': R.comparison(plan_list), 'coverage': R.coverage_summary(plan_list), 'acc': R.accuracy(today=today),
            'trained_chosen': sum(1 for pl in enough if pl.kind == 'entrenado'),
            'stockout_weeks': sum(pl.stockout_weeks for pl in with_history), 'total_weeks': sum(pl.total_weeks for pl in with_history),
            'unmet_units': sum(pl.unmet_units for pl in with_history), 'observed_units': sum(pl.observed_units for pl in with_history)}


@roles(*VIEWERS)
def learning(request):
    today = timezone.localdate()
    user = request.user
    rep = _replenishment_summary(today)
    finance = user.role in S.FINANCE
    return render(request, 'aprendizaje/learning.html', {
        'title': 'Lo aprendido', 'rep': rep, 'acc': rep['acc'], 'plans': rep['plans'], 'methods': rep['methods'], 'enough': len(rep['enough']),
        'records': ForecastRecord.objects.filter(actual__isnull=False).select_related('product')[:16],
        'pagos': pagos.status() if finance else None, 'cobranza': cobranza.overview(today) if finance else None,
        'finance': finance, 'manager': user.role in S.MANAGERS, 'is_admin': user.role in S.ADMINS, 'token_form': TokenForm(),
        'min_weeks': series.MIN_WEEKS})


@login_required
def methodology(request):
    """Detalle para evaluación académica: diseño de validación, métricas, clasificación de cada pieza, supuestos y límites."""
    today = timezone.localdate()
    rep = _replenishment_summary(today)
    p = R.current_params()
    pay, cob = pagos.status(), cobranza.overview(today)
    constants = {'warmup': series.WARMUP, 'test': series.TEST_WEEKS, 'test_reduced': series.TEST_WEEKS_REDUCED, 'select_max': series.SELECT_MAX,
                 'min_weeks': series.MIN_WEEKS, 'full_weeks': series.FULL_WEEKS, 'min_select': series.MIN_SELECT_ORIGINS, 'min_seasonal': series.MIN_SEASONAL,
                 'min_quantile': series.MIN_QUANTILE, 'intermittent': int(series.INTERMITTENT * 100), 'ridge_lambda': 1, 'ridge_features': series.GlobalRidge.D,
                 'ridge_cap': int(series.GlobalRidge.CAP), 'candidates': [c for c in series.CANDIDATES],
                 'min_rate': str(R.MIN_RATE).replace('.', ','), 'max_coverage': R.MAX_COVERAGE_DAYS}
    horizons = Counter(pl.horizon_weeks for pl in rep['enough'])
    reduced = sum(1 for pl in rep['enough'] if pl.fit['design']['reduced'])
    safety = Counter(pl.safety_method for pl in rep['all'] if pl.safety_method)
    return render(request, 'aprendizaje/methodology.html', {
        'title': 'Metodología', 'rep': rep, 'params': p, 'k': constants, 'horizons': sorted(horizons.items()), 'reduced': reduced,
        'short': len(rep['plans']) - len(rep['enough']), 'safety': safety, 'pagos': pay, 'cobranza': cob,
        'pagos_k': {'lr': pagos.LR, 'l2': pagos.L2, 'epochs': pagos.EPOCHS, 'min_examples': pagos.MIN_EXAMPLES, 'few': pagos.FEW},
        'arms': len(cobranza.ARMS), 'today': today, 'updated': timezone.localtime()})


@roles(*S.ADMINS)
@require_POST
def retrain(request):
    form = TokenForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'La solicitud venció. Vuelva a intentarlo.')
        return redirect('learning')
    try:
        S.operation(request.user, form.cleaned_data['token'], 'retrain_payments')
    except ValidationError as e:
        messages.error(request, ' '.join(e.messages))
        return redirect('learning')
    n = pagos.retrain()
    S.event(request.user, 'Modelo de pagos reentrenado', 'model-pagos', f'{n} ejemplos')
    messages.success(request, f'Modelo de pagos reconstruido con {n} {"ingreso identificado vigente" if n == 1 else "ingresos identificados vigentes"}.')
    return redirect('learning')
