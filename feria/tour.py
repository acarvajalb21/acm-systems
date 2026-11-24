"""Recorrido guiado de la demostración: siete pasos, entre cinco y siete minutos.

Cada paso enlaza al registro que el escenario dejó preparado (`DemoState.cases`). La lectura es tolerante: si el
escenario no se ha generado, falta una clave o el registro ya no existe, el paso lleva a la pantalla general del
módulo. El recorrido no cambia permisos: cuando un paso necesita otro perfil lo dice y remite al selector de
perfiles de la demostración.
"""
from django.urls import NoReverseMatch, reverse
from inventory import services as S
from .flow import ROLE_LABELS, profiles

SESSION_KEY = 'recorrido'
TOTAL = 7
VIEWERS = ('inventory', 'finance', 'admin', 'owner')   # quienes abren reposición, pronósticos y «lo aprendido»


def cases():
    """Casos preparados del escenario vigente, o un diccionario vacío si todavía no hay escenario."""
    try:
        from .models import DemoState
        found = DemoState.current().cases
        return found if isinstance(found, dict) else {}
    except Exception:
        return {}


def case(data, path, default=None):
    node = data
    for part in path.split('.'):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def local(url):
    """Solo rutas de la propia plataforma (los casos guardan rutas locales; cualquier otra cosa se descarta)."""
    return url if isinstance(url, str) and url.startswith('/') and not url.startswith('//') and '\\' not in url else None


def _url(name, *args, suffix=''):
    try:
        return reverse(name, args=args) + suffix
    except NoReverseMatch:
        return None


def _action_url(action_id, status=None):
    """Detalle de una propuesta de agente, si existe (y, si se pide, si sigue en ese estado)."""
    if not action_id:
        return None
    from agentes.models import AgentAction
    qs = AgentAction.objects.filter(pk=action_id)
    if status:
        qs = qs.filter(status=status)
    return _url('agent_action', action_id) if qs.exists() else None


def _agents(user):
    from agentes import runner
    from agentes.agents import REGISTRY
    return any(runner.can_see(user, a) for a in REGISTRY.values())


def _step_1(data, user):
    return {'url': _url('home'), 'prepared': True, 'extra': [('Mapa de flujo', _url('flow_map'), S.EVERYONE)]}


def _step_2(data, user):
    url = local(case(data, 'faltante.forecast_url'))
    name = case(data, 'faltante.product_name')
    return {'url': url or _url('replenishment'), 'prepared': bool(url), 'record': name and f'Referencia preparada: {name}',
            'extra': [('Reposición', _url('replenishment'), VIEWERS)]}


def _open_proposal(agent):
    """La propuesta abierta de mayor valor de un agente: respaldo cuando la preparada ya se decidió."""
    from agentes.models import AgentAction
    found = AgentAction.objects.filter(agent=agent, status=AgentAction.Status.PROPOSED).order_by('-amount', 'pk').first()
    return _url('agent_action', found.pk) if found else None


def _step_3(data, user):
    action = case(data, 'reposicion.action_id') or case(data, 'faltante.action_id')
    prepared = _action_url(action, 'proposed')
    url = prepared or _open_proposal('reposicion') or _action_url(action)
    record = 'Propuesta preparada del agente de reposición' if prepared else 'Propuesta abierta del agente de reposición' if url and url != _action_url(action) else (
        'La propuesta preparada ya se decidió: muestra su resultado' if url else None)
    return {'url': url or local(case(data, 'reposicion.url')) or _url('agents', suffix='#por-aprobar'), 'prepared': bool(url),
            'record': record, 'extra': [('Pedidos a proveedores', _url('purchase_orders'), S.DOCUMENTS)]}


def _step_4(data, user):
    url = local(case(data, 'recepcion.url'))
    number = case(data, 'recepcion.invoice_number')
    codes = case(data, 'recepcion.codes_plain') or case(data, 'recepcion.codes') or []
    lines = []
    for c in codes if isinstance(codes, (list, tuple)) else []:
        if isinstance(c, dict):
            label = c.get('label') or ' · '.join(str(x) for x in (c.get('product'), c.get('lot') and f'lote {c["lot"]}', c.get('units') and f'{c["units"]} unidades por caja') if x)
            lines.append({'code': str(c.get('code') or ''), 'label': str(label)})
        elif isinstance(c, (list, tuple)) and c:
            lines.append({'code': str(c[0]), 'label': str(c[1]) if len(c) > 1 else ''})
        else:
            lines.append({'code': str(c), 'label': ''})
    return {'url': url or _url('reception'), 'prepared': bool(url), 'record': number and f'Factura preparada: {number}', 'codes': [c for c in lines if c['code']][:12],
            'extra': [('Existencias por lote', _url('stock'), S.MANAGERS), ('Documentos de prueba', local(case(data, 'documentos.url')), S.EVERYONE)]}


def _step_5(data, user):
    action = _action_url(case(data, 'banco.claro.action_id'), 'proposed')
    url = action or local(case(data, 'banco.claro.url'))
    record = 'Ingreso preparado que menciona su factura' if url else None
    if not url:
        url = _open_proposal('conciliacion')
        record = 'Propuesta abierta del agente de conciliación' if url else None
    return {'url': url or _url('bank'), 'prepared': bool(url), 'record': record,
            'extra': [('Banco y recaudos', _url('bank'), S.FINANCE), ('Cartera', _url('portfolio'), S.FINANCE)]}


def _step_6(data, user):
    return {'url': _url('copilot') or _url('home'), 'prepared': True, 'extra': []}


def _step_7(data, user):
    return {'url': _url('agents_history'), 'prepared': True, 'extra': [('Lo aprendido', _url('learning'), VIEWERS), ('Metodología', _url('methodology'), S.EVERYONE)]}


# número, título, perfiles que pueden abrirlo (None = cualquiera con agentes), perfil sugerido si hay que cambiar, qué mostrar, guion hablado, resolución
STEPS = (
    (1, 'El problema y el tablero general', S.EVERYONE, '',
     'El Resumen del perfil y, desde su franja, el mapa de flujo completo.',
     'Una distribuidora compra, almacena, vende y cobra con información repartida en varias áreas. Aquí todo queda en un solo lugar: qué necesita atención, '
     'qué puede ocurrir y qué se recomienda hacer. El mapa muestra el recorrido de la solicitud al cobro y dónde se está acumulando el trabajo.',
     _step_1),
    (2, 'Una referencia con riesgo de faltante', VIEWERS, 'inventory',
     'El pronóstico de la referencia preparada: consumo semanal, banda de incertidumbre y punto de pedido.',
     'Esta referencia sale rápido y lo disponible no alcanza hasta que llegue un nuevo pedido. El pronóstico usa las salidas semanales registradas y muestra '
     'su incertidumbre; con el tiempo de entrega del proveedor se calcula cuándo y cuánto pedir.',
     _step_2),
    (3, 'El agente de reposición propone un pedido', S.MANAGERS, 'inventory',
     'La propuesta con sus evidencias; al aprobarla se prepara el pedido y su envío queda simulado.',
     'El agente no decide solo: deja una propuesta con los datos que consultó. Al aprobarla, la plataforma vuelve a calcular con la información de este momento. '
     'Si algo cambió, lo dice. El pedido al proveedor es simulado; nada sale de la plataforma.',
     _step_3),
    (4, 'Recibir la mercancía', S.RECEIVERS, 'reception',
     'La factura por recibir; se teclean los códigos de prueba de las cajas (sin cámara) y se confirma la entrada.',
     'Cuando llega la mercancía se recibe contra la factura. Cada código suma unidades a su lote con su fecha de vencimiento. Al confirmar, las existencias '
     'cambian y el lote queda con su trazabilidad: de qué factura vino y cada movimiento que tenga después.',
     _step_4),
    (5, 'Una conciliación sugerida', S.FINANCE, 'finance',
     'El ingreso del extracto con el cliente y las facturas sugeridas; se confirma su aplicación local.',
     'El extracto del banco trae un ingreso. La plataforma lo cruza con la cartera y sugiere a qué cliente y a qué facturas corresponde, con sus razones. '
     'Al confirmarlo, el saldo del cliente baja y queda un recibo en el sistema contable simulado.',
     _step_5),
    (6, 'El copiloto responde con fuentes', S.EVERYONE, '',
     'Una pregunta sobre la operación y, debajo de la respuesta, las fuentes consultadas.',
     'El copiloto responde preguntas con los datos de la plataforma y con los permisos de quien pregunta. Cada respuesta enumera los registros que consultó, '
     'para comprobarla en un clic. Si no tiene el dato, lo dice en vez de suponerlo.',
     _step_6),
    (7, 'Las decisiones y lo que aprende el sistema', None, 'owner',
     'El historial de decisiones (quién, cuándo, qué se ejecutó) y la pantalla Lo aprendido.',
     'Todo lo aprobado, descartado o que dejó de aplicar queda registrado con su resultado. Con esas decisiones y con lo que pasa después, los modelos '
     'ajustan sus pronósticos y sugerencias. Son resultados de funcionamiento sobre datos sintéticos, no de una operación real.',
     _step_7),
)


def steps(user, data=None):
    """Los siete pasos resueltos para una persona: adónde lleva cada uno y si su perfil puede abrirlo."""
    data = cases() if data is None else data
    out = []
    for n, title, roles, switch, show, script, resolve in STEPS:
        allowed = _agents(user) if roles is None else user.role in roles
        info = resolve(data, user)
        needs = profiles(VIEWERS if roles is None else roles, ' o ')
        out.append({'n': n, 'title': title, 'show': show, 'script': script, 'allowed': allowed, 'needs': needs, 'switch_role': switch,
                    'switch_label': ROLE_LABELS.get(switch, ''),
                    'url': info['url'] if allowed else None, 'prepared': info.get('prepared', False), 'record': info.get('record') if allowed else None,
                    'codes': info.get('codes', []) if allowed else [], 'extra': [(label, url) for label, url, who in info.get('extra', []) if url and user.role in who] if allowed else []})
    return out


def bar(step):
    """Lo que la barra del recorrido necesita en cada pantalla (se guarda en la sesión)."""
    return {'n': step['n'], 'total': TOTAL, 'title': step['title'], 'show': step['show'], 'last': step['n'] == TOTAL}
