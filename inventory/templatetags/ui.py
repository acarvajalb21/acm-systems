import uuid
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from django import template
from django.templatetags.static import static
from django.utils.html import format_html
from inventory.services import pct as _pct

register = template.Library()


@register.filter
def cop(value):
    """Pesos colombianos: $1.234.567 (centavos solo cuando existen)."""
    try:
        value = Decimal(value or 0)
    except (InvalidOperation, TypeError, ValueError):
        return value
    sign = '−' if value < 0 else ''
    value = abs(value)
    whole = f'{value:,.0f}' if value == value.to_integral_value() else f'{value:,.2f}'
    return sign + '$' + whole.replace(',', '_').replace('.', ',').replace('_', '.')


@register.filter
def pesos(value):
    """Pesos colombianos redondeados al peso, sin centavos: para indicadores, tableros y gráficos.

    Los documentos (facturas, pagos, extractos) usan `cop`, que conserva los centavos cuando existen."""
    try:
        value = Decimal(value or 0)
    except (InvalidOperation, TypeError, ValueError):
        return value
    value = value.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    sign = '−' if value < 0 else ''
    return sign + '$' + f'{abs(value):,.0f}'.replace(',', '.')


@register.filter
def num(value):
    try:
        return f'{int(value):,}'.replace(',', '.')
    except (TypeError, ValueError):
        return value


@register.filter
def pct(value):
    return _pct(value or 0) + ' %'


CONNECTOR_STATES = {'accepted': 'Aceptada', 'draft': 'Sin validar', 'rejected': 'Rechazada', 'pending': 'En proceso', 'notsent': 'Sin envío registrado', 'sent': 'Enviado'}


@register.filter
def estado_conector(value):
    """Estado que devuelve el conector contable simulado («Accepted», «NotSent»…), en español. Un texto ya redactado se deja igual."""
    text = str(value or '').strip()
    return CONNECTOR_STATES.get(text.lower(), text)


@register.filter
def fecha(value):
    """Fecha como dd/mm/aaaa a partir de una fecha o de un texto ISO (AAAA-MM-DD); cualquier otro texto se deja igual."""
    if hasattr(value, 'strftime'):
        return value.strftime('%d/%m/%Y')
    text = str(value or '')
    try:
        return date.fromisoformat(text[:10]).strftime('%d/%m/%Y') if len(text) >= 10 else text
    except ValueError:
        return text


@register.filter
def absval(value):
    return abs(value)


@register.filter
def qty(value, product):
    """Cantidad con la unidad de registro del producto concordada: {{ linea.quantity|qty:linea.product }} → «24 galones»."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return value
    unit = getattr(product, 'unit', '') if n == 1 else getattr(product, 'units', '')
    return f'{n:,}'.replace(',', '.') + (f' {unit}' if unit else '')


@register.simple_tag
def icon(name, cls=''):
    return format_html('<svg class="icon {}" aria-hidden="true" focusable="false"><use href="{}#{}"></use></svg>', cls, static('iconos.svg'), name)


@register.simple_tag
def new_token():
    """Clave única para formularios de un botón (evita registrar dos veces la misma operación)."""
    return uuid.uuid4()


@register.filter
def get_item(mapping, key):
    """Valor de un diccionario por clave (o la clave si no está)."""
    return mapping.get(key, key) if hasattr(mapping, 'get') else key
