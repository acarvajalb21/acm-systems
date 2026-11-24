import csv
from decimal import Decimal, InvalidOperation
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError
from django.db.models import Count, Q, Sum
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from inventory import services as S
from inventory.payables import outflow_candidates
from inventory.forms import TokenForm, ReasonForm
from inventory.views import roles, run_form
from contable import services as contable
from contable.models import Submission, CatalogItem
from . import forms as F, services as C, discounts
from .models import (Party, PriceList, PriceItem, Sale, Receivable, BankAccount, StatementImport, BankTransaction, PaymentApplication,
                     PayerLink, Collection, Voucher, with_balance, ZERO)


def _post(request, action, success, fallback):
    """Acciones de un botón con token: muestran el error sin perder la pantalla."""
    form = TokenForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'La solicitud venció. Vuelva a intentarlo.')
        return redirect(fallback)
    try:
        action(form.cleaned_data['token'])
    except ValidationError as e:
        messages.error(request, ' '.join(e.messages))
        return redirect(fallback)
    except (IntegrityError, OperationalError):
        messages.error(request, 'La operación cambió mientras se guardaba o la base está ocupada. Revise y vuelva a intentar.')
        return redirect(fallback)
    if success:
        messages.success(request, success)
    return redirect(fallback)


# ------------------------------------------------------------------ terceros

@roles(*S.SALES)
def parties(request):
    q = request.GET.get('q', '').strip()
    rows = Party.objects.annotate(n_sales=Count('sales', filter=~Q(sales__status='cancelled')))
    if q:
        rows = rows.filter(Q(name__icontains=q) | Q(identification__icontains=q))
    balances = {}
    for r in C.open_receivables():
        balances[r.party_id] = balances.get(r.party_id, ZERO) + r.balance
    rows = list(rows[:300])
    for p in rows:
        p.balance = balances.get(p.pk, ZERO)
    return render(request, 'cartera/parties.html', {'parties': rows, 'q': q, 'title': 'Clientes'})


def _party_initial(p):
    branch = p.branches.filter(office=0).first()
    return {'id_type': p.id_type, 'identification': p.identification, 'name': p.name, 'kind': p.kind, 'default_context': p.default_context,
            'price_list': p.price_list_id, 'credit_days': p.credit_days, 'requires_po': p.requires_po, 'notes': p.notes, 'active': p.active,
            'einvoice_email': getattr(branch, 'einvoice_email', ''), 'address': getattr(branch, 'address', ''), 'city': getattr(branch, 'city', '')}


@roles(*S.SALES)
def party_form(request, pk=None):
    party = get_object_or_404(Party, pk=pk) if pk else None
    form = F.PartyForm(request.POST or None, initial=_party_initial(party) if party else None)
    back = reverse('party', args=[pk]) if pk else reverse('parties')
    return run_form(request, form, lambda d: C.save_party(request.user, party, d, d['token']), 'Editar cliente' if party else 'Nuevo cliente',
                    lambda p: reverse('party', args=[p.pk]), {'back_url': back, 'done': 'Cliente guardado.', 'submit': 'Guardar cliente',
                                                               'intro': 'Una identidad fiscal por cliente. El contexto (mostrador, empresa o contratista) se elige en cada venta.'})


@roles(*S.SALES)
def party_detail(request, pk):
    party = get_object_or_404(Party.objects.select_related('price_list'), pk=pk)
    finance = request.user.role in S.FINANCE
    receivables = list(with_balance(party.receivables.select_related('sale')).order_by('-issue_date', '-number'))
    open_rows = [r for r in receivables if r.balance > 0]
    buckets = C.aging(open_rows)
    buckets['old'] = buckets['d90'] + buckets['over']
    for r in receivables:
        r.paid = r.amount - r.balance
    applications = PaymentApplication.objects.select_related('receivable', 'collection', 'voucher').filter(receivable__party=party).order_by('-created_at')[:40]
    pending = []
    if finance:
        for txn in BankTransaction.objects.select_related('payer').filter(status=BankTransaction.Status.PENDING)[:200]:
            if any(c['party'].pk == party.pk for c in C.suggestions(txn)['candidates']):
                pending.append(txn)
    links = party.payer_links.select_related('payer').exclude(status='rejected')
    return render(request, 'cartera/party_detail.html', {
        'party': party, 'branch': party.branches.filter(office=0).first(), 'receivables': receivables, 'buckets': buckets,
        'total': sum((r.balance for r in open_rows), ZERO), 'overdue': sum((r.balance for r in open_rows if r.days_late > 0), ZERO),
        'applications': applications, 'pending': pending, 'links': links, 'contacts': party.contacts.all(),
        'sales': party.sales.exclude(status='cancelled').order_by('-created_at')[:15], 'finance': finance, 'title': party.name,
        'last_statement': StatementImport.objects.filter(status='confirmed').order_by('-confirmed_at').first(),
        'token_form': TokenForm(), 'contact_form': F.ContactForm(), 'today': timezone.localdate()})


@roles(*S.SALES)
def contact_new(request, pk):
    party = get_object_or_404(Party, pk=pk)
    form = F.ContactForm(request.POST or None)
    return run_form(request, form, lambda d: C.add_contact(request.user, party.pk, d, d['token']), f'Contacto de cobranza · {party.name}',
                    reverse('party', args=[pk]), {'back_url': reverse('party', args=[pk]), 'done': 'Contacto agregado.', 'submit': 'Agregar contacto'})


@roles(*S.SALES)
@require_POST
def contact_remove(request, pk, contact):
    return _post(request, lambda t: C.remove_contact(request.user, contact, t), 'Contacto eliminado.', reverse('party', args=[pk]))


@roles(*S.SALES)
def party_statement(request, pk):
    party = get_object_or_404(Party, pk=pk)
    rows = with_balance(party.receivables.all()).filter(balance__gt=0).order_by('due_date')
    today = timezone.localdate()
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="estado-de-cuenta-{party.identification}-{today:%Y%m%d}.csv"'
    response.write('﻿')
    w = csv.writer(response, delimiter=';')
    w.writerow([f'Estado de cuenta de {party.name}', f'{party.get_id_type_display()} {party.identification}', f'Corte {today:%d/%m/%Y}'])
    w.writerow(['Factura', 'Emisión', 'Vencimiento', 'Días vencida', 'Valor', 'Abonos y ajustes', 'Saldo'])
    total = ZERO
    for r in rows:
        late = max((today - r.due_date).days, 0)
        w.writerow([r.document, f'{r.issue_date:%d/%m/%Y}', f'{r.due_date:%d/%m/%Y}', late, f'{r.amount:.2f}'.replace('.', ','),
                    f'{r.amount - r.balance:.2f}'.replace('.', ','), f'{r.balance:.2f}'.replace('.', ',')])
        total += r.balance
    w.writerow(['Total', '', '', '', '', '', f'{total:.2f}'.replace('.', ',')])
    S.event(request.user, 'Estado de cuenta descargado', f'party-{party.pk}', party.name)
    return response


@roles(*S.FINANCE)
@require_POST
def payer_remove(request, pk, link):
    return _post(request, lambda t: C.remove_payer(request.user, link, t), 'Pagador desvinculado.', reverse('party', args=[pk]))


# ------------------------------------------------------------------- precios

@roles(*S.SALES)
def prices(request):
    lists = PriceList.objects.filter(active=True).order_by('name')
    today = timezone.localdate()
    products = {}
    for item in PriceItem.objects.select_related('product', 'price_list').filter(valid_from__lte=today).order_by('product__name', 'price_list__name', '-valid_from'):
        row = products.setdefault(item.product_id, {'product': item.product, 'prices': {}})
        row['prices'].setdefault(item.price_list_id, item)
    rows = [{'product': r['product'], 'cells': [r['prices'].get(pl.pk) for pl in lists]} for r in products.values()]
    upcoming = PriceItem.objects.select_related('product', 'price_list').filter(valid_from__gt=today).order_by('valid_from')
    return render(request, 'cartera/prices.html', {'lists': lists, 'rows': rows, 'upcoming': upcoming, 'title': 'Listas de precios',
                                                   'can_edit': request.user.role in S.FINANCE})


@roles(*S.FINANCE)
def price_new(request):
    form = F.PriceForm(request.POST or None, initial={'price_list': request.GET.get('lista'), 'product': request.GET.get('referencia')})
    return run_form(request, form, lambda d: C.save_price(request.user, d['price_list'], d['product'], d['price'], d['valid_from'], d['token']),
                    'Registrar precio', reverse('prices'), {'back_url': reverse('prices'), 'done': 'Precio registrado.', 'submit': 'Guardar precio',
                                                           'note': 'Un precio ya usado en ventas no se modifica: registre una nueva fecha de vigencia.'})


# -------------------------------------------------------------------- ventas

@roles(*S.SALES)
def sales(request):
    tab = request.GET.get('vista', 'todas')
    q = request.GET.get('q', '').strip()
    qs = Sale.objects.select_related('party', 'created_by').order_by('-created_at')
    filters = {'borradores': Q(status__in=['draft', 'rejected']), 'pendientes': Q(status__in=['sending', 'uncertain']), 'emitidas': Q(status='issued')}
    counts = {k: Sale.objects.filter(v).count() for k, v in filters.items()}
    if tab in filters:
        qs = qs.filter(filters[tab])
    if q:
        qs = qs.filter(Q(party__name__icontains=q) | Q(party__identification__icontains=q) | Q(fiscal_name__icontains=q))
    today = timezone.localdate()
    day = Sale.objects.filter(status='issued', issue_date=today)
    return render(request, 'cartera/sales.html', {'sales': qs[:200], 'tab': tab, 'q': q, 'counts': counts, 'title': 'Ventas y facturas',
                                                  'k_today': day.aggregate(s=Sum('total'))['s'] or ZERO, 'k_today_n': day.count(),
                                                  'can_issue': C.can_issue(request.user), 'environment': contable.environment()})


def _sale_header(sale):
    return {f: getattr(sale, f) for f in ('context', 'issue_date', 'seller_id', 'condition', 'payment_type_id', 'due_date', 'cash_method', 'cash_reference',
                                          'purchase_order', 'observations', 'beneficiary_name', 'beneficiary_document', 'beneficiary_relation', 'internal_note')} | {
        'party': sale.party_id, 'price_list': sale.price_list_id}


@roles(*S.SALES)
def sale_form(request, pk=None):
    sale = get_object_or_404(Sale, pk=pk) if pk else None
    if sale and not sale.editable:
        messages.info(request, 'Esta venta ya no se puede modificar.')
        return redirect('sale', pk=pk)
    Lines = F.sale_line_formset(extra=0 if sale else 3)
    initial = None
    if request.method != 'POST':
        if sale:
            initial = _sale_header(sale)
        else:
            party = Party.objects.filter(pk=request.GET.get('tercero')).first() if request.GET.get('tercero', '').isdigit() else None
            initial = {'party': party.pk if party else None, 'context': party.default_context if party else 'counter',
                       'price_list': (party.price_list_id or PriceList.objects.filter(context=party.default_context, active=True).values_list('pk', flat=True).first())
                       if party else PriceList.objects.filter(context='counter', active=True).values_list('pk', flat=True).first()}
    form = F.SaleForm(request.POST or None, initial=initial)
    line_initial = [{'product': l.product_id, 'quantity': l.quantity, 'unit_price': None if l.list_price == l.unit_price else l.unit_price,
                     'price_reason': l.price_reason, 'tax_rate': str(int(l.tax_rate)), 'prior_movement': l.prior_movement_id}
                    for l in sale.lines.all()] if sale else None
    lines = Lines(request.POST or None, prefix='lines', initial=line_initial)
    if request.method == 'POST' and form.is_valid() and lines.is_valid():
        try:
            saved = C.save_sale(request.user, sale.pk if sale else None, form.cleaned_data, lines.lines(), form.cleaned_data['token'])
        except ValidationError as e:
            form.add_error(None, e)
        except (IntegrityError, OperationalError):
            form.add_error(None, 'La venta cambió mientras se guardaba o la base está ocupada. Revise y vuelva a intentar.')
        else:
            messages.success(request, 'Borrador guardado. Revíselo y envíelo al sistema contable.')
            return redirect('sale', pk=saved.pk)
    today = timezone.localdate()
    price_rows = {}
    for item in PriceItem.objects.select_related('product', 'price_list').filter(valid_from__lte=today, price_list__active=True).order_by('-valid_from'):
        price_rows.setdefault((item.product_id, item.price_list_id), item)
    table = {}
    for (pid, lid), item in price_rows.items():
        table.setdefault(item.product, {})[lid] = item.price
    lists = list(PriceList.objects.filter(active=True))
    reference = sorted(([p, [table[p].get(pl.pk) for pl in lists]] for p in table), key=lambda r: r[0].name)
    return render(request, 'cartera/sale_form.html', {'form': form, 'lines': lines, 'sale': sale, 'lists': lists, 'reference': reference,
                                                      'title': f'Editar {sale.label}' if sale else 'Nueva venta'})


@roles(*S.SALES)
def sale_detail(request, pk):
    sale = get_object_or_404(Sale.objects.select_related('party', 'branch', 'price_list', 'created_by', 'issued_by'), pk=pk)
    receivable = with_balance(Receivable.objects.filter(sale=sale)).first()
    sellers = dict(CatalogItem.objects.filter(kind='seller').values_list('contable_id', 'name'))
    contact = sale.party.contacts.filter(channel='email').first()
    return render(request, 'cartera/sale_detail.html', {
        'sale': sale, 'lines': sale.lines.select_related('product', 'prior_movement__batch').prefetch_related('stock__batch', 'stock__movement'),
        'receivable': receivable, 'submissions': Submission.objects.filter(kind='invoice', target_id=str(sale.pk)).order_by('created_at'),
        'applications': PaymentApplication.objects.select_related('collection', 'voucher').filter(receivable=receivable) if receivable else [],
        'seller': sellers.get(sale.seller_id, sale.seller_id), 'can_issue': C.can_issue(request.user), 'finance': request.user.role in S.FINANCE,
        'token_form': TokenForm(), 'cancel_form': ReasonForm(), 'mail_form': F.MailForm(initial={'to': (contact.value if contact else sale.branch.einvoice_email)}),
        'title': sale.label, 'show_beneficiary': request.user.role in S.SALES,
        'discount': discounts.active_discount(sale), 'discount_request': discounts.pending_request(sale), 'can_discount': request.user.role in S.ADMINS})


@roles(*S.SALES)
@require_POST
def sale_issue(request, pk):
    return _post(request, lambda t: C.issue_sale(request.user, pk, t), None, reverse('sale', args=[pk]))


@roles(*S.SALES)
@require_POST
def sale_retry(request, pk):
    return _post(request, lambda t: C.retry_sale(request.user, pk, t), None, reverse('sale', args=[pk]))


@roles(*S.SALES)
@require_POST
def sale_cancel(request, pk):
    form = ReasonForm(request.POST)
    if form.is_valid():
        try:
            C.cancel_sale(request.user, pk, form.cleaned_data['reason'], form.cleaned_data['token'])
            messages.success(request, 'Venta cancelada; las reservas de inventario se liberaron.')
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
    else:
        messages.error(request, 'Escriba el motivo de la cancelación.')
    return redirect('sale', pk=pk)


@roles(*S.SALES)
@require_POST
def sale_refresh(request, pk):
    try:
        sale = C.refresh_sale(request.user, pk)
        messages.success(request, f'Saldo en el sistema contable: {sale.contable_balance:,.0f}'.replace(',', '.') if sale.contable_balance is not None else 'Consulta realizada.')
    except ValidationError as e:
        messages.error(request, ' '.join(e.messages))
    return redirect('sale', pk=pk)


@roles(*S.SALES)
@require_POST
def sale_mail(request, pk):
    form = F.MailForm(request.POST)
    if form.is_valid():
        try:
            C.send_invoice_mail(request.user, pk, form.cleaned_data['to'], form.cleaned_data['token'])
            messages.success(request, 'Envío simulado registrado: el correo no salió de la plataforma.')
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
    else:
        messages.error(request, 'Escriba un correo válido.')
    return redirect('sale', pk=pk)


@roles(*S.SALES)
def sale_pdf(request, pk):
    sale = get_object_or_404(Sale, pk=pk)
    if not sale.pdf:
        raise Http404
    S.event(request.user, 'PDF de factura consultado', f'sale-{sale.pk}', sale.fiscal_name)
    return FileResponse(sale.pdf.open('rb'), content_type='application/pdf', as_attachment=False, filename=f'{sale.fiscal_name or sale.pk}.pdf')


# ------------------------------------------------------------------- cartera

@roles(*S.FINANCE)
def portfolio(request):
    rows = list(C.open_receivables())
    buckets = C.aging(rows)
    buckets['late'] = buckets['d30'] + buckets['d60'] + buckets['d90'] + buckets['over']
    ctx = request.GET.get('contexto', '')
    by_party = {}
    for r in rows:
        if ctx and (r.sale.context if r.sale else r.party.default_context) != ctx:
            continue
        p = by_party.setdefault(r.party_id, {'party': r.party, 'total': ZERO, 'overdue': ZERO, 'n': 0, 'oldest': r.due_date})
        p['total'] += r.balance
        p['n'] += 1
        p['oldest'] = min(p['oldest'], r.due_date)
        if r.days_late > 0:
            p['overdue'] += r.balance
    parties = sorted(by_party.values(), key=lambda p: (-p['overdue'], -p['total']))
    unidentified = BankTransaction.objects.filter(status=BankTransaction.Status.PENDING)
    return render(request, 'cartera/portfolio.html', {
        'parties': parties, 'buckets': buckets, 'total': sum((r.balance for r in rows), ZERO), 'contexto': ctx, 'contexts': Sale._meta.get_field('context').choices,
        'unidentified': unidentified.aggregate(s=Sum('amount'), n=Count('pk')), 'title': 'Cartera',
        'diff': Sale.objects.filter(status='issued', contable_balance__isnull=False).count()})


@roles(*S.FINANCE)
def opening_new(request):
    form = F.OpeningForm(request.POST or None, initial={'party': request.GET.get('tercero')})
    return run_form(request, form, lambda d: C.add_opening(request.user, d, d['token']), 'Saldo inicial de cartera',
                    lambda r: reverse('party', args=[r.party_id]), {'back_url': reverse('portfolio'), 'done': 'Saldo inicial registrado.', 'submit': 'Registrar saldo',
                                                                    'intro': 'Registre las facturas emitidas antes de la fecha de corte con el saldo que muestra el sistema contable. Indique la procedencia del dato.'})


# --------------------------------------------------------------------- banco

@roles(*S.FINANCE)
def bank(request):
    tab = request.GET.get('estado', 'pending')
    qs = BankTransaction.objects.select_related('account', 'party', 'payer').order_by('-date', '-pk')
    tabs = {'pending': Q(status='pending', amount__gt=0), 'identified': Q(status='identified'), 'applied': Q(status='applied'), 'other': Q(status='other')}
    counts = {k: BankTransaction.objects.filter(v).count() for k, v in tabs.items()}
    rows = list(qs.filter(tabs.get(tab, tabs['pending']))[:150])
    if tab == 'pending':
        for t in rows:
            t.hint = C.suggestions(t)
    for t in rows:
        t.left = C.remaining(t)
    pending = BankTransaction.objects.filter(tabs['pending']).aggregate(s=Sum('amount'))['s'] or ZERO
    return render(request, 'cartera/bank.html', {'rows': rows, 'tab': tab, 'counts': counts, 'pending_total': pending, 'title': 'Banco y recaudos',
                                                 'imports': StatementImport.objects.select_related('account', 'uploaded_by')[:6],
                                                 'accounts': BankAccount.objects.filter(active=True).exists()})


@roles(*S.FINANCE)
def bank_import(request):
    form = F.StatementForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        f = form.cleaned_data['file']
        try:
            imp = C.preview_statement(request.user, form.cleaned_data['account'], f.name, f.read(), form.cleaned_data['token'])
        except ValidationError as e:
            form.add_error(None, e)
        else:
            return redirect('bank_preview', pk=imp.pk)
    return render(request, 'cartera/bank_import.html', {'form': form, 'title': 'Importar extracto', 'accounts': BankAccount.objects.filter(active=True).exists()})


@roles(*S.FINANCE)
def bank_preview(request, pk):
    imp = get_object_or_404(StatementImport.objects.select_related('account', 'uploaded_by', 'confirmed_by'), pk=pk)
    if request.method == 'POST':
        action = request.POST.get('action')
        fn = C.confirm_statement if action == 'confirm' else C.discard_statement
        done = 'Extracto importado. Revise los movimientos por identificar.' if action == 'confirm' else 'Importación descartada.'
        target = reverse('bank') if action == 'confirm' else reverse('bank_import')
        form = TokenForm(request.POST)
        if form.is_valid():
            try:
                fn(request.user, imp.pk, form.cleaned_data['token'])
                messages.success(request, done)
                return redirect(target)
            except ValidationError as e:
                messages.error(request, ' '.join(e.messages))
    inflow = sum(Decimal(r['amount']) for r in imp.rows if Decimal(r['amount']) > 0)
    outflow = sum(Decimal(r['amount']) for r in imp.rows if Decimal(r['amount']) < 0)
    return render(request, 'cartera/bank_preview.html', {'imp': imp, 'token_form': TokenForm(), 'inflow': inflow, 'outflow': outflow,
                                                         'title': f'Revisión del extracto · {imp.file_name}'})


@roles(*S.FINANCE)
def bank_txn(request, pk):
    txn = get_object_or_404(BankTransaction.objects.select_related('account', 'party', 'payer', 'statement'), pk=pk)
    result = C.suggestions(txn)
    left = C.remaining(txn)
    chosen = request.POST.get('party') or request.GET.get('tercero') or (str(txn.party_id) if txn.party_id else '')
    if not chosen and result['decision'] == 'suggested':
        chosen = str(result['candidates'][0]['party'].pk)
    party = Party.objects.filter(pk=chosen).first() if chosen.isdigit() else None
    error = None
    if request.method == 'POST' and request.POST.get('action') == 'apply':
        form = TokenForm(request.POST)
        allocations = []
        try:
            for key, value in request.POST.items():
                if key.startswith('amount_') and value.strip():
                    allocations.append((int(key[7:]), C.parse_money(value)))
        except (ValidationError, InvalidOperation, ValueError):
            error = 'Revise los valores: use solo números.'
        if error is None and form.is_valid() and party:
            try:
                C.apply_transaction(request.user, txn.pk, party.pk, allocations, form.cleaned_data['token'],
                                    remember_payer=bool(request.POST.get('remember')), relation=request.POST.get('relation', ''))
            except ValidationError as e:
                error = ' '.join(e.messages)
            else:
                messages.success(request, 'Pago aplicado. Revise abajo el estado del recibo de caja en el sistema contable.')
                return redirect('bank_txn', pk=txn.pk)
        elif error is None:
            error = 'Elija el cliente y vuelva a intentar.'
    plan, excess, recs = [], ZERO, []
    if party:
        plan, excess = C.propose_allocation(txn, party, left)
        proposed = {r.pk: a for r, a, _ in plan}
        recs = list(C.open_receivables(party))
        for r in recs:
            r.proposed = proposed.get(r.pk)
            r.mentioned = any(x.pk == r.pk and k == 'instruction' for x, _, k in plan)
    collections = Collection.objects.select_related('party', 'sale').filter(transaction=txn)
    applications = PaymentApplication.objects.select_related('receivable', 'voucher').filter(collection__transaction=txn).order_by('-created_at')
    return render(request, 'cartera/bank_txn.html', {
        'txn': txn, 'result': result, 'left': left, 'party': party, 'recs': recs, 'excess': excess, 'error': error, 'token_form': TokenForm(),
        'note_form': F.NoteForm(), 'reason_form': ReasonForm(), 'collections': collections, 'applications': applications,
        'parties': Party.objects.filter(active=True).order_by('name'), 'title': 'Movimiento bancario', 'outflows': outflow_candidates(txn),
        'payer_known': txn.payer and party and PayerLink.objects.filter(payer=txn.payer, party=party, status='confirmed').exists()})


@roles(*S.FINANCE)
@require_POST
def bank_other(request, pk):
    form = F.NoteForm(request.POST)
    if form.is_valid():
        try:
            C.mark_other(request.user, pk, form.cleaned_data['note'], form.cleaned_data['token'])
            messages.success(request, 'Movimiento marcado como no cartera.')
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
    else:
        messages.error(request, 'Explique por qué no corresponde a cartera.')
    return redirect('bank_txn', pk=pk)


@roles(*S.FINANCE)
@require_POST
def bank_reopen(request, pk):
    return _post(request, lambda t: C.reopen_transaction(request.user, pk, t), 'Movimiento devuelto a por identificar.', reverse('bank_txn', args=[pk]))


@roles(*S.FINANCE)
@require_POST
def bank_counter(request, pk):
    ids = [int(x) for x in request.POST.getlist('collection') if x.isdigit()]
    return _post(request, lambda t: C.link_counter(request.user, pk, ids, t), 'Movimiento conciliado con los recaudos de contado.', reverse('bank_txn', args=[pk]))


@roles(*S.FINANCE)
@require_POST
def application_reverse(request, pk):
    app = get_object_or_404(PaymentApplication.objects.select_related('collection'), pk=pk)
    form = ReasonForm(request.POST)
    target = reverse('bank_txn', args=[app.collection.transaction_id]) if app.collection.transaction_id else reverse('party', args=[app.collection.party_id])
    if form.is_valid():
        try:
            C.reverse_application(request.user, pk, form.cleaned_data['reason'], form.cleaned_data['token'])
            messages.success(request, 'Aplicación revertida.')
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
    else:
        messages.error(request, 'Escriba el motivo de la reversión.')
    return redirect(target)


@roles(*S.FINANCE)
def bank_accounts(request):
    form = F.BankAccountForm(request.POST or None)

    def save(d):
        S.operation(request.user, d['token'], 'bank_account')
        acc = BankAccount.objects.create(name=d['name'], bank=d['bank'], last4=d['last4'], contable_payment_id=d['contable_payment_id'], active=d['active'])
        S.event(request.user, 'Cuenta bancaria registrada', f'account-{acc.pk}', str(acc))
        return acc
    from django.db import transaction
    return run_form(request, form, lambda d: transaction.atomic(save)(d), 'Cuentas receptoras', reverse('bank_accounts'),
                    {'back_url': reverse('bank'), 'done': 'Cuenta registrada.', 'submit': 'Agregar cuenta', 'accounts': BankAccount.objects.all()},
                    template='cartera/bank_accounts.html')


# --------------------------------------------------------------- integración

@roles(*S.FINANCE)
def contable_status(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        form = TokenForm(request.POST)
        if form.is_valid():
            try:
                S.require(request.user, S.FINANCE)
                if action == 'catalogs':
                    summary = contable.sync_catalogs()
                    messages.success(request, 'Catálogos actualizados: ' + ', '.join(f'{k} {v}' for k, v in summary.items()))
                elif action == 'pending':
                    done = contable.process_pending()
                    messages.success(request, f'{len(done)} envío(s) revisado(s).')
                elif action == 'rerun' and request.POST.get('submission', '').isdigit():
                    contable.rerun_handler(int(request.POST['submission']))
                    messages.success(request, 'Resultado registrado en la plataforma.')
                S.event(request.user, 'Acción sobre el conector contable', action)
            except Exception as e:  # noqa: BLE001 — se informa a la persona sin detener la página
                messages.error(request, f'No fue posible completar la acción: {e}')
        return redirect('contable_status')
    subs = Submission.objects.select_related('created_by')[:80]
    return render(request, 'cartera/contable_status.html', {
        'subs': subs, 'catalog': CatalogItem.objects.order_by('kind', 'name'), 'environment': contable.environment(), 'config': {
            'stamp': contable.settings.CONTABLE['stamp_send'], 'mail': contable.settings.CONTABLE['mail_send'], 'partner': True},
        'counts': {s: Submission.objects.filter(status=s).count() for s in ('pending', 'uncertain', 'review', 'rejected')},
        'vouchers': Voucher.objects.exclude(status__in=('accepted', 'linked')).select_related('collection__party')[:30],
        'token_form': TokenForm(), 'title': 'Sistema contable (simulador)'})


# ---------------------------------------------------------------- resultados

@roles(*S.FINANCE)
def results(request):
    """Resultados de gestión: ventas, costo y margen del mes, vista anual y lo que se debe.

    Los cálculos son los de `cartera/results.py`. La comparación del mes en curso usa el mismo número de días del
    mes anterior; un mes cerrado se compara con el mes cerrado anterior."""
    from feria import resumen
    from . import results as R
    today = timezone.localdate()
    month = R.parse_month(request.GET.get('mes'))
    r = R.report(month)
    partial = month == today.replace(day=1)
    if partial:
        start, end, prev_start, prev_end = resumen.same_days(today)
        previous = resumen.sales_between(prev_start, prev_end)
        span = f'los mismos {today.day} días' if today.day > 1 else 'el mismo día'
        basis = f'{span} de {R.MONTHS[prev_start.month - 1]}'
        period = f'Del 1 al {today.day} de {R.MONTHS[month.month - 1]} de {month.year}' if today.day > 1 else f'1 de {R.MONTHS[month.month - 1]} de {month.year}'
    else:
        previous = r['trend'][-2]['revenue']
        basis = f'{R.month_name(r["prev"])}, mes completo'
        period = f'{R.month_name(month).capitalize()}, mes completo'
    change = resumen.pct_change(r['sales']['revenue'], previous)
    compare = {'previous': previous, 'basis': basis, 'change': resumen.pct_text(change) if change is not None else None, 'down': change is not None and change < 0}
    return render(request, 'cartera/results.html', {
        'title': 'Resultados', 'r': r, 'y': R.year_report(R.parse_year(request.GET.get('anio'))), 'debts': R.debts_block(), 'today': today,
        'partial': partial, 'period': period, 'compare': compare})
