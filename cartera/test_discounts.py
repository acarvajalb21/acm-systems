"""Jerarquía dirección–administración, permisos mensuales de descuento y resultados de la dirección (datos sintéticos)."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from inventory.models import User, Supplier, Invoice, InvoiceLine, Batch, Movement
from . import discounts as D, services as C
from .models import Sale, SaleDiscount, DiscountRequest, DiscountGrant
from .results import report, parse_month
from .tests import Base, PASSWORD, tok, TODAY


class DiscountBase(Base):
    def setUp(self):
        super().setUp()
        self.admin = self.users['admin']
        self.owner = User.objects.create_user('owner', password=PASSWORD, role='owner', must_change_password=False, first_name='Dirección')

    def draft(self, qty=2, user=None):
        return C.save_sale(user or self.billing, None, self.header(self.ferreteria), [{'product': self.prod_a, 'quantity': qty}], tok())

    def grant(self, user, sale, values=None, percent=None, reason='Acuerdo de despacho'):
        return D.grant_discount(user, sale.pk, values or {}, percent, reason, tok())

    def issue(self, sale):
        with self.captureOnCommitCallbacks(execute=True):
            C.issue_sale(self.finance, sale.pk, tok())
        return Sale.objects.get(pk=sale.pk)


class Quota(DiscountBase):
    def test_sales_are_registered_at_list_price_and_only_admins_discount(self):
        sale = self.draft()
        self.assertEqual(sale.total, Decimal('200000.00'))
        for user in (self.billing, self.finance, self.users['manager']):
            with self.assertRaises(PermissionDenied):
                self.grant(user, sale, percent='10')

    def test_monthly_permits_then_owner_confirmation(self):
        self.admin.discount_quota = 2
        self.admin.save()
        a, b, c = self.draft(), self.draft(), self.draft()
        outcome, d = self.grant(self.admin, a, percent='10')
        self.assertEqual((outcome, d.amount, d.consumed), ('applied', Decimal('20000.00'), True))
        self.assertEqual(Sale.objects.get(pk=a.pk).total, Decimal('180000.00'))
        # Ajustar el descuento de la misma venta no gasta otro permiso.
        outcome, d = self.grant(self.admin, a, values={str(a.lines.get().pk): '30000'})
        self.assertEqual((outcome, d.consumed, D.quota_status(self.admin)['used']), ('applied', False, 1))
        self.assertEqual(SaleDiscount.objects.filter(sale=a, status='active').count(), 1)
        self.grant(self.admin, b, percent='5')
        q = D.quota_status(self.admin)
        self.assertEqual((q['used'], q['available']), (2, 0))
        # Sin permisos: queda en espera de la dirección y la venta no se puede emitir.
        outcome, req = self.grant(self.admin, c, percent='15')
        self.assertEqual((outcome, req.status, req.amount), ('pending', 'pending', Decimal('30000.00')))
        self.assertEqual(Sale.objects.get(pk=c.pk).total, Decimal('200000.00'))
        with self.assertRaisesMessage(ValidationError, 'confirmación de la dirección'):
            C.issue_sale(self.finance, c.pk, tok())
        with self.assertRaises(PermissionDenied):
            D.decide_request(self.admin, req.pk, True, '', tok())
        D.decide_request(self.owner, req.pk, True, 'Cliente institucional', tok())
        c = Sale.objects.get(pk=c.pk)
        self.assertEqual(c.total, Decimal('170000.00'))
        d = D.active_discount(c)
        self.assertEqual((d.applied_by, d.approved_by, d.consumed), (self.admin, self.owner, False))
        self.assertEqual(D.quota_status(self.admin)['used'], 2)
        self.assertEqual(self.issue(c).status, Sale.Status.ISSUED)

    def test_owner_rejects_and_sale_stays_at_list_price(self):
        sale = self.draft()
        _, req = self.grant(self.admin, sale, percent='20')
        D.decide_request(self.owner, req.pk, False, 'No aplica', tok())
        self.assertEqual(DiscountRequest.objects.get(pk=req.pk).status, 'rejected')
        sale = self.issue(sale)
        self.assertEqual((sale.status, sale.total), (Sale.Status.ISSUED, Decimal('200000.00')))
        self.assertTrue(any('rechazó' in a[2] for a in D.alerts(self.admin)))

    def test_owner_discounts_without_permits(self):
        sale = self.draft()
        outcome, d = self.grant(self.owner, sale, percent='50')
        self.assertEqual((outcome, d.consumed, d.approved_by), ('applied', False, self.owner))
        self.assertEqual(self.issue(sale).total, Decimal('100000.00'))

    def test_more_permits_requested_and_granted(self):
        req = D.request_permits(self.admin, 5, 'Pedido empresarial', tok())
        with self.assertRaisesMessage(ValidationError, 'Ya tiene una solicitud'):
            D.request_permits(self.admin, 2, 'Otra', tok())
        D.decide_request(self.owner, req.pk, True, '', tok(), quantity=3)
        q = D.quota_status(self.admin)
        self.assertEqual((q['extra'], q['available']), (3, 3))
        self.assertEqual(DiscountRequest.objects.get(pk=req.pk).granted, 3)
        D.set_quota(self.owner, self.admin.pk, 20, tok())
        self.admin.refresh_from_db()
        self.assertEqual(D.quota_status(self.admin)['available'], 23)
        with self.assertRaises(PermissionDenied):
            D.set_quota(self.admin, self.admin.pk, 99, tok())
        with self.assertRaisesMessage(ValidationError, 'administración'):
            D.set_quota(self.owner, self.finance.pk, 5, tok())

    def test_permits_renew_each_month(self):
        self.admin.discount_quota = 1
        self.admin.save()
        last = D.month_start() - timedelta(days=1)
        with mock.patch('cartera.discounts.month_start', return_value=last.replace(day=1)):
            self.grant(self.admin, self.draft(), percent='10')
            self.assertEqual(D.quota_status(self.admin, last.replace(day=1))['available'], 0)
        self.assertEqual(D.quota_status(self.admin)['available'], 1)

    def test_editing_or_cancelling_voids_discount_and_request(self):
        self.admin.discount_quota = 1
        self.admin.save()
        a, b = self.draft(), self.draft()
        self.grant(self.admin, a, percent='10')
        _, req = self.grant(self.admin, b, percent='10')
        a = C.save_sale(self.billing, a.pk, self.header(self.ferreteria), [{'product': self.prod_a, 'quantity': 3}], tok())
        self.assertEqual((a.total, D.active_discount(a)), (Decimal('300000.00'), None))
        self.assertEqual(SaleDiscount.objects.get(sale=a).status, 'removed')
        C.cancel_sale(self.billing, b.pk, 'Cliente desistió', tok())
        self.assertEqual(DiscountRequest.objects.get(pk=req.pk).status, 'void')
        # Volver a conceder sobre la venta modificada no gasta un permiso nuevo.
        outcome, d = self.grant(self.admin, a, percent='10')
        self.assertEqual((outcome, d.consumed), ('applied', False))

    def test_discount_cannot_exceed_line_and_needs_reason(self):
        sale = self.draft()
        with self.assertRaisesMessage(ValidationError, 'supera el valor'):
            self.grant(self.owner, sale, values={str(sale.lines.get().pk): '999999'})
        with self.assertRaisesMessage(ValidationError, 'motivo'):
            self.grant(self.owner, sale, percent='5', reason=' ')
        with self.assertRaisesMessage(ValidationError, 'no tiene descuento'):
            D.remove_discount(self.owner, sale.pk, tok())


class Hierarchy(DiscountBase):
    def login(self, user):
        c = Client()
        c.force_login(user)
        session = c.session
        session['user_version'] = user.session_version
        session.save()
        return c

    def test_admin_cannot_manage_owner_or_admin_accounts(self):
        other_admin = User.objects.create_user('admin2', password=PASSWORD, role='admin', must_change_password=False)
        c = self.login(self.admin)
        page = c.get(reverse('new_user'))
        roles = [v for v, _ in page.context['form'].fields['role'].choices]
        self.assertNotIn('owner', roles)
        self.assertNotIn('admin', roles)
        self.assertEqual(c.get(reverse('reset_password', args=[self.owner.pk])).status_code, 403)
        self.assertEqual(c.get(reverse('toggle_user', args=[other_admin.pk, 'deactivate'])).status_code, 403)
        self.assertEqual(c.get(reverse('reset_password', args=[self.finance.pk])).status_code, 200)
        o = self.login(self.owner)
        roles = [v for v, _ in o.get(reverse('new_user')).context['form'].fields['role'].choices]
        self.assertIn('owner', roles)
        self.assertEqual(o.get(reverse('reset_password', args=[self.admin.pk])).status_code, 200)

    def test_pages_by_role(self):
        sale = self.draft()
        o, a, b = self.login(self.owner), self.login(self.admin), self.login(self.billing)
        self.assertTemplateUsed(o.get(reverse('home')), 'home.html')
        results = o.get(reverse('results'))
        self.assertTemplateUsed(results, 'cartera/results.html')
        self.assertIn('r', results.context)
        # Las coordenadas del gráfico usan punto decimal aunque la interfaz esté en español.
        self.assertNotRegex(results.content.decode(), r'(?:y1|x|width|height)="\d+,\d')
        self.assertTemplateUsed(a.get(reverse('home')), 'home.html')
        for client in (o, a):
            self.assertEqual(client.get(reverse('discounts')).status_code, 200)
            self.assertEqual(client.get(reverse('sale_discount', args=[sale.pk])).status_code, 200)
        self.assertEqual(b.get(reverse('discounts')).status_code, 403)
        self.assertEqual(b.get(reverse('sale_discount', args=[sale.pk])).status_code, 403)
        self.assertFalse(b.get(reverse('sale', args=[sale.pk])).context['can_discount'])        # facturación no concede descuentos
        self.assertNotIn('discount', b.get(reverse('sale_new')).context['lines'].forms[0].fields)   # ni los digita en la venta
        # La dirección ve el pedido y lo decide desde su pantalla.
        resp = a.post(reverse('sale_discount', args=[sale.pk]), {'token': str(tok()), 'porcentaje': '10', 'motivo': 'Acuerdo comercial'})
        self.assertRedirects(resp, reverse('sale', args=[sale.pk]))
        req = DiscountRequest.objects.get(sale=sale)
        self.assertEqual(list(o.get(reverse('discounts')).context['pending']), [req])
        self.assertEqual(o.post(reverse('discount_decide', args=[req.pk]), {'token': str(tok()), 'decision': 'aprobar'}).status_code, 302)
        self.assertEqual(Sale.objects.get(pk=sale.pk).total, Decimal('180000.00'))
        self.assertEqual(a.post(reverse('discount_quota', args=[self.admin.pk]), {'token': str(tok()), 'cupo': '50'}).status_code, 403)
        self.assertEqual(o.post(reverse('discount_quota', args=[self.admin.pk]), {'token': str(tok()), 'cupo': '20'}).status_code, 302)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.discount_quota, 20)


class OwnerYearAndDebts(DiscountBase):
    def test_year_view_and_debts(self):
        from django.db.models import Sum
        from .models import Receivable, with_balance
        from .results import year_report, debts_block
        self.issue(self.draft())
        self.issue(self.draft(qty=3))
        Receivable.objects.create(party=self.rojas, source='opening', prefix='FE', number=77, issue_date=TODAY() - timedelta(days=400),
                                  due_date=TODAY() - timedelta(days=370), amount=Decimal('125000'), created_by=self.finance)
        year = TODAY().year
        y = year_report(year)
        issued = Sale.objects.filter(status='issued', issue_date__year=year)
        self.assertEqual(y['total'], issued.aggregate(s=Sum('subtotal'))['s'])
        self.assertEqual(len(y['months']), 12)
        self.assertEqual(y['best']['revenue'], max(m['revenue'] for m in y['months']))
        self.assertTrue(all(m['revenue'] == 0 for m in y['months'] if m['future']))
        prev = Sale.objects.filter(status='issued', issue_date__year=year - 1).aggregate(s=Sum('subtotal'))['s']
        self.assertEqual(y['prev_total'], prev or Decimal(0))
        if y['prev_same']:
            self.assertEqual(y['growth'], ((y['same'] - y['prev_same']) * 100 / y['prev_same']).quantize(Decimal('0.1')))
        self.assertEqual(year_report(year - 1)['next_year'], year)
        d = debts_block()
        open_total = sum(r.balance for r in with_balance(Receivable.objects.all()) if r.balance > 0)
        self.assertEqual(d['ar']['total'], open_total)
        self.assertEqual(sum(s['amount'] for s in d['ar']['summary']), d['ar']['total'])
        self.assertEqual(sum(e['total'] for e in d['ar']['entities']) + Decimal(0), d['ar']['total'] if not d['ar']['more'] else sum(e['total'] for e in d['ar']['entities']))
        for e in d['ar']['entities']:
            self.assertEqual(sum(doc['balance'] for doc in e['docs']), e['total'])
        page = Hierarchy.login(self, self.owner).get(reverse('results') + f'?anio={year}')
        self.assertEqual((page.status_code, page.context['y']['year'], page.context['debts']['ar']['total']), (200, year, open_total))


class OwnerResults(DiscountBase):
    def test_gross_profit_losses_and_portfolio(self):
        vendor = Supplier.objects.create(name='Proveedor Andino S.A.S.', nit='900123456', payment_days=60)
        inv = Invoice.objects.create(supplier=vendor, number='LA-1', issue_date=TODAY() - timedelta(days=5), due_date=TODAY() + timedelta(days=55),
                                     subtotal=Decimal('400000'), total=Decimal('400000'), review=Invoice.Review.VALIDATED, terms={},
                                     validated_by=self.finance, validated_at=timezone.now(), created_by=self.finance)
        InvoiceLine.objects.create(invoice=inv, product=self.prod_a, description='Referencia A', quantity=10, unit_price=Decimal('40000'))
        sale = self.draft(qty=3)
        self.grant(self.owner, sale, percent='10')
        sale = self.issue(sale)
        batch = Batch.objects.get(product=self.prod_a, lot='L1')
        Batch.objects.filter(pk=batch.pk).update(physical=1)
        Movement.objects.create(batch=batch, quantity=-1, kind='writeoff', loss_reason='expiry', actor=self.users['manager'])
        r = report(parse_month(''))
        self.assertEqual(r['sales']['revenue'], Decimal('270000.00'))
        self.assertEqual(r['sales']['cogs'], Decimal('120000.00'))
        self.assertEqual(r['sales']['profit'], Decimal('150000.00'))
        self.assertEqual(r['sales']['discounts'], Decimal('30000.00'))
        self.assertEqual(r['inventory_losses'], Decimal('40000.00'))
        self.assertEqual(r['result'], Decimal('110000.00'))
        self.assertEqual([l['label'] for l in r['leaks']], ['Bajas por vencimiento', 'Descuentos concedidos a clientes'])
        self.assertEqual((r['ar_total'], r['ar_overdue']), (Decimal('270000.00'), Decimal('0.00')))
        self.assertEqual(r['owe'], Decimal('400000.00'))
        self.assertEqual(len(r['trend']), 6)
        self.assertTrue(r['chart']['cols'][-1]['sales'].startswith('M'))
        self.assertEqual(parse_month('2099-01'), TODAY().replace(day=1))
