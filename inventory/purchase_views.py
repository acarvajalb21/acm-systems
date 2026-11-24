"""Pedidos a proveedores: lista, detalle con el texto preparado, envío simulado y cierre."""
from datetime import date
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from . import services as S, compras
from .forms import TokenForm
from .models import PurchaseOrder
from .views import roles

TABS = {'open': ('Abiertos', PurchaseOrder.OPEN), 'received': ('Recibidos', ('received',)), 'cancelled': ('Cancelados', ('cancelled',))}


@roles(*S.DOCUMENTS)
def purchase_orders(request):
    tab = request.GET.get('estado', 'open')
    if tab not in TABS:
        tab = 'open'
    qs = PurchaseOrder.objects.select_related('supplier', 'created_by').prefetch_related('lines__product')
    rows = list(qs.filter(status__in=TABS[tab][1])[:100])
    counts = {k: PurchaseOrder.objects.filter(status__in=v[1]).count() for k, v in TABS.items()}
    return render(request, 'purchase_orders.html', {'title': 'Pedidos a proveedores', 'rows': rows, 'tab': tab, 'tabs': TABS, 'counts': counts,
                                                    'can_act': request.user.role in S.MANAGERS})


@roles(*S.DOCUMENTS)
def purchase_order(request, pk):
    order = get_object_or_404(PurchaseOrder.objects.select_related('supplier', 'created_by', 'sent_by', 'closed_by', 'invoice').prefetch_related('lines__product'), pk=pk)
    return render(request, 'purchase_order.html', {'title': order.number, 'order': order, 'text': compras.order_text(order), 'delivery': compras.delivery(order),
                                                   'can_act': request.user.role in S.MANAGERS, 'token_form': TokenForm()})


@roles(*S.MANAGERS)
@require_POST
def purchase_order_action(request, pk, action):
    order = get_object_or_404(PurchaseOrder, pk=pk)
    form = TokenForm(request.POST)
    back = reverse('purchase_order', args=[pk])
    if not form.is_valid():
        messages.error(request, 'La solicitud venció. Vuelva a intentarlo.')
        return redirect(back)
    token = form.cleaned_data['token']
    try:
        if action == 'enviar':
            raw = request.POST.get('llegada', '').strip()
            expected = date.fromisoformat(raw) if raw else None
            compras.mark_sent(request.user, order.pk, token, expected_on=expected)
            done = ('Envío simulado registrado: ningún mensaje salió de la plataforma. Al recibir la factura que cite el pedido en '
                    '«orden de compra», se cerrará solo.')
        elif action == 'recibir':
            compras.close_order(request.user, order.pk, token, received=True, note=request.POST.get('nota', ''))
            done = 'Pedido cerrado como recibido.'
        elif action == 'cancelar':
            compras.close_order(request.user, order.pk, token, received=False, note=request.POST.get('nota', ''))
            done = 'Pedido cancelado.'
        else:
            messages.error(request, 'Acción no reconocida.')
            return redirect(back)
    except ValueError:
        messages.error(request, 'La fecha de llegada no es válida.')
        return redirect(back)
    except ValidationError as e:
        messages.error(request, ' '.join(e.messages))
        return redirect(back)
    except (IntegrityError, OperationalError):
        messages.error(request, 'La operación cambió mientras se guardaba. Revise y vuelva a intentar.')
        return redirect(back)
    messages.success(request, done)
    return redirect(back)
