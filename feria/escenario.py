"""Generador del escenario sintético de la demostración.

Simula día a día, con el reloj controlado, unos 24 meses de operación de una distribuidora de insumos y deja en la
fecha de referencia los casos preparados que usa el recorrido. Todo saldo sale de llamar a los servicios de la
plataforma (compras, recepción, ventas, cartera, banco, agentes); aquí no se escriben saldos a mano. Solo los datos
maestros sin servicio propio (equipo, catálogo, listas, cuenta bancaria) se crean directamente.

    from feria.escenario import generate
    state, passwords = generate(date(2026, 11, 18), seed=4711)

Misma semilla y misma fecha producen los mismos datos, incluidos los identificadores. Las claves de las cuentas de
demostración se generan al final, fuera de la simulación, y solo se devuelven a quien llama.
"""
import math
import random
import secrets
import time
import unicodedata
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models import F, Sum
from django.db.models.signals import post_save
from django.utils import timezone
from agentes import runner, signals as agent_signals
from agentes.agents import REGISTRY
from agentes.models import AgentAction
from aprendizaje import signals as learning_signals
from cartera import discounts, services as C
from cartera.models import (BankAccount, BankTransaction, Collection, Party, PaymentApplication, PriceList, Receivable, Sale, with_balance)
from contable import services as contable
from contable.models import Submission
from inventory import compras, demo_documents as demo, payables as P, recepcion, services as S
from inventory.models import (Batch, Invoice, InvoiceLine, Product, ProductBarcode, PurchaseOrder, Reservation, Settings, Supplier, SupplierPayment,
                              SupplyRequest, User)
from . import datos_base as B
from .casos import Cases
from .reloj import Clock, controlled

VOLUME = 3            # multiplica las unidades semanales del catálogo para dar tamaño de distribuidora mediana
TAX = Decimal('19')
ZERO = Decimal('0.00')
DEFAULT_SEED = 4711
DEFAULT_MONTHS = 24


def tok():
    return uuid.uuid4()


def plain(text):
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()
    return ' '.join(text.upper().replace('.', '').replace(',', '').split())


def round_up(q, pack):
    return int(math.ceil(q / pack) * pack) if q > 0 else 0


def gtin_for(index):
    """GTIN-14 ficticio con prefijo de uso interno 0299 y dígito verificador válido."""
    from inventory.codes import check_digit
    body = f'0299555{index:06d}'
    return body + check_digit(body)


def box_code(gtin, expiry, lot, serial=''):
    """Código de caja en la forma legible de GS1, la misma que se puede teclear o pegar en el campo del lector."""
    return f'(01){gtin}(17){expiry:%y%m%d}(10){lot}' + (f'(21){serial}' if serial else '')


class Item:
    """Referencia del catálogo con su política de la «operación anterior» (mínimo y cantidad fija)."""

    def __init__(self, row, index):
        (self.code, self.name, self.category, self.unit, self.pack, self.shelf, self.controlled, self.supplier, cost, self.pattern, mean, self.extra) = row
        self.index = index
        self.cost = Decimal(cost)
        self.mean = mean * VOLUME
        self.end = self.extra.get('end', mean) * VOLUME
        self.size = self.extra.get('size', 0) * VOLUME
        self.product = None
        self.minimum = self.quantity = self.alert = 0
        self.gtin = gtin_for(index + 1)
        self.lot_seq = 0

    def reference_mean(self):
        if self.pattern == 'lluvias':
            return self.mean * (1 + (self.extra['peak'] - 1) * 0.6)
        if self.pattern == 'fin_de_ano':
            return self.mean * 1.5
        if self.pattern == 'intermitente':
            return self.size * self.extra['prob'] + self.mean * 0.1
        return self.mean

    def set_policy(self, lead, mean=None):
        mean = mean if mean is not None else self.reference_mean()
        if self.pattern == 'intermitente':
            self.minimum = round_up(self.size * 0.9, self.pack)
            self.quantity = round_up(self.size * 1.6, self.pack)
            return
        self.minimum = max(int(math.ceil(mean * (lead + 7) / 7 * 1.5)), self.pack)
        weeks = 5 if mean >= 30 else 7
        self.quantity = max(round_up(min(mean * weeks, mean * self.shelf * 4.3 * 0.4), self.pack), self.pack)


class Scenario(Cases):
    def __init__(self, reference_date, seed=DEFAULT_SEED, months=DEFAULT_MONTHS, out=None):
        self.D = reference_date
        self.seed = seed
        self.months = months
        start = reference_date - timedelta(days=round(months * 30.44))
        self.S = start - timedelta(days=start.weekday())
        span = (self.D - self.S).days
        agent = self.D - timedelta(days=min(120, span // 2))
        self.agent_start = agent - timedelta(days=agent.weekday())
        self.rng = random.Random(f'escenario:{seed}')
        self.out = out or (lambda text: None)
        self.clock = Clock(self.S, 6, 0)
        self.items = [Item(row, i) for i, row in enumerate(B.PRODUCTS)]
        self.by_code = {it.code: it for it in self.items}
        self.suppliers, self.supplier_data = {}, {s['key']: s for s in B.SUPPLIERS}
        self.clients = {c['key']: dict(c) for c in B.CLIENTS}
        self.parties, self.users, self.lists = {}, {}, {}
        self.avail, self.incoming = defaultdict(int), defaultdict(int)
        self.deliveries = defaultdict(list)       # día → entregas de proveedores
        self.receipts_due = defaultdict(list)     # día → saldos de recepciones parciales
        self.pay_plan = defaultdict(list)         # día → facturas de proveedor por pagar
        self.scheduled = defaultdict(list)        # día → pagos programados por confirmar
        self.client_pay = defaultdict(list)       # día → [(cliente, cuota, valor)]
        self.week_sales = defaultdict(lambda: defaultdict(int))
        self.requests_due = defaultdict(list)
        self.to_decide, self.to_dispatch = defaultdict(list), defaultdict(list)
        self.bank_rows, self.bank_seq, self.balance = [], 0, B.OPENING_BALANCE
        self.cash_box = ZERO
        self.staged = defaultdict(list)
        self.hold, self.hold_parties = set(), set()
        self.protected_invoices, self.protected_batches = set(), set()
        self.suspended = {}
        self.delayed = {it.code: list(it.extra['delays']) for it in self.items if 'delays' in it.extra}   # entregas que el proveedor retrasará
        self.block_faltante = False
        self.counters = defaultdict(int)
        self.anomalies = []
        self.cases = {}
        self.case = {'abiertos': {}, 'no_order': set()}   # datos de trabajo de los casos preparados
        self.documents = {}
        self.invoice_seq = defaultdict(int)
        self.po_seq = 4000
        self.last_statement = None
        self.discounts_month = defaultdict(int)
        self.agents_ok = True
        self.timing = defaultdict(float)

    # ------------------------------------------------------------------ utilidades
    def step(self, low=40, high=200):
        if self.clock.now.hour < 21:
            self.clock.tick(self.rng.randint(low, high))
        else:
            self.clock.tick(2)

    def timed(self, name, fn, *args):
        started = time.monotonic()
        try:
            return fn(*args)
        finally:
            self.timing[name] += time.monotonic() - started

    def anomaly(self, where, error):
        text = ' '.join(error.messages) if isinstance(error, ValidationError) else f'{type(error).__name__}: {error}'
        self.anomalies.append(f'{self.clock.today:%d/%m/%Y} · {where}: {text}'[:300])

    def business(self, day):
        """El día hábil igual o anterior (los domingos no se opera)."""
        return day - timedelta(days=1) if day.weekday() == 6 else day

    def next_business(self, day):
        return day + timedelta(days=1) if day.weekday() == 6 else day

    def b(self, days_before):
        return self.business(self.D - timedelta(days=days_before))

    def frac(self, day):
        return (day - self.S).days / max((self.D - self.S).days, 1)

    def at(self, day, fn, *args):
        if day >= self.S:
            self.staged[day].append((fn, args))

    def flush(self):
        """En una transacción larga (pruebas) los envíos al simulador contable no salen solos: se procesan aquí."""
        for pk in Submission.objects.filter(status=Submission.Status.PENDING).values_list('pk', flat=True):
            contable.process(pk)

    def refresh_stock(self):
        today = self.clock.today
        self.avail = defaultdict(int, {r['product_id']: r['u'] for r in Batch.objects.filter(expiry__gte=today).values('product_id')
                                       .annotate(u=Sum(F('physical') - F('reserved')))})

    def cost_of(self, item, day):
        return item.cost if (day - self.S).days < 365 else (item.cost * Decimal('1.05')).quantize(Decimal('1'))

    def location(self, item):
        return B.LOCATION_CONTROLLED if item.controlled else B.LOCATION

    def new_lot(self, item, day):
        item.lot_seq += 1
        return f'L{day:%y%m}-{item.index + 1:02d}{item.lot_seq:03d}'

    def in_agent_period(self, day=None):
        return (day or self.clock.today) >= self.agent_start

    # ------------------------------------------------------------- datos maestros
    def setup(self):
        now = self.clock.now
        contable.sync_catalogs()
        Settings.current()
        for role, username, first, last, area, _ in B.TEAM:
            u = User(username=username, first_name=first, last_name=last, email=f'{username.split(".")[0]}@ejemplo.example', role=role,
                     must_change_password=False, can_issue_invoices=role == 'billing', date_joined=now)
            u.set_unusable_password()
            u.save()
            self.users[username] = u
        u = self.users
        self.owner, self.admin, self.inv, self.rec = u['direccion.demo'], u['administracion.demo'], u['inventario.demo'], u['recepcion.demo']
        self.fin, self.bill, self.req1, self.req2 = u['contabilidad.demo'], u['facturacion.demo'], u['solicitante.demo'], u['proyectos.demo']
        for user in u.values():
            S.event(self.owner, 'Cuenta de demostración creada', f'user-{user.pk}', f'{user.display_name} · {user.get_role_display()}')
        discounts.set_quota(self.owner, self.admin.pk, 3, tok())
        self.admin.refresh_from_db()
        for s in B.SUPPLIERS:
            self.suppliers[s['key']] = S.save_supplier(self.fin, None, {
                'name': s['name'], 'nit': s['nit'], 'email': s['email'], 'phone': s['phone'], 'payment_days': s['payment_days'],
                'discount_base': s['base'], 'active': True}, [{'days': d, 'percent': Decimal(p)} for d, p in s['tiers']], tok())
        for code, name, context, _ in B.PRICE_LISTS:
            self.lists[context] = PriceList.objects.create(code=code, name=name, context=context)
            S.event(self.fin, 'Lista de precios creada', f'pricelist-{self.lists[context].pk}', name)
        for item in self.items:
            item.set_policy(self.supplier_data[item.supplier]['lead'][0])
            if item.pattern not in ('lanzamiento', 'nueva'):
                self.create_product(item, self.S)
        self.account = BankAccount.objects.create(**B.BANK)
        S.event(self.fin, 'Cuenta bancaria registrada', f'account-{self.account.pk}', str(self.account))
        for c in self.clients.values():
            c['account'] = f'{4100000000 + 7919 * (len(self.parties) + 3) + self.rng.randint(0, 6999):010d}'
            if c['behaviour'] != 'nuevo':
                self.create_party(c)
        self.opening_stock()
        self.build_demand()

    def create_product(self, item, day):
        supplier = self.supplier_data[item.supplier]
        item.product = Product.objects.create(
            code=item.code, name=item.name, category=item.category, generic=B.GENERIC[item.category], manufacturer=supplier['name'],
            presentation=B.PRESENTATION[item.unit].format(n=item.pack), unit=item.unit, minimum=item.minimum, pack_size=item.pack,
            controlled_storage=item.controlled)
        ProductBarcode.objects.create(product=item.product, gtin=item.gtin, units=item.pack, created_by=self.inv)
        S.event(self.inv, 'Producto creado', item.product.pk, f'{item.name} · {item.code}')
        for _, _, context, margin in B.PRICE_LISTS:
            C.save_price(self.fin, self.lists[context], item.product, self.price(item, margin, day), day, tok())

    def price(self, item, margin, day):
        value = self.cost_of(item, day) * margin * (Decimal('1.01') if (day - self.S).days >= 365 else 1)
        return Decimal(int(round(float(value) / 100.0)) * 100)

    def create_party(self, c):
        company = c['id_type'] == '31'
        party = C.save_party(self.bill, None, {
            'id_type': c['id_type'], 'identification': c['ident'], 'name': c['name'], 'kind': 'company' if company else 'person',
            'default_context': c['context'], 'price_list': None, 'credit_days': c['credit'], 'requires_po': bool(c.get('po')), 'active': True, 'notes': '',
            'einvoice_email': f'facturacion@{c["key"]}.cliente.example' if c['key'] != 'mostrador' else '',
            'address': '', 'city': c['city']}, tok())
        if c.get('contact'):
            name, channel, value = c['contact']
            C.add_contact(self.fin, party.pk, {'name': name, 'role': 'collection', 'channel': channel, 'value': value, 'preferred': True, 'notes': ''}, tok())
        self.parties[c['key']] = party
        c['party'] = party
        return party

    def opening_stock(self):
        self.clock.at(self.S, 6, 30)
        for item in self.items:
            if item.product is None:
                continue
            total = round_up(item.minimum + item.quantity * self.rng.uniform(0.45, 0.9), item.pack)
            first = round_up(total * 0.4, item.pack)
            life = item.shelf * 30
            for qty, share in ((first, 0.5), (total - first, 0.85)):
                if qty > 0:
                    S.adjust_in(self.inv, item.product, self.new_lot(item, self.S), self.S + timedelta(days=int(life * share)), self.location(item), qty,
                                'initial', 'Saldo inicial al iniciar el registro', tok())
            self.clock.tick(20)

    # ------------------------------------------------------------------- demanda
    def weekly_mean(self, item, monday):
        frac, month = self.frac(monday), (monday + timedelta(days=3)).month
        pat, mean = item.pattern, item.mean
        if pat == 'lluvias':
            peak = item.extra['peak']
            return mean * {3: 1.25, 4: peak, 5: peak, 6: 1.2, 9: 1.3, 10: peak, 11: peak * 0.95, 12: 1.15}.get(month, 0.85)
        if pat == 'fin_de_ano':
            peak = item.extra['peak']
            return mean * {10: 1.6, 11: peak * 0.85, 12: peak}.get(month, 0.8)
        if pat in ('creciente', 'decreciente'):
            return mean + (item.end - mean) * frac
        if pat == 'sin_rotacion':
            return mean if monday < self.D - timedelta(days=150) else 0
        if pat == 'lanzamiento':
            return mean if monday >= self.D - timedelta(days=30) else 0
        if pat == 'nueva':
            return 0
        return mean

    def build_demand(self):
        """Serie semanal por referencia, con su propia secuencia aleatoria (no depende del resto de la simulación)."""
        self.mondays = []
        m = self.S
        while m < self.D:
            self.mondays.append(m)
            m += timedelta(days=7)
        self.demand, self.spikes = {}, {}
        for item in self.items:
            rng = random.Random(f'{self.seed}:demanda:{item.code}')
            series = []
            for monday in self.mondays:
                mean = self.weekly_mean(item, monday)
                if mean <= 0:
                    units = 0
                elif item.pattern == 'intermitente':
                    r = rng.random()
                    units = int(round(item.size * math.exp(rng.gauss(0, 0.35)))) if r < item.extra['prob'] else (rng.randint(1, 4) if r > 0.88 else 0)
                elif item.pattern == 'bajo' or item.extra.get('low') or mean < 12:
                    units = self.poisson(rng, mean)
                else:
                    units = max(0, int(round(rng.gauss(mean, 0.17 * mean + 1))))
                series.append(units)
            self.demand[item.code] = series
            for frac, units in item.extra.get('spikes', []):
                index = min(int(frac * len(self.mondays)), len(self.mondays) - 6)
                if index > 6:
                    self.spikes[(item.code, index)] = units * VOLUME

    @staticmethod
    def poisson(rng, mean):
        limit, k, p = math.exp(-mean), 0, 1.0
        while True:
            p *= rng.random()
            if p <= limit:
                return k
            k += 1

    def split(self, units):
        if units <= 6:
            return [units]
        k = 2 if units < 30 else 3 if units < 90 else 4 if units < 200 else 5
        weights = [self.rng.uniform(0.5, 1.5) for _ in range(k)]
        parts = [max(1, int(units * w / sum(weights))) for w in weights]
        parts[0] += units - sum(parts)
        return [p for p in parts if p > 0]

    def active_buyers(self, monday, index):
        out = []
        for c in self.clients.values():
            if 'party' not in c or (c['key'] in self.suspended and monday >= self.suspended[c['key']]):
                continue
            if c['every'] == 2 and (index + len(c['key'])) % 2:
                continue
            out.append(c)
        return out

    def plan_week(self, monday):
        index = self.mondays.index(monday)
        buyers = self.active_buyers(monday, index)
        cats = ['mantenimiento', 'adhesivos', 'sellantes', 'recubrimientos']
        for item in self.items:
            if item.product is None:
                continue
            units = self.demand[item.code][index]
            spike = self.spikes.get((item.code, index))
            if spike:
                target = self.clients['pradera' if item.category == 'mantenimiento' else 'vias']
                self.week_sales[(monday + timedelta(days=target['weekday'] + 1), target['key'])][item.code] += spike
            if units <= 0:
                continue
            ci = cats.index(item.category)
            for chunk in self.split(units):
                requester = self.req1 if item.category in B.REQUEST_CATEGORIES['solicitante.demo'] else None
                if item.category in B.REQUEST_CATEGORIES['proyectos.demo'] and (requester is None or self.rng.random() < 0.45):
                    requester = self.req2
                if requester and self.rng.random() < 0.06:
                    qty = min(chunk, self.rng.randint(2, 12))
                    self.requests_due[monday + timedelta(days=self.rng.randint(0, 4))].append((requester, item, qty))
                    chunk -= qty
                    if chunk <= 0:
                        continue
                weights = [c['cats'][ci] * (0.6 if c['id_type'] == '13' else 1.0) for c in buyers]
                buyer = self.rng.choices(buyers, weights=weights)[0]
                self.week_sales[(monday + timedelta(days=buyer['weekday']), buyer['key'])][item.code] += chunk
            # Sin existencias: el área interna también la pide y la solicitud queda rechazada como demanda no atendida.
            if item.pattern == 'agotados' and self.avail[item.product.pk] <= 0:
                requester = self.req2 if item.category == 'recubrimientos' else self.req1
                for _ in range(self.rng.randint(1, 2)):
                    self.requests_due[monday + timedelta(days=self.rng.randint(0, 4))].append((requester, item, self.rng.randint(2, 8)))

    # ---------------------------------------------------------------------- ventas
    def sell(self, key, wanted, day=None, hold=False, method=None, prior=None, discount=None, draft=False, issue_date=None):
        """Registra y emite una venta. `wanted` = {código: unidades}; vende lo que haya disponible."""
        c, day = self.clients[key], day or self.clock.today
        party = c['party']
        lines = []
        for code in sorted(wanted):
            item = self.by_code[code]
            if item.product is None:
                continue
            free = self.avail[item.product.pk]
            if code == 'MNT-001' and self.block_faltante and not prior:
                free = max(free - self.case.get('faltante_floor', 0), 0)
            qty = min(wanted[code], free) if not (prior and code in prior) else wanted[code]
            if qty > 0:
                lines.append({'product': item.product, 'quantity': qty, 'unit_price': None, 'price_reason': '', 'tax_rate': TAX,
                              'prior_movement': (prior or {}).get(code)})
        if not lines:
            return None
        credit = c['credit'] > 0 and c['behaviour'] != 'contado'
        method = method or {'quintero': 'transfer', 'montealegre': 'card'}.get(key) or self.rng.choice(['cash', 'cash', 'cash', 'card', 'cash', 'transfer'])
        self.counters['ref'] += 1
        reference = '' if credit or method == 'cash' else (f'APROB-{410000 + self.counters["ref"]}' if method == 'card' else f'TRF-{770000 + self.counters["ref"]}')
        header = {'party': party, 'context': c['context'], 'price_list': self.lists[c['context']], 'issue_date': issue_date or day, 'seller_id': '401',
                  'condition': 'credit' if credit else 'cash', 'payment_type_id': '204' if credit else {'cash': '201', 'card': '202', 'transfer': '203'}[method],
                  'due_date': (issue_date or day) + timedelta(days=c['credit']) if credit else None, 'cash_method': '' if credit else method,
                  'cash_reference': reference, 'purchase_order': f'OC-{52000 + self.counters["ref"]}' if c.get('po') else '', 'observations': ''}
        try:
            self.step()
            sale = C.save_sale(self.bill, None, header, lines, tok())
            if discount:
                percent, reason = discount
                self.step(30, 90)
                state, obj = discounts.grant_discount(self.admin, sale.pk, None, percent, reason, tok())
                if state == 'pending' and not draft:
                    self.step(200, 900)
                    discounts.decide_request(self.owner, obj.pk, True, 'Aprobado por la dirección', tok())
            if draft:
                return sale
            self.step(20, 80)
            C.issue_sale(self.bill, sale.pk, tok())
            self.flush()
        except ValidationError as e:
            self.anomaly(f'venta a {c["name"]}', e)
            return None
        sale.refresh_from_db()
        if sale.status != Sale.Status.ISSUED:
            self.anomalies.append(f'{day:%d/%m/%Y} · la venta {sale.pk} quedó en estado {sale.status}: {sale.status_detail}')
            return sale
        self.counters['ventas'] += 1
        for line in lines:
            if not line['prior_movement']:
                self.avail[line['product'].pk] -= line['quantity']
        if credit:
            rec = sale.receivable
            if hold:
                self.hold.add(rec.pk)
                self.hold_parties.add(party.pk)
            else:
                self.plan_collection(c, rec)
        else:
            col = sale.collections.first()
            if method == 'cash':
                self.cash_box += sale.total
            else:
                when = self.next_business(day + timedelta(days=1)) if method == 'card' else day
                text = f'ABONO VENTAS DATAFONO {reference}' if method == 'card' else f'TRANSFERENCIA RECIBIDA {plain(c["name"])}'
                self.bank(when, sale.total, text, reference, holder=plain(c['name']) if method == 'transfer' and key != 'mostrador' else '',
                          meta={'kind': 'counter', 'collections': [col.pk]})
        return sale

    def plan_collection(self, c, rec):
        beh = c['behaviour']
        delay = {'puntual': self.rng.randint(-4, 1), 'retraso': self.rng.randint(4, 19), 'moroso': self.rng.randint(28, 70),
                 'nuevo': self.rng.randint(-2, 6)}.get(beh, 0)
        when = self.next_business(max(rec.due_date + timedelta(days=delay), rec.issue_date + timedelta(days=2)))
        if beh in ('retraso', 'moroso') and self.rng.random() < 0.12:
            first = (rec.amount * Decimal('0.6')).quantize(Decimal('1000'))
            self.client_pay[when].append((c['key'], rec.pk, first))
            self.client_pay[self.next_business(when + timedelta(days=self.rng.randint(12, 22)))].append((c['key'], rec.pk, rec.amount - first))
        else:
            self.client_pay[when].append((c['key'], rec.pk, rec.amount))

    def sales_of_day(self, day):
        self.clock.at(day, 10, 15)
        keys = sorted(k for k in self.week_sales if k[0] == day)
        for key in keys:
            wanted = self.week_sales.pop(key)
            c = self.clients[key[1]]
            if 'party' not in c or (c['key'] in self.suspended and day >= self.suspended[c['key']]):
                continue
            discount = None
            month = day.replace(day=1)
            if c['id_type'] == '31' and c['context'] == 'company' and self.discounts_month[month] < 4 and self.rng.random() < 0.07 and day < self.b(3):
                discount = (self.rng.choice([3, 4, 5, 6, 8]), self.rng.choice(B.DISCOUNT_REASONS))
                self.discounts_month[month] += 1
            self.sell(key[1], wanted, day, discount=discount)

    def collections_of_day(self, day):
        """Cada cliente transfiere lo previsto para hoy junto con lo de los días siguientes de su corrida de pagos
        (las empresas pagan por corridas de unos diez días; las personas, lo de tres días)."""
        today = self.client_pay.pop(day, [])
        if not today:
            return
        by_client = defaultdict(list)
        for key, rec, amount in today:
            by_client[key].append((rec, amount))
        for key in sorted(by_client):
            for ahead in range(1, 11 if self.clients[key]['id_type'] == '31' else 4):
                later = self.client_pay.get(day + timedelta(days=ahead), [])
                mine = [x for x in later if x[0] == key]
                for x in mine:
                    later.remove(x)
                    by_client[key].append((x[1], x[2]))
            c, rows = self.clients[key], by_client[key]
            total = sum((a for _, a in rows), ZERO)
            numbers = list(Receivable.objects.filter(pk__in=[r for r, _ in rows]).order_by('number').values_list('number', flat=True))
            name, profile = plain(c['name']), c['payer']
            if profile == 'mention' and self.rng.random() < 0.85:
                text, holder, holder_id = 'PAGO ' + ' '.join(f'FE {n}' for n in numbers[:4]) + f' {name[:28]}', name, ''
            elif profile == 'third':
                text, holder, holder_id = f'TRANSFERENCIA DE {c["third"][0]}', c['third'][0], c['third'][1]
            elif profile == 'bare':
                text, holder, holder_id = 'TRANSFERENCIA RECIBIDA', '', ''
            else:
                text, holder, holder_id = 'TRANSFERENCIA RECIBIDA BANCA VIRTUAL', name, c['ident']
            self.bank(day, total, text, '', origin=c['account'], holder=holder, holder_id=holder_id,
                      meta={'kind': 'income', 'client': key, 'recs': rows})

    # ----------------------------------------------------------------------- banco
    def bank(self, day, amount, description, reference='', origin='', holder='', holder_id='', meta=None):
        self.bank_seq += 1
        self.bank_rows.append({'n': self.bank_seq, 'date': day, 'amount': Decimal(amount), 'description': description,
                               'reference': reference or f'TRX{self.bank_seq:07d}', 'origin': origin, 'holder': holder, 'holder_id': holder_id,
                               'meta': meta or {'kind': 'expense'}})

    def statement_csv(self, rows):
        out = ['Fecha;Descripción;Referencia;Cuenta origen;Titular;NIT;Valor;Saldo']
        for r in rows:
            self.balance += r['amount']
            out.append(';'.join([f'{r["date"]:%d/%m/%Y}', r['description'], r['reference'], r['origin'], r['holder'], r['holder_id'],
                                 f'{r["amount"]:.2f}', f'{self.balance:.2f}']))
        return ('\n'.join(out) + '\n').encode('utf-8')

    def fixed_expenses(self, day):
        if day.day == 5:
            self.bank(day, Decimal('-6850000'), 'PAGO ARRIENDO BODEGA', meta={'kind': 'expense'})
            self.bank(day, Decimal('-1730000') - self.rng.randint(0, 260) * 1000, 'PAGO SERVICIOS PUBLICOS', meta={'kind': 'expense'})
        if day.day in (15, 28):
            self.bank(day, Decimal('-9640000'), 'PAGO NOMINA QUINCENAL', meta={'kind': 'expense'})
        if day.day == 1:
            self.bank(day, Decimal('-48900'), 'COMISION MANEJO DE CUENTA', meta={'kind': 'expense'})
        if day.weekday() == 4 and self.cash_box > 0:
            self.bank(day, self.cash_box, 'CONSIGNACION EFECTIVO CAJA GENERAL', meta={'kind': 'cash_deposit'})
            self.cash_box = ZERO

    def import_statement(self, day, name):
        """Importa el extracto con los movimientos anteriores a `day` y deja identificado lo que contabilidad reconoce."""
        rows = sorted((r for r in self.bank_rows if r['date'] < day), key=lambda r: (r['date'], r['n']))
        if not rows:
            return
        self.bank_rows = [r for r in self.bank_rows if r['date'] >= day]
        self.clock.at(day, 8, 20)
        raw = self.statement_csv(rows)
        try:
            imp = C.preview_statement(self.fin, self.account, name, raw, tok())
            self.step(60, 200)
            C.confirm_statement(self.fin, imp.pk, tok())
        except ValidationError as e:
            self.anomaly('importación de extracto', e)
            return
        self.last_statement = imp.pk
        txns = list(BankTransaction.objects.filter(statement=imp).order_by('pk'))
        if len(txns) != len(rows):
            self.anomalies.append(f'{day:%d/%m/%Y} · el extracto {name} creó {len(txns)} movimientos de {len(rows)}')
            return
        proposals = {}
        if self.in_agent_period(day):
            self.clock.tick(120)
            self.run_agent('conciliacion')
            for a in AgentAction.objects.filter(agent='conciliacion', status='proposed'):
                proposals[a.payload.get('txn_id')] = a
        for txn, row in zip(txns, rows):
            try:
                self.identify(txn, row['meta'], proposals.get(txn.pk))
            except ValidationError as e:
                self.anomaly(f'movimiento bancario {txn.pk}', e)
        self.counters['extractos'] += 1

    def identify(self, txn, meta, proposal=None):
        kind = meta['kind']
        if kind == 'income':
            c = self.clients[meta['client']]
            party = c['party']
            self.step(30, 120)
            if (proposal is not None and proposal.kind == 'apply_bank' and proposal.payload.get('party_id') == party.pk
                    and party.pk not in self.hold_parties):
                done = self.decide(self.fin, proposal)
                if done is not None and done.status == 'executed':
                    return
            self.apply_income(txn, party, meta['recs'], 'tesorería' if c['payer'] == 'third' else '')
        elif kind == 'counter':
            self.step(20, 60)
            if proposal is not None and proposal.kind == 'link_counter' and proposal.payload.get('collection_ids') == meta['collections']:
                done = self.decide(self.fin, proposal)
                if done is not None and done.status == 'executed':
                    return
            C.link_counter(self.fin, txn.pk, meta['collections'], tok())
        elif kind == 'cash_deposit':
            self.step(20, 60)
            C.mark_other(self.fin, txn.pk, 'Consignación del efectivo de caja: los recaudos ya están registrados en las ventas de contado', tok())
        elif kind == 'supplier_payment':
            self.step(20, 60)
            P.reconcile_payment(self.fin, meta['payment'], txn.pk, tok())
        # 'expense' queda como salida que no es cartera; 'case' queda por identificar para la demostración.

    def apply_income(self, txn, party, planned, relation=''):
        ids = [r for r, _ in planned]
        balances = {r.pk: r.balance for r in with_balance(Receivable.objects.filter(pk__in=ids))}
        left, allocations = txn.amount, {}
        for rid, amount in planned:
            take = min(amount, balances.get(rid, ZERO) - allocations.get(rid, ZERO), left)
            if take > 0:
                allocations[rid] = allocations.get(rid, ZERO) + take
                left -= take
        if left > 0:
            for rec in C.open_receivables(party):
                if left <= 0:
                    break
                if rec.pk in self.hold:
                    continue
                take = min(left, rec.balance - allocations.get(rec.pk, ZERO))
                if take > 0:
                    allocations[rec.pk] = allocations.get(rec.pk, ZERO) + take
                    left -= take
        if not allocations:
            self.anomalies.append(f'{self.clock.today:%d/%m/%Y} · el ingreso {txn.pk} de {party.name} no encontró cuotas abiertas')
            return
        C.apply_transaction(self.fin, txn.pk, party.pk, sorted(allocations.items()), tok(), remember_payer=True, relation=relation)
        self.flush()
        self.counters['ingresos aplicados'] += 1

    # --------------------------------------------------------------------- agentes
    def run_agent(self, key):
        if not self.agents_ok:
            return None
        agent = REGISTRY[key]
        now = self.clock.now
        period = f'{key}:{now:%Y%m%d}' if agent.schedule.startswith('daily') else runner.period_key(agent, now)
        started = time.monotonic()
        try:
            run = runner.run_agent(key, 'schedule', now=now, force_period=period)
        except Exception as e:   # un agente en mal estado no detiene la simulación
            self.anomaly(f'agente {key}', e)
            return None
        finally:
            self.timing[f'agente {key}'] += time.monotonic() - started
        if run is not None and run.status != 'succeeded':
            self.anomalies.append(f'{now:%d/%m/%Y} · el agente {key} terminó con error: {run.error}')
        self.counters['turnos de agentes'] += 1
        self.clock.tick(15)
        return run

    def decide(self, user, action, approve=True, note=''):
        try:
            done = runner.decide(user, action.pk, approve, note)
        except ValidationError as e:
            self.anomaly(f'decisión sobre «{action.title[:60]}»', e)
            return None
        except Exception as e:
            self.anomaly(f'decisión sobre «{action.title[:60]}»', e)
            return None
        self.flush()
        if done.status == 'executed':
            self.counters['propuestas aprobadas'] += 1
        return done

    def proposals(self, key, kind=None):
        qs = AgentAction.objects.filter(agent=key, status='proposed').order_by('pk')
        return list(qs.filter(kind=kind) if kind else qs)

    def morning_agents(self, day):
        self.clock.at(day, 6, 0)
        self.run_agent('resumen')
        self.clock.at(day, 6, 30)
        self.run_agent('vencimientos')
        if day.weekday() == 0:
            # El pronóstico de la semana queda registrado el lunes, antes de conocerla; el resto de días este turno no cambia decisiones.
            self.clock.at(day, 6, 45)
            self.run_agent('reposicion')
        self.clock.at(day, 7, 0)
        self.run_agent('pronto_pago')
        self.clock.at(day, 8, 0)
        self.run_agent('cobranza')

    # --------------------------------------------------------------------- compras
    def blocked(self, item, day):
        """Referencias que el proveedor no está despachando (entrega retrasada en curso) o reservadas para un caso."""
        if item.code in self.case.get('no_order', ()):
            return True
        return item.code == 'MNT-001' and self.faltante_blocked(day)

    def order(self, supplier_key, rows, note='', arrive=None, source='manual', extra=None):
        """Crea el pedido, registra su envío simulado y agenda la entrega. rows = [(item, cantidad)]."""
        data, supplier, day = self.supplier_data[supplier_key], self.suppliers[supplier_key], self.clock.today
        lead = max(2, data['lead'][0] + self.rng.randint(-data['lead'][1], data['lead'][1]))
        if arrive is None:
            arrive = self.next_business(day + timedelta(days=lead))
        lines = [{'product': it.product, 'quantity': q, 'unit_cost': self.cost_of(it, day), 'reason': note or 'Disponible en el mínimo de la referencia'}
                 for it, q in rows]
        try:
            self.step()
            order = compras.create_order(self.inv, supplier, lines, note, tok(), source=source, expected_on=day + timedelta(days=data['lead'][0]))
            self.step(30, 90)
            compras.mark_sent(self.inv, order.pk, tok())
        except ValidationError as e:
            self.anomaly(f'pedido a {supplier.name}', e)
            return None
        self.schedule_delivery(order, supplier_key, rows, arrive, extra)
        return order

    def schedule_delivery(self, order, supplier_key, rows, arrive, extra=None):
        for it, q in rows:
            self.incoming[it.product.pk] += q
        if arrive:
            self.deliveries[arrive].append({'order': order.pk, 'number': order.number, 'supplier': supplier_key, 'rows': rows,
                                            'ordered_on': self.clock.today, **(extra or {})})
        self.counters['pedidos'] += 1

    def delayed_arrival(self, item, day, arrive):
        """Entrega retrasada: el proveedor incumple y la referencia queda sin existencias dos o tres semanas."""
        pending = self.delayed.get(item.code)
        if not pending or self.frac(day) < pending[0] or day > self.D - timedelta(days=75):
            return arrive
        pending.pop(0)
        rate = max(self.weekly_mean(item, day) / 7.0, 0.1)
        cover = self.avail[item.product.pk] / rate
        return self.next_business(day + timedelta(days=int(cover) + self.rng.randint(13, 20)))

    def review_purchases(self, day):
        self.clock.at(day, 8, 40)
        agent_mode = self.in_agent_period(day) and self.agents_ok
        if agent_mode and day.weekday() == 0:
            self.approve_orders(day)
        for key in self.suppliers:
            lax = agent_mode and not (key == 'quimicos' and self.block_faltante)
            rows = []
            for it in self.items:
                if it.supplier != key or it.product is None or it.pattern == 'nueva' or self.blocked(it, day):
                    continue
                if it.pattern == 'sin_rotacion' and day > self.D - timedelta(days=200):
                    continue
                level = self.avail[it.product.pk] + self.incoming[it.product.pk]
                if level <= (it.alert if lax else it.minimum):
                    rows.append((it, it.quantity))
            # Una referencia cuya entrega se va a retrasar viaja en su propio pedido, para no demorar las demás.
            for it, q in [r for r in rows if r[0].pattern == 'agotados']:
                normal = self.next_business(day + timedelta(days=self.supplier_data[key]['lead'][0]))
                late = self.delayed_arrival(it, day, normal)
                if late != normal:
                    rows.remove((it, q))
                    self.order(key, [(it, q)], '', arrive=late, extra={'note': 'El proveedor entregó con retraso', 'partial': False})
            if rows:
                self.order(key, rows, 'Pedido de respaldo: disponible por debajo del mínimo' if agent_mode else '')

    def approve_orders(self, day):
        """Inventario revisa las propuestas del agente de reposición y prepara los pedidos."""
        for action in self.proposals('reposicion', 'purchase_order'):
            supplier_key = next((k for k, s in self.suppliers.items() if s.pk == action.payload.get('supplier_id')), None)
            if supplier_key is None or (supplier_key == 'quimicos' and self.faltante_blocked(day)):
                continue
            codes = set(Product.objects.filter(pk__in=action.payload.get('products', [])).values_list('code', flat=True))
            if codes & set(self.case.get('no_order', ())):
                continue
            before = set(PurchaseOrder.objects.filter(source='agent').values_list('pk', flat=True))
            self.step(60, 240)
            done = self.decide(self.inv, action)
            if done is None or done.status != 'executed':
                continue
            order = PurchaseOrder.objects.filter(source='agent').exclude(pk__in=before).first()
            if order is None:
                continue
            rows = [(self.by_code[l.product.code], l.quantity) for l in order.lines.select_related('product')]
            try:
                self.step(30, 90)
                compras.mark_sent(self.inv, order.pk, tok())
            except ValidationError as e:
                self.anomaly(f'envío del pedido {order.number}', e)
            data = self.supplier_data[supplier_key]
            lead = max(2, data['lead'][0] + self.rng.randint(-data['lead'][1], data['lead'][1]))
            self.schedule_delivery(order, supplier_key, rows, self.next_business(day + timedelta(days=lead)))

    def invoice_data(self, d, day, number=None, issue=None):
        """Datos de la factura del proveedor para un pedido entregado (los mismos sirven para el PDF o el XML)."""
        data = self.supplier_data[d['supplier']]
        self.invoice_seq[d['supplier']] += 1
        number = number or f'{data["prefix"]}-{20000 + self.invoice_seq[d["supplier"]] * 7}'
        issue = issue or day
        lines = []
        for it, q in d['rows']:
            expiry = d.get('expiry', {}).get(it.code) or day + timedelta(days=it.shelf * 30 - self.rng.randint(10, 70))
            if (it.pattern == 'bajo' or it.extra.get('low')) and 'expiry' not in d and self.rng.random() < 0.14 and day < self.D - timedelta(days=75):
                expiry = day + timedelta(days=self.rng.randint(34, 48))   # lote de corta fecha: parte no alcanzará a salir
            price = self.cost_of(it, d['ordered_on'])
            lines.append({'item': it, 'supplier_code': f'{data["prefix"]}{it.code[-3:]}{it.index + 11}', 'description': it.name, 'quantity': q, 'unit_price': price,
                          'discount': (price * q * Decimal(str(d.get('line_discount', 0)))).quantize(Decimal('1')), 'tax_rate': TAX,
                          'expected_lot': self.new_lot(it, day), 'expected_expiry': expiry})
        return {'supplier_name': data['name'], 'supplier_nit': data['nit'], 'number': number, 'cufe': 'SIM' + f'{self.rng.getrandbits(244):061x}',
                'issue_date': issue, 'due_date': issue + timedelta(days=data['payment_days']), 'order_number': d['number'], 'ordered_on': d['ordered_on'],
                'payment_days': data['payment_days'], 'discount_base': data['base'], 'discount_tiers': [{'days': n, 'percent': p} for n, p in data['tiers']],
                'lines': lines}

    def register_invoice(self, d, day, data=None):
        data = data or self.invoice_data(d, day)
        supplier, kind = self.suppliers[d['supplier']], self.supplier_data[d['supplier']]['doc']
        header = {'supplier': supplier, 'number': data['number'], 'issue_date': data['issue_date'], 'due_date': data['due_date'],
                  'ordered_on': data['ordered_on'], 'order_number': data['order_number'], 'cufe': data['cufe'], 'global_discount': Decimal(0),
                  'retention': Decimal(0), 'notes': d.get('note', '')}
        lines = [{'product': l['item'].product, **{k: l[k] for k in ('supplier_code', 'description', 'quantity', 'unit_price', 'discount', 'tax_rate',
                                                                     'expected_lot', 'expected_expiry')}} for l in data['lines']]
        uploads = []
        if kind == 'xml':
            uploads = [(f'factura_{data["number"]}.xml', demo.invoice_xml(data))]
        elif kind == 'pdf':
            uploads = [(f'factura_{data["number"]}.pdf', demo.invoice_pdf(data))]
            header['document_terms'] = {'payment_days': data['payment_days'], 'base': data['discount_base'],
                                        'tiers': [[t['days'], str(t['percent'])] for t in data['discount_tiers']], 'printed_text': demo.terms_text(demo.normalize(data))}
        self.step()
        invoice = S.create_invoice(self.inv, header, lines, uploads, tok(), source=kind)
        self.step(60, 300)
        S.validate_invoice(self.fin, invoice.pk, tok())
        self.counters['facturas de proveedor'] += 1
        return invoice, data

    def receive(self, invoice, rows, day, note='', scanner=False):
        """rows = [(línea, item, cantidad, lote, vencimiento)]."""
        self.step(60, 300)
        if scanner:
            invoice = Invoice.objects.select_related('supplier').get(pk=invoice.pk)
            location = B.LOCATION_CONTROLLED if all(it.controlled for _, it, _, _, _ in rows) else B.LOCATION
            session = recepcion.open_session(self.rec, invoice)
            for line, it, q, lot, expiry in rows:
                boxes, rest = divmod(q, it.pack)
                if boxes:
                    result = recepcion.scan(self.rec, session.pk, box_code(it.gtin, expiry, lot), boxes)
                    if not result.get('ok'):
                        raise ValidationError(result.get('message', 'No se pudo leer la caja.'))
                if rest:
                    recepcion.manual(self.rec, session.pk, line.pk, lot, expiry, rest)
                self.clock.tick(25)
            recepcion.confirm(self.rec, session.pk, day, location, note, tok())
        else:
            S.receive_invoice(self.inv, invoice.pk, [{'line': line.pk, 'quantity': q, 'lot': lot, 'expiry': expiry, 'location': self.location(it)}
                                                     for line, it, q, lot, expiry in rows], day, note, tok())
        for _, it, q, _, expiry in rows:
            self.incoming[it.product.pk] -= q
            if expiry >= day:
                self.avail[it.product.pk] += q
        self.counters['recepciones'] += 1

    def deliver(self, d, day):
        """Llega el pedido: factura registrada y validada, y entrada por lotes (a veces parcial o con otro lote)."""
        try:
            invoice, data = self.register_invoice(d, day)
        except ValidationError as e:
            self.anomaly(f'factura del pedido {d["number"]}', e)
            return None
        lines = list(invoice.lines.order_by('pk'))
        rows, later, notes = [], [], []
        partial = d.get('partial') if 'partial' in d else (len(lines) > 0 and self.rng.random() < 0.07)
        for i, (line, l) in enumerate(zip(lines, data['lines'])):
            it, q, lot = l['item'], l['quantity'], l['expected_lot']
            if d.get('other_lot') or ('other_lot' not in d and self.rng.random() < 0.05):
                lot = self.new_lot(it, day)
                notes.append(f'{it.name}: llegó el lote {lot} en lugar del anunciado')
            if partial and i == len(lines) - 1 and q >= 2 * it.pack:
                now = round_up(q * 0.5, it.pack)
                later.append((line, it, q - now, lot, l['expected_expiry']))
                q = now
                notes.append(f'{it.name}: llegó una parte; el proveedor completa en los próximos días')
            rows.append((line, it, q, lot, l['expected_expiry']))
        if d.get('note'):
            notes.append(d['note'])
        if d.get('receive', True):
            try:
                self.receive(invoice, rows, day, '. '.join(notes)[:300], scanner=self.in_agent_period(day) and day > self.agent_start + timedelta(days=21))
            except ValidationError as e:
                self.anomaly(f'recepción de {invoice.number}', e)
                return invoice
            if later:
                when = self.next_business(day + timedelta(days=self.rng.randint(3, 6)))
                if d.get('hold_rest'):
                    self.case['parcial'] = {'invoice': invoice.pk, 'line': later[0][0].pk, 'order': d['order']}
                else:
                    self.receipts_due[when].append((invoice.pk, later))
        if not d.get('protected'):
            self.plan_payment(invoice, d['supplier'], day)
        else:
            self.protected_invoices.add(invoice.pk)
        if not d.get('receive', True):
            self.case['por_recibir'] = {'invoice': invoice.pk, 'order': d['order'], 'data': data}
        if d.get('after'):
            d['after'](invoice)
        if self.rng.random() < 0.1 and not d.get('protected'):
            self.at(self.next_business(day + timedelta(days=2)), self.credit_note, invoice.pk, d['supplier'])
        return invoice

    def late_receipts(self, day):
        for invoice_pk, rows in self.receipts_due.pop(day, []):
            try:
                self.receive(Invoice.objects.get(pk=invoice_pk), rows, day, 'Saldo de la recepción parcial')
            except ValidationError as e:
                self.anomaly(f'saldo de recepción de la factura {invoice_pk}', e)

    # ------------------------------------------------------------ cuentas por pagar
    def plan_payment(self, invoice, supplier_key, day):
        data = self.supplier_data[supplier_key]
        r = self.rng.random()
        if data['tiers'] and r < 0.62:
            when, plan = invoice.issue_date + timedelta(days=data['tiers'][0][0] - self.rng.randint(1, 2)), 'discount'
        elif r < 0.9:
            when, plan = invoice.due_date - timedelta(days=self.rng.randint(0, 3)), 'due'
        else:
            when, plan = invoice.due_date + timedelta(days=self.rng.randint(3, 12)), 'late'
        self.pay_plan[self.next_business(max(when, day + timedelta(days=1)))].append((invoice.pk, plan))

    def credit_note(self, invoice_pk, supplier_key):
        inv = Invoice.objects.get(pk=invoice_pk)
        if inv.balance <= 0 or inv.allocations.exists():
            return
        self.counters['nc'] += 1
        amount = (inv.total * Decimal(self.rng.choice(['0.02', '0.03', '0.05']))).quantize(Decimal('100'))
        try:
            if self.counters['nc'] % 4 == 0:
                P.add_adjustment(self.fin, inv.pk, 'charge', Decimal(self.rng.choice([65000, 85000, 120000])), f'Flete adicional · guía {30000 + self.counters["nc"]}', tok())
            else:
                P.add_credit_note(self.fin, self.suppliers[supplier_key], f'NC-{self.supplier_data[supplier_key]["prefix"]}-{700 + self.counters["nc"]}',
                                  self.clock.today, amount, self.rng.choice(['Unidades con empaque averiado', 'Diferencia de precio frente al pedido',
                                                                              'Faltante reconocido por el proveedor']), inv, amount, tok())
        except ValidationError as e:
            self.anomaly(f'nota crédito de {inv.number}', e)

    def pay(self, supplier, invoice_pks, day, note=''):
        """Pago confirmado de una o varias facturas del mismo proveedor, con el descuento que corresponde a la fecha."""
        busy = set(SupplierPayment.objects.filter(status='scheduled', allocations__invoice_id__in=invoice_pks).values_list('allocations__invoice_id', flat=True))
        rows = []
        for inv in Invoice.objects.filter(pk__in=invoice_pks).order_by('pk'):
            if inv.pk in busy or inv.pk in self.protected_invoices or inv.balance <= 0 or inv.review != 'validated':
                continue
            s = P.settlement(inv, day)
            rows.append({'invoice': inv, 'amount': s['cash'], 'discount': s['discount']})
        if not rows:
            return None
        self.counters['egreso'] += 1
        reference = f'EGR-{6000 + self.counters["egreso"]}'
        try:
            self.step()
            payment = P.register_payment(self.fin, supplier, rows, day, 'transfer', reference, [], note, tok())
        except ValidationError as e:
            self.anomaly(f'pago a {supplier.name}', e)
            return None
        self.bank(day, -payment.amount, f'PAGO PROVEEDORES {plain(supplier.name)[:34]}', reference, meta={'kind': 'supplier_payment', 'payment': payment.pk})
        self.counters['pagos a proveedores'] += 1
        return payment

    def payments_of_day(self, day):
        self.clock.at(day, 15, 20)
        agent_mode = self.in_agent_period(day) and self.agents_ok
        plans = dict(self.pay_plan.pop(day, []))
        if agent_mode:
            wanted = {pk for d, items in self.pay_plan.items() for pk, plan in items if plan == 'discount'} | {pk for pk, plan in plans.items() if plan == 'discount'}
            for action in self.proposals('pronto_pago', 'schedule_payment'):
                ids = set(action.payload.get('invoices', []))
                if action.payload.get('reason') != 'discount' or not ids <= wanted or ids & self.protected_invoices or day >= self.b(1):
                    continue
                done = self.decide(self.fin, action)
                if done is not None and done.status == 'executed':
                    p = SupplierPayment.objects.get(pk=done.result['supplier_payment'])
                    self.scheduled[self.next_business(p.scheduled_on)].append(p.pk)
            for pk in self.scheduled.pop(day, []):
                self.confirm_scheduled(pk, day)
        by_supplier = defaultdict(list)
        for inv in Invoice.objects.filter(pk__in=list(plans)).select_related('supplier').order_by('pk'):
            by_supplier[inv.supplier].append(inv.pk)
        for supplier, pks in by_supplier.items():
            self.pay(supplier, pks, day)

    def confirm_scheduled(self, pk, day):
        p = SupplierPayment.objects.select_related('supplier').get(pk=pk)
        if p.status != 'scheduled':
            return
        rows = []
        for a in p.allocations.select_related('invoice'):
            inv = Invoice.objects.get(pk=a.invoice_id)
            if inv.balance > 0:
                s = P.settlement(inv, day)
                rows.append({'invoice': inv, 'amount': s['cash'], 'discount': s['discount']})
        if not rows:
            return
        self.counters['egreso'] += 1
        reference = f'EGR-{6000 + self.counters["egreso"]}'
        try:
            self.step()
            p = P.confirm_payment(self.fin, p.pk, rows, day, 'transfer', reference, [], '', tok())
        except ValidationError as e:
            self.anomaly(f'confirmación del pago programado {pk}', e)
            return
        self.bank(day, -p.amount, f'PAGO PROVEEDORES {plain(p.supplier.name)[:34]}', reference, meta={'kind': 'supplier_payment', 'payment': p.pk})
        self.counters['pagos a proveedores'] += 1

    # ------------------------------------------------------------------ solicitudes
    def requests_of_day(self, day):
        self.clock.at(day, 9, 10)
        agent_mode = self.in_agent_period(day) and self.agents_ok
        for res_pk, action in self.to_dispatch.pop(day, []):
            try:
                self.step(30, 120)
                S.finish_reservation(self.inv, res_pk, action, tok())
                if action == 'release':
                    r = Reservation.objects.get(pk=res_pk)
                    self.avail[r.batch.product_id] += r.quantity
            except ValidationError as e:
                try:
                    S.finish_reservation(self.inv, res_pk, 'release', tok())
                except ValidationError:
                    self.anomaly('entrega de reserva', e)
        created = []
        for requester, item, qty in self.requests_due.pop(day, []):
            if item.product is None:
                continue
            purpose = self.rng.choice(B.REQUEST_PURPOSES[requester.username])
            try:
                self.step(60, 400)
                r = S.request_stock(requester, item.product, qty, purpose, tok(), needed_by=day + timedelta(days=self.rng.randint(2, 6)))
            except ValidationError as e:
                self.anomaly('solicitud', e)
                continue
            self.counters['solicitudes'] += 1
            if self.rng.random() < 0.04:
                self.step(60, 300)
                S.resolve_request(requester, r.pk, 'cancel', '', tok())
            else:
                created.append((r, item))
        if not created:
            return
        proposals = {}
        if agent_mode:
            self.clock.tick(300)
            self.run_agent('solicitudes')
            proposals = {a.payload.get('request_id'): a for a in self.proposals('solicitudes', 'reserve_request')}
        for r, item in created:
            self.step(120, 600)
            try:
                action = proposals.get(str(r.pk))
                batch = S.suggested_batch(item.product, r.quantity)
                if batch is None:
                    S.resolve_request(self.inv, r.pk, 'reject', 'Sin existencias suficientes en un lote vigente', tok())
                    self.counters['solicitudes rechazadas'] += 1
                    continue
                if action is not None:
                    done = self.decide(self.inv, action)
                    if done is None or done.status != 'executed':
                        S.approve(self.inv, r.pk, batch.pk, tok())
                else:
                    S.approve(self.inv, r.pk, batch.pk, tok())
                self.avail[item.product.pk] -= r.quantity
                res = Reservation.objects.get(request=r)
                when = self.next_business(day + timedelta(days=self.rng.randint(1, 2)))
                self.to_dispatch[when].append((res.pk, 'release' if self.rng.random() < 0.03 else 'dispatch'))
            except ValidationError as e:
                self.anomaly('aprobación de solicitud', e)

    # ------------------------------------------------------------------------ bajas
    def writeoffs(self, day):
        self.clock.at(day, 7, 30)
        agent_mode = self.in_agent_period(day) and self.agents_ok
        if agent_mode:
            for action in self.proposals('vencimientos', 'writeoff_expired'):
                if action.payload.get('batch_id') in self.protected_batches:
                    continue
                self.step(30, 120)
                if self.decide(self.inv, action) is not None:
                    self.counters['bajas por vencimiento'] += 1
            return
        for b in Batch.objects.filter(physical__gt=F('reserved'), expiry__lt=day).exclude(pk__in=self.protected_batches).order_by('pk'):
            try:
                self.step(30, 120)
                S.adjust_out(self.inv, b.pk, b.physical - b.reserved, 'writeoff', 'expiry', 'Baja por vencimiento del lote', tok())
                self.counters['bajas por vencimiento'] += 1
            except ValidationError as e:
                self.anomaly('baja por vencimiento', e)

    def damage(self, day):
        candidates = [it for it in self.items if it.product is not None and self.avail[it.product.pk] > 3 * it.pack and it.code != 'MNT-001']
        if not candidates:
            return
        item = self.rng.choice(candidates)
        batch = Batch.objects.filter(product=item.product, expiry__gte=day, physical__gt=F('reserved') + 3).exclude(pk__in=self.protected_batches).order_by('expiry').first()
        if batch is None:
            return
        reason, note = self.rng.choice(B.DAMAGE_NOTES)
        qty = self.rng.randint(1, 3)
        try:
            S.adjust_out(self.inv, batch.pk, qty, 'writeoff', reason, note, tok())
            self.avail[item.product.pk] -= qty
            self.counters['bajas por daño'] += 1
        except ValidationError as e:
            self.anomaly('baja por daño', e)

    # ------------------------------------------------------------------- cobranza
    def reminders(self, day):
        """Contabilidad aprueba los recordatorios de cobro; el envío queda simulado y algunos clientes adelantan el pago."""
        self.clock.at(day, 8, 10)
        for action in self.proposals('cobranza', 'collection_reminder'):
            self.step(30, 120)
            done = self.decide(self.fin, action)
            if done is None or done.status != 'executed':
                continue
            self.counters['recordatorios de cobro'] += 1
            party_id = action.payload.get('party_id')
            c = next((x for x in self.clients.values() if x.get('party') and x['party'].pk == party_id), None)
            if c is None:
                continue
            chance = {'retraso': 0.5, 'moroso': 0.28}.get(c['behaviour'], 0.6)
            ids = set(action.payload.get('receivables', []))
            for when in sorted(d for d in self.client_pay if d > day + timedelta(days=9)):
                for entry in [e for e in self.client_pay[when] if e[0] == c['key'] and e[1] in ids]:
                    if self.rng.random() < chance:
                        self.client_pay[when].remove(entry)
                        self.client_pay[self.next_business(day + timedelta(days=self.rng.randint(2, 8)))].append(entry)

    # ------------------------------------------------------------------ día a día
    def day(self, day):
        weekday = day.weekday()
        if weekday == 6:
            self.collections_of_day(day)       # una transferencia puede entrar en domingo
            return
        agent_mode = self.in_agent_period(day) and self.agents_ok
        self.clock.at(day, 5, 50)
        self.refresh_stock()
        if weekday == 0:
            self.plan_week(day)
        if agent_mode:
            self.morning_agents(day)
        if weekday == 0 or agent_mode:
            self.timed('bajas', self.writeoffs, day)
        if agent_mode and weekday in (1, 4) and day < self.D - timedelta(days=11):
            self.timed('cobranza', self.reminders, day)
        if day == self.b(1):
            self.before_last_import(day)
        if (agent_mode and weekday == 0) or (not agent_mode and day.day == 1) or (not agent_mode and day.day == 2 and weekday == 0) or day == self.b(1):
            self.timed('banco', self.import_statement, day, f'extracto_{day:%Y-%m-%d}.csv')
        if weekday in (0, 3):
            self.timed('pedidos', self.review_purchases, day)
        self.clock.at(day, 9, 0)
        for d in self.deliveries.pop(day, []):
            self.timed('recepciones', self.deliver, d, day)
        self.late_receipts(day)
        self.timed('solicitudes', self.requests_of_day, day)
        self.timed('ventas', self.sales_of_day, day)
        self.collections_of_day(day)
        self.timed('pagos', self.payments_of_day, day)
        self.fixed_expenses(day)
        if self.rng.random() < 0.025:
            self.clock.at(day, 16, 0)
            self.damage(day)
        self.clock.at(day, 16, 20)
        for fn, args in self.staged.pop(day, []):
            try:
                fn(*args)
            except ValidationError as e:
                self.anomaly(f'caso preparado {fn.__name__}', e)
            self.clock.tick(90)

    def run(self):
        post_save.disconnect(agent_signals.request_created, sender='inventory.SupplyRequest')
        post_save.disconnect(agent_signals.bank_movement_created, sender='cartera.BankTransaction')
        # El modelo de pagos no aprende en línea durante la simulación: al cerrar se reentrena en orden cronológico con
        # todas las identificaciones (la misma evaluación secuencial, sin recalcularla en cada pago).
        post_save.disconnect(learning_signals.application_saved, sender='cartera.PaymentApplication')
        try:
            self.setup()
            self.stage_cases()
            day, month = self.S, None
            started = time.monotonic()
            while day < self.D:
                if day.replace(day=1) != month:
                    month = day.replace(day=1)
                    self.out(f'  {month:%m/%Y} · {self.counters["ventas"]} ventas · {self.counters["facturas de proveedor"]} compras · '
                             f'{self.counters["ingresos aplicados"]} ingresos aplicados · {time.monotonic() - started:5.1f} s')
                if day == self.agent_start:
                    self.lean_minimums(day)
                elapsed = (day - self.S).days
                if elapsed == 365:
                    self.update_prices(day)
                if elapsed in (120, 240, 365, 480, 600):
                    self.update_policies(day)
                self.day(day)
                day += timedelta(days=1)
            self.closing()
        finally:
            post_save.connect(agent_signals.request_created, sender='inventory.SupplyRequest')
            post_save.connect(agent_signals.bank_movement_created, sender='cartera.BankTransaction')
            post_save.connect(learning_signals.application_saved, sender='cartera.PaymentApplication')

    def update_prices(self, day):
        """Segunda vigencia de precios: los proveedores suben 5 % y las listas se ajustan."""
        self.clock.at(day, 6, 10)
        for item in self.items:
            if item.product is None:
                continue
            for _, _, context, margin in B.PRICE_LISTS:
                C.save_price(self.fin, self.lists[context], item.product, self.price(item, margin, day), day, tok())

    def lean_minimums(self, day):
        """Al entrar en turno el agente de reposición, el mínimo del catálogo queda solo como nivel de alerta: lo que
        se consume mientras llega un pedido. El punto de pedido lo calcula el agente con el pronóstico."""
        self.clock.at(day, 6, 20)
        for item in self.items:
            lead = self.supplier_data[item.supplier]['lead'][0]
            mean = self.weekly_mean(item, day) if item.pattern in ('creciente', 'decreciente') else item.reference_mean()
            item.alert = max(int(math.ceil(mean * (lead + 2) / 7 * 0.8)), item.pack if item.pattern != 'intermitente' else item.minimum // 2, 1)
            if item.product is not None and item.pattern != 'nueva':
                Product.objects.filter(pk=item.product.pk).update(minimum=item.alert)
        S.event(self.inv, 'Mínimos del catálogo ajustados', 'catalogo', 'El punto de pedido lo calcula el agente de reposición; el mínimo queda como nivel de alerta')

    def update_policies(self, day):
        """La operación anterior sube a mano el mínimo de la referencia que viene creciendo."""
        for item in self.items:
            if item.pattern == 'creciente' and item.product is not None:
                item.set_policy(self.supplier_data[item.supplier]['lead'][0], self.weekly_mean(item, day) * 1.25)
                if self.in_agent_period(day):
                    continue
                Product.objects.filter(pk=item.product.pk).update(minimum=item.minimum)
                S.event(self.inv, 'Mínimo de la referencia actualizado', item.product.pk, f'{item.name}: mínimo {item.minimum}')


def generate(reference_date=None, seed=DEFAULT_SEED, months=DEFAULT_MONTHS, out=None):
    """Genera el escenario en la base actual (debe estar vacía). Devuelve (DemoState, {usuario: clave})."""
    from .models import DemoState
    from .huella import table_summary
    if User.objects.exists() or Product.objects.exists():
        raise ValidationError('La base ya tiene datos: el escenario se genera sobre una base recién creada.')
    reference_date = reference_date or timezone.localdate()
    started = time.monotonic()
    from . import documentos
    scenario = Scenario(reference_date, seed, months, out)
    with controlled(scenario.clock, seed):
        scenario.run()
    scenario.cases['documentos'] = documentos.write(scenario.documents)
    summary = table_summary()
    passwords = {}
    for user in User.objects.order_by('pk'):
        passwords[user.username] = 'Feria-' + secrets.token_urlsafe(9)
        user.set_password(passwords[user.username])
        user.save(update_fields=['password'])
    state = DemoState.objects.create(reference_date=reference_date, seed=seed, months=months, generated_at=timezone.now(),
                                     duration_seconds=round(time.monotonic() - started, 1), cases=scenario.cases,
                                     summary={**summary, 'conteos': dict(sorted(scenario.counters.items())), 'anomalias': scenario.anomalies[:40],
                                              'tiempos': {k: round(v, 1) for k, v in sorted(scenario.timing.items())}})
    return state, passwords


def fast_sqlite():
    """La base se construye desde cero: no hace falta sincronizar el disco en cada operación."""
    if connection.vendor == 'sqlite':
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA synchronous=OFF')
            cursor.execute('PRAGMA journal_mode=MEMORY')
            cursor.execute('PRAGMA temp_store=MEMORY')
            cursor.execute('PRAGMA cache_size=-262144')
