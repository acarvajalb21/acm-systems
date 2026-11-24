"""Pantallas de cuentas por pagar: pendientes, programación, confirmación, conciliación y exportación a Excel."""
from datetime import date, timedelta
from decimal import Decimal
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError
from django.db.models import Q, Sum
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from . import forms, payables as P, services as S
from .forms import uploads
from .models import Invoice, Supplier, SupplierPayment, PaymentAllocation, PaymentFile, with_payable_balance
from .views import roles, run_form

ZERO = Decimal('0.00')


def _money(raw):
    from cartera.services import parse_money
    return parse_money(raw) if str(raw or '').strip() else ZERO


def _post_rows(request, invoices):
    rows = []
    for inv in invoices:
        rows.append({'invoice': inv, 'amount': _money(request.POST.get(f'amount_{inv.pk}')), 'discount': _money(request.POST.get(f'discount_{inv.pk}')),
                     'discount_note': request.POST.get(f'note_{inv.pk}', '')})
    return [r for r in rows if r['amount'] or r['discount']]


def _keep_typed(request, invoices):
    """Tras un envío con error, cada fila vuelve con lo que la persona escribió (valor, descuento y soporte), no con el cálculo inicial."""
    if request.method != 'POST':
        return
    for inv in invoices:
        if f'amount_{inv.pk}' in request.POST or f'discount_{inv.pk}' in request.POST:
            inv.typed = {'amount': request.POST.get(f'amount_{inv.pk}', '')[:30], 'discount': request.POST.get(f'discount_{inv.pk}', '')[:30],
                         'note': request.POST.get(f'note_{inv.pk}', '')[:200]}


def _day(raw):
    """Fecha de la consulta (`?fecha=aaaa-mm-dd`); hoy si falta o no es válida."""
    try:
        return date.fromisoformat(raw) if raw else timezone.localdate()
    except ValueError:
        return timezone.localdate()


def _plan(invoices, on):
    for inv in invoices:
        inv.offer = P.settlement(inv, on)
    return invoices


@roles(*S.FINANCE)
def payables(request):
    tab = request.GET.get('vista', 'pendientes')
    supplier = Supplier.objects.filter(pk=request.GET.get('proveedor')).first() if request.GET.get('proveedor', '').isdigit() else None
    rows = S.payables(supplier)
    today = timezone.localdate()
    year = PaymentAllocation.objects.filter(payment__status__in=SupplierPayment.SETTLED, payment__paid_on__year=today.year)
    year = year.aggregate(saved=Sum('discount'), lost=Sum('lost_discount'), paid=Sum('amount'))
    scheduled = SupplierPayment.objects.select_related('supplier').filter(status=SupplierPayment.Status.SCHEDULED).order_by('scheduled_on')
    done = SupplierPayment.objects.select_related('supplier').exclude(status=SupplierPayment.Status.SCHEDULED).order_by('-paid_on', '-pk')
    if supplier:
        scheduled, done = scheduled.filter(supplier=supplier), done.filter(supplier=supplier)
    return render(request, 'payables.html', {
        'rows': rows, 'tab': tab, 'supplier': supplier, 'suppliers': Supplier.objects.filter(invoices__isnull=False).distinct().order_by('name'),
        'scheduled': scheduled, 'done': done[:60], 'year': year, 'total': sum((i.offer['balance'] for i in rows), ZERO),
        'offer': sum((i.offer['discount'] for i in rows), ZERO), 'scheduled_total': scheduled.aggregate(s=Sum('amount'))['s'] or ZERO,
        'overdue': sum((i.offer['balance'] for i in rows if i.days_to_due < 0), ZERO), 'title': 'Cuentas por pagar'})


@roles(*S.FINANCE)
def payment_new(request):
    ids = [int(x) for x in (request.POST.getlist('factura') or request.GET.getlist('factura') or request.GET.get('facturas', '').split(',')) if str(x).isdigit()]
    invoices = list(with_payable_balance(Invoice.objects.filter(pk__in=ids, review=Invoice.Review.VALIDATED).select_related('supplier')).order_by('due_date'))
    suppliers = {i.supplier_id for i in invoices}
    if not invoices or len(suppliers) != 1:
        messages.error(request, 'Seleccione facturas validadas de un solo proveedor.')
        return redirect('payables')
    supplier = invoices[0].supplier
    mode = request.POST.get('mode') or request.GET.get('modo', 'programar')
    form = forms.PaymentPlanForm(request.POST or None, request.FILES or None, initial={'on': _day(request.GET.get('fecha'))})
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        try:
            rows = _post_rows(request, invoices)
            if mode == 'pagar':
                p = P.register_payment(request.user, supplier, rows, d['on'], d['method'], d['reference'], uploads(d['files']), d['note'], d['token'])
                messages.success(request, 'Pago registrado. Concílielo cuando importe el extracto del banco.')
            else:
                p = P.schedule_payment(request.user, supplier, rows, d['on'], d['method'], d['note'], d['token'])
                messages.success(request, 'Pago programado. Confírmelo con el soporte cuando el banco lo ejecute.')
            return redirect('supplier_payment', pk=p.pk)
        except ValidationError as e:
            form.add_error(None, e)
        except (IntegrityError, OperationalError):
            form.add_error(None, 'La información cambió mientras se guardaba. Revise y vuelva a intentar.')
    on = form.cleaned_data['on'] if form.is_bound and form.is_valid() else _day(request.GET.get('fecha'))
    _keep_typed(request, invoices)
    return render(request, 'payment_plan.html', {'form': form, 'invoices': _plan(invoices, on), 'supplier': supplier, 'mode': mode, 'on': on,
                                                 'ids': ','.join(str(i.pk) for i in invoices), 'title': ('Registrar pago' if mode == 'pagar' else 'Programar pago') + f' · {supplier.name}'})


@roles(*S.FINANCE)
def payment_detail(request, pk):
    p = get_object_or_404(SupplierPayment.objects.select_related('supplier', 'created_by', 'confirmed_by'), pk=pk)
    allocations = list(p.allocations.select_related('invoice'))
    invoices = list(with_payable_balance(Invoice.objects.filter(pk__in=[a.invoice_id for a in allocations])))
    on = _day(request.GET.get('fecha'))
    form = forms.PaymentPlanForm(request.POST or None, request.FILES or None, initial={'on': on, 'method': p.method})
    if request.method == 'POST' and request.POST.get('action') == 'confirm' and form.is_valid():
        d = form.cleaned_data
        try:
            P.confirm_payment(request.user, p.pk, _post_rows(request, invoices), d['on'], d['method'], d['reference'], uploads(d['files']), d['note'], d['token'])
            messages.success(request, 'Pago confirmado con su soporte.')
            return redirect('supplier_payment', pk=p.pk)
        except ValidationError as e:
            form.add_error(None, e)
    planned = {a.invoice_id: a for a in allocations}
    _keep_typed(request, invoices)
    for inv in _plan(invoices, on):
        inv.planned = planned.get(inv.pk)
    candidates = []
    if p.status == SupplierPayment.Status.CONFIRMED:
        from cartera.models import BankTransaction
        candidates = list(BankTransaction.objects.select_related('account').filter(amount=-p.amount, supplier_payment__isnull=True,
                                                                                  date__range=(p.paid_on - timedelta(days=3), p.paid_on + timedelta(days=10))))
    return render(request, 'supplier_payment.html', {
        'p': p, 'allocations': allocations, 'invoices': invoices, 'form': form, 'on': on, 'candidates': candidates,
        'token_form': forms.TokenForm(), 'reason_form': forms.ReasonForm(), 'text_form': forms.TextForm(),
        'bank': getattr(p, 'bank_transaction', None), 'title': f'Pago a {p.supplier.name}'})


def _action(request, fn, done, target):
    form = forms.TokenForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'La solicitud venció. Vuelva a intentarlo.')
        return redirect(target)
    try:
        fn(form.cleaned_data['token'])
        messages.success(request, done)
    except ValidationError as e:
        messages.error(request, ' '.join(e.messages))
    return redirect(target)


@roles(*S.FINANCE)
@require_POST
def payment_cancel(request, pk):
    return _action(request, lambda t: P.cancel_payment(request.user, pk, request.POST.get('reason', ''), t), 'Pago anulado.', reverse('supplier_payment', args=[pk]))


@roles(*S.FINANCE)
@require_POST
def payment_contable(request, pk):
    return _action(request, lambda t: P.set_contable_receipt(request.user, pk, request.POST.get('text', ''), t), 'Egreso del sistema contable vinculado.',
                   reverse('supplier_payment', args=[pk]))


@roles(*S.FINANCE)
@require_POST
def payment_reconcile(request, pk):
    txn = request.POST.get('transaction', '')
    target = request.POST.get('next') or reverse('supplier_payment', args=[pk])
    if not target.startswith('/'):
        target = reverse('supplier_payment', args=[pk])
    return _action(request, lambda t: P.reconcile_payment(request.user, pk, int(txn) if txn.isdigit() else 0, t), 'Pago conciliado con el movimiento del banco.', target)


@roles(*S.FINANCE)
def payment_file(request, pk):
    f = get_object_or_404(PaymentFile, pk=pk)
    S.event(request.user, 'Soporte de pago consultado', f'supplier-payment-{f.payment_id}', f.name)
    return FileResponse(f.file.open('rb'), content_type=f.content_type, as_attachment=False, filename=f.name)


@roles(*S.FINANCE)
def supplier_account(request, pk):
    supplier = get_object_or_404(Supplier, pk=pk)
    invoices = list(with_payable_balance(supplier.invoices.exclude(review=Invoice.Review.VOID)).order_by('-issue_date', '-pk'))
    open_rows = [i for i in invoices if i.review == Invoice.Review.VALIDATED and i.balance_due > 0]
    today = timezone.localdate()
    for i in open_rows:
        i.offer = P.settlement(i, today)
    notes = list(supplier.credit_notes.all())
    return render(request, 'supplier_account.html', {
        'supplier': supplier, 'invoices': invoices, 'open_ids': {i.pk for i in open_rows}, 'balance': sum((i.balance_due for i in open_rows), ZERO),
        'overdue': sum((i.balance_due for i in open_rows if i.due_date < today), ZERO), 'offer': sum((i.offer['discount'] for i in open_rows), ZERO),
        'notes': notes, 'available': sum((n.available for n in notes), ZERO), 'payments': supplier.payments.order_by('-paid_on', '-pk')[:30],
        'today': today, 'title': f'Cuenta de {supplier.name}'})


@roles(*S.FINANCE)
def credit_note_new(request, pk):
    supplier = get_object_or_404(Supplier, pk=pk)
    form = forms.CreditNoteForm(request.POST or None, supplier=supplier, initial={'invoice': request.GET.get('factura')})
    back = reverse('supplier_account', args=[pk])
    return run_form(request, form, lambda d: P.add_credit_note(request.user, supplier, d['number'], d['issue_date'], d['amount'], d['note'], d['invoice'],
                                                               d['apply_amount'] or (min(d['amount'], with_payable_balance(Invoice.objects.filter(pk=d['invoice'].pk)).get().balance)
                                                                                     if d['invoice'] else None), d['token']),
                    f'Nota crédito · {supplier.name}', back, {'back_url': back, 'done': 'Nota crédito registrada.', 'submit': 'Registrar nota crédito',
                                                             'intro': 'La nota crédito financiera reduce el saldo. Una devolución física se registra aparte en inventario.'})


@roles(*S.FINANCE)
def adjustment_new(request, pk):
    inv = get_object_or_404(Invoice.objects.select_related('supplier'), pk=pk)
    form = forms.AdjustmentForm(request.POST or None)
    back = reverse('invoice', args=[pk])
    return run_form(request, form, lambda d: P.add_adjustment(request.user, pk, d['kind'], d['amount'], d['reference'], d['token']),
                    f'Ajuste · factura {inv.number}', back, {'back_url': back, 'done': 'Ajuste registrado.', 'submit': 'Registrar ajuste',
                                                            'intro': 'Use «Diferencia de descuento» cuando el proveedor cobra el excedente de un descuento que no aceptó.'})


# ------------------------------------------------------- exportación a Excel

@roles(*S.FINANCE)
def ledger_export(request):
    data = P.export_payables()
    S.event(request.user, 'Cuentas por pagar exportadas a Excel')
    response = HttpResponse(data, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="cuentas_por_pagar_{timezone.localdate():%Y%m%d}.xlsx"'
    return response
