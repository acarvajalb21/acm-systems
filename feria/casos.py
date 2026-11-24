"""Casos preparados del escenario: se siembran durante la simulación para que existan en la fecha de referencia.

Cada caso nace de operaciones normales (un pedido, una venta, una solicitud) hechas en el día adecuado con los
servicios de la plataforma; nada se fuerza escribiendo estados. Al cerrar la simulación se ejecutan los agentes y
se arma `DemoState.cases` con los identificadores y las URL de cada caso.
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.urls import reverse
from agentes.models import AgentAction
from aprendizaje import pagos
from cartera import discounts
from cartera.models import BankTransaction, Receivable, with_balance
from inventory import payables as P, services as S
from inventory.models import Batch, Invoice, PurchaseOrder, Reservation, SupplierPayment, with_payable_balance
from . import datos_base as B

FALTANTE = 'MNT-001'
ZERO = Decimal('0.00')


def tok():
    return uuid.uuid4()


class Cases:
    # ----------------------------------------------------------------- siembra
    def stage_cases(self):
        D = self.D
        day = timedelta(days=1)
        self.case.update({'faltante_target': int(self.by_code[FALTANTE].mean * 0.8), 'abiertos': {}})
        for (code, index), units in sorted(self.spikes.items()):
            item = self.by_code[code]
            lead = self.supplier_data[item.supplier]['lead'][0]
            self.at(self.business(self.mondays[index] - (lead + 6) * day), self.project_order, item, units)
        self.at(self.b(24), self.new_client, 'remache')
        self.at(self.b(17), self.new_client, 'salgado')
        self.at(self.b(47), self.launch_product)
        self.at(self.b(12), self.new_reference)
        # Cartera vencida en tres tramos: la venta se hace el día que deja la cuota con esa mora en la fecha de referencia.
        self.suspended.update({'rueda': D - 100 * day, 'sabana': D - 60 * day})
        for key, slot, late in (('rueda', 'd90', 97), ('sabana', 'd45', 45), ('pulgarin', 'd15', 15)):
            self.at(self.business(D - (late + self.clients[key]['credit']) * day), self.held_sale, key, slot, 3)
        # Banco: una venta que se pagará citando la factura, dos ventas iguales a dos clientes y dos para el extracto nuevo.
        self.at(self.b(20), self.held_sale, 'piston', 'claro', 3)
        self.at(self.b(9), self.ambiguous_sales)
        self.at(self.b(18), self.held_sale, 'mirador', 'extracto_1', 2)
        self.at(self.b(12), self.held_sale, 'ospina', 'extracto_2', 2)
        # Compras: lotes de corta fecha, pronto pago, recepción parcial, factura por recibir y pedidos en tránsito.
        self.at(self.b(40), self.staged_order, 'vencido')
        self.at(self.b(16), self.staged_order, 'por_vencer')
        self.at(self.b(13), self.staged_order, 'pronto_pago')
        self.at(self.b(12), self.staged_order, 'parcial')
        self.at(self.b(8), self.staged_order, 'por_recibir')
        self.at(self.b(11), self.open_order, 'importadora')
        self.at(self.b(9), self.open_order, 'recubrimientos')
        self.at(self.b(5), self.open_order, 'adhesivos')
        self.at(self.b(3), self.open_order, 'cintas')
        # Solicitudes, ventas y descuentos de los últimos días.
        self.at(self.b(3), self.prior_delivery)
        self.at(self.b(2), self.request_to_reserve)
        self.at(self.b(1), self.last_day)

    def faltante_blocked(self, day):
        """Desde aquí no se pide más la referencia del caso «faltante próximo», para que llegue baja a la fecha."""
        if self.block_faltante or day < self.D - timedelta(days=28):
            return self.block_faltante
        item = self.by_code[FALTANTE]
        pid = item.product.pk
        projected = self.avail[pid] + self.incoming[pid] - self.weekly_mean(item, day) / 7.0 * (self.D - day).days
        if projected >= self.case['faltante_target'] or day >= self.D - timedelta(days=9):
            self.block_faltante = True
            self.case['faltante_floor'] = int(item.mean * 0.35)
        return self.block_faltante

    def basket(self, key, lines=3, exclude=()):
        """Canasta verosímil para un cliente con referencias que tienen existencias de sobra."""
        c = self.clients[key]
        cats = ['mantenimiento', 'adhesivos', 'sellantes', 'recubrimientos']
        pool = [it for it in self.items if it.product is not None and it.code != FALTANTE and it.code not in exclude
                and it.pattern in ('estable_alto', 'estable_medio', 'lluvias', 'creciente') and self.avail[it.product.pk] >= it.mean]
        pool.sort(key=lambda it: (-c['cats'][cats.index(it.category)], it.code))
        chosen = pool[:max(lines * 2, 4)]
        self.rng.shuffle(chosen)
        return {it.code: max(int(it.mean * self.rng.uniform(0.08, 0.2)), 2) for it in chosen[:lines]}

    def held_sale(self, key, slot, lines):
        """Venta a crédito cuya cuota queda abierta hasta la fecha de referencia."""
        sale = self.sell(key, self.basket(key, lines), hold=True)
        if sale is None or sale.status != 'issued':
            self.anomalies.append(f'No se pudo preparar la venta del caso {slot}')
            return
        rec = sale.receivable
        self.case[slot] = {'sale': sale.pk, 'rec': rec.pk, 'party': sale.party_id, 'total': sale.total, 'number': rec.number, 'client': key}

    def ambiguous_sales(self):
        wanted = {code: qty for code, qty in self.basket('edificios', 2).items()}
        for code in list(wanted):
            wanted[code] = min(wanted[code], self.avail[self.by_code[code].product.pk] // 2)
        a = self.sell('edificios', dict(wanted), hold=True)
        b = self.sell('metal', dict(wanted), hold=True)
        if a is None or b is None or a.total != b.total:
            self.anomalies.append('No se pudieron preparar las dos ventas de igual valor del caso ambiguo')
            return
        self.case['ambiguo'] = {'total': a.total, 'parties': [a.party_id, b.party_id], 'recs': [a.receivable.pk, b.receivable.pk]}

    def new_client(self, key):
        self.create_party(self.clients[key])

    def project_order(self, item, units):
        from .escenario import round_up
        if item.product is not None:
            self.order(item.supplier, [(item, round_up(units, item.pack))], 'Pedido por proyecto de un cliente')

    def launch_product(self):
        from .escenario import round_up
        item = next(it for it in self.items if it.pattern == 'lanzamiento')
        item.set_policy(self.supplier_data[item.supplier]['lead'][0])
        item.alert = item.minimum = max(item.minimum // 2, item.pack)
        self.create_product(item, self.clock.today)
        self.order(item.supplier, [(item, round_up(item.mean * 8, item.pack))], 'Primer pedido de la referencia nueva', arrive=self.b(31))

    def new_reference(self):
        item = next(it for it in self.items if it.pattern == 'nueva')
        item.minimum, item.quantity, item.alert = 2 * item.pack, 4 * item.pack, 2 * item.pack
        self.create_product(item, self.clock.today)

    def staged_order(self, kind):
        from .escenario import round_up
        D, code = self.D, self.by_code
        if kind == 'vencido':
            it = code['MNT-006']
            expiry = D - timedelta(days=2)
            self.order('quimicos', [(it, round_up(it.mean * 8, it.pack))], 'Lote de corta fecha ofrecido por el proveedor', arrive=self.b(35), extra={
                'partial': False, 'other_lot': False, 'expiry': {it.code: expiry}, 'line_discount': '0.25',
                'note': 'Lote de corta fecha aceptado con descuento del proveedor', 'after': lambda inv: self.remember_batch('vencido', it, expiry)})
        elif kind == 'por_vencer':
            it = code['SEL-003']
            expiry = D + timedelta(days=18)
            # Unas 6,6 semanas de consumo medio: tras las dos semanas que lleva en bodega quedan más unidades de las que salen en los 18 días
            # que faltan para vencer, aunque las últimas semanas hayan salido hasta un 40 % por encima de la media (con 5,6 el margen era del 20 %
            # y en algunas fechas de referencia el caso quedaba sin unidades en riesgo).
            self.order('sellantes', [(it, round_up(self.weekly_mean(it, D) * 6.6, it.pack))], 'Lote de corta fecha ofrecido por el proveedor', arrive=self.b(10), extra={
                'partial': False, 'other_lot': False, 'expiry': {it.code: expiry}, 'line_discount': '0.15',
                'note': 'Lote de corta fecha aceptado con descuento del proveedor', 'after': lambda inv: self.remember_batch('por_vencer', it, expiry)})
        elif kind == 'pronto_pago':
            rows = [(code[c], max(round_up(code[c].quantity * 0.5, code[c].pack), code[c].pack)) for c in ('MNT-002', 'MNT-003')]
            self.order('quimicos', rows, '', arrive=self.b(7), extra={'partial': False, 'other_lot': False, 'protected': True,
                                                                      'after': lambda inv: self.case.update(pronto_pago=inv.pk)})
        elif kind == 'parcial':
            rows = [(code['ADH-003'], 3 * code['ADH-003'].pack), (code['ADH-005'], 4 * code['ADH-005'].pack)]
            self.order('adhesivos', rows, '', arrive=self.b(3), extra={'partial': True, 'hold_rest': True, 'other_lot': False, 'protected': True})
        elif kind == 'por_recibir':
            rows = [(code['SEL-001'], 2 * code['SEL-001'].pack), (code['SEL-002'], 2 * code['SEL-002'].pack), (code['SEL-004'], code['SEL-004'].pack)]
            self.order('sellantes', rows, '', arrive=self.b(1), extra={'partial': False, 'other_lot': False, 'protected': True, 'receive': False})

    def remember_batch(self, field, item, expiry):
        batch = Batch.objects.filter(product=item.product, expiry=expiry).order_by('-pk').first()
        if batch is not None:
            self.case[field] = batch.pk
            self.protected_batches.add(batch.pk)

    def open_order(self, key):
        """Pedido enviado que sigue en tránsito en la fecha de referencia; su factura queda como documento por importar."""
        mine = [it for it in self.items if it.supplier == key and it.product is not None and it.pattern not in ('sin_rotacion', 'nueva') and it.code != FALTANTE]
        pool = [it for it in mine if it.pattern != 'intermitente' and self.incoming[it.product.pk] == 0]
        if not pool:   # todo lo del proveedor ya viene en camino: se repone igual lo que menos cobertura tiene
            pool = [it for it in mine if it.code not in ('ADH-003', 'ADH-005')] or mine
        pool.sort(key=lambda it: ((self.avail[it.product.pk] + self.incoming[it.product.pk]) / max(it.minimum, 1), it.code))
        rows = [(it, it.quantity) for it in pool[:2]]
        new = next((it for it in self.items if it.pattern == 'nueva' and it.supplier == key and it.product is not None), None)
        if new is not None:
            rows.append((new, new.quantity))
        if not rows:
            return
        order = self.order(key, rows, '', arrive=False)
        if order is not None:
            self.case['abiertos'][key] = {'order': order.pk, 'number': order.number, 'supplier': key, 'rows': rows, 'ordered_on': self.clock.today}

    def prior_delivery(self):
        """Entrega con remisión que se factura después (la venta no vuelve a descontar el inventario)."""
        item = self.by_code['ADH-005']
        qty = item.pack
        batch = S.suggested_batch(item.product, qty)
        if batch is None:
            return
        movement = S.exit_stock(self.inv, batch.pk, qty, 'Remisión R-2291', 'Entrega urgente a Planta Procesadora La Pradera; se factura después', tok())
        self.avail[item.product.pk] -= qty
        self.case['remision'] = (item.code, qty, movement)

    def request_to_reserve(self):
        item = self.by_code['SEL-002']
        r = S.request_stock(self.req1, item.product, 4, 'Sellado de ventanas de la oficina de servicio técnico', tok(), needed_by=self.D + timedelta(days=1))
        self.case['solicitud_reserva'] = r.pk

    def before_last_import(self, day):
        """Movimientos del día anterior que quedan por identificar: uno claro, uno ambiguo y uno sin coincidencias."""
        when = day - timedelta(days=1)
        clear, amb = self.case.get('claro'), self.case.get('ambiguo')
        if clear:
            c = self.clients[clear['client']]
            from .escenario import plain
            self.bank(when, clear['total'], f'PAGO FACTURA FE {clear["number"]} {plain(c["name"])[:26]}', '', origin=c['account'], holder=plain(c['name']),
                      holder_id=c['ident'], meta={'kind': 'case', 'case': 'claro'})
        if amb:
            self.bank(when, amb['total'], 'CONSIGNACION NACIONAL EN OFICINA', '', meta={'kind': 'case', 'case': 'ambiguo'})
        self.bank(when, Decimal('873400'), 'TRANSFERENCIA RECIBIDA BANCA VIRTUAL', '', origin='5550017788', holder='AGROINSUMOS LA RIBERA',
                  meta={'kind': 'case', 'case': 'sin_coincidencia'})

    def last_day(self):
        """Último día simulado: lo que el equipo dejó pendiente para hoy."""
        from .escenario import plain
        day, D = self.clock.today, self.D
        # Faltante próximo: un pedido extraordinario deja la referencia de alta rotación con pocos días de cobertura.
        item = self.by_code[FALTANTE]
        extra = self.avail[item.product.pk] - self.case['faltante_target']
        if extra > 0:
            self.block_faltante = True
            self.case['faltante_floor'] = self.case['faltante_target']
            self.sell('pradera', {FALTANTE: extra}, day)
        # Solicitudes: una reserva aprobada por entregar y dos pendientes (una sin existencias suficientes).
        if self.case.get('solicitud_reserva'):
            batch = S.suggested_batch(self.by_code['SEL-002'].product, 4)
            if batch is not None:
                res = S.approve(self.inv, self.case['solicitud_reserva'], batch.pk, tok())
                self.case['reserva'] = res.pk
        a = S.request_stock(self.req1, self.by_code['MNT-003'].product, 6, 'Mantenimiento preventivo de tableros eléctricos', tok(), needed_by=D + timedelta(days=2))
        scarce = self.by_code['REC-002']
        largest = max((b.available for b in Batch.objects.filter(product=scarce.product, expiry__gte=D)), default=0)
        b = S.request_stock(self.req2, scarce.product, max(largest + 4, 8), 'Pintura de la estructura del mezzanine nuevo', tok(), needed_by=D + timedelta(days=3))
        self.case['solicitudes'] = (a.pk, b.pk)
        # Venta que factura la entrega previa.
        if self.case.get('remision'):
            code, qty, movement = self.case['remision']
            sale = self.sell('pradera', {code: qty}, day, prior={code: movement})
            if sale is not None:
                self.case['venta_remision'] = (sale.pk, str(movement.pk))
        # Descuentos: la administración agota su cupo del mes y el siguiente descuento espera a la dirección.
        guard = 0
        while discounts.quota_status(self.admin)['available'] > 0 and guard < 5:
            key = ('vias', 'metal', 'esquina', 'sanrafael', 'piston')[guard]
            guard += 1
            self.sell(key, self.basket(key, 2), day, discount=(self.rng.choice([3, 5]), self.rng.choice(B.DISCOUNT_REASONS)))
        sale = self.sell('altos', self.basket('altos', 3), day, discount=(8, 'El cliente pide igualar la cotización de la obra'), draft=True)
        if sale is not None:
            req = sale.discount_requests.filter(status='pending').first()
            self.case['descuento'] = (sale.pk, req.pk if req else None)
        # Borrador listo para emitir hoy.
        draft = self.sell('esquina', self.basket('esquina', 3), day, draft=True, issue_date=D)
        if draft is not None:
            self.case['borrador'] = draft.pk
        # Un pago a proveedor confirmado ayer y dos ingresos de clientes: saldrán en el extracto nuevo.
        inv = (with_payable_balance(Invoice.objects.filter(review='validated')).filter(balance_due__gt=0).exclude(pk__in=self.protected_invoices)
               .exclude(allocations__payment__status='scheduled').order_by('due_date', 'pk').first())
        if inv is not None:
            self.pay(inv.supplier, [inv.pk], day)
        for slot, mention in (('extracto_1', True), ('extracto_2', False)):
            info = self.case.get(slot)
            if not info:
                continue
            c = self.clients[info['client']]
            name = plain(c['name'])
            if mention:
                self.bank(day, info['total'], f'PAGO FE {info["number"]} {name[:28]}', '', origin=c['account'], holder=name, meta={'kind': 'case'})
            else:
                self.bank(day, info['total'], 'TRANSFERENCIA RECIBIDA BANCA VIRTUAL', '', origin=c['account'], holder=name, holder_id=c['ident'], meta={'kind': 'case'})
        self.bank(day, Decimal('-38900'), 'COMISION TRANSFERENCIAS BANCA VIRTUAL', '', meta={'kind': 'expense'})

    # ------------------------------------------------------------------ cierre
    def closing(self):
        """Fecha de referencia: reentrena el modelo de pagos, corre los agentes una vez y registra los casos.

        Todo queda fechado en los primeros minutos del día, de modo que al abrir la demostración ese mismo día no
        haya registros con hora futura (los agentes diarios ya tienen su turno de hoy y no lo repiten)."""
        D = self.D
        self.clock.at(D, 0, 5)
        self.refresh_stock()
        self.timed('reentrenar pagos', pagos.retrain)
        for minute, key in enumerate(('contable', 'resumen', 'vencimientos', 'reposicion', 'pronto_pago', 'solicitudes', 'conciliacion', 'cobranza', 'avisos')):
            self.clock.at(D, 0, 10 + 2 * minute)
            self.run_agent(key)
        self.clock.at(D, 0, 30)
        self.build_documents()
        self.build_cases()

    def build_documents(self):
        """Datos de los documentos de demostración (los escribe `feria.documentos`)."""
        from .escenario import box_code
        D = self.D
        invoices = []
        for key, kind in (('recubrimientos', 'pdf'), ('importadora', 'pdf'), ('adhesivos', 'xml'), ('cintas', 'xml')):
            info = self.case['abiertos'].get(key)
            if not info:
                continue
            data = self.invoice_data(info, D - timedelta(days=1), issue=D - timedelta(days=1))
            for line in data['lines']:
                line.pop('item')
            invoices.append({'kind': kind, 'supplier': key, 'order': info['order'], 'order_number': info['number'], 'data': data})
        rows = sorted(self.bank_rows, key=lambda r: (r['date'], r['n']))
        self.bank_rows = []
        codes = []
        pending = self.case.get('por_recibir')
        if pending:
            serial = 0
            for line in pending['data']['lines']:
                it = line['item']
                for _ in range(line['quantity'] // it.pack):
                    serial += 1
                    codes.append({'code': box_code(it.gtin, line['expected_expiry'], line['expected_lot'], f'{D:%y%m%d}{serial:03d}'), 'product': it.name,
                                  'product_code': it.code, 'lot': line['expected_lot'], 'expiry': line['expected_expiry'].isoformat(), 'units': it.pack,
                                  'gtin': it.gtin})
        self.documents = {'invoices': invoices, 'statement': self.statement_csv(rows) if rows else b'', 'statement_rows': len(rows), 'codes': codes,
                          'reception_invoice': Invoice.objects.get(pk=pending['invoice']).number if pending else '',
                          'reception_supplier': self.supplier_data['sellantes']['name'] if pending else '', 'reference_date': D}

    def action(self, agent, kind, match):
        for a in AgentAction.objects.filter(agent=agent, kind=kind, status='proposed').order_by('pk'):
            if match(a.payload):
                return a.pk
        return None

    def build_cases(self):
        D, case, cases = self.D, self.case, {}
        cases['reference_date'] = D.isoformat()
        accounts, names = {}, {}
        for role, username, first, last, area, _ in B.TEAM:
            accounts.setdefault(role, username)
            if accounts[role] != username:
                accounts[f'{role}_2'] = username
            names[username] = f'{first} {last}'
        cases['accounts'], cases['account_names'] = accounts, names
        cases['patrones'] = {it.code: it.pattern for it in self.items}

        # 1. Faltante próximo y propuesta de reposición.
        item = self.by_code[FALTANTE]
        supplier = self.suppliers[item.supplier]
        rep = self.action('reposicion', 'purchase_order', lambda p: item.product.pk in p.get('products', []))
        rep_obj = AgentAction.objects.filter(pk=rep).first()
        cases['faltante'] = {'product_id': item.product.pk, 'product_code': item.code, 'product_name': item.name, 'available': self.avail[item.product.pk],
                             'supplier_id': supplier.pk, 'forecast_url': reverse('forecast', args=[item.product.pk]), 'action_id': rep,
                             'agents_url': reverse('agents')}
        cases['reposicion'] = {'action_id': rep, 'supplier_id': supplier.pk, 'product_ids': rep_obj.payload.get('products', []) if rep_obj else [],
                               'url': reverse('agents')}

        # 2. Vencimientos.
        cases['vencimiento'] = {}
        for field, name in (('por_vencer', 'por_vencer'), ('vencido', 'vencido')):
            b = Batch.objects.select_related('product').filter(pk=case.get(field)).first()
            if b is None:
                continue
            entry = {'batch_id': b.pk, 'product_id': b.product_id, 'product_code': b.product.code, 'lot': b.lot, 'units': b.physical,
                     'days_left': (b.expiry - D).days, 'url': reverse('trace', args=[b.pk])}
            if name == 'vencido':
                entry['action_id'] = self.action('vencimientos', 'writeoff_expired', lambda p: p.get('batch_id') == b.pk)
            cases['vencimiento'][name] = entry

        # 3. Recepción.
        cases['recepcion'] = {}
        pending = case.get('por_recibir')
        if pending:
            inv = Invoice.objects.get(pk=pending['invoice'])
            cases['recepcion'] = {'invoice_id': inv.pk, 'invoice_number': inv.number, 'order_id': pending['order'], 'supplier_id': inv.supplier_id,
                                  'url': reverse('reception_invoice', args=[inv.pk]), 'codes': [c['code'] for c in self.documents['codes']],
                                  'codes_plain': self.documents['codes']}
        partial = case.get('parcial')
        if partial:
            inv = Invoice.objects.get(pk=partial['invoice'])
            line = inv.lines.get(pk=partial['line'])
            cases['recepcion']['parcial'] = {'invoice_id': inv.pk, 'invoice_number': inv.number, 'line_id': line.pk, 'quantity': line.quantity,
                                             'received': line.received, 'order_id': partial['order'], 'url': reverse('invoice', args=[inv.pk])}

        # 4. Pronto pago.
        if case.get('pronto_pago'):
            inv = Invoice.objects.select_related('supplier').get(pk=case['pronto_pago'])
            offer = P.settlement(inv, D)
            cases['pronto_pago'] = {'invoice_id': inv.pk, 'invoice_number': inv.number, 'supplier_id': inv.supplier_id,
                                    'deadline': offer['deadline'].isoformat() if offer['deadline'] else None, 'discount': str(offer['discount']),
                                    'action_id': self.action('pronto_pago', 'schedule_payment', lambda p: inv.pk in p.get('invoices', [])),
                                    'url': reverse('invoice_pay', args=[inv.pk])}

        # 5. Banco.
        bank = {'account_id': self.account.pk, 'import_url': reverse('bank_import')}
        pending_txns = list(BankTransaction.objects.filter(status='pending', amount__gt=0).order_by('pk'))
        for txn in pending_txns:
            url = reverse('bank_txn', args=[txn.pk])
            if case.get('claro') and f'FE {case["claro"]["number"]} ' in txn.description:
                bank['claro'] = {'txn_id': txn.pk, 'party_id': case['claro']['party'], 'receivable_id': case['claro']['rec'], 'url': url,
                                 'action_id': self.action('conciliacion', 'apply_bank', lambda p: p.get('txn_id') == txn.pk)}
            elif case.get('ambiguo') and txn.description.startswith('CONSIGNACION NACIONAL'):
                bank['ambiguo'] = {'txn_id': txn.pk, 'party_ids': case['ambiguo']['parties'], 'receivable_ids': case['ambiguo']['recs'], 'url': url}
            elif txn.holder_name == 'AGROINSUMOS LA RIBERA':
                bank['sin_coincidencia'] = {'txn_id': txn.pk, 'url': url}
        cases['banco'] = bank

        # 6. Cartera vencida en tres tramos.
        cartera = {}
        reminders = {a.payload.get('party_id'): a.pk for a in AgentAction.objects.filter(agent='cobranza', kind='collection_reminder', status='proposed')}
        for slot in ('d15', 'd45', 'd90'):
            info = case.get(slot)
            if not info:
                continue
            rec = Receivable.objects.get(pk=info['rec'])
            cartera[slot] = {'party_id': rec.party_id, 'receivable_id': rec.pk, 'days': (D - rec.due_date).days, 'action_id': reminders.get(rec.party_id),
                             'url': reverse('party', args=[rec.party_id])}
        if cartera.get('d15') and cartera['d15']['action_id'] is None:
            # La estrategia elegida para ese cliente puede esperar más días de mora: se usa otro cliente del mismo tramo con propuesta.
            for rec in with_balance(Receivable.objects.all()).filter(balance__gt=0, due_date__range=(D - timedelta(days=24), D - timedelta(days=8))).order_by('due_date', 'pk'):
                if rec.party_id in reminders and rec.party_id not in {c['party_id'] for c in cartera.values()}:
                    cartera['d15'] = {'party_id': rec.party_id, 'receivable_id': rec.pk, 'days': (D - rec.due_date).days, 'action_id': reminders[rec.party_id],
                                      'url': reverse('party', args=[rec.party_id])}
                    break
        cases['cartera'] = cartera

        # 7. Sin historial.
        cases['sin_historial'] = {}
        for name, pattern in (('lanzada', 'lanzamiento'), ('nueva', 'nueva')):
            it = next((x for x in self.items if x.pattern == pattern and x.product is not None), None)
            if it is not None:
                cases['sin_historial'][name] = {'product_id': it.product.pk, 'product_code': it.code, 'forecast_url': reverse('forecast', args=[it.product.pk])}

        idle = next((x for x in self.items if x.pattern == 'sin_rotacion' and x.product is not None), None)
        if idle is not None:
            cases['sin_rotacion'] = {'product_id': idle.product.pk, 'product_code': idle.code, 'forecast_url': reverse('forecast', args=[idle.product.pk])}

        # 8. Solicitudes.
        cases['solicitudes'] = {}
        if case.get('solicitudes'):
            a, b = case['solicitudes']
            cases['solicitudes']['con_reserva'] = {'request_id': str(a), 'product_id': self.by_code['MNT-003'].product.pk, 'url': reverse('approve', args=[a]),
                                                   'action_id': self.action('solicitudes', 'reserve_request', lambda p: p.get('request_id') == str(a))}
            cases['solicitudes']['sin_existencias'] = {'request_id': str(b), 'product_id': self.by_code['REC-002'].product.pk, 'url': reverse('approve', args=[b])}
        if case.get('reserva'):
            res = Reservation.objects.get(pk=case['reserva'])
            cases['solicitudes']['reserva_por_entregar'] = {'reservation_id': str(res.pk), 'request_id': str(res.request_id), 'url': reverse('reservations')}

        # 9. Descuentos.
        if case.get('descuento'):
            sale_pk, req_pk = case['descuento']
            cases['descuentos'] = {'request_id': req_pk, 'sale_id': sale_pk, 'admin_user_id': self.admin.pk, 'quota': self.admin.discount_quota or 3,
                                   'url': reverse('discounts')}

        # 10. Ventas.
        cases['ventas'] = {}
        if case.get('borrador'):
            cases['ventas']['borrador'] = {'sale_id': case['borrador'], 'url': reverse('sale', args=[case['borrador']])}
        if case.get('venta_remision'):
            sale_pk, movement = case['venta_remision']
            cases['ventas']['entrega_previa'] = {'sale_id': sale_pk, 'movement_id': movement, 'url': reverse('sale', args=[sale_pk])}

        # 11. Pedidos.
        cases['pedidos'] = {}
        transit = case['abiertos'].get('recubrimientos') or next(iter(case['abiertos'].values()), None)
        if transit:
            cases['pedidos']['en_transito'] = {'order_id': transit['order'], 'number': transit['number'], 'url': reverse('purchase_order', args=[transit['order']])}
        if partial:
            order = PurchaseOrder.objects.get(pk=partial['order'])
            cases['pedidos']['parcial'] = {'order_id': order.pk, 'number': order.number, 'invoice_id': partial['invoice'], 'url': reverse('purchase_order', args=[order.pk])}
        cases['pedidos']['abiertos'] = [{'order_id': v['order'], 'number': v['number'], 'supplier_id': self.suppliers[k].pk} for k, v in sorted(case['abiertos'].items())]
        self.cases = cases
