"""Pantallas de agentes: propuestas por aprobar, ficha de cada agente, detalle de una propuesta e historial de decisiones.

Todo lo que se muestra de un agente es lo que el agente guardó: su texto de «por qué», las cifras de su última
revisión y el resultado de cada decisión. No se redactan razonamientos aquí.
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from inventory import services as S
from inventory.models import User
from . import runner
from .agents import REGISTRY, params_for
from .models import AgentAction, AgentRun, AgentConfig

VIEWERS = ('inventory', 'finance', 'admin', 'owner')
STAGES = ('Detectar', 'Analizar', 'Proponer', 'Revisar', 'Ejecutar', 'Registrar')
STATUS_CHIPS = {'proposed': 'warn', 'executed': 'ok', 'rejected': 'plain', 'failed': 'bad', 'superseded': 'info'}
EVENTS = {'solicitudes': 'y cuando se crea una solicitud', 'conciliacion': 'y cuando se importa un extracto del banco'}
# Lo que cada agente lee (está en su código): texto, nombre de URL para abrirlo y perfiles que pueden abrirlo.
SOURCES = {
    'pronto_pago': (('Facturas de proveedores validadas con saldo', 'payables', S.FINANCE), ('Condiciones de pronto pago de cada factura', 'invoices', S.DOCUMENTS)),
    'conciliacion': (('Ingresos por identificar del extracto', 'bank', S.FINANCE), ('Facturas abiertas de cada cliente', 'portfolio', S.FINANCE),
                     ('Identificaciones anteriores de pagos', 'learning', VIEWERS)),
    'cobranza': (('Cartera vencida por cliente', 'portfolio', S.FINANCE), ('Contactos de cobranza de los clientes', 'parties', S.SALES),
                 ('Resultado de los recordatorios anteriores', 'learning', VIEWERS)),
    'vencimientos': (('Existencias por lote y fecha de vencimiento', 'stock', S.MANAGERS), ('Costo de compra de cada lote', 'invoices', S.DOCUMENTS)),
    'reposicion': (('Salidas semanales de cada referencia', 'movements', S.MANAGERS), ('Existencias, pedidos abiertos y mercancía en tránsito', 'replenishment', VIEWERS),
                   ('Tiempos de entrega de cada proveedor', 'purchase_orders', S.DOCUMENTS)),
    'solicitudes': (('Solicitudes pendientes', 'requests', S.MANAGERS), ('Lotes vigentes, del que vence primero al último', 'stock', S.MANAGERS)),
    'contable': (('Cola de envíos al sistema contable simulado', 'contable_status', S.FINANCE),),
    'avisos': (('Alertas vigentes de cada perfil', 'alerts', S.EVERYONE),),
    'resumen': (('Ventas emitidas y recaudo del día anterior', 'results', S.FINANCE), ('Cartera, cuentas por pagar, lotes y solicitudes al momento de la revisión', 'flow_map', S.EVERYONE)),
}
# Cifras que cada agente guarda al terminar una revisión: clave → (rótulo, tipo).
METRICS = {
    'ahorro_posible': ('Ahorro posible por pronto pago', 'cop'), 'facturas_con_descuento': ('Facturas con descuento vigente', 'num'), 'proveedores_vencidos': ('Proveedores con facturas vencidas', 'num'),
    'listo_para_aplicar': ('Listo para aplicar', 'cop'), 'ambiguos': ('Ingresos ambiguos', 'num'), 'sin_coincidencia': ('Ingresos sin coincidencia', 'num'),
    'por_recordar': ('Cartera por recordar', 'cop'), 'ya_recordados': ('Clientes con recordatorio reciente', 'num'), 'sin_contacto': ('Clientes sin contacto de cobranza', 'num'),
    'esperan_estrategia': ('Clientes que esperan el momento de su estrategia', 'num'),
    'lotes_vencidos': ('Lotes vencidos con saldo', 'num'), 'por_vencer': ('Lotes por vencer', 'num'),
    'pedir_ya': ('Referencias por pedir ya', 'num'), 'pedir_pronto': ('Referencias por pedir pronto', 'num'), 'pedidos_sugeridos': ('Pedidos sugeridos', 'num'),
    'sin_proveedor': ('Referencias sin proveedor habitual', 'num'), 'riesgo_vencimiento': ('Unidades en riesgo de vencer sin usarse', 'num'),
    'semanas_evaluadas': ('Semanas de pronóstico evaluadas', 'num'), 'aciertos': ('Semanas acertadas', 'num'),
    'listas': ('Solicitudes listas para reservar', 'num'), 'sin_existencias': ('Solicitudes sin existencias suficientes', 'num'),
    'revisados': ('Envíos revisados', 'num'), 'new': ('Avisos nuevos', 'num'), 'people': ('Personas avisadas', 'num'),
    'facturado_ayer': ('Facturado el día anterior, sin IVA', 'cop'), 'recaudado_ayer': ('Recaudado el día anterior', 'cop'),
    'cartera_vencida': ('Cartera vencida al momento de la revisión', 'cop'), 'proveedores_vencido': ('Vencido con proveedores al momento de la revisión', 'cop'),
    'propuestas': ('Propuestas por decidir', 'num'), 'descuentos': ('Descuentos por confirmar', 'num'), 'solicitudes': ('Solicitudes pendientes', 'num'),
}


ROLE_NAMES = dict(User.Role.choices)
ROLE_ORDER = ('requester', 'inventory', 'reception', 'billing', 'finance', 'admin', 'owner')


def _profiles(roles):
    """Nombres visibles de los perfiles, para decir quién puede decidir."""
    names = [ROLE_NAMES[r] for r in ROLE_ORDER if r in roles]
    return names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' y ' + names[-1]


def visible_agents(user):
    return [a for a in REGISTRY.values() if runner.can_see(user, a)]


def _url(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return None


def _sources(agent, user):
    return [{'label': label, 'url': _url(name) if user.role in roles else None} for label, name, roles in SOURCES.get(agent.key, ())]


def _metrics(run):
    """Las cifras que el agente guardó en su última revisión, con rótulo legible. Lo que no se reconoce no se muestra."""
    out = []
    for key, value in ((run.metrics or {}) if run else {}).items():
        if key not in METRICS or isinstance(value, (list, dict)):
            continue
        label, kind = METRICS[key]
        if kind == 'cop':
            try:
                value = S.cop(Decimal(str(value)))
            except InvalidOperation:
                continue
        out.append({'label': label, 'value': value})
    return out


def trigger_label(agent):
    extra = EVENTS.get(agent.key)
    return agent.schedule_label + (f' {extra}' if extra else '')


def stages(action):
    """Dónde va una propuesta en detectar → analizar → proponer → revisar → ejecutar → registrar."""
    status = action.status
    done = {'proposed': (0, 1, 2), 'executed': (0, 1, 2, 3, 4, 5), 'rejected': (0, 1, 2, 3, 5), 'failed': (0, 1, 2, 3, 5),
            'superseded': (0, 1, 2, 3, 5) if action.decided_by_id else (0, 1, 2, 5)}[status]
    current = {'proposed': 3, 'failed': 4}.get(status)
    notes = {'proposed': 'Espera la revisión de un perfil autorizado.', 'executed': 'Aprobada, ejecutada y registrada en la auditoría.',
             'rejected': 'Se descartó en la revisión: no se ejecutó nada.', 'failed': 'Se aprobó, pero la ejecución no pasó la validación: no se cambió nada.',
             'superseded': ('Al aprobarla se volvió a comprobar y la situación había cambiado: no se ejecutó nada.' if action.decided_by_id
                            else 'Una revisión posterior del agente encontró que la situación cambió: se retiró sin ejecutar.')}
    return {'steps': [{'label': label, 'state': 'is-current' if i == current else 'is-done' if i in done else '', 'skipped': i not in done and i != current and status != 'proposed'}
                      for i, label in enumerate(STAGES)], 'note': notes[status]}


def next_action(card):
    """Siguiente acción de un agente, en una frase."""
    agent, cfg, last = card['agent'], card['cfg'], card['last']
    if not cfg.enabled:
        return 'En pausa: no revisa hasta que se active.'
    if last and last.status == AgentRun.Status.FAILED:
        return 'Revisar el error de la última ejecución.'
    if card['pending']:
        return f'Decidir {card["pending"]} propuesta{"s" if card["pending"] != 1 else ""}.' if card['can_run'] else f'{card["pending"]} propuesta{"s" if card["pending"] != 1 else ""} espera{"n" if card["pending"] != 1 else ""} a un perfil autorizado.'
    when = card['next']
    return ('Actúa sola. ' if agent.automatic else '') + (f'Próxima revisión: {timezone.localtime(when):%d/%m %H:%M}.' if when else '')


@login_required
def agents_home(request):
    user = request.user
    agents = visible_agents(user)
    if not agents:
        raise PermissionDenied('Su perfil no tiene agentes asignados.')
    keys = [a.key for a in agents]
    pending = list(runner.pending_for(user).order_by('agent', '-benefit', 'created_at'))
    for p in pending:
        p.track = stages(p)
    by_key = {a.key: a for a in agents}
    configs = {c.key: c for c in AgentConfig.objects.filter(key__in=keys)}
    open_counts = {}
    for key in AgentAction.objects.filter(agent__in=keys, status=AgentAction.Status.PROPOSED).values_list('agent', flat=True):
        open_counts[key] = open_counts.get(key, 0) + 1
    cards = []
    for a in agents:
        last = AgentRun.objects.filter(agent=a.key).first()
        cfg = configs.get(a.key) or runner.config(a.key)
        values = params_for(a, cfg.params)
        card = {'agent': a, 'cfg': cfg, 'last': last, 'next': runner.next_run(a) if cfg.enabled else None,
                'pending': open_counts.get(a.key, 0), 'can_run': runner.can_approve(user, a), 'trigger': trigger_label(a),
                'sources': _sources(a, user), 'metrics': _metrics(last if last and last.status == AgentRun.Status.SUCCEEDED else None),
                'first': next((p for p in pending if p.agent == a.key), None),
                'params': [{'p': p, 'value': values[p.key]} for p in a.params]}
        card['state'] = ('pause', 'En pausa') if not cfg.enabled else ('bad', 'Con error') if last and last.status == AgentRun.Status.FAILED else ('ok', 'Activo')
        card['next_action'] = next_action(card)
        cards.append(card)
    groups = []
    for a in agents:
        items = [p for p in pending if p.agent == a.key]
        if items:
            groups.append({'agent': a, 'items': items, 'benefit': sum((p.benefit or Decimal(0) for p in items), Decimal(0))})
    since = timezone.now() - timedelta(days=7)
    executed = AgentAction.objects.filter(agent__in=keys, status=AgentAction.Status.EXECUTED, decided_at__gte=since)
    done = list(AgentAction.objects.filter(agent__in=keys).exclude(status=AgentAction.Status.PROPOSED).select_related('decided_by').order_by('-decided_at', '-pk')[:8])
    resumen = AgentRun.objects.filter(agent='resumen', status='succeeded').first() if 'resumen' in keys else None
    return render(request, 'agentes/agents.html', {
        'title': 'Agentes', 'cards': cards, 'groups': groups, 'done': done, 'names': {k: a.name for k, a in by_key.items()},
        'resumen': resumen, 'resumen_metrics': _metrics(resumen), 'resumen_day': timezone.localtime(resumen.started_at).date() - timedelta(days=1) if resumen else None,
        'runs': AgentRun.objects.filter(agent__in=keys).exclude(agent='avisos', proposed=0, status='succeeded').select_related('actor')[:12],
        'k_pending': len(pending), 'k_benefit': sum((p.benefit or Decimal(0) for p in pending), Decimal(0)),
        'k_executed': executed.count(), 'k_executed_value': executed.aggregate(s=Sum('benefit'))['s'] or Decimal(0),
        'k_active': sum(1 for c in cards if c['cfg'].enabled), 'is_admin': user.role in S.ADMINS, 'today': timezone.localdate(), 'stage_names': STAGES,
    })


# -------------------------------------------------------------------- detalle
def _evidence(action, user):
    """Registros que sustentan la propuesta, leídos de su `payload`. Solo se enlaza lo que el perfil puede abrir."""
    role, p, out = user.role, action.payload or {}, []

    def add(label, value, name=None, args=(), roles=()):
        out.append({'label': label, 'value': value, 'url': _url(name, *args) if name and role in roles else None})

    kind = action.kind
    if kind == 'schedule_payment':
        from inventory.models import Invoice, Supplier, with_payable_balance
        supplier = Supplier.objects.filter(pk=p.get('supplier_id')).first()
        if supplier:
            add('Proveedor', supplier.name, 'supplier_account', (supplier.pk,), S.FINANCE)
        for inv in with_payable_balance(Invoice.objects.filter(pk__in=p.get('invoices', []))):
            add('Factura', f'{inv.number} · saldo hoy {S.cop(inv.balance)} · vence el {inv.due_date:%d/%m/%Y}', 'invoice', (inv.pk,), S.DOCUMENTS)
        if p.get('scheduled_on'):
            add('Fecha propuesta para el pago', date.fromisoformat(p['scheduled_on']).strftime('%d/%m/%Y'))
    elif kind in ('apply_bank', 'link_counter'):
        from cartera.models import BankTransaction, Party
        txn = BankTransaction.objects.filter(pk=p.get('txn_id')).first()
        if txn:
            add('Ingreso del extracto', f'{txn.date:%d/%m/%Y} · {S.cop(txn.amount)} · {txn.holder_name or txn.description or "sin titular"} · {txn.get_status_display().lower()}',
                'bank_txn', (txn.pk,), S.FINANCE)
        party = Party.objects.filter(pk=p.get('party_id')).first()
        if party:
            add('Cliente sugerido', party.name, 'party', (party.pk,), S.SALES)
        if kind == 'link_counter':
            add('Recaudos de contado que coinciden', str(len(p.get('collection_ids', []))))
    elif kind == 'collection_reminder':
        from cartera.models import Party, Receivable, with_balance
        party = Party.objects.filter(pk=p.get('party_id')).first()
        if party:
            add('Cliente', party.name, 'party', (party.pk,), S.SALES)
        for r in with_balance(Receivable.objects.filter(pk__in=p.get('receivables', []))):
            add('Factura vencida', f'{r.document} · saldo hoy {S.cop(r.balance)} · venció el {r.due_date:%d/%m/%Y}', 'sale' if r.sale_id else None, (r.sale_id,) if r.sale_id else (), S.SALES)
        if p.get('contact'):
            add('Contacto registrado', f'{p.get("channel") or "sin canal"} · {p["contact"]} (envío simulado)')
        strategy = p.get('strategy') or {}
        if strategy.get('evidence'):
            add('Lo aprendido de recordatorios anteriores', strategy['evidence'], 'learning', (), VIEWERS)
    elif kind == 'writeoff_expired':
        from inventory.models import Batch
        b = Batch.objects.select_related('product').filter(pk=p.get('batch_id')).first()
        if b:
            add('Lote', f'{b.product.name} · lote {b.lot} · venció el {b.expiry:%d/%m/%Y} · hoy tiene {b.physical} en físico y {b.reserved} reservadas', 'trace', (b.pk,), S.MANAGERS)
    elif kind == 'reserve_request':
        from inventory.models import Batch, SupplyRequest
        r = SupplyRequest.objects.select_related('product', 'requester').filter(pk=p.get('request_id')).first()
        if r:
            add('Solicitud', f'{r.requester.display_name} · {r.product.qty(r.quantity)} de {r.product.name} · {r.get_status_display().lower()}',
                'approve' if r.status == 'pending' else 'requests', (r.pk,) if r.status == 'pending' else (), S.MANAGERS)
        b = Batch.objects.filter(pk=p.get('batch_id')).first()
        if b:
            add('Lote sugerido', f'Lote {b.lot} · vence el {b.expiry:%d/%m/%Y} · {b.available} disponibles hoy', 'trace', (b.pk,), S.MANAGERS)
    elif kind == 'purchase_order':
        from inventory.models import Product, Supplier
        supplier = Supplier.objects.filter(pk=p.get('supplier_id')).first()
        if supplier:
            add('Proveedor', supplier.name, 'suppliers', (), S.DOCUMENTS)
        products = list(Product.objects.filter(pk__in=p.get('products', [])))
        available = {x.pk: x.available for x in S.availability(active_only=False)} if products else {}
        for prod in products:
            add('Referencia', f'{prod.name} · {prod.code} · hoy hay {prod.qty(available.get(prod.pk, 0))} disponibles', 'forecast', (prod.pk,), VIEWERS)
    return out


def _timeline(action):
    """Historial de la propuesta: lo que quedó registrado, en orden."""
    from inventory.models import Audit
    rows = []
    run = action.run
    origin = 'Revisión del agente.'
    if run:
        origin = f'Revisión {run.get_trigger_display().lower()} del agente' + (f', pedida por {run.actor.display_name}.' if run.actor_id else '.')
    rows.append({'when': action.created_at, 'title': 'Detectada y propuesta', 'detail': origin})
    if action.refreshed_at - action.created_at > timedelta(minutes=1) and (action.decided_at is None or action.refreshed_at < action.decided_at):
        rows.append({'when': action.refreshed_at, 'title': 'Recalculada', 'detail': 'Una revisión posterior actualizó sus cifras con los datos de ese momento.'})
    if action.decided_at:
        who = action.decided_by.display_name if action.decided_by_id else 'El agente, al recalcular'
        verb = {'executed': 'aprobó la propuesta', 'rejected': 'descartó la propuesta', 'failed': 'aprobó la propuesta', 'superseded': 'pidió ejecutarla' if action.decided_by_id else 'la retiró'}[action.status]
        rows.append({'when': action.decided_at, 'title': f'{who} {verb}', 'detail': action.note})
        outcome = {'executed': 'Ejecutada', 'rejected': 'Sin ejecución', 'failed': 'No se pudo ejecutar', 'superseded': 'Ya no aplica'}[action.status]
        rows.append({'when': action.decided_at, 'title': outcome, 'detail': (action.result or {}).get('message', '') or ('No se cambió ningún dato.' if action.status != 'executed' else '')})
    for a in Audit.objects.filter(target=f'agent-action-{action.pk}').select_related('actor').order_by('created_at'):
        rows.append({'when': a.created_at, 'title': f'Auditoría: {a.event}', 'detail': a.actor.display_name if a.actor_id else ''})
    return sorted(rows, key=lambda r: r['when'])


@login_required
def action_detail(request, pk):
    action = AgentAction.objects.select_related('run__actor', 'decided_by').filter(pk=pk).first()
    agent = REGISTRY.get(action.agent) if action else None
    if action is None or agent is None:
        raise Http404
    if not runner.can_see(request.user, agent):
        raise PermissionDenied('Su perfil no ve las propuestas de este agente.')
    can_decide = action.status == AgentAction.Status.PROPOSED and runner.can_approve(request.user, agent)
    return render(request, 'agentes/action.html', {
        'title': 'Propuesta', 'section': 'agents', 'nav_group': 'inteligencia', 'a': action, 'agent': agent, 'track': stages(action), 'chip': STATUS_CHIPS[action.status], 'can_decide': can_decide,
        'evidence': _evidence(action, request.user), 'timeline': _timeline(action), 'approvers': _profiles(agent.approve_roles),
        'message': (action.payload or {}).get('message') if action.kind == 'collection_reminder' else '',
    })


# ------------------------------------------------------------------ historial
def _day(value):
    try:
        return date.fromisoformat(value or '')
    except ValueError:
        return None


@login_required
def history(request):
    """Historial de decisiones: quién, cuándo, qué se ejecutó y con qué resultado. Filtrable y paginado."""
    agents = visible_agents(request.user)
    if not agents:
        raise PermissionDenied('Su perfil no tiene agentes asignados.')
    keys = [a.key for a in agents]
    f = {k: request.GET.get(k, '').strip() for k in ('agente', 'estado', 'quien', 'desde', 'hasta', 'q')}
    qs = AgentAction.objects.filter(agent__in=keys).exclude(status=AgentAction.Status.PROPOSED).select_related('decided_by')
    statuses = [(v, l) for v, l in AgentAction.Status.choices if v != 'proposed']
    if f['agente'] in keys:
        qs = qs.filter(agent=f['agente'])
    if f['estado'] in dict(statuses):
        qs = qs.filter(status=f['estado'])
    if f['quien'] == 'auto':
        qs = qs.filter(decided_by__isnull=True)
    elif f['quien'].isdigit():
        qs = qs.filter(decided_by_id=int(f['quien']))
    since, until = _day(f['desde']), _day(f['hasta'])
    if since:
        qs = qs.filter(decided_at__date__gte=since)
    if until:
        qs = qs.filter(decided_at__date__lte=until)
    if f['q']:
        qs = qs.filter(Q(title__icontains=f['q']) | Q(detail__icontains=f['q']) | Q(note__icontains=f['q']))
    base = AgentAction.objects.filter(agent__in=keys).exclude(status=AgentAction.Status.PROPOSED)
    page = Paginator(qs.order_by('-decided_at', '-pk'), 25).get_page(request.GET.get('pagina'))
    for a in page:
        a.chip = STATUS_CHIPS[a.status]
    query = request.GET.copy()
    query.pop('pagina', None)
    people = User.objects.filter(pk__in=base.exclude(decided_by__isnull=True).values('decided_by')).order_by('first_name', 'username')
    return render(request, 'agentes/history.html', {
        'title': 'Historial de decisiones', 'section': 'agents_history', 'nav_group': 'inteligencia', 'page': page, 'filters': f, 'query': query.urlencode(), 'filtered': any(f.values()),
        'agents': agents, 'statuses': statuses, 'people': people, 'names': {a.key: a.name for a in agents},
        'counts': {s: base.filter(status=s).count() for s, _ in statuses}, 'total': base.count(),
    })


# ------------------------------------------------------------------ decisiones
def _already(action):
    who = f' por {action.decided_by.display_name}' if action.decided_by_id else ''
    when = f' el {timezone.localtime(action.decided_at):%d/%m/%Y a las %H:%M}' if action.decided_at else ''
    return f'«{action.title}» ya estaba decidida ({action.get_status_display().lower()}{who}{when}). No se repitió la operación.'


@login_required
@require_POST
def decide(request):
    """Una sola ruta para aprobar o descartar una propuesta, o aprobar varias seleccionadas.

    Al aprobar, la herramienta vuelve a comprobar los datos del momento (existencias, saldos, permisos, vigencia).
    Cada resultado se explica: ejecutada, ya no aplica (y por qué) o no se pudo ejecutar; repetir el envío no duplica."""
    action = request.POST.get('accion', '')
    note = request.POST.get('nota', '')
    if ':' in action:
        verb, pk = action.split(':', 1)
        ids, approve = [pk], verb == 'aprobar'
    elif action == 'aprobar_seleccion':
        ids, approve = request.POST.getlist('seleccion'), True
    else:
        ids, approve = [], True
    back = redirect('agents')
    if request.POST.get('volver') == 'detalle' and len(ids) == 1 and ids[0].isdigit():
        back = redirect('agent_action', pk=int(ids[0]))
    if not ids:
        messages.info(request, 'Seleccione al menos una propuesta.')
        return back
    executed, rejected = [], []
    for pk in ids:
        try:
            a = runner.decide(request.user, int(pk), approve, note)
        except (ValueError, AgentAction.DoesNotExist):
            continue
        except PermissionDenied as e:
            messages.error(request, str(e))
            continue
        except ValidationError as e:
            current = AgentAction.objects.select_related('decided_by').filter(pk=pk).first() if str(pk).isdigit() else None
            if current is not None and current.status != AgentAction.Status.PROPOSED:
                messages.info(request, _already(current))
            else:
                messages.error(request, ' '.join(e.messages))
            continue
        if a.status == AgentAction.Status.EXECUTED:
            executed.append(a)
        elif a.status == AgentAction.Status.REJECTED:
            rejected.append(a)
        elif a.status == AgentAction.Status.SUPERSEDED:
            messages.warning(request, f'Ya no aplica: «{a.title}». {a.result.get("message", "La situación cambió desde que se preparó.")} No se cambió ningún dato.')
        else:
            messages.error(request, f'No se pudo ejecutar «{a.title}»: {a.result.get("message", "la validación del módulo no lo permitió.")} No se cambió ningún dato.')
    if len(executed) == 1:
        messages.success(request, executed[0].result.get('message', 'Propuesta ejecutada.'))
    elif executed:
        messages.success(request, f'{len(executed)} propuestas ejecutadas. El resultado de cada una está en el historial de decisiones.')
    if rejected:
        messages.success(request, f'{len(rejected)} propuesta{"s" if len(rejected) != 1 else ""} descartada{"s" if len(rejected) != 1 else ""}. No se ejecutó nada.')
    return back


@login_required
@require_POST
def run_now(request, key):
    agent = REGISTRY.get(key)
    if agent is None or not runner.can_approve(request.user, agent):
        raise PermissionDenied('Su perfil no puede ejecutar este agente.')
    run = runner.run_agent(key, 'manual', actor=request.user)
    if run and run.status == AgentRun.Status.SUCCEEDED:
        messages.success(request, f'{agent.name}: {run.summary}')
    else:
        messages.error(request, f'{agent.name} no pudo completar la revisión: {run.error if run else "intente de nuevo"}')
    return redirect('agents')


@login_required
@require_POST
def configure(request, key):
    agent = REGISTRY.get(key)
    if agent is None or request.user.role not in S.ADMINS:
        raise PermissionDenied('Solo la administración y la dirección ajustan los agentes.')
    cfg = runner.config(key)
    before = (cfg.enabled, dict(cfg.params))
    cfg.enabled = request.POST.get('activo') == 'on'
    params = {}
    for p in agent.params:
        try:
            params[p.key] = min(max(int(request.POST.get(p.key, p.default)), p.minimum), p.maximum)
        except (TypeError, ValueError):
            params[p.key] = p.default
    cfg.params, cfg.updated_by = params, request.user
    cfg.save()
    S.event(request.user, 'Agente ajustado', f'agent-{key}', f'{agent.name}: activo {before[0]}→{cfg.enabled}; {before[1]}→{params}'[:300])
    messages.success(request, f'{agent.name}: {"activo" if cfg.enabled else "en pausa"}. Ajustes guardados.')
    return redirect('agents')
