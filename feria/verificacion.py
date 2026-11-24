"""Comprobaciones del escenario sintético: los saldos cuadran con sus documentos y los casos preparados existen.

`run(state)` devuelve una lista de resultados (grupo, comprobación, correcto, detalle). Las comprobaciones de los
casos se evalúan con la fecha fijada en la fecha de referencia del escenario, que es para la que se prepararon.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from django.db.models import Count, F, Q, Sum
from agentes.models import AgentAction, AgentRun
from cartera import discounts, services as C
from cartera.models import (BankAccount, BankTransaction, DiscountRequest, PaymentApplication, Receivable, ReceivableAdjustment, Sale, SaleStock,
                            with_balance)
from contable.models import SimulatedDocument, Submission
from inventory import codes, compras, services as S
from inventory.einvoice import parse_invoice_xml
from inventory.models import (Batch, CreditNoteApplication, Invoice, InvoiceLine, Movement, PayableAdjustment, PaymentAllocation, Product,
                              ProductBarcode, PurchaseOrder, Reservation, Supplier, SupplierPayment, SupplyRequest, User, with_payable_balance)
from inventory.pdf_invoice import parse_invoice_pdf
from . import documentos
from .reloj import frozen

ZERO = Decimal('0.00')


@dataclass
class Check:
    group: str
    name: str
    ok: bool
    detail: str = ''


class Verifier:
    def __init__(self, state):
        self.state = state
        self.cases = state.cases or {}
        self.day = state.reference_date
        self.results = []
        self.group = ''

    def check(self, name, ok, detail=''):
        self.results.append(Check(self.group, name, bool(ok), str(detail)))
        return bool(ok)

    def attempt(self, name, fn):
        """Una comprobación que falla por una excepción cuenta como fallo, con su causa."""
        try:
            fn()
        except Exception as e:  # la verificación debe terminar y decir qué no pudo comprobar
            self.check(name, False, f'{type(e).__name__}: {e}')

    def case(self, path):
        node = self.cases
        for part in path.split('.'):
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                return None
        return node

    def proposed(self, action_id, agent, kind):
        a = AgentAction.objects.filter(pk=action_id).first() if action_id else None
        return a is not None and a.status == 'proposed' and a.agent == agent and a.kind == kind

    # --------------------------------------------------------------- saldos
    def inventory(self):
        self.group = 'Inventario'
        moved = dict(Movement.objects.values_list('batch_id').annotate(s=Sum('quantity')))
        reserved = dict(Reservation.objects.filter(status='active').values_list('batch_id').annotate(s=Sum('quantity')))
        batches = list(Batch.objects.all())
        bad = [b.pk for b in batches if b.physical != (moved.get(b.pk) or 0)]
        self.check('El físico de cada lote es la suma de sus movimientos', not bad, f'{len(batches)} lotes' if not bad else f'lotes con diferencia: {bad[:8]}')
        bad = [b.pk for b in batches if b.reserved != (reserved.get(b.pk) or 0)]
        self.check('Lo reservado de cada lote es la suma de sus reservas activas', not bad, f'{len(reserved)} lotes con reservas' if not bad else f'lotes con diferencia: {bad[:8]}')
        bad = [b.pk for b in batches if b.physical < 0 or b.reserved < 0 or b.reserved > b.physical]
        self.check('Ningún lote tiene saldo negativo ni reservas mayores que su físico', not bad, bad[:8] if bad else '')
        orphans = Movement.objects.filter(kind='receipt', invoice_line__isnull=True).count()
        self.check('Toda entrada por factura está ligada a su línea', orphans == 0, f'{Movement.objects.filter(kind="receipt").count()} entradas')

    def purchases(self):
        self.group = 'Compras'
        received = dict(Movement.objects.filter(kind='receipt').values_list('invoice_line_id').annotate(s=Sum('quantity')))
        lines = list(InvoiceLine.objects.all())
        bad = [l.pk for l in lines if l.received != (received.get(l.pk) or 0) or l.received > l.quantity]
        self.check('Lo recibido de cada línea de factura es la suma de sus entradas', not bad, f'{len(lines)} líneas' if not bad else f'líneas con diferencia: {bad[:8]}')
        pending = defaultdict(int)
        for l in lines:
            pending[l.invoice_id] += l.quantity - l.received
        bad = []
        for o in PurchaseOrder.objects.select_related('invoice'):
            if o.status == 'received' and o.invoice_id and pending[o.invoice_id] > 0:
                bad.append(f'{o.number} recibido con mercancía pendiente')
            if o.status in PurchaseOrder.OPEN and o.invoice_id and o.invoice.review != 'void' and pending[o.invoice_id] == 0:
                bad.append(f'{o.number} abierto con su factura ya recibida')
            if o.status == 'sent' and o.sent_at is None:
                bad.append(f'{o.number} enviado sin fecha de envío')
        self.check('Los pedidos abiertos y cerrados son coherentes con sus facturas', not bad,
                   f'{PurchaseOrder.objects.count()} pedidos, {PurchaseOrder.objects.filter(status__in=PurchaseOrder.OPEN).count()} abiertos' if not bad else '; '.join(bad[:5]))

    def payables(self):
        self.group = 'Cuentas por pagar'
        paid, disc = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
        for inv, amount, discount in PaymentAllocation.objects.filter(payment__status__in=SupplierPayment.SETTLED).values_list('invoice_id', 'amount', 'discount'):
            paid[inv] += amount
            disc[inv] += discount
        credit = dict(CreditNoteApplication.objects.values_list('invoice_id').annotate(s=Sum('amount')))
        adjust = defaultdict(lambda: ZERO)
        for a in PayableAdjustment.objects.all():
            adjust[a.invoice_id] += a.signed
        bad, n = [], 0
        for inv in with_payable_balance(Invoice.objects.exclude(review='void')):
            n += 1
            expected = inv.total - paid[inv.pk] - disc[inv.pk] - (credit.get(inv.pk) or ZERO) + adjust[inv.pk]
            if inv.balance_due != expected or inv.balance != expected or expected < 0:
                bad.append(inv.number)
        self.check('El saldo de cada factura de proveedor = total − pagos − descuentos − notas crédito ± ajustes', not bad,
                   f'{n} facturas' if not bad else f'facturas con diferencia: {bad[:8]}')
        bad = [p.pk for p in SupplierPayment.objects.filter(status__in=SupplierPayment.SETTLED)
               if p.amount != (p.allocations.aggregate(s=Sum('amount'))['s'] or ZERO)]
        self.check('El valor de cada pago confirmado es la suma de lo aplicado a sus facturas', not bad, bad[:8] if bad else f'{SupplierPayment.objects.count()} pagos')
        reconciled = SupplierPayment.objects.filter(status='reconciled')
        bad = [p.pk for p in reconciled.select_related('bank_transaction') if not hasattr(p, 'bank_transaction') or -p.bank_transaction.amount != p.amount]
        self.check('Cada pago conciliado tiene su egreso en el extracto por el mismo valor', not bad, bad[:8] if bad else f'{reconciled.count()} conciliados')

    def receivables(self):
        self.group = 'Cartera'
        applied = dict(PaymentApplication.objects.filter(reversed_at__isnull=True).values_list('receivable_id').annotate(s=Sum('amount')))
        adjusted = dict(ReceivableAdjustment.objects.values_list('receivable_id').annotate(s=Sum('amount')))
        simulated = {(d.prefix, d.number): d.balance for d in SimulatedDocument.objects.filter(kind='invoice')}
        bad, mismatch, n = [], [], 0
        for r in with_balance(Receivable.objects.all()):
            n += 1
            expected = r.amount - (applied.get(r.pk) or ZERO) - (adjusted.get(r.pk) or ZERO)
            if r.balance != expected or expected < 0:
                bad.append(r.document)
            if simulated.get((r.prefix, r.number)) != expected:
                mismatch.append(r.document)
        self.check('El saldo de cada cuenta por cobrar = valor − aplicaciones vigentes − ajustes', not bad, f'{n} cuentas por cobrar' if not bad else bad[:8])
        self.check('El saldo de cada cuenta por cobrar coincide con el documento en el simulador contable', not mismatch, mismatch[:8] if mismatch else f'{len(simulated)} documentos')
        issued = Sale.objects.filter(status='issued')
        bad = issued.filter(Q(receivable__isnull=True) | Q(fiscal_number__isnull=True)).count()
        stuck = Sale.objects.filter(status__in=('sending', 'uncertain', 'rejected')).count() + Submission.objects.exclude(status='accepted').count()
        self.check('Toda venta emitida tiene número fiscal y cuenta por cobrar; ningún envío quedó a medias', bad == 0 and stuck == 0,
                   f'{issued.count()} ventas emitidas' if bad == 0 and stuck == 0 else f'{bad} sin cuenta por cobrar, {stuck} envíos sin aceptar')
        out = dict(SaleStock.objects.filter(line__sale__status='issued').values_list('line_id').annotate(s=Sum('quantity')))
        bad = [l for l, q in Sale.objects.filter(status='issued').values_list('lines__pk', 'lines__quantity') if out.get(l) != q]
        self.check('Cada línea vendida descontó exactamente sus unidades del inventario', not bad, bad[:8] if bad else '')

    def bank(self):
        self.group = 'Banco'
        used = defaultdict(lambda: ZERO)
        for txn, amount in PaymentApplication.objects.filter(collection__source='bank', reversed_at__isnull=True, collection__transaction__isnull=False) \
                .values_list('collection__transaction_id', 'amount'):
            used[txn] += amount
        for c in C.Collection.objects.filter(transaction__isnull=False).exclude(source='bank').values_list('transaction_id', 'amount'):
            used[c[0]] += c[1]
        bad, states, n = [], [], 0
        for t in BankTransaction.objects.all():
            n += 1
            u = used[t.pk]
            if t.amount > 0 and u > t.amount:
                bad.append(t.pk)
            expected = 'other' if t.status == 'other' else 'pending' if u <= 0 else 'applied' if u >= t.amount else 'identified'
            if t.amount < 0 and t.status != 'other':
                states.append(t.pk)
            elif t.status != expected or (t.status == 'other' and u > 0):
                states.append(t.pk)
        self.check('En cada ingreso bancario, lo aplicado no supera su valor', not bad, f'{n} movimientos' if not bad else bad[:8])
        self.check('El estado de cada movimiento corresponde a lo aplicado', not states, states[:8] if states else '')

    def generation(self):
        self.group = 'Generación'
        anomalies = (self.state.summary or {}).get('anomalias', [])
        self.check('La simulación terminó sin operaciones rechazadas', not anomalies, f'{len(anomalies)}: ' + ' | '.join(anomalies[:3]) if anomalies else '')
        failed = AgentRun.objects.exclude(status='succeeded').count()
        self.check('Todos los turnos de los agentes terminaron bien', failed == 0, f'{AgentRun.objects.count()} turnos' if not failed else f'{failed} con error')
        accounts = self.cases.get('accounts', {})
        users = {u.username: u for u in User.objects.filter(username__in=accounts.values())}
        ok = len(accounts) == 8 and all(users.get(name) is not None and users[name].is_active and not users[name].must_change_password
                                        and users[name].has_usable_password() and users[name].role == role.split('_')[0] for role, name in accounts.items())
        self.check('Las ocho cuentas de demostración existen, están activas y tienen su perfil', ok, ', '.join(sorted(accounts.values())))

    # ---------------------------------------------------------------- series
    def patterns(self):
        self.group = 'Patrones de salida'
        patterns = self.cases.get('patrones', {})
        products = {p.code: p for p in Product.objects.all()}
        weekly = defaultdict(lambda: defaultdict(int))
        for code, at, qty in Movement.objects.filter(kind__in=S.OUTFLOWS).values_list('batch__product__code', 'created_at', 'quantity'):
            d = at.date()
            weekly[code][d - timedelta(days=d.weekday())] += -qty
        first = min((w for s in weekly.values() for w in s), default=self.day)
        span = max((self.day - first).days // 7, 1)

        def mean(code, lo, hi):
            weeks = [w for w in weekly[code] if lo <= (self.day - w).days < hi]
            return sum(weekly[code][w] for w in weeks) / max((hi - lo) / 7, 1)

        by = defaultdict(list)
        for code, pat in patterns.items():
            by[pat].append(code)
        self.check('El catálogo tiene 32 referencias en cuatro categorías', len(products) == 32 and len(set(p.category for p in products.values())) == 4,
                   f'{len(products)} referencias')
        if span >= 60:
            up, down = by['creciente'][0], by['decreciente'][0]
            self.check('La referencia creciente sube y la decreciente baja', mean(up, 0, 84) > 1.6 * mean(up, span * 7 - 168, span * 7 - 84)
                       and mean(down, 0, 84) < 0.6 * mean(down, span * 7 - 168, span * 7 - 84), f'{up}: {mean(up, 0, 84):.0f}/sem ahora; {down}: {mean(down, 0, 84):.0f}/sem ahora')
            zero_share = min(sum(1 for w in range(span) if not weekly[c].get(first + timedelta(days=7 * w))) / span for c in by['intermitente'])
            self.check('Las referencias intermitentes tienen muchas semanas en cero', zero_share >= 0.4, f'al menos {zero_share:.0%} de semanas sin salidas')
            rejected = SupplyRequest.objects.filter(status='rejected', product__code__in=by['agotados']).count()
            self.check('Las referencias con periodos sin existencias tienen solicitudes rechazadas (demanda no atendida)', rejected >= 3, f'{rejected} solicitudes rechazadas')
            losses = dict(Movement.objects.filter(kind='writeoff').values_list('loss_reason').annotate(n=Count('pk')))
            self.check('El historial tiene bajas por vencimiento y por daño', losses.get('expiry', 0) >= 2 and losses.get('damage', 0) >= 2,
                       ', '.join(f'{k}: {v}' for k, v in sorted(losses.items())))
        idle = by['sin_rotacion'][0]
        self.check('La referencia sin rotación reciente conserva existencias y no sale hace meses', mean(idle, 0, 120) == 0
                   and (Batch.objects.filter(product__code=idle, expiry__gte=self.day).aggregate(s=Sum('physical'))['s'] or 0) > 0, idle)

    # ----------------------------------------------------------------- casos
    def prepared(self):
        self.group = 'Casos preparados'
        D = self.day
        with frozen(D):
            for name, fn in (('Faltante próximo', self.c_shortage), ('Lotes por vencer y vencido', self.c_expiry), ('Recepción', self.c_reception),
                             ('Pronto pago', self.c_discount), ('Banco', self.c_bank), ('Cartera vencida', self.c_portfolio),
                             ('Referencias sin historial', self.c_new), ('Solicitudes', self.c_requests), ('Descuentos', self.c_sale_discount),
                             ('Ventas', self.c_sales), ('Pedidos', self.c_orders), ('Documentos para importar', self.c_documents)):
                self.attempt(name, fn)

    def c_shortage(self):
        c = self.case('faltante') or {}
        p = Product.objects.get(pk=c['product_id'])
        free = Batch.objects.filter(product=p, expiry__gte=self.day).aggregate(s=Sum(F('physical') - F('reserved')))['s'] or 0
        transit = InvoiceLine.objects.filter(product=p).exclude(invoice__review='void').aggregate(s=Sum(F('quantity') - F('received')))['s'] or 0
        ordered = compras.on_order([p]).get(p.pk, 0)
        from aprendizaje import reposicion as R
        [plan] = R.plans(products=[p])
        self.check('Faltante próximo: referencia de alta rotación con pocos días de cobertura y sin pedidos en camino',
                   0 <= free <= 1.2 * (plan.rate or free + 1) and transit == 0 and ordered == 0, f'{p.name}: {free} disponibles, {plan.rate:.0f} por semana')
        self.check('Faltante próximo: la posición está bajo el punto de pedido', plan.needs_order and plan.status == 'pedir_ya',
                   f'posición {plan.position}, punto de pedido {plan.reorder_point:.0f}, estado «{plan.status_label}»')
        a = AgentAction.objects.filter(pk=c.get('action_id')).first()
        self.check('Reposición: hay una propuesta de pedido lista para aprobar que incluye esa referencia',
                   self.proposed(c.get('action_id'), 'reposicion', 'purchase_order') and p.pk in a.payload.get('products', [])
                   and self.case('reposicion.action_id') == a.pk, a.title[:120] if a else 'sin propuesta')

    def c_expiry(self):
        soon, expired = self.case('vencimiento.por_vencer') or {}, self.case('vencimiento.vencido') or {}
        b = Batch.objects.select_related('product').get(pk=soon['batch_id'])
        since = self.day - timedelta(days=28)
        rate = -(Movement.objects.filter(batch__product=b.product, kind__in=S.OUTFLOWS, created_at__date__gte=since).aggregate(s=Sum('quantity'))['s'] or 0) / 28
        left = (b.expiry - self.day).days
        risk = b.physical - rate * left
        self.check('Lote por vencer en 30 días o menos con unidades en riesgo', b.physical > 0 and 0 <= left <= 30 and risk > 0,
                   f'{b.product.name} · lote {b.lot}: {b.physical} unidades, vence en {left} días, {risk:.0f} en riesgo')
        b = Batch.objects.select_related('product').get(pk=expired['batch_id'])
        self.check('Lote vencido con saldo y propuesta de baja', b.expiry < self.day and b.physical - b.reserved > 0
                   and self.proposed(expired.get('action_id'), 'vencimientos', 'writeoff_expired'),
                   f'{b.product.name} · lote {b.lot}: {b.physical} unidades, venció el {b.expiry:%d/%m/%Y}')

    def c_reception(self):
        c = self.case('recepcion') or {}
        inv = Invoice.objects.get(pk=c['invoice_id'])
        boxes = 0
        products = {l.product_id: l for l in inv.lines.all()}
        units = defaultdict(int)
        for raw in c.get('codes', []):
            scan = codes.parse(raw)
            code = ProductBarcode.objects.filter(gtin=scan.gtin).first()
            if scan.kind == 'gs1' and code and code.product_id in products and scan.lot and scan.expiry and scan.serial:
                boxes += 1
                units[code.product_id] += code.units
        self.check('Factura validada por recibir, sin unidades recibidas', inv.review == 'validated' and inv.received_units == 0 and inv.expected_units > 0
                   and inv.reception == 'pending', f'{inv.number}: {inv.expected_units} unidades')
        self.check('Los códigos de prueba de las cajas se leen y cubren exactamente la factura', boxes == len(c.get('codes', [])) > 0
                   and all(units[pid] == l.quantity for pid, l in products.items()), f'{boxes} cajas')
        p = c.get('parcial') or {}
        inv = Invoice.objects.get(pk=p['invoice_id'])
        line = inv.lines.get(pk=p['line_id'])
        self.check('Recepción parcial: factura validada con una línea recibida a medias', inv.review == 'validated' and inv.reception == 'partial'
                   and 0 < line.received < line.quantity, f'{inv.number}: {line.received} de {line.quantity} en {line.product.name}')

    def c_discount(self):
        c = self.case('pronto_pago') or {}
        inv = Invoice.objects.get(pk=c['invoice_id'])
        offer = S.discount_for(inv, self.day)
        days = (offer['deadline'] - self.day).days if offer['deadline'] else None
        self.check('Pronto pago: factura cuyo descuento vence en 2 a 4 días, con propuesta del agente',
                   inv.balance > 0 and offer['amount'] > 0 and days is not None and 2 <= days <= 4 and self.proposed(c.get('action_id'), 'pronto_pago', 'schedule_payment'),
                   f'{inv.number}: {S.cop(offer["amount"])} hasta el {offer["deadline"]:%d/%m/%Y}' if offer['deadline'] else 'sin descuento vigente')

    def c_bank(self):
        c = self.case('banco') or {}
        t = BankTransaction.objects.get(pk=c['claro']['txn_id'])
        hint = C.suggestions(t)
        self.check('Banco: ingreso con sugerencia clara (menciona la factura) y propuesta de aplicación', t.status == 'pending' and hint['decision'] == 'suggested'
                   and hint['candidates'][0]['party'].pk == c['claro']['party_id'] and self.proposed(c['claro'].get('action_id'), 'conciliacion', 'apply_bank'),
                   f'{t.description} · {hint["decision"]}')
        t = BankTransaction.objects.get(pk=c['ambiguo']['txn_id'])
        hint = C.suggestions(t)
        top = {x['party'].pk for x in hint['candidates'] if x['level'] == hint['candidates'][0]['level']} if hint['candidates'] else set()
        self.check('Banco: ingreso ambiguo (el valor coincide con el saldo de dos clientes y no trae referencia)', t.status == 'pending'
                   and hint['decision'] == 'ambiguous' and set(c['ambiguo']['party_ids']) <= top and not t.holder_name, f'{S.cop(t.amount)} · {len(top)} candidatos')
        t = BankTransaction.objects.get(pk=c['sin_coincidencia']['txn_id'])
        self.check('Banco: ingreso sin coincidencias', t.status == 'pending' and C.suggestions(t)['decision'] == 'none', t.description)
        self.check('Banco: no quedan otros ingresos por identificar', BankTransaction.objects.filter(status='pending', amount__gt=0).count() == 3,
                   f'{BankTransaction.objects.filter(status="pending", amount__gt=0).count()} por identificar')

    def c_portfolio(self):
        ranges = {'d15': (8, 24), 'd45': (38, 52), 'd90': (91, 400)}
        for slot, (lo, hi) in ranges.items():
            c = self.case(f'cartera.{slot}') or {}
            r = with_balance(Receivable.objects.filter(pk=c.get('receivable_id'))).first()
            late = (self.day - r.due_date).days if r else None
            self.check(f'Cartera vencida, tramo de {lo}–{hi if hi < 400 else "más"} días, con propuesta de cobranza',
                       r is not None and r.balance > 0 and lo <= late <= hi and self.proposed(c.get('action_id'), 'cobranza', 'collection_reminder'),
                       f'{r.party.name}: {r.document}, {late} días de mora' if r else 'sin cuota')

    def c_new(self):
        a, b = self.case('sin_historial.lanzada') or {}, self.case('sin_historial.nueva') or {}
        first = Movement.objects.filter(batch__product_id=a['product_id']).order_by('created_at').first()
        out = Movement.objects.filter(batch__product_id=a['product_id'], kind__in=S.OUTFLOWS).count()
        self.check('Referencia lanzada hace unas cuatro semanas, con poco historial', first is not None and 21 <= (self.day - first.created_at.date()).days <= 45 and out > 0,
                   f'primer movimiento el {first.created_at:%d/%m/%Y}, {out} salidas' if first else 'sin movimientos')
        p = Product.objects.get(pk=b['product_id'])
        self.check('Referencia recién creada, sin movimientos', p.active and not Movement.objects.filter(batch__product=p).exists(), p.name)
        from aprendizaje import reposicion as R
        p = Product.objects.get(pk=self.case('sin_rotacion.product_id'))
        [plan] = R.plans(products=[p])
        self.check('Referencia sin rotación reciente: el pronóstico la deja como «sin consumo» y no sugiere pedirla', plan.status in (('sin_consumo',) if self.state.months >= 12 else ('sin_consumo', 'sin_datos'))
                   and not plan.needs_order and plan.available > 0, f'{p.name}: estado «{plan.status_label}», {plan.available} disponibles')

    def c_requests(self):
        a, b, r = (self.case(f'solicitudes.{k}') or {} for k in ('con_reserva', 'sin_existencias', 'reserva_por_entregar'))
        req = SupplyRequest.objects.get(pk=a['request_id'])
        self.check('Solicitud pendiente con propuesta de reserva', req.status == 'pending' and self.proposed(a.get('action_id'), 'solicitudes', 'reserve_request'),
                   f'{req.product.name} × {req.quantity}')
        req = SupplyRequest.objects.get(pk=b['request_id'])
        self.check('Solicitud pendiente sin existencias suficientes en un lote', req.status == 'pending' and S.suggested_batch(req.product, req.quantity) is None,
                   f'{req.product.name} × {req.quantity}')
        res = Reservation.objects.select_related('request', 'batch').get(pk=r['reservation_id'])
        self.check('Reserva aprobada por entregar', res.status == 'active' and res.request.status == 'approved' and res.batch.reserved >= res.quantity,
                   f'lote {res.batch.lot} × {res.quantity}')

    def c_sale_discount(self):
        c = self.case('descuentos') or {}
        req = DiscountRequest.objects.select_related('sale', 'requested_by').get(pk=c['request_id'])
        admin = User.objects.get(pk=c['admin_user_id'])
        quota = discounts.quota_status(admin, req.month)
        self.check('Descuento en espera de la dirección, con el cupo mensual de administración agotado', req.status == 'pending' and req.kind == 'sale'
                   and req.sale.status == 'draft' and admin.discount_quota > 0 and quota['available'] == 0,
                   f'{S.cop(req.amount)} ({req.percent} %) · cupo {quota["used"]} de {quota["total"]}')

    def c_sales(self):
        a, b = self.case('ventas.borrador') or {}, self.case('ventas.entrega_previa') or {}
        sale = Sale.objects.get(pk=a['sale_id'])
        enough = all((Batch.objects.filter(product=l.product, expiry__gte=self.day).aggregate(s=Sum(F('physical') - F('reserved')))['s'] or 0) >= l.quantity
                     for l in sale.lines.all())
        self.check('Venta en borrador lista para emitir', sale.status == 'draft' and sale.lines.exists() and enough and not sale.discount_requests.filter(status='pending').exists(),
                   f'{sale.party.name} · {S.cop(sale.total)}')
        sale = Sale.objects.get(pk=b['sale_id'])
        line = sale.lines.filter(prior_movement_id=b['movement_id']).first()
        stock = SaleStock.objects.filter(line=line).first() if line else None
        doubled = Movement.objects.filter(reference=sale.fiscal_name, kind='exit').count()
        self.check('Venta que factura una entrega previa sin duplicar la salida', sale.status == 'issued' and line is not None and stock is not None
                   and str(stock.movement_id) == b['movement_id'] and doubled == 0, f'{sale.fiscal_name} · {line.product.name if line else ""}')

    def c_orders(self):
        a, b = self.case('pedidos.en_transito') or {}, self.case('pedidos.parcial') or {}
        o = PurchaseOrder.objects.get(pk=a['order_id'])
        self.check('Pedido enviado y en tránsito', o.status == 'sent' and o.invoice_id is None and o.sent_at is not None, o.number)
        o = PurchaseOrder.objects.select_related('invoice').get(pk=b['order_id'])
        ok = o.status in PurchaseOrder.OPEN and o.invoice_id == b['invoice_id']
        for line in o.lines.select_related('product'):
            inv_line = o.invoice.lines.filter(product=line.product).first()
            others = sum(l.quantity for l in type(line).objects.filter(product=line.product, order__status__in=PurchaseOrder.OPEN, order__invoice__isnull=True))
            ok = ok and inv_line is not None and compras.on_order([line.product]).get(line.product_id, 0) == others
        self.check('Pedido facturado y parcialmente recibido: cada unidad pendiente cuenta una sola vez', ok, o.number)

    def c_documents(self):
        c = self.case('documentos') or {}
        folder = documentos.folder()
        files = {f['name']: f for f in c.get('files', [])}
        kinds = defaultdict(list)
        for f in files.values():
            kinds[f['kind']].append(f)
        self.check('Están los documentos de demostración: 2 facturas PDF, 2 XML, un extracto y los códigos',
                   len(kinds['factura_pdf']) == 2 and len(kinds['factura_xml']) == 2 and len(kinds['extracto']) == 1 and len(kinds['codigos']) == 2
                   and all((folder / name).is_file() for name in files), ', '.join(sorted(files)))
        ok, detail = True, []
        for f in kinds['factura_pdf'] + kinds['factura_xml']:
            raw = (folder / f['name']).read_bytes()
            data = parse_invoice_pdf(raw) if f['kind'] == 'factura_pdf' else parse_invoice_xml(raw)
            supplier = Supplier.objects.filter(nit=data['supplier_nit']).first()
            order = PurchaseOrder.objects.filter(number=data['order_number'], supplier=supplier, status='sent', invoice__isnull=True).first()
            wanted = {l.product.name: l.quantity for l in order.lines.select_related('product')} if order else {}
            read = {l['description']: l['quantity'] for l in data['lines']}
            good = supplier is not None and order is not None and wanted == read and not Invoice.objects.filter(supplier=supplier, number__iexact=data['number']).exists()
            ok = ok and good and not data.get('warnings')
            detail.append(f'{f["name"]} → {data["order_number"]}' + ('' if good else ' (no coincide)'))
        self.check('Cada factura de demostración se lee y corresponde a un pedido abierto sin factura', ok, '; '.join(detail))
        name = kinds['extracto'][0]['name']
        rows, warnings = C.parse_statement(name, (folder / name).read_bytes())
        account = BankAccount.objects.get(pk=self.case('banco.account_id'))
        known = sum(1 for r in rows if BankTransaction.objects.filter(account=account, fingerprint=C.fingerprint(account, r)).exists())
        self.check('El extracto nuevo se lee y sus movimientos aún no están importados', len(rows) >= 4 and not warnings and known == 0
                   and any(Decimal(r['amount']) < 0 for r in rows), f'{len(rows)} movimientos')

    def run(self):
        for part in (self.inventory, self.purchases, self.payables, self.receivables, self.bank, self.generation, self.patterns, self.prepared):
            self.attempt(part.__name__, part)
        return self.results


def run(state):
    return Verifier(state).run()
