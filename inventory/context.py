from django.conf import settings
from django.utils.functional import SimpleLazyObject
from . import services as S

# Nombre de URL → sección (página resaltada en la navegación).
SECTIONS = {
    'operations': 'operations',
    'home': 'home', 'stock': 'stock', 'trace': 'stock', 'adjust_in': 'stock', 'adjust_out': 'stock', 'exit_stock': 'stock',
    'movements': 'movements', 'products': 'products', 'new_product': 'products', 'edit_product': 'products', 'product_codes': 'products',
    'requests': 'requests', 'new_request': 'requests', 'approve': 'requests', 'resolve_request': 'requests',
    'reservations': 'reservations', 'reserve': 'reservations', 'finish_reservation': 'reservations',
    'invoices': 'invoices', 'invoice_new': 'invoices', 'invoice_import': 'invoices', 'invoice': 'invoices', 'invoice_edit': 'invoices',
    'invoice_void': 'invoices', 'invoice_receive': 'invoices', 'adjustment_new': 'invoices',
    'invoice_pay': 'payables', 'payables': 'payables', 'payment_new': 'payables', 'supplier_payment': 'payables',
    'suppliers': 'suppliers', 'new_supplier': 'suppliers', 'edit_supplier': 'suppliers', 'supplier_account': 'suppliers', 'credit_note_new': 'suppliers',
    'alerts': 'alerts', 'settings': 'alerts', 'users': 'users', 'new_user': 'users', 'reset_password': 'users',
    'toggle_user': 'users', 'audit': 'audit',
    'sales': 'sales', 'sale_new': 'sales', 'sale': 'sales', 'sale_edit': 'sales', 'sale_discount': 'sales',
    'parties': 'parties', 'party_new': 'parties', 'party': 'parties', 'party_edit': 'parties', 'contact_new': 'parties',
    'prices': 'prices', 'price_new': 'prices', 'portfolio': 'portfolio', 'opening_new': 'portfolio',
    'bank': 'bank', 'bank_import': 'bank', 'bank_preview': 'bank', 'bank_accounts': 'bank', 'bank_txn': 'bank', 'contable_status': 'contable',
    'discounts': 'discounts', 'results': 'results',
    'replenishment': 'replenishment', 'forecast': 'replenishment',
    'purchase_orders': 'purchase_orders', 'purchase_order': 'purchase_orders', 'purchase_order_action': 'purchase_orders',
    'agents': 'agents', 'agent_action': 'agents', 'agents_history': 'agents', 'learning': 'learning', 'learning_retrain': 'learning', 'methodology': 'methodology', 'copilot': 'copilot',
    'copilot_ask': 'copilot', 'flow_map': 'flow_map', 'tour': 'tour',
    'reception': 'reception', 'reception_invoice': 'reception',
}

# Sección → grupo de la barra superior (Resumen, Operaciones, Inventario, Compras, Finanzas, Inteligencia; `admin` va en el menú de la persona).
GROUPS = {
    'home': 'resumen', 'flow_map': 'resumen', 'tour': 'resumen',
    'operations': 'operaciones', 'requests': 'operaciones', 'reservations': 'operaciones', 'reception': 'operaciones',
    'stock': 'inventario', 'movements': 'inventario', 'products': 'inventario',
    'replenishment': 'compras', 'purchase_orders': 'compras', 'invoices': 'compras', 'suppliers': 'compras',
    'payables': 'finanzas', 'sales': 'finanzas', 'parties': 'finanzas', 'prices': 'finanzas', 'portfolio': 'finanzas', 'bank': 'finanzas',
    'discounts': 'finanzas', 'results': 'finanzas', 'contable': 'finanzas',
    'agents': 'inteligencia', 'learning': 'inteligencia', 'methodology': 'inteligencia', 'copilot': 'inteligencia',
    'alerts': 'admin', 'users': 'admin', 'audit': 'admin',
}


def _pending_discounts(user):
    if user.role != 'owner':
        return 0
    from cartera.models import DiscountRequest
    return DiscountRequest.objects.filter(status=DiscountRequest.Status.PENDING).count()


def _agents(user):
    cached = getattr(user, '_agents_ctx', None)
    if cached is None:
        from agentes import runner
        cached = user._agents_ctx = runner.strip(user)
    return cached


def navigation(request):
    base = {'company_name': settings.COMPANY_NAME, 'app_name': settings.APP_NAME, 'profile_switch': settings.FERIA['profile_switch']}
    u = request.user
    if not u.is_authenticated:
        return base
    match = getattr(request, 'resolver_match', None)
    section = SECTIONS.get(match.url_name if match else '', '')
    return {
        **base,
        'is_requester': u.role == 'requester',
        'manager': u.role in S.MANAGERS,
        'finance': u.role in S.FINANCE,
        'documents': u.role in S.DOCUMENTS,
        'administrator': u.role in S.ADMINS,
        'is_owner': u.role == 'owner',
        'can_request': u.role in S.REQUESTERS,
        'sales_role': u.role in S.SALES,
        'receiver': u.role in S.RECEIVERS,
        'is_reception': u.role == 'reception',
        'section': section,
        'nav_group': GROUPS.get(section, ''),
        'unread_alerts': SimpleLazyObject(lambda: sum(1 for a in S.alerts(u) if not a['read'])),
        'pending_discounts': SimpleLazyObject(lambda: _pending_discounts(u)),
        'agents_visible': SimpleLazyObject(lambda: _agents(u)['visible']),
        'agents_pending': SimpleLazyObject(lambda: _agents(u)['pending']),
        'agents_strip': SimpleLazyObject(lambda: _agents(u)['strip']),
    }
