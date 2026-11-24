"""Agentes: cada recorrido va de la propuesta al efecto real, sin duplicar y con los permisos del módulo."""
from datetime import datetime, timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from cartera.models import Receivable, BankTransaction, PaymentApplication, PartyContact, with_balance
from cartera.tests import Base, tok, TODAY, PASSWORD
from inventory import services as S
from inventory.models import (Audit, User, Supplier, Invoice, DiscountTier, SupplyRequest, Reservation, Batch, Movement, SupplierPayment)
from aprendizaje import reposicion as R
from aprendizaje.models import CollectionAttempt
from aprendizaje.tests import LearningBase
from inventory.models import PurchaseOrder
from . import runner
from .models import AgentAction, AgentRun, AgentConfig

TERMS = {'payment_days': 120, 'base': 'subtotal', 'tiers': [[30, '5'], [60, '3']]}


class AgentBase(Base):
    def setUp(self):
        super().setUp()
        self.requester, self.manager, self.admin = self.users['requester'], self.users['manager'], self.users['admin']
        self.owner = User.objects.create_user('owner', password=PASSWORD, role='owner', must_change_password=False)

    def review(self, key):
        r = runner.run_agent(key, 'manual')
        self.assertEqual(r.status, 'succeeded', r.error)
        return list(AgentAction.objects.filter(agent=key, status='proposed'))

    def login(self, user):
        c = Client()
        c.force_login(user)
        s = c.session
        s['user_version'] = user.session_version
        s.save()
        return c


class Requests(AgentBase):
    def test_request_event_proposes_fefo_reservation_and_approval_reserves_once(self):
        with self.captureOnCommitCallbacks(execute=True):
            req = S.request_stock(self.requester, self.prod_a, 3, 'Despacho obra norte', tok())
        [p] = AgentAction.objects.filter(agent='solicitudes', status='proposed')
        self.assertEqual(AgentRun.objects.filter(agent='solicitudes', trigger='event').count(), 1)
        lot = Batch.objects.get(product=self.prod_a, lot='L1')  # vence primero y alcanza
        self.assertEqual(p.payload['batch_id'], lot.pk)
        self.assertIn('Primero vence, primero sale', p.detail)
        for user in (self.requester, self.finance, self.billing):
            with self.assertRaises(Exception):
                runner.decide(user, p.pk, True)
        a = runner.decide(self.manager, p.pk, True)
        self.assertEqual(a.status, 'executed')
        self.assertEqual(SupplyRequest.objects.get(pk=req.pk).status, 'approved')
        self.assertEqual(Batch.objects.get(pk=lot.pk).reserved, 3)
        with self.assertRaisesMessage(ValidationError, 'ya fue decidida'):
            runner.decide(self.manager, p.pk, True)
        self.assertEqual(Reservation.objects.count(), 1)
        self.assertEqual(self.review('solicitudes'), [])

    def test_stale_proposal_is_not_executed(self):
        req = S.request_stock(self.requester, self.prod_a, 2, 'Mostrador', tok())
        [p] = self.review('solicitudes')
        S.approve(self.manager, req.pk, Batch.objects.get(product=self.prod_a, lot='L2').pk, tok())
        a = runner.decide(self.manager, p.pk, True)
        self.assertEqual(a.status, 'superseded')
        self.assertEqual(Reservation.objects.count(), 1)
        # Al recalcular, lo que perdió su base queda «ya no aplica».
        S.request_stock(self.requester, self.prod_b, 2, 'Mostrador', tok())
        [p2] = self.review('solicitudes')
        SupplyRequest.objects.filter(pk=p2.payload['request_id']).update(status='cancelled')
        self.review('solicitudes')
        self.assertEqual(AgentAction.objects.get(pk=p2.pk).status, 'superseded')


class Bank(AgentBase):
    def test_statement_event_proposes_and_applies_payment_with_receipt(self):
        sale = self.sale(self.ferreteria, [(self.prod_a, 2, None, '')])
        row = [TODAY().strftime('%d/%m/%Y'), f'PAGO {sale.fiscal_name} FERRETERIA', 'TRF-1', '12345678', 'FERRETERIA EL TORNILLO', '200.000,00', '1.000.000,00']
        with self.captureOnCommitCallbacks(execute=True):
            self.statement([row])
        [p] = AgentAction.objects.filter(agent='conciliacion', status='proposed')
        self.assertEqual((p.kind, p.confidence, p.benefit), ('apply_bank', 'Alta', Decimal('200000.00')))
        self.assertIn(sale.fiscal_name, p.detail)
        with self.captureOnCommitCallbacks(execute=True):
            a = runner.decide(self.finance, p.pk, True)
        self.assertEqual(a.status, 'executed', a.result)
        rec = with_balance(Receivable.objects.filter(sale=sale)).get()
        self.assertEqual(rec.balance, Decimal('0.00'))
        self.assertEqual(BankTransaction.objects.get().status, 'applied')
        self.assertEqual(PaymentApplication.objects.filter(receivable=rec).count(), 1)
        self.assertEqual(self.review('conciliacion'), [])


class Suppliers(AgentBase):
    def setUp(self):
        super().setUp()
        self.vendor = Supplier.objects.create(name='Proveedor Andino S.A.S.', nit='900123456', payment_days=120)
        DiscountTier.objects.bulk_create([DiscountTier(supplier=self.vendor, days=30, percent=Decimal('5')), DiscountTier(supplier=self.vendor, days=60, percent=Decimal('3'))])

    def invoice(self, number, days_ago, total='1000000'):
        issue = TODAY() - timedelta(days=days_ago)
        return Invoice.objects.create(supplier=self.vendor, number=number, issue_date=issue, due_date=issue + timedelta(days=120), subtotal=Decimal(total),
                                      total=Decimal(total), review=Invoice.Review.VALIDATED, terms=TERMS, validated_by=self.finance,
                                      validated_at=timezone.now(), created_by=self.finance)

    def test_prompt_payment_scheduled_before_discount_deadline(self):
        inv = self.invoice('LA-1', days_ago=20)        # el 5 % vence en 10 días
        self.invoice('LA-2', days_ago=100)             # fuera del horizonte: sin propuesta de descuento
        [p] = self.review('pronto_pago')
        self.assertEqual(p.benefit, Decimal('50000.00'))
        self.assertEqual(p.payload['scheduled_on'], (inv.issue_date + timedelta(days=28)).isoformat())
        with self.assertRaises(Exception):
            runner.decide(self.manager, p.pk, True)
        a = runner.decide(self.finance, p.pk, True)
        self.assertEqual(a.status, 'executed', a.result)
        pay = SupplierPayment.objects.get(status='scheduled')
        self.assertEqual((pay.amount, pay.allocations.get().discount), (Decimal('950000.00'), Decimal('50000.00')))
        self.assertEqual(self.review('pronto_pago'), [])

    def test_disabled_agent_skips_its_turn(self):
        self.invoice('LA-1', days_ago=20)
        AgentConfig.objects.create(key='pronto_pago', enabled=False)
        at = timezone.make_aware(datetime.combine(TODAY(), datetime.min.time()) + timedelta(hours=9))
        runs = runner.run_due(now=at)
        self.assertNotIn('pronto_pago', [r.agent for r in runs])
        self.assertFalse(AgentAction.objects.filter(agent='pronto_pago').exists())


class Collections(AgentBase):
    def test_reminder_prepared_with_contact_and_not_repeated_in_the_week(self):
        rec = Receivable.objects.create(party=self.rojas, source='opening', prefix='FE', number=900, issue_date=TODAY() - timedelta(days=60),
                                        due_date=TODAY() - timedelta(days=30), amount=Decimal('350000'), created_by=self.finance)
        PartyContact.objects.create(party=self.rojas, name='Camilo Rojas', role='collection', channel='chat', value='300 555 0142', preferred=True)
        [p] = self.review('cobranza')
        self.assertIn('FE-900', p.payload['message'])
        self.assertIn('$350.000', p.payload['message'])
        a = runner.decide(self.finance, p.pk, True)
        self.assertEqual(a.status, 'executed', a.result)
        # El envío es simulado: queda el texto en pantalla y un evento de auditoría; ningún enlace sale de la plataforma.
        self.assertEqual((a.result['state'], a.result['state_label'], a.result['external']), ('simulated', 'Envío simulado registrado', False))
        self.assertEqual(a.result['text'], p.payload['message'])
        self.assertEqual(a.result['link'], reverse('party', args=[self.rojas.pk]))
        self.assertNotIn('://', str(a.result))
        self.assertTrue(Audit.objects.filter(event='Envío simulado de recordatorio de cobro', actor=self.finance).exists())
        self.assertEqual(self.review('cobranza'), [])
        self.assertEqual(with_balance(Receivable.objects.filter(pk=rec.pk)).get().balance, Decimal('350000.00'))
        # El intento queda con envío registrado y la ventana abierta: se evaluará al cumplirse, no antes.
        attempt = CollectionAttempt.objects.get()
        self.assertEqual((attempt.sent, attempt.outcome, attempt.channel), (True, 'pending', 'chat'))

    def test_reminder_without_contact_is_only_prepared_and_is_not_evaluated(self):
        Receivable.objects.create(party=self.rojas, source='opening', prefix='FE', number=901, issue_date=TODAY() - timedelta(days=60),
                                  due_date=TODAY() - timedelta(days=30), amount=Decimal('350000'), created_by=self.finance)
        self.rojas.branches.update(einvoice_email='')
        [p] = self.review('cobranza')
        self.assertIn('Sin contacto de cobranza registrado', p.detail)
        a = runner.decide(self.finance, p.pk, True)
        self.assertEqual((a.status, a.result['state']), ('executed', 'prepared'))
        attempt = CollectionAttempt.objects.get()
        self.assertEqual((attempt.sent, attempt.outcome), (False, 'skipped'))
        self.assertFalse(Audit.objects.filter(event='Envío simulado de recordatorio de cobro').exists())


class Expiry(AgentBase):
    def test_expired_lot_written_off_only_free_units(self):
        b = Batch.objects.create(product=self.prod_a, lot='OLD', expiry=TODAY() - timedelta(days=3), location='Bodega 1', physical=7, reserved=2)
        [p] = self.review('vencimientos')
        self.assertTrue(p.title.startswith('Dar de baja 5 unidades de Referencia A por vencimiento'), p.title)
        a = runner.decide(self.manager, p.pk, True)
        self.assertEqual(a.status, 'executed', a.result)
        b.refresh_from_db()
        self.assertEqual((b.physical, b.reserved), (2, 2))
        self.assertEqual(Movement.objects.get(batch=b, kind='writeoff').loss_reason, 'expiry')
        self.assertEqual(self.review('vencimientos'), [])


class Schedule(AgentBase):
    def test_turns_run_once_per_period_and_daily_agents_wait_for_their_hour(self):
        early = timezone.make_aware(datetime.combine(TODAY(), datetime.min.time()) + timedelta(hours=5, minutes=10))
        first = {r.agent for r in runner.run_due(now=early)}
        self.assertEqual(first, {'conciliacion', 'solicitudes', 'contable', 'avisos'})
        self.assertEqual([r.agent for r in runner.run_due(now=early + timedelta(minutes=1))], [])   # mismo turno: nada se repite
        later = early + timedelta(hours=3)
        second = {r.agent for r in runner.run_due(now=later)}
        self.assertTrue({'resumen', 'vencimientos', 'pronto_pago', 'cobranza'} <= second)
        resumen = AgentRun.objects.get(agent='resumen')
        self.assertIn('se facturaron', resumen.summary)

    def test_pages_and_settings_by_role(self):
        S.request_stock(self.requester, self.prod_a, 1, 'Mostrador', tok())
        self.review('solicitudes')
        for user, status in ((self.owner, 200), (self.admin, 200), (self.finance, 200), (self.manager, 200), (self.requester, 403), (self.billing, 403)):
            self.assertEqual(self.login(user).get(reverse('agents')).status_code, status, user.role)
        titles = lambda page: [item.title for group in page.context['groups'] for item in group['items']]
        page = self.login(self.manager).get(reverse('agents'))
        self.assertEqual(len(titles(page)), 1)
        self.assertTrue(titles(page)[0].startswith('Reservar 1 unidad de Referencia A'), titles(page))
        self.assertContains(page, 'Reservar 1 unidad de Referencia A')
        self.assertEqual(titles(self.login(self.finance).get(reverse('agents'))), [])       # contabilidad no ve propuestas de inventario
        home = self.login(self.manager).get(reverse('home'))
        self.assertEqual((home.context['agents_pending'], home.context['agents_strip']['pending']), (1, 1))
        resp = self.login(self.manager).post(reverse('agents_decide'), {'accion': 'aprobar_seleccion', 'seleccion': [str(AgentAction.objects.get().pk)]})
        self.assertRedirects(resp, reverse('agents'))
        self.assertEqual(AgentAction.objects.get().status, 'executed')
        self.assertEqual(self.login(self.finance).post(reverse('agents_configure', args=['cobranza']), {'activo': 'on', 'dias_minimos': '5'}).status_code, 403)
        self.login(self.owner).post(reverse('agents_configure', args=['cobranza']), {'dias_minimos': '5', 'repetir_cada_dias': '3'})
        cfg = AgentConfig.objects.get(key='cobranza')
        self.assertEqual((cfg.enabled, cfg.params), (False, {'dias_minimos': 5, 'repetir_cada_dias': 3}))
        self.assertEqual(self.login(self.manager).post(reverse('agents_run', args=['solicitudes'])).status_code, 302)
        self.assertEqual(self.login(self.finance).post(reverse('agents_run', args=['solicitudes'])).status_code, 403)


class Replenishment(LearningBase):
    """Propuestas de pedido: presupuesto, recálculo al aprobar y propuestas que perdieron su base."""

    def review(self):
        run = runner.run_agent('reposicion', 'manual')
        self.assertEqual(run.status, 'succeeded', run.error)
        return run, list(AgentAction.objects.filter(agent='reposicion', status='proposed'))

    def budget(self, value):
        cfg = runner.config('reposicion')
        cfg.params = {**cfg.params, 'presupuesto': value}
        cfg.save()

    def test_budget_cuts_the_proposal_by_pack_and_committed_orders_consume_it(self):
        p, batch = self.weekly_product()
        self.lead_time_invoice(p)                       # costo unitario 1.000; sugerencia sin tope: 40 unidades
        self.budget(25000)
        run, [prop] = self.review()
        self.assertIn('20 Referencia C', prop.title)                       # 25.000 alcanzan para dos empaques de 10
        self.assertEqual((prop.amount, prop.payload['quantities']), (Decimal('20000.00'), {str(p.pk): 20}))
        self.assertIn('recortado de 40 por presupuesto', prop.detail)
        self.assertIn('Presupuesto de compra disponible: $25.000', prop.detail)
        self.assertEqual((run.metrics['por_fuera_presupuesto'], run.metrics['presupuesto']), (1, '25000.00'))
        self.assertIn('1 recortada o por fuera del presupuesto de $25.000', run.summary)
        a = runner.decide(self.manager, prop.pk, True)
        self.assertEqual(a.status, 'executed', a.result)
        order = PurchaseOrder.objects.get()
        self.assertEqual((order.units, order.estimated_cost), (20, Decimal('20000.00')))
        self.assertIn('Recortada de 40 a 20 por presupuesto', order.lines.get().reason)
        # El pedido ya preparado consume el presupuesto de esta revisión: quedan 5.000 y no alcanza para otro empaque.
        self.assertEqual(R.committed_cost(), Decimal('20000.00'))
        run, props = self.review()
        self.assertEqual((props, run.metrics['por_fuera_presupuesto']), ([], 1))
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_approval_recalculates_the_quantity_with_todays_data(self):
        p, batch = self.weekly_product()
        self.lead_time_invoice(p)
        _, [prop] = self.review()
        self.assertEqual(prop.payload['quantities'], {str(p.pk): 40})
        # Entre la propuesta y la aprobación llegan 10 unidades: sigue haciendo falta pedir, pero menos.
        S.adjust_in(self.manager, p, 'LC8', TODAY() + timedelta(days=300), 'Bodega 1', 10, 'adjust_in', 'Conteo', tok())
        a = runner.decide(self.manager, prop.pk, True)
        self.assertEqual(a.status, 'executed', a.result)
        self.assertTrue(a.result['recalculated'])
        self.assertIn('se recalcularon con los datos de hoy', a.result['message'])
        self.assertEqual(PurchaseOrder.objects.get().units, 30)             # no las 40 de la propuesta
        with self.assertRaisesMessage(ValidationError, 'ya fue decidida'):
            runner.decide(self.manager, prop.pk, True)
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_proposal_no_longer_applies_when_the_budget_stops_covering_it(self):
        p, batch = self.weekly_product()
        self.lead_time_invoice(p)
        _, [prop] = self.review()
        self.budget(5000)                                                   # la dirección baja el tope antes de la aprobación
        a = runner.decide(self.manager, prop.pk, True)
        self.assertEqual(a.status, 'superseded')
        self.assertIn('presupuesto de compra disponible ya no alcanza', a.result['message'])
        self.assertFalse(PurchaseOrder.objects.exists())
        # Y si llega la mercancía, al volver a revisar la propuesta abierta queda «ya no aplica» sin que nadie la toque.
        self.budget(0)
        _, [prop2] = self.review()
        S.adjust_in(self.manager, p, 'LC9', TODAY() + timedelta(days=300), 'Bodega 1', 200, 'adjust_in', 'Llegó mercancía', tok())
        _, props = self.review()
        self.assertEqual(props, [])
        self.assertEqual(AgentAction.objects.get(pk=prop2.pk).status, 'superseded')


class DetailAndHistory(AgentBase):
    """Detalle de una propuesta, decisión desde el detalle e historial filtrable."""

    def proposal(self, quantity=3):
        req = S.request_stock(self.requester, self.prod_a, quantity, 'Despacho obra norte', tok())
        p = AgentAction.objects.filter(agent='solicitudes', status='proposed', payload__request_id=str(req.pk)).first()
        if p is None:
            runner.run_agent('solicitudes', 'manual')
            p = AgentAction.objects.get(agent='solicitudes', status='proposed', payload__request_id=str(req.pk))
        return req, p

    def test_detail_shows_stage_evidence_and_respects_roles(self):
        req, p = self.proposal()
        lot = Batch.objects.get(product=self.prod_a, lot='L1')
        page = self.login(self.manager).get(reverse('agent_action', args=[p.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertEqual([s['state'] for s in page.context['track']['steps']], ['is-done', 'is-done', 'is-done', 'is-current', '', ''])
        self.assertEqual([s['label'] for s in page.context['track']['steps']], ['Detectar', 'Analizar', 'Proponer', 'Revisar', 'Ejecutar', 'Registrar'])
        self.assertTrue(page.context['can_decide'])
        urls = [e['url'] for e in page.context['evidence']]
        self.assertEqual(urls, [reverse('approve', args=[req.pk]), reverse('trace', args=[lot.pk])])
        self.assertContains(page, 'Primero vence, primero sale')
        self.assertContains(page, 'Aprobar y ejecutar')
        # La dirección la ve y la decide; contabilidad, facturación y solicitantes no ven a este agente.
        self.assertEqual(self.login(self.owner).get(reverse('agent_action', args=[p.pk])).status_code, 200)
        for user in (self.finance, self.billing, self.requester):
            self.assertEqual(self.login(user).get(reverse('agent_action', args=[p.pk])).status_code, 403, user.role)
        self.assertEqual(self.login(self.manager).get(reverse('agent_action', args=[999999])).status_code, 404)
        # Quien ve al agente pero no decide (contabilidad con reposición) abre el detalle sin botones.
        vendor = Supplier.objects.create(name='Proveedor Andino S.A.S.', nit='900123456')
        order = AgentAction.objects.create(agent='reposicion', kind='purchase_order', dedupe_key='pedido:x', title='Preparar pedido a Proveedor Andino',
                                           detail='Consumo semanal y entrega.', payload={'supplier_id': vendor.pk, 'products': [self.prod_a.pk]})
        page = self.login(self.finance).get(reverse('agent_action', args=[order.pk]))
        self.assertEqual((page.status_code, page.context['can_decide']), (200, False))
        self.assertContains(page, 'puede verla, pero no decidirla')
        self.assertNotContains(page, 'Aprobar y ejecutar')

    def test_stale_proposal_approved_from_detail_changes_nothing_and_says_why(self):
        req, p = self.proposal(quantity=2)
        S.approve(self.manager, req.pk, Batch.objects.get(product=self.prod_a, lot='L2').pk, tok())    # alguien la atendió a mano
        client = self.login(self.manager)
        response = client.post(reverse('agents_decide'), {'accion': f'aprobar:{p.pk}', 'volver': 'detalle', 'nota': 'Reviso'}, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse('agent_action', args=[p.pk]))
        p.refresh_from_db()
        self.assertEqual((p.status, p.result['message']), ('superseded', 'La solicitud ya fue atendida.'))
        self.assertEqual(Reservation.objects.count(), 1)
        self.assertContains(response, 'Ya no aplica: «Reservar 2 unidades de Referencia A')
        self.assertContains(response, 'La solicitud ya fue atendida. No se cambió ningún dato.')
        self.assertFalse(response.context['can_decide'])
        self.assertEqual([s['state'] for s in response.context['track']['steps']], ['is-done', 'is-done', 'is-done', 'is-done', '', 'is-done'])
        self.assertTrue(response.context['track']['steps'][4]['skipped'])

    def test_changed_availability_is_recalculated_and_explained(self):
        req, p = self.proposal(quantity=3)
        first = Batch.objects.get(product=self.prod_a, lot='L1')
        S.reserve(self.manager, first.pk, 4, 'Otra reserva', tok())                              # el lote propuesto ya no alcanza
        response = self.login(self.manager).post(reverse('agents_decide'), {'accion': f'aprobar:{p.pk}', 'volver': 'detalle'}, follow=True)
        p.refresh_from_db()
        self.assertEqual(p.status, 'executed', p.result)
        self.assertEqual(Reservation.objects.get(request=req).batch.lot, 'L2')
        self.assertContains(response, 'Reservado el lote L2')

    def test_double_submission_does_not_duplicate(self):
        req, p = self.proposal()
        client = self.login(self.manager)
        first = client.post(reverse('agents_decide'), {'accion': f'aprobar:{p.pk}'}, follow=True)
        self.assertContains(first, 'Reservado el lote L1')
        second = client.post(reverse('agents_decide'), {'accion': f'aprobar:{p.pk}', 'volver': 'detalle'}, follow=True)
        self.assertContains(second, 'ya estaba decidida (ejecutada por Manager')
        self.assertContains(second, 'No se repitió la operación.')
        self.assertEqual(Reservation.objects.count(), 1)
        self.assertEqual(Batch.objects.get(product=self.prod_a, lot='L1').reserved, 3)
        self.assertEqual(AgentRun.objects.get(pk=p.run_id).executed, 1)
        self.assertEqual(Audit.objects.filter(event='Propuesta de agente aprobada', target=f'agent-action-{p.pk}').count(), 1)
        # La misma propuesta dos veces en un solo envío tampoco se duplica.
        again = client.post(reverse('agents_decide'), {'accion': 'aprobar_seleccion', 'seleccion': [str(p.pk), str(p.pk)]}, follow=True)
        self.assertEqual(Reservation.objects.count(), 1)
        self.assertContains(again, 'ya estaba decidida')

    def test_history_is_filterable_and_limited_to_visible_agents(self):
        _, done = self.proposal()
        runner.decide(self.manager, done.pk, True, 'Entrega de la mañana')
        _, discarded = self.proposal(quantity=1)
        runner.decide(self.owner, discarded.pk, False, 'No procede')
        Receivable.objects.create(party=self.rojas, source='opening', prefix='FE', number=900, issue_date=TODAY() - timedelta(days=60),
                                  due_date=TODAY() - timedelta(days=30), amount=Decimal('350000'), created_by=self.finance)
        PartyContact.objects.create(party=self.rojas, name='Camilo Rojas', role='collection', channel='chat', value='300 555 0142', preferred=True)
        [reminder] = self.review('cobranza')
        runner.decide(self.finance, reminder.pk, True)
        client = self.login(self.owner)
        page = client.get(reverse('agents_history'))
        self.assertEqual(page.status_code, 200)
        self.assertEqual({a.pk for a in page.context['page']}, {done.pk, discarded.pk, reminder.pk})
        self.assertContains(page, 'Entrega de la mañana')
        self.assertContains(page, 'Reservado el lote L1')

        def ids(query):
            return [a.pk for a in client.get(reverse('agents_history') + query).context['page']]

        self.assertEqual(ids('?estado=rejected'), [discarded.pk])
        self.assertEqual(set(ids('?agente=solicitudes')), {done.pk, discarded.pk})
        self.assertEqual(ids(f'?quien={self.manager.pk}'), [done.pk])
        self.assertEqual(ids('?q=Camilo'), [reminder.pk])
        self.assertEqual(ids(f'?desde={TODAY() + timedelta(days=1)}'), [])
        self.assertEqual(len(ids(f'?desde={TODAY()}&hasta={TODAY()}')), 3)
        self.assertContains(client.get(reverse('agents_history') + '?q=nada-coincide'), 'Ninguna decisión coincide con estos filtros')
        # Contabilidad solo ve las decisiones de sus agentes; inventario, las de los suyos.
        self.assertEqual([a.pk for a in self.login(self.finance).get(reverse('agents_history')).context['page']], [reminder.pk])
        self.assertEqual({a.pk for a in self.login(self.manager).get(reverse('agents_history')).context['page']}, {done.pk, discarded.pk})
        for user in (self.requester, self.billing):
            self.assertEqual(self.login(user).get(reverse('agents_history')).status_code, 403)

    def test_agents_page_shows_each_agent_with_trigger_evidence_and_next_action(self):
        self.proposal()
        runner.run_agent('solicitudes', 'manual')
        runner.run_agent('resumen', 'manual')
        page = self.login(self.owner).get(reverse('agents'))
        cards = {c['agent'].key: c for c in page.context['cards']}
        self.assertEqual(len(cards), 9)
        self.assertEqual(cards['solicitudes']['trigger'], 'Cada 30 minutos y cuando se crea una solicitud')
        self.assertEqual(cards['solicitudes']['next_action'], 'Decidir 1 propuesta.')
        self.assertEqual(cards['solicitudes']['metrics'], [{'label': 'Solicitudes listas para reservar', 'value': 1}, {'label': 'Solicitudes sin existencias suficientes', 'value': 0}])
        self.assertTrue(all(c['sources'] and c['agent'].purpose for c in cards.values()))
        self.assertIn('Actúa sola', cards['contable']['next_action'])
        self.assertContains(page, 'Evidencias consultadas')
        self.assertContains(page, 'Las ventas y el recaudo son del día anterior')
        self.assertContains(page, 'Facturado el día anterior, sin IVA')
        # Inventario no recibe enlaces a módulos de contabilidad entre las evidencias.
        html = self.login(self.manager).get(reverse('agents')).content.decode()
        for name in ('bank', 'portfolio', 'payables', 'results'):
            self.assertNotIn(f'href="{reverse(name)}"', html)
