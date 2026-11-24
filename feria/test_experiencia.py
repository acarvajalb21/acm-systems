"""Resumen, mapa de flujo y recorrido guiado: cada perfil ve lo suyo, las cifras coinciden con las de cada módulo
y el recorrido funciona con y sin escenario generado."""
import re
from datetime import date, timedelta
from decimal import Decimal
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from agentes import runner
from agentes.models import AgentAction
from cartera import services as C
from cartera.models import Receivable, Sale
from cartera.tests import Base, PASSWORD, TODAY, tok
from inventory import compras, services as S
from inventory.models import Batch, DiscountTier, Invoice, InvoiceLine, Movement, Product, Receipt, Supplier, User
from . import flow, resumen, tour
from .models import DemoState

TERMS = {'payment_days': 120, 'base': 'subtotal', 'tiers': [[30, '5'], [60, '3']]}
HREF = re.compile(r'href="(/[^"#?]*)')


def week_start(day):
    return day - timedelta(days=day.weekday())


class Escena(Base):
    """Una operación pequeña con algo pendiente en cada estación del flujo."""

    def setUp(self):
        super().setUp()
        self.manager, self.admin, self.requester = self.users['manager'], self.users['admin'], self.users['requester']
        self.owner = User.objects.create_user('owner', password=PASSWORD, role='owner', must_change_password=False, first_name='Dirección')
        self.reception = User.objects.create_user('reception', password=PASSWORD, role='reception', must_change_password=False, first_name='Recepción')
        self.other = User.objects.create_user('otra', password=PASSWORD, role='requester', must_change_password=False, first_name='Otra')
        self.people = {'owner': self.owner, 'admin': self.admin, 'inventory': self.manager, 'reception': self.reception, 'finance': self.finance,
                       'billing': self.billing, 'requester': self.requester}
        today = TODAY()
        # Compra y recepción: un proveedor con descuento por pronto pago, una factura por recibir y otra vencida.
        self.vendor = Supplier.objects.create(name='Proveedor Andino S.A.S.', nit='900123456', payment_days=120, email='pedidos@proveedor.example')
        DiscountTier.objects.bulk_create([DiscountTier(supplier=self.vendor, days=30, percent=Decimal('5')), DiscountTier(supplier=self.vendor, days=60, percent=Decimal('3'))])
        self.to_receive = self.invoice('AND-77', 20, quantity=24, received=0)
        self.overdue = self.invoice('AND-10', 150, quantity=5, received=5, terms={})
        # Inventario: una referencia con diez semanas de consumo que ya necesita pedido, y un lote vencido con saldo.
        self.fast = self.weekly_product()
        Batch.objects.create(product=self.prod_b, lot='VENCIDO', expiry=today - timedelta(days=4), location='Bodega 1', physical=6)
        # Solicitudes: una propia y otra de una segunda persona.
        S.request_stock(self.requester, self.prod_a, 2, 'DESTINO-PROPIO taller', tok(), needed_by=today + timedelta(days=1))
        S.request_stock(self.other, self.prod_a, 1, 'DESTINO-AJENO bodega', tok())
        # Ventas: una emitida hoy, una del mes anterior, un borrador y cartera vencida.
        self.sold = self.sale(self.ferreteria, [(self.prod_a, 2, None, '')])
        old = self.sale(self.rojas, [(self.prod_b, 1, None, '')])
        Sale.objects.filter(pk=old.pk).update(issue_date=(today.replace(day=1) - timedelta(days=1)).replace(day=1))
        self.draft = C.save_sale(self.finance, None, self.header(self.rojas), [{'product': self.prod_b, 'quantity': 1, 'unit_price': None, 'price_reason': ''}], tok())
        Receivable.objects.create(party=self.rojas, source='opening', prefix='FE', number=900, issue_date=today - timedelta(days=70),
                                  due_date=today - timedelta(days=40), amount=Decimal('350000'), created_by=self.finance)
        # Cobro: un ingreso del banco sin identificar.
        self.statement([[today.strftime('%d/%m/%Y'), 'CONSIGNACION TITULAR-BANCARIO', 'REF-9', '55554444', 'TITULAR-BANCARIO', '123.456,00', '1.000.000,00']])
        # Pedido abierto a un proveedor.
        self.order = compras.create_order(self.manager, self.vendor, [{'product': self.prod_b, 'quantity': 12}], '', tok())
        for key in ('solicitudes', 'vencimientos', 'reposicion', 'pronto_pago', 'conciliacion'):
            run = runner.run_agent(key, 'manual')
            self.assertEqual(run.status, 'succeeded', run.error)

    def invoice(self, number, days_ago, quantity, received, terms=TERMS):
        issue = TODAY() - timedelta(days=days_ago)
        inv = Invoice.objects.create(supplier=self.vendor, number=number, issue_date=issue, due_date=issue + timedelta(days=120), subtotal=Decimal('1000000'),
                                     total=Decimal('1000000'), review=Invoice.Review.VALIDATED, terms=terms, validated_by=self.finance,
                                     validated_at=timezone.now(), created_by=self.finance)
        InvoiceLine.objects.create(invoice=inv, product=self.prod_a, description='Referencia A', quantity=quantity, unit_price=Decimal('40000'), received=received)
        return inv

    def weekly_product(self, weeks=10, per_week=10, initial=104):
        p = Product.objects.create(code='REF-C', name='Referencia veloz', manufacturer='Andino', pack_size=10)
        monday = week_start(TODAY())

        def backdate(movement, day):
            at = timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time()) + timedelta(hours=10))
            Movement.objects.filter(pk=movement.pk).update(created_at=at)

        first = S.adjust_in(self.manager, p, 'LC1', TODAY() + timedelta(days=300), 'Bodega 1', initial, 'initial', '', tok())
        backdate(first, monday - timedelta(days=7 * weeks))
        batch = Batch.objects.get(product=p, lot='LC1')
        for w in range(weeks, 0, -1):
            backdate(S.exit_stock(self.manager, batch.pk, per_week, f'REM-{w}', '', tok()), monday - timedelta(days=7 * w - 2))
        ordered = TODAY() - timedelta(days=40)
        inv = Invoice.objects.create(supplier=self.vendor, number='AND-1', issue_date=ordered + timedelta(days=3), due_date=ordered + timedelta(days=63), ordered_on=ordered,
                                     review=Invoice.Review.VALIDATED, subtotal=Decimal('20000'), total=Decimal('20000'), created_by=self.manager,
                                     validated_by=self.finance, validated_at=timezone.now(), terms={})
        InvoiceLine.objects.create(invoice=inv, product=p, description=p.name, quantity=20, unit_price=Decimal('1000'), received=20)
        Receipt.objects.create(invoice=inv, received_on=ordered + timedelta(days=7), actor=self.manager)
        from cartera.models import PriceItem
        for pl in self.lists.values():
            PriceItem.objects.create(price_list=pl, product=p, price=Decimal('5000'), valid_from=TODAY() - timedelta(days=30), created_by=self.finance)
        return p

    def login(self, user):
        c = Client()
        c.force_login(user)
        s = c.session
        s['user_version'] = user.session_version
        s.save()
        return c

    def kpis(self, page):
        return {k['key']: k for k in page.context['indicators']}


class ResumenPorPerfil(Escena):
    def test_every_profile_gets_its_summary_without_errors_or_links_it_cannot_open(self):
        for role, user in self.people.items():
            client = self.login(user)
            for name in ('home', 'flow_map', 'tour'):
                page = client.get(reverse(name))
                with self.subTest(role=role, page=name):
                    self.assertEqual(page.status_code, 200)
                    html = page.content.decode()
                    self.assertNotIn('style="', html)
                    self.assertNotIn('onclick', html)
                    if name == 'home':
                        self.assertTemplateUsed(page, 'home.html')
                        self.assertEqual(page.context['failed'], [])
                        self.assertTrue(3 <= len(page.context['indicators']) <= 5)
                        self.assertTrue(all(k['period'] and k['url'] for k in page.context['indicators']))
                        self.assertIsNotNone(page.context['recommendation'])
                        self.assertLessEqual(len(page.context['attention']), 6)
                    # Ningún enlace de la página lleva a un módulo que este perfil no puede abrir.
                    for url in sorted(set(HREF.findall(html))):
                        if url.startswith(('/static/', '/manifest', '/sw.js')):
                            continue
                        with self.subTest(role=role, page=name, link=url):
                            self.assertIn(client.get(url).status_code, (200, 302), url)

    def test_profiles_do_not_see_data_from_modules_they_cannot_open(self):
        hidden = {
            'requester': ('DESTINO-AJENO', 'Proveedor Andino', 'Ferretería El Tornillo', 'TITULAR-BANCARIO', 'AND-77', 'Referencia veloz se agotaría'),
            'reception': ('DESTINO-PROPIO', 'Ferretería El Tornillo', 'Camilo Rojas', 'TITULAR-BANCARIO', '$350.000', 'Referencia veloz se agotaría'),
            'billing': ('DESTINO-PROPIO', 'Proveedor Andino', 'TITULAR-BANCARIO', 'AND-77', 'Referencia veloz se agotaría', '$350.000'),
            'inventory': ('Ferretería El Tornillo', 'Camilo Rojas', 'TITULAR-BANCARIO', '$350.000', '$123.456'),
            'finance': ('DESTINO-PROPIO', 'DESTINO-AJENO', 'Referencia veloz se agotaría', 'VENCIDO'),
        }
        for role, words in hidden.items():
            client = self.login(self.people[role])
            for name in ('home', 'flow_map', 'tour'):
                html = client.get(reverse(name)).content.decode()
                for word in words:
                    with self.subTest(role=role, page=name, word=word):
                        self.assertNotIn(word, html)
        # Cada quien sí ve lo suyo.
        self.assertContains(self.login(self.requester).get(reverse('flow_map')), 'DESTINO-PROPIO')
        self.assertContains(self.login(self.reception).get(reverse('home')), 'AND-77')
        self.assertContains(self.login(self.finance).get(reverse('flow_map') + '?etapa=cobro'), 'TITULAR-BANCARIO')
        self.assertContains(self.login(self.manager).get(reverse('home')), 'Referencia veloz se agotaría')

    def test_summary_blocks_for_inventory_and_finance(self):
        page = self.login(self.manager).get(reverse('home'))
        titles = ' | '.join(a['title'] for a in page.context['attention'])
        self.assertIn('Agente de solicitudes: 2 propuestas por decidir', titles)
        self.assertIn('Dar de baja 6 unidades de Referencia B', titles)
        self.assertEqual([a['level'] for a in page.context['attention']], sorted((a['level'] for a in page.context['attention']), key=resumen.RANK.get))
        rec = page.context['recommendation']
        proposal = AgentAction.objects.get(agent='reposicion', status='proposed')
        self.assertEqual(rec['url'], reverse('agent_action', args=[proposal.pk]))
        self.assertIn('Aprobar el pedido sugerido a Proveedor Andino', rec['title'])
        risk = page.context['risks'][0]
        self.assertIn('Referencia veloz se agotaría', risk['title'])
        self.assertEqual(risk['url'], reverse('forecast', args=[self.fast.pk]))
        self.assertTrue(risk['when'].startswith('en ') or risk['when'] in ('hoy', 'mañana'))
        page = self.login(self.finance).get(reverse('home'))
        self.assertIn('ahorrar', page.context['recommendation']['title'])
        self.assertTrue(any('Se perdería el descuento' in r['title'] for r in page.context['risks']))
        self.assertTrue(any(a['title'] == 'Factura vencida' or 'vencida' in a['title'] for a in page.context['attention']))

    def test_one_broken_block_does_not_break_the_page(self):
        from unittest import mock
        with mock.patch('feria.resumen.risks', side_effect=RuntimeError('fallo simulado')), self.assertLogs('feria.resumen', level='ERROR'):
            page = self.login(self.owner).get(reverse('home'))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['failed'], ['los riesgos próximos'])
        self.assertContains(page, 'Una parte del Resumen no se pudo calcular')
        with mock.patch('aprendizaje.reposicion.plans', side_effect=RuntimeError('fallo simulado')):
            page = self.login(self.manager).get(reverse('home'))
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context['plans_failed'])


class CifrasCoinciden(Escena):
    """El número del Resumen (y el del mapa) es el mismo que muestra el módulo al que enlaza."""

    def test_direction_indicators_match_results_portfolio_and_payables(self):
        client = self.login(self.owner)
        k = self.kpis(client.get(reverse('home')))
        results = client.get(reverse('results')).context
        self.assertEqual(k['ventas']['value'], S.cop(results['r']['sales']['revenue']))
        self.assertEqual(results['r']['sales']['revenue'], Decimal('200000.00'))          # solo la venta de este mes
        self.assertEqual(k['recaudo']['value'], S.cop(results['r']['collected']))
        self.assertEqual(k['riesgo_vencimiento']['value'], S.cop(results['r']['stock_soon']))
        portfolio = client.get(reverse('portfolio')).context
        self.assertEqual(k['cartera_vencida']['value'], S.cop(portfolio['buckets']['late']))
        self.assertEqual(portfolio['buckets']['late'], Decimal('350000.00'))
        payables = client.get(reverse('payables')).context
        self.assertEqual(k['pagar_vencidas']['value'], S.cop(payables['overdue']))
        self.assertEqual(payables['overdue'], Decimal('1000000.00'))
        self.assertEqual(k['ventas']['url'], reverse('results'))
        self.assertEqual((k['cartera_vencida']['url'], k['pagar_vencidas']['url']), (reverse('portfolio'), reverse('payables')))
        # La comparación es equivalente: mismos días del mes anterior, con su base a la vista.
        self.assertIn('frente a', k['ventas']['delta']['text'])
        self.assertIn(resumen.MONTHS[(TODAY().replace(day=1) - timedelta(days=1)).month - 1], k['ventas']['delta']['text'])

    def test_inventory_finance_reception_and_billing_indicators_match_their_modules(self):
        client = self.login(self.manager)
        k = self.kpis(client.get(reverse('home')))
        self.assertEqual(k['por_pedir']['value'], str(client.get(reverse('replenishment')).context['to_order']))
        self.assertEqual(k['solicitudes']['value'], str(client.get(reverse('requests') + '?estado=pending').context['requests'].count()))
        soon = [b for b in client.get(reverse('stock')).context['batches'] if 0 <= b.days_left <= 90]
        self.assertEqual(k['por_vencer']['value'], str(len(soon)))
        client = self.login(self.finance)
        k = self.kpis(client.get(reverse('home')))
        self.assertEqual(k['por_identificar']['value'], S.cop(client.get(reverse('bank')).context['pending_total']))
        self.assertEqual(k['por_identificar']['value'], '$123.456')
        payables = client.get(reverse('payables')).context
        self.assertEqual(k['descuentos']['value'], S.cop(payables['offer']))
        self.assertEqual(k['cartera_vencida']['value'], S.cop(client.get(reverse('portfolio')).context['buckets']['late']))
        client = self.login(self.reception)
        k = self.kpis(client.get(reverse('home')))
        module = client.get(reverse('reception')).context
        self.assertEqual((k['por_recibir']['value'], k['unidades']['value']), (str(module['k_pending']), str(module['k_units'])))
        self.assertEqual((module['k_pending'], module['k_units']), (1, 24))
        client = self.login(self.billing)
        k = self.kpis(client.get(reverse('home')))
        sales = client.get(reverse('sales')).context
        self.assertEqual(k['borradores']['value'], str(sales['counts']['borradores']))
        self.assertEqual(k['ventas_hoy']['value'], S.cop(sales['k_today']))
        self.assertEqual(k['envios']['value'], str(sales['counts']['pendientes']))

    def test_station_counts_match_their_modules(self):
        client = self.login(self.owner)
        st = {s.key: s for s in flow.stations(self.owner)}
        self.assertEqual(st['solicitud'].count, client.get(reverse('requests') + '?estado=pending').context['requests'].count())
        self.assertEqual(st['inventario'].count, client.get(reverse('replenishment')).context['to_order'])
        self.assertEqual(st['compra'].count, client.get(reverse('purchase_orders')).context['counts']['open'])
        self.assertEqual(st['recepcion'].count, client.get(reverse('reception')).context['k_pending'])
        sales = client.get(reverse('sales')).context['counts']
        self.assertEqual(st['venta'].count, sales['borradores'] + sales['pendientes'])
        bank = client.get(reverse('bank')).context
        self.assertEqual((st['cobro'].count, st['cobro'].value), (bank['counts']['pending'], S.cop(bank['pending_total'])))
        self.assertEqual([s.key for s in flow.stations(self.owner)], list(flow.ORDER))
        # El Resumen muestra las mismas estaciones del mapa.
        home = client.get(reverse('home'))
        self.assertEqual([(s.key, s.value, s.state) for s in home.context['stations']], [(s.key, s.value, s.state) for s in st.values()])

    def test_equivalent_periods(self):
        self.assertEqual(resumen.same_days(date(2026, 11, 18)), (date(2026, 11, 1), date(2026, 11, 18), date(2026, 10, 1), date(2026, 10, 18)))
        self.assertEqual(resumen.same_days(date(2026, 3, 31))[2:], (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertIsNone(resumen.pct_change(Decimal('10'), Decimal('0')))
        self.assertEqual(resumen.pct_text(resumen.pct_change(Decimal('150'), Decimal('100'))), '+50,0 %')
        self.assertIn('no hay base', resumen.comparison(Decimal('10'), Decimal('0'), date(2026, 11, 18), 'ventas')['text'])


class MapaDeFlujo(Escena):
    ALLOWED = {'owner': set(flow.ORDER), 'admin': set(flow.ORDER), 'inventory': {'solicitud', 'inventario', 'compra', 'recepcion'},
               'finance': {'compra', 'venta', 'cobro'}, 'reception': {'recepcion'}, 'billing': {'venta'}, 'requester': {'solicitud'}}

    def test_stations_with_and_without_permission(self):
        for role, allowed in self.ALLOWED.items():
            stations = flow.stations(self.people[role])
            with self.subTest(role=role):
                self.assertEqual({s.key for s in stations if s.allowed}, allowed)
                for s in stations:
                    if not s.allowed:
                        self.assertEqual((s.count, s.value, s.records, s.links, s.state, s.module), (None, '—', [], [], '', ''))
                self.assertEqual(sum(1 for s in stations if s.selected), 1)
        mine = {s.key: s for s in flow.stations(self.requester)}['solicitud']
        self.assertEqual((mine.count, mine.state), (1, 'warning'))                       # solo la suya; se necesita mañana
        both = {s.key: s for s in flow.stations(self.manager)}['solicitud']
        self.assertEqual(both.count, 2)
        inv = {s.key: s for s in flow.stations(self.manager)}['inventario']
        self.assertEqual((inv.state, inv.records[0]['url']), ('critical', reverse('forecast', args=[self.fast.pk])))

    def test_stage_parameter_works_without_javascript(self):
        client = self.login(self.finance)
        page = client.get(reverse('flow_map') + '?etapa=cobro')
        html = page.content.decode()
        self.assertEqual([s.key for s in page.context['stations'] if s.selected], ['cobro'])
        self.assertRegex(html, r'id="flujo-tab-cobro"[^>]*aria-selected="true"')
        self.assertRegex(html, r'id="flujo-panel-cobro"[^>]*tabindex="0">')            # visible: sin el atributo hidden
        self.assertRegex(html, r'id="flujo-panel-venta"[^>]*tabindex="0" hidden>')
        self.assertEqual(html.count('aria-selected="true"'), 1)
        self.assertIn(f'href="{reverse("bank")}"', html)
        # Una estación ajena se puede elegir, pero su panel explica el acceso restringido.
        page = client.get(reverse('flow_map') + '?etapa=inventario')
        self.assertEqual([s.key for s in page.context['stations'] if s.selected], ['inventario'])
        self.assertContains(page, 'Inventario: su perfil no abre esta estación')
        self.assertNotContains(page, reverse('replenishment'))
        # Un valor desconocido abre la estación más urgente del perfil.
        page = client.get(reverse('flow_map') + '?etapa=no-existe')
        self.assertTrue(next(s for s in page.context['stations'] if s.selected).allowed)
        self.assertEqual(client.get(reverse('flow_map')).status_code, 200)

    def test_drawing_is_server_side_svg_without_inline_styles(self):
        html = self.login(self.owner).get(reverse('flow_map')).content.decode()
        self.assertEqual(html.count('<svg class="flujo-escena"'), 6)
        self.assertEqual(html.count('class="flujo-pulso"'), 6)
        self.assertNotRegex(html, r'(?:points|x|y|d|cx|cy|r)="[^"]*\d,\d{1,2}[ "][^"]*"\s*(?:class|>)')   # coordenadas con punto decimal
        self.assertNotIn('<style', html)
        self.assertEqual(len({tuple(sorted(s.items())) for scene in flow.SCENES.values() for s in scene if s['c'] == 'fl-lamp'}), 1)


class RecorridoGuiado(Escena):
    def test_tour_without_scenario_falls_back_to_module_pages(self):
        self.assertFalse(DemoState.current().ready)
        client = self.login(self.manager)
        page = client.get(reverse('tour'))
        self.assertContains(page, 'El escenario de la demostración todavía no se ha generado')
        steps = page.context['steps']
        self.assertEqual([s['n'] for s in steps], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual((steps[1]['url'], steps[1]['prepared']), (reverse('replenishment'), False))
        open_order = AgentAction.objects.get(agent='reposicion', status='proposed')
        self.assertEqual(steps[2]['url'], reverse('agent_action', args=[open_order.pk]))        # sin caso preparado: la propuesta abierta
        self.assertEqual(steps[3]['url'], reverse('reception'))
        self.assertFalse(steps[4]['allowed'])                                            # la conciliación es de contabilidad
        self.assertIsNone(steps[4]['url'])
        self.assertEqual(steps[6]['url'], reverse('agents_history'))
        self.assertTrue(all(s['script'] and s['show'] for s in steps))
        self.assertNotContains(page, 'feria-bar')
        # Iniciar deja la barra en cada pantalla; «Siguiente» avanza al registro del paso.
        self.assertRedirects(client.post(reverse('tour'), {'accion': 'iniciar'}), reverse('home'))
        self.assertContains(client.get(reverse('operations')), '1 de 7')
        self.assertRedirects(client.post(reverse('tour'), {'accion': 'siguiente'}), reverse('replenishment'))
        bar = client.get(reverse('stock'))
        self.assertContains(bar, 'feria-bar')
        self.assertContains(bar, '2 de 7')
        client.post(reverse('tour'), {'accion': 'ir', 'paso': '4'})
        # El paso 5 necesita otro perfil: el recorrido no cambia permisos y lo dice.
        response = client.post(reverse('tour'), {'accion': 'siguiente'}, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse('tour') + '#paso-5')
        self.assertContains(response, 'El paso 5 necesita el perfil Contabilidad, Administración o Dirección')
        self.assertContains(response, 'Este paso necesita el perfil Contabilidad, Administración o Dirección')
        self.assertEqual(client.get(reverse('bank')).status_code, 403)
        self.assertRedirects(client.post(reverse('tour'), {'accion': 'terminar'}), reverse('tour'))
        self.assertNotContains(client.get(reverse('operations')), 'feria-bar')

    def test_tour_uses_prepared_cases_and_ignores_unsafe_or_stale_ones(self):
        proposal = AgentAction.objects.get(agent='reposicion', status='proposed')
        forecast = reverse('forecast', args=[self.fast.pk])
        reception = reverse('reception_invoice', args=[self.to_receive.pk])
        DemoState.objects.create(reference_date=TODAY(), seed=1, generated_at=timezone.now(), cases={
            'faltante': {'forecast_url': forecast, 'product_name': 'Referencia veloz'},
            'reposicion': {'action_id': proposal.pk, 'url': reverse('agents')},
            'recepcion': {'url': reception, 'invoice_number': 'AND-77', 'codes_plain': [{'code': '(01)07701234500018(10)L77', 'label': 'Caja de Referencia A'}], 'codes': ['0107701234500018']},
            'banco': {'claro': {'url': 'https://sitio-externo.example/robo', 'action_id': 999999}},
            'documentos': {'url': '//otro-sitio.example/x'}})
        steps = tour.steps(self.owner)
        self.assertEqual((steps[1]['url'], steps[1]['prepared'], steps[1]['record']), (forecast, True, 'Referencia preparada: Referencia veloz'))
        self.assertEqual(steps[2]['url'], reverse('agent_action', args=[proposal.pk]))
        self.assertEqual((steps[3]['url'], steps[3]['codes']), (reception, [{'code': '(01)07701234500018(10)L77', 'label': 'Caja de Referencia A'}]))
        self.assertEqual((steps[4]['url'], steps[4]['prepared']), (reverse('bank'), False))   # la ruta externa y la propuesta inexistente se descartan
        self.assertNotIn('otro-sitio', str(steps))
        client = self.login(self.owner)
        page = client.get(reverse('tour'))
        self.assertNotContains(page, 'todavía no se ha generado')
        self.assertContains(page, '(01)07701234500018(10)L77')
        self.assertNotContains(page, 'sitio-externo')
        client.post(reverse('tour'), {'accion': 'ir', 'paso': '2'})
        self.assertRedirects(client.post(reverse('tour'), {'accion': 'siguiente'}), reverse('agent_action', args=[proposal.pk]))
        self.assertContains(client.get(reverse('agent_action', args=[proposal.pk])), '3 de 7')
        # Cuando la propuesta preparada ya se decidió, el paso muestra su resultado (no queda otra abierta).
        self.assertEqual(runner.decide(self.manager, proposal.pk, True).status, 'executed')
        again = tour.steps(self.owner)[2]
        self.assertEqual((again['url'], again['record']), (reverse('agent_action', args=[proposal.pk]), 'La propuesta preparada ya se decidió: muestra su resultado'))
        # Los códigos de recepción no se muestran a quien no puede recibir.
        self.assertNotContains(self.login(self.billing).get(reverse('tour')), '(01)07701234500018(10)L77')
        self.assertEqual(tour.cases()['recepcion']['invoice_number'], 'AND-77')

    def test_tour_state_cookie_is_signed(self):
        client = self.login(self.manager)
        client.cookies['feria_recorrido'] = '3:1700000000'                                  # sin firma: se ignora
        self.assertNotContains(client.get(reverse('operations')), 'feria-bar')
        self.assertEqual(client.get(reverse('tour')).context['active'], None)
