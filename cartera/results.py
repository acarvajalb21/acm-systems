"""Resultados del negocio para la dirección, con lo que registra la plataforma.

Es una lectura de gestión, no el estado de resultados contable: no incluye nómina, arriendo, servicios,
impuestos ni ventas facturadas por fuera de la plataforma. El costo de lo vendido usa el costo neto de
compra de cada lote (factura del proveedor recibida en ese lote); si el lote no tiene compra registrada
(saldo inicial), se usa el último costo validado de la misma referencia.
"""
import json
from datetime import date, timedelta
from decimal import Decimal
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone
from inventory import services as S
from inventory.models import Batch, InvoiceLine, Invoice, Movement, PaymentAllocation, SupplierPayment, money
from .models import Sale, SaleLine, SaleStock, PaymentApplication, SaleDiscount, DiscountRequest
from . import services as C

ZERO = Decimal('0.00')
MONTHS = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']
LOSS_LABELS = {'expiry': 'Bajas por vencimiento', 'storage': 'Bajas por almacenamiento inadecuado', 'damage': 'Bajas por daño o derrame', 'other': 'Otras bajas'}


def parse_month(value, today=None):
    today = today or timezone.localdate()
    current = today.replace(day=1)
    try:
        y, m = (int(x) for x in (value or '').split('-'))
        chosen = date(y, m, 1)
    except (ValueError, TypeError):
        return current
    return min(chosen, current) if chosen.year > 2000 else current


def shift(month, delta):
    y, m = divmod(month.year * 12 + month.month - 1 + delta, 12)
    return date(y, m + 1, 1)


def month_name(month):
    return f'{MONTHS[month.month - 1]} {month.year}'


def _unit(line):
    return (line.quantity * line.unit_price - line.discount) / line.quantity if line.quantity else line.unit_price


class Costs:
    """Costo unitario neto por lote, con respaldo en el último costo validado de la referencia."""

    def __init__(self):
        self.by_batch, acc = {}, {}
        for m in Movement.objects.filter(kind='receipt', invoice_line__isnull=False).select_related('invoice_line'):
            q, v = acc.get(m.batch_id, (0, ZERO))
            acc[m.batch_id] = (q + m.quantity, v + _unit(m.invoice_line) * m.quantity)
        self.by_batch = {b: v / q for b, (q, v) in acc.items() if q}
        self.by_product = {}
        for line in InvoiceLine.objects.filter(invoice__review=Invoice.Review.VALIDATED).order_by('invoice__issue_date', 'pk'):
            self.by_product[line.product_id] = _unit(line)

    def of(self, batch):
        return self.by_batch.get(batch.pk, self.by_product.get(batch.product_id))


def _month_sales(start, end):
    return Sale.objects.filter(status=Sale.Status.ISSUED, issue_date__gte=start, issue_date__lt=end)


def sales_block(costs, start, end):
    sales = _month_sales(start, end)
    revenue = sales.aggregate(s=Sum('subtotal'))['s'] or ZERO
    discounts = SaleLine.objects.filter(sale__in=sales).aggregate(s=Sum('discount'))['s'] or ZERO
    cogs, uncosted = ZERO, 0
    for st in SaleStock.objects.filter(line__sale__in=sales).select_related('batch'):
        unit = costs.of(st.batch)
        if unit is None:
            uncosted += st.quantity
        else:
            cogs += unit * st.quantity
    cogs = money(cogs)
    profit = money(revenue - cogs)
    return {'revenue': money(revenue), 'cogs': cogs, 'profit': profit, 'discounts': money(discounts), 'count': sales.count(),
            'uncosted': uncosted, 'margin': (profit * 100 / revenue).quantize(Decimal('0.1')) if revenue else None}


def losses_block(costs, start, end):
    rows, uncosted = {}, 0
    for m in Movement.objects.filter(kind__in=('writeoff', 'adjust_out'), created_at__date__gte=start, created_at__date__lt=end).select_related('batch'):
        unit = costs.of(m.batch)
        label = LOSS_LABELS.get(m.loss_reason or 'other', 'Otras bajas') if m.kind == 'writeoff' else 'Ajustes negativos de inventario'
        if unit is None:
            uncosted += -m.quantity
            continue
        rows[label] = rows.get(label, ZERO) + unit * -m.quantity
    return {k: money(v) for k, v in rows.items()}, uncosted


def report(month):
    start, end = month, shift(month, 1)
    today = timezone.localdate()
    costs = Costs()
    sales = sales_block(costs, start, end)
    inventory_losses, loss_uncosted = losses_block(costs, start, end)
    pay = PaymentAllocation.objects.filter(payment__status__in=SupplierPayment.SETTLED, payment__paid_on__gte=start, payment__paid_on__lt=end)
    prompt = pay.aggregate(saved=Sum('discount'), lost=Sum('lost_discount'))
    saved, lost = money(prompt['saved'] or ZERO), money(prompt['lost'] or ZERO)
    inv_loss_total = money(sum(inventory_losses.values(), ZERO))
    result = money(sales['profit'] - inv_loss_total + saved)

    leaks = [{'label': 'Descuentos concedidos a clientes', 'amount': sales['discounts'], 'hint': 'En ventas facturadas del mes'}]
    leaks += [{'label': k, 'amount': v, 'hint': 'Valorizadas al costo de compra'} for k, v in inventory_losses.items()]
    leaks.append({'label': 'Pronto pago no aprovechado', 'amount': lost, 'hint': 'Descuento de proveedores que se dejó pasar'})
    leaks = sorted([l for l in leaks if l['amount'] > 0], key=lambda l: -l['amount'])
    top = max((l['amount'] for l in leaks), default=ZERO)
    for l in leaks:
        l['share'] = int(round(float(l['amount'] / top) * 100)) if top else 0

    # Cartera (quién nos debe), al día de hoy.
    rows = list(C.open_receivables())
    buckets = C.aging(rows, today)
    total_ar = money(sum((r.balance for r in rows), ZERO))
    overdue_ar = money(total_ar - buckets['current'])
    debtors = {}
    for r in rows:
        d = debtors.setdefault(r.party_id, {'party': r.party, 'balance': ZERO, 'late': ZERO, 'count': 0})
        d['balance'] += r.balance
        d['count'] += 1
        if r.days_late > 0:
            d['late'] += r.balance
    labels = [('current', 'Al día'), ('d30', '1 a 30 días'), ('d60', '31 a 60 días'), ('d90', '61 a 90 días'), ('over', 'Más de 90 días')]
    peak = max(buckets.values()) if rows else ZERO
    aging = [{'key': k, 'label': lbl, 'amount': money(buckets[k]), 'share': int(round(float(buckets[k] / peak) * 100)) if peak else 0} for k, lbl in labels]
    collected = PaymentApplication.objects.filter(reversed_at__isnull=True, collection__date__gte=start, collection__date__lt=end).aggregate(s=Sum('amount'))['s'] or ZERO

    # Lo que debemos (proveedores) y lo que tenemos (inventario a costo).
    payables = S.payables()
    owe = money(sum((i.offer['balance'] for i in payables), ZERO))
    owe_late = money(sum((i.offer['balance'] for i in payables if i.days_to_due < 0), ZERO))
    owe_soon = money(sum((i.offer['balance'] for i in payables if 0 <= i.days_to_due <= 30), ZERO))
    stock_value, stock_soon, stock_uncosted = ZERO, ZERO, 0
    for b in Batch.objects.filter(physical__gt=0):
        unit = costs.of(b)
        if unit is None:
            stock_uncosted += b.physical
            continue
        stock_value += unit * b.physical
        if b.expiry <= today + timedelta(days=60):
            stock_soon += unit * b.physical

    trend = [dict(sales_block(costs, shift(month, -i), shift(month, -i + 1)), month=shift(month, -i)) for i in range(5, 0, -1)] + [dict(sales, month=month)]
    for t in trend:
        t['name'] = month_name(t['month'])
        t['short'] = MONTHS[t['month'].month - 1][:3].capitalize()

    granted = SaleDiscount.objects.filter(month=start).exclude(status=SaleDiscount.Status.REPLACED)
    return {
        'month': month, 'month_name': month_name(month), 'prev': shift(month, -1), 'next': shift(month, 1) if shift(month, 1) <= today.replace(day=1) else None,
        'sales': sales, 'result': result, 'saved': saved, 'lost': lost, 'inventory_losses': inv_loss_total, 'loss_uncosted': loss_uncosted,
        'leaks': leaks, 'leak_total': money(sum((l['amount'] for l in leaks), ZERO)),
        'ar_total': total_ar, 'ar_overdue': overdue_ar, 'ar_count': len(rows), 'ar_late_count': sum(1 for r in rows if r.days_late > 0),
        'owe_late_count': sum(1 for i in payables if i.days_to_due < 0), 'aging': aging, 'collected': money(collected),
        'debtors': sorted(debtors.values(), key=lambda d: -d['balance'])[:5],
        'owe': owe, 'owe_late': owe_late, 'owe_soon': owe_soon, 'owe_count': len(payables),
        'stock_value': money(stock_value), 'stock_soon': money(stock_soon), 'stock_uncosted': stock_uncosted,
        'trend': trend, 'chart': column_chart(trend),
        'discounts_granted': granted.count(), 'discounts_amount': granted.aggregate(s=Sum('amount'))['s'] or ZERO,
        'pending_requests': DiscountRequest.objects.filter(status=DiscountRequest.Status.PENDING).count(),
    }


def _nice(value):
    if value <= 0:
        return Decimal(1)
    exp = len(str(int(value))) - 1
    base = Decimal(10) ** exp
    for step in (1, 2, 2.5, 5, 10):
        if value <= base * Decimal(str(step)):
            return base * Decimal(str(step))
    return base * 10


def _bar_path(x, y0, y1, w, r=4):
    """Columna con extremos de datos redondeados (4 px) y apoyada en la línea base."""
    top, bottom = min(y0, y1), max(y0, y1)
    h = bottom - top
    if h < 0.5:
        return ''
    r = min(r, h, w / 2)
    if y1 < y0:  # positiva: redondea arriba
        return (f'M{x:.1f},{bottom:.1f}V{top + r:.1f}Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f}H{x + w - r:.1f}'
                f'Q{x + w:.1f},{top:.1f} {x + w:.1f},{top + r:.1f}V{bottom:.1f}Z')
    return (f'M{x:.1f},{top:.1f}V{bottom - r:.1f}Q{x:.1f},{bottom:.1f} {x + r:.1f},{bottom:.1f}H{x + w - r:.1f}'
            f'Q{x + w:.1f},{bottom:.1f} {x + w:.1f},{bottom - r:.1f}V{top:.1f}Z')


def _short_money(v):
    v = float(v)
    if abs(v) >= 1_000_000:
        return f'${v / 1_000_000:,.1f} M'.replace(',', 'X').replace('.', ',').replace('X', '.').replace(',0 M', ' M')
    if abs(v) >= 1_000:
        return f'${v / 1_000:,.0f} mil'.replace(',', '.')
    return f'${v:,.0f}'.replace(',', '.')


def _n(v):
    """Coordenada SVG con punto decimal (la plantilla localizaría la coma)."""
    return f'{float(v):.1f}'


def _tick(v, millions):
    """Una sola unidad en todo el eje: millones si el máximo los alcanza."""
    if not v:
        return '$0'
    if millions:
        text = f'{float(v) / 1_000_000:.1f}'.replace('.', ',').replace(',0', '')
        return f'${text} M'
    return _short_money(v)


def column_chart(trend, width=640, height=260):
    """Columnas agrupadas (ventas sin IVA y margen bruto estimado) con un solo eje en pesos."""
    left, right, top, bottom = 64, 12, 24, 36
    plot_w, plot_h = width - left - right, height - top - bottom
    hi = max([t['revenue'] for t in trend] + [t['profit'] for t in trend] + [ZERO])
    lo = min([t['profit'] for t in trend] + [ZERO])
    span_hi = _nice(hi) if hi > 0 else Decimal(1)
    span_lo = -_nice(-lo) if lo < 0 else ZERO
    rng = span_hi - span_lo

    def y(v):
        return top + float((span_hi - v) / rng) * plot_h

    base = y(ZERO)
    millions = max(span_hi, -span_lo) >= 1_000_000
    ticks = [{'y': _n(y(span_lo + rng * k / 4)), 'label': _tick(span_lo + rng * k / 4, millions)} for k in range(5)]
    group = plot_w / len(trend)
    bw = min(26, (group - 18) / 2)
    cols = []
    for i, t in enumerate(trend):
        x0 = left + i * group + (group - (2 * bw + 2)) / 2
        cols.append({'x_label': _n(left + i * group + group / 2), 'label': t['short'], 'name': t['name'],
                     'sales': _bar_path(x0, base, y(t['revenue']), bw), 'profit': _bar_path(x0 + bw + 2, base, y(t['profit']), bw),
                     'revenue': t['revenue'], 'profit_value': t['profit'], 'margin': t['margin'], 'last': i == len(trend) - 1,
                     'sales_top': _n(y(t['revenue']) - 6), 'profit_top': _n((y(t['profit']) - 6) if t['profit'] >= 0 else y(t['profit']) + 14),
                     'sales_x': _n(x0 + bw / 2), 'profit_x': _n(x0 + bw * 1.5 + 2),
                     'sales_short': _short_money(t['revenue']), 'profit_short': _short_money(t['profit']),
                     'hit_x': _n(left + i * group), 'hit_w': _n(group),
                     'tip': json.dumps({'title': t['name'].capitalize(), 'rows': [['Ventas sin IVA', _cop_text(t['revenue']), 'sales'],
                                                                                 ['Margen bruto estimado', _cop_text(t['profit']), 'profit'],
                                                                                 ['Costo de compra de lo vendido', _cop_text(t['cogs']), ''],
                                                                                 ['Margen', (f"{t['margin']} %".replace('.', ',') if t['margin'] is not None else '—'), ''],
                                                                                 ['Facturas', str(t['count']), '']]}, ensure_ascii=False)})
    return {'width': width, 'height': height, 'left': left, 'right': width - right, 'top': top, 'base': _n(base), 'bottom': _n(top + plot_h),
            'plot_h': _n(plot_h), 'tick_x': left - 8, 'month_y': height - 12,
            'ticks': ticks, 'cols': cols, 'empty': hi <= 0 and lo >= 0}


# ---------------------------------------------------------------- vista anual
def _cop_text(v):
    return '$' + f'{float(v):,.0f}'.replace(',', '.')


def _empty_block():
    return {'revenue': ZERO, 'cogs': ZERO, 'profit': ZERO, 'count': 0, 'margin': None, 'discounts': ZERO, 'uncosted': 0}


def year_report(year, costs=None, today=None):
    """Ventas facturadas mes a mes de un año, con el año anterior para comparar."""
    today = today or timezone.localdate()
    current_month = today.replace(day=1)
    costs = costs or Costs()
    months = []
    for m in range(1, 13):
        start = date(year, m, 1)
        future = start > current_month
        block = _empty_block() if future else sales_block(costs, start, shift(start, 1))
        before = date(year - 1, m, 1)
        prev = _month_sales(before, shift(before, 1)).aggregate(s=Sum('subtotal'))['s'] or ZERO
        collected = ZERO if future else (PaymentApplication.objects.filter(reversed_at__isnull=True, collection__date__gte=start,
                                                                           collection__date__lt=shift(start, 1)).aggregate(s=Sum('amount'))['s'] or ZERO)
        months.append(dict(block, month=start, name=month_name(start), short=MONTHS[m - 1][:3].capitalize(), future=future,
                           partial=start == current_month, prev=money(prev), collected=money(collected)))
    closed = [mo for mo in months if not mo['future']]
    # El mes en curso está incompleto: no entra en el promedio, el mes más bajo ni la comparación con el año anterior.
    complete = [mo for mo in closed if not mo['partial']]
    with_sales = [mo for mo in closed if mo['revenue'] > 0]
    best = max(with_sales, key=lambda mo: mo['revenue']) if with_sales else None
    lows = [mo for mo in complete if mo['revenue'] > 0]
    worst = min(lows, key=lambda mo: mo['revenue']) if len(lows) > 1 else None
    for mo in months:
        mo['best'] = best is not None and mo is best
    total = money(sum((mo['revenue'] for mo in months), ZERO))
    profit = money(sum((mo['profit'] for mo in months), ZERO))
    base = complete or closed
    prev_same = money(sum((mo['prev'] for mo in base), ZERO))
    same = money(sum((mo['revenue'] for mo in base), ZERO))
    prev_total = money(sum((mo['prev'] for mo in months), ZERO))
    first = Sale.objects.filter(status=Sale.Status.ISSUED).order_by('issue_date').values_list('issue_date', flat=True).first()
    first_year = min(first.year, today.year) if first else today.year
    return {
        'year': year, 'months': months, 'best': best, 'worst': worst, 'total': total, 'profit': profit,
        'best_label': MONTHS[best['month'].month - 1].capitalize() if best else '',
        'worst_label': MONTHS[worst['month'].month - 1] if worst else '',
        'margin': (profit * 100 / total).quantize(Decimal('0.1')) if total else None,
        'count': sum(mo['count'] for mo in months), 'collected': money(sum((mo['collected'] for mo in months), ZERO)),
        'average': money(sum((mo['revenue'] for mo in base), ZERO) / len(base)) if base else ZERO, 'elapsed': len(closed),
        'complete': len(complete), 'with_sales': len(with_sales), 'prev_same': prev_same, 'prev_total': prev_total, 'same': same,
        'period': (f"enero a {MONTHS[base[-1]['month'].month - 1]}" if len(base) > 1 else MONTHS[base[0]['month'].month - 1]) if base else '',
        'growth': ((same - prev_same) * 100 / prev_same).quantize(Decimal('0.1')) if prev_same else None,
        'prev_year': year - 1 if year - 1 >= first_year else None, 'next_year': year + 1 if year < today.year else None,
        'years': list(range(first_year, today.year + 1)), 'chart': year_chart(months, year),
    }


def parse_year(value, today=None):
    today = today or timezone.localdate()
    try:
        y = int(value)
    except (TypeError, ValueError):
        return today.year
    return y if 2000 <= y <= today.year else today.year


def _scale(values):
    hi = max(values + [ZERO])
    span = _nice(hi) if hi > 0 else Decimal(1)
    return span, span >= 1_000_000


def year_chart(months, year, width=720, height=280):
    """12 meses con el mejor mes resaltado. Dos geometrías: sola o junto al año anterior (la página alterna)."""
    left, right, top, bottom = 64, 12, 30, 36
    plot_w, plot_h = width - left - right, height - top - bottom
    base = top + plot_h
    group = plot_w / 12
    layouts = {}
    for mode in ('single', 'compare'):
        values = [mo['revenue'] for mo in months] + ([mo['prev'] for mo in months] if mode == 'compare' else [])
        span, millions = _scale(values)

        def y(v, span=span):
            return top + float((span - v) / span) * plot_h

        ticks = [{'y': _n(y(span * k / 4)), 'label': _tick(span * k / 4, millions)} for k in range(5)]
        bars = []
        for i, mo in enumerate(months):
            gx = left + i * group
            if mode == 'single':
                bw = min(30, group - 14)
                x = gx + (group - bw) / 2
                bars.append({'cur': _bar_path(x, base, y(mo['revenue']), bw), 'prev': '', 'label_x': _n(x + bw / 2), 'label_y': _n(y(mo['revenue']) - 8)})
            else:
                bw = min(18, (group - 12) / 2)
                x = gx + (group - (2 * bw + 2)) / 2
                bars.append({'prev': _bar_path(x, base, y(mo['prev']), bw), 'cur': _bar_path(x + bw + 2, base, y(mo['revenue']), bw),
                             'label_x': _n(x + bw * 1.5 + 2), 'label_y': _n(y(mo['revenue']) - 8)})
        layouts[mode] = {'ticks': ticks, 'bars': bars}
    cols = []
    for i, mo in enumerate(months):
        rows = [['Ventas sin IVA ' + str(year), _cop_text(mo['revenue']), 'sales'], ['Año ' + str(year - 1), _cop_text(mo['prev']), 'prev'],
                ['Margen bruto estimado', _cop_text(mo['profit']), 'profit']]
        if mo['margin'] is not None:
            rows.append(['Margen', f"{mo['margin']} %".replace('.', ','), ''])
        rows += [['Facturas', str(mo['count']), ''], ['Recaudo aplicado', _cop_text(mo['collected']), '']]
        title = mo['name'].capitalize() + (' · mejor mes' if mo['best'] else '') + (' · en curso' if mo['partial'] else '')
        tip = {'title': title, 'rows': rows, 'hint': '' if mo['future'] else 'Clic para ver el detalle del mes'}
        cols.append({'month': mo, 'single': layouts['single']['bars'][i], 'compare': layouts['compare']['bars'][i],
                     'hit_x': _n(left + i * group), 'hit_w': _n(group), 'x_label': _n(left + i * group + group / 2),
                     'tip': json.dumps(tip, ensure_ascii=False),
                     'aria': f"{mo['name']}: ventas sin IVA {_cop_text(mo['revenue'])}, año anterior {_cop_text(mo['prev'])}",
                     'short_value': _short_money(mo['revenue'])})
    return {'width': width, 'height': height, 'left': left, 'right': width - right, 'top': top, 'base': _n(base), 'plot_h': _n(plot_h),
            'tick_x': left - 8, 'month_y': height - 12, 'single_ticks': layouts['single']['ticks'],
            'compare_ticks': layouts['compare']['ticks'], 'cols': cols}


# ------------------------------------------------------------ lo que se debe
BUCKETS = [('current', 'Al día'), ('d30', '1 a 30 días'), ('d60', '31 a 60 días'), ('d90', '61 a 90 días'), ('over', 'Más de 90 días')]


def _bucket(late):
    return 'current' if late <= 0 else 'd30' if late <= 30 else 'd60' if late <= 60 else 'd90' if late <= 90 else 'over'


def debts_block(today=None, limit=15):
    """Quién nos debe y a quién debemos, por tercero y antigüedad, con los documentos detrás de cada cifra."""
    today = today or timezone.localdate()
    ar = []
    for r in C.open_receivables():
        late = (today - r.due_date).days
        ar.append({'entity_id': r.party_id, 'entity': r.party.name, 'entity_url': reverse('party', args=[r.party_id]),
                   'label': f'{r.prefix}-{r.number}' + (f' cuota {r.quote}' if r.quote > 1 else ''),
                   'url': reverse('sale', args=[r.sale_id]) if r.sale_id else reverse('party', args=[r.party_id]),
                   'issue': r.issue_date, 'due': r.due_date, 'late': late, 'balance': money(r.balance), 'bucket': _bucket(late)})
    ap = []
    for i in S.payables():
        late = -i.days_to_due
        ap.append({'entity_id': i.supplier_id, 'entity': i.supplier.name, 'entity_url': reverse('supplier_account', args=[i.supplier_id]),
                   'label': i.number, 'url': reverse('invoice', args=[i.pk]), 'issue': i.issue_date, 'due': i.due_date, 'late': late,
                   'balance': money(i.offer['balance']), 'bucket': _bucket(late)})
    return {'ar': _group(ar, 'cliente', limit), 'ap': _group(ap, 'proveedor', limit)}


def _group(docs, noun, limit):
    labels = dict(BUCKETS)
    totals = {k: ZERO for k, _ in BUCKETS}
    counts = {k: 0 for k, _ in BUCKETS}
    entities = {}
    for d in docs:
        totals[d['bucket']] += d['balance']
        counts[d['bucket']] += 1
        e = entities.setdefault(d['entity_id'], {'name': d['entity'], 'url': d['entity_url'], 'total': ZERO, 'late': ZERO,
                                                  'amounts': {k: ZERO for k, _ in BUCKETS}, 'counts': {k: 0 for k, _ in BUCKETS}, 'docs': []})
        e['total'] += d['balance']
        e['amounts'][d['bucket']] += d['balance']
        e['counts'][d['bucket']] += 1
        if d['late'] > 0:
            e['late'] += d['balance']
        e['docs'].append(dict(d, bucket_label=labels[d['bucket']]))
    total = money(sum(totals.values(), ZERO))
    ordered = sorted(entities.values(), key=lambda e: -e['total'])
    peak = ordered[0]['total'] if ordered else ZERO
    for e in ordered:
        e['docs'].sort(key=lambda d: -d['late'])
        e['buckets'] = ' '.join(k for k, _ in BUCKETS if e['counts'][k])
        cursor, segs = 0.0, []
        for k, lbl in BUCKETS:
            if not e['amounts'][k]:
                continue
            w = float(e['amounts'][k] / peak) * 100 if peak else 0
            tip = {'title': e['name'], 'rows': [[lbl, _cop_text(e['amounts'][k]), 'age-' + k], ['Documentos', str(e['counts'][k]), ''],
                                                ['Total del ' + noun, _cop_text(e['total']), '']],
                   'hint': 'Abra la fila para ver cada documento'}
            segs.append({'key': k, 'x': _n(cursor), 'w': _n(w), 'tip': json.dumps(tip, ensure_ascii=False),
                         'aria': f"{e['name']}: {lbl.lower()} {_cop_text(e['amounts'][k])}"})
            cursor += w
        e['segments'] = segs
        e['tip'] = json.dumps({'title': e['name'], 'rows': [[lbl, _cop_text(e['amounts'][k]), 'age-' + k] for k, lbl in BUCKETS if e['amounts'][k]]
                               + [['Total', _cop_text(e['total']), '']], 'hint': 'Enter para ver los documentos'}, ensure_ascii=False)
    summary, cursor = [], 0.0
    for k, lbl in BUCKETS:
        w = float(totals[k] / total) * 100 if total else 0
        tip = {'title': lbl, 'rows': [['Saldo', _cop_text(totals[k]), 'age-' + k], ['Documentos', str(counts[k]), ''],
                                      ['Participación', f'{w:.1f} %'.replace('.', ','), '']], 'hint': 'Clic para ver solo este tramo'}
        summary.append({'key': k, 'label': lbl, 'amount': money(totals[k]), 'count': counts[k], 'x': _n(cursor), 'w': _n(w),
                        'tip': json.dumps(tip, ensure_ascii=False)})
        cursor += w
    return {'total': total, 'overdue': money(total - totals['current']), 'count': len(docs), 'entities_count': len(ordered),
            'entities': ordered[:limit], 'more': max(0, len(ordered) - limit), 'summary': summary,
            'oldest': max((d['late'] for d in docs), default=0)}
