"""Documentos sintéticos de compra: la «Factura de demostración» en PDF y en XML.

Sirven para mostrar la carga de facturas sin usar documentos reales. El PDF es de texto, con campos etiquetados
(`pdf_invoice.py` lo lee con pdfplumber); el XML es un UBL Invoice mínimo que `einvoice.parse_invoice_xml` ya sabe
leer. Ninguno tiene validez fiscal y ambos lo dicen en su contenido.

Datos de entrada (`data`), iguales para los dos formatos:

    supplier_name, supplier_nit          proveedor (NIT solo dígitos, sin dígito de verificación)
    buyer_name, buyer_nit                opcionales; por defecto, la empresa de la demostración
    number, cufe                         número de factura y código único (opcional)
    issue_date, due_date                 fechas (date o 'aaaa-mm-dd')
    order_number, ordered_on             orden de compra y fecha del pedido (opcionales)
    lines                                [{supplier_code, description, quantity, unit_price, discount, tax_rate,
                                           expected_lot, expected_expiry}]
    global_discount, retention           opcionales
    payment_days, discount_base, discount_tiers, payment_terms_text
                                         condiciones de pago: plazo, base ('subtotal' o 'total') y tramos
                                         [{'days': 10, 'percent': '3'}] de pronto pago

`expected_reading(data, source)` devuelve lo que cada lector debe entregar para esos datos (ida y vuelta).
"""
import textwrap
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from xml.sax.saxutils import escape
from django.conf import settings
from contable.simulator import tiny_pdf
from .models import nit_check_digit

CENT = Decimal('0.01')
TITLE = 'FACTURA DE DEMOSTRACIÓN'
DISCLAIMER = 'Documento de demostración sin validez fiscal'
BUYER_NIT = '900555010'
WRAP = 78
LINES_PER_PAGE = 52
BASES = {'subtotal': 'Subtotal antes de impuestos', 'total': 'Total de la factura'}


def _money(value):
    return Decimal(str(value or 0)).quantize(CENT, ROUND_HALF_UP)


def _day(value):
    if not value:
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _clean(text):
    return ' '.join(str(text or '').split())


def _percent(value):
    """'3', '2.5': sin ceros sobrantes."""
    text = format(Decimal(str(value)).normalize(), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def normalize(data):
    """Datos completos y con tipos fijos: fechas `date`, valores `Decimal`, textos sin espacios repetidos."""
    lines = []
    for raw in data['lines']:
        quantity = int(raw['quantity'])
        price, discount, rate = _money(raw['unit_price']), _money(raw.get('discount')), _money(raw.get('tax_rate'))
        net = _money(quantity * price - discount)
        lines.append({'supplier_code': _clean(raw.get('supplier_code')), 'description': _clean(raw.get('description'))[:200], 'quantity': quantity,
                      'unit_price': price, 'discount': discount, 'tax_rate': rate, 'expected_lot': _clean(raw.get('expected_lot')).upper(),
                      'expected_expiry': _day(raw.get('expected_expiry')), 'net': net, 'tax': _money(net * rate / 100)})
    subtotal = _money(sum((l['net'] for l in lines), Decimal(0)))
    tax = _money(sum((l['tax'] for l in lines), Decimal(0)))
    global_discount, retention = _money(data.get('global_discount')), _money(data.get('retention'))
    issue = _day(data['issue_date'])
    tiers = sorted(({'days': int(t['days']), 'percent': _percent(t['percent'])} for t in data.get('discount_tiers') or []), key=lambda t: t['days'])
    days = data.get('payment_days')
    due = _day(data.get('due_date'))
    if days is None and due:
        days = (due - issue).days
    return {
        'supplier_name': _clean(data['supplier_name'])[:160], 'supplier_nit': ''.join(c for c in str(data['supplier_nit']) if c.isdigit()),
        'buyer_name': _clean(data.get('buyer_name') or settings.COMPANY_NAME), 'buyer_nit': ''.join(c for c in str(data.get('buyer_nit') or BUYER_NIT) if c.isdigit()),
        'number': _clean(data['number'])[:40], 'cufe': _clean(data.get('cufe')).replace(' ', '')[:120], 'issue_date': issue, 'due_date': due,
        'order_number': _clean(data.get('order_number'))[:40], 'ordered_on': _day(data.get('ordered_on')), 'currency': 'COP',
        'lines': lines, 'subtotal': subtotal, 'tax': tax, 'global_discount': global_discount, 'retention': retention,
        'total': _money(subtotal - global_discount + tax - retention),
        'payment_days': int(days) if days is not None else None, 'discount_base': data.get('discount_base') or ('subtotal' if tiers else ''),
        'discount_tiers': tiers, 'payment_terms_text': _clean(data.get('payment_terms_text')),
    }


def _cop(value):
    """$ 1.234.567,00"""
    whole, cents = f'{_money(value):,.2f}'.split('.')
    return '$ ' + whole.replace(',', '.') + ',' + cents


def _show(day):
    return f'{day:%d/%m/%Y}' if day else ''


def terms_text(d):
    """Condiciones de pago en una frase, como irían impresas."""
    if d['payment_terms_text']:
        return d['payment_terms_text']
    parts = [f'Pago a {d["payment_days"]} días desde la emisión.' if d['payment_days'] is not None else '']
    for t in d['discount_tiers']:
        parts.append(f'{t["percent"]} % de descuento si paga dentro de {t["days"]} días.')
    return ' '.join(p for p in parts if p)


def _wrapped(label, text):
    chunks = textwrap.wrap(text, WRAP, break_long_words=False, break_on_hyphens=False) or ['']
    return [f'{label}: {chunks[0]}'] + [f'{label} (continúa): {c}' for c in chunks[1:]]


def invoice_lines(data):
    """Las líneas de texto del PDF, en orden. Cada campo va etiquetado en su propia línea."""
    d = normalize(data)
    out = [TITLE, DISCLAIMER, '']
    out += _wrapped('Proveedor', d['supplier_name'])
    out += [f'NIT del proveedor: {d["supplier_nit"]}-{nit_check_digit(d["supplier_nit"])}']
    out += _wrapped('Cliente', d['buyer_name'])
    out += [f'NIT del cliente: {d["buyer_nit"]}-{nit_check_digit(d["buyer_nit"])}',
            f'Número de factura: {d["number"]}']
    if d['cufe']:
        # Un código largo y sin espacios se parte en tramos; el lector los vuelve a unir.
        chunks = textwrap.wrap(d['cufe'], 60, break_long_words=True, break_on_hyphens=False)
        out += ['Código único: ' + chunks[0]] + ['Código único (continúa): ' + c for c in chunks[1:]]
    out += [f'Fecha de emisión: {_show(d["issue_date"])}', f'Fecha de vencimiento: {_show(d["due_date"])}',
            f'Orden de compra: {d["order_number"]}', f'Fecha del pedido: {_show(d["ordered_on"])}', f'Moneda: {d["currency"]}', '', 'DETALLE']
    for i, l in enumerate(d['lines'], 1):
        out += [f'Línea {i}', f'Código: {l["supplier_code"]}']
        out += _wrapped('Descripción', l['description'])
        out += [f'Cantidad: {l["quantity"]}', f'Precio unitario: {_cop(l["unit_price"])}', f'Descuento: {_cop(l["discount"])}',
                f'IVA: {_percent(l["tax_rate"])} %', f'Lote: {l["expected_lot"]}', f'Vencimiento: {_show(l["expected_expiry"])}']
    out += ['', 'TOTALES', f'Subtotal: {_cop(d["subtotal"])}', f'Descuento global: {_cop(d["global_discount"])}', f'IVA total: {_cop(d["tax"])}',
            f'Retenciones: {_cop(d["retention"])}', f'Total a pagar: {_cop(d["total"])}', '', 'CONDICIONES DE PAGO',
            f'Plazo de pago: {d["payment_days"] if d["payment_days"] is not None else ""} días',
            f'Base del descuento: {BASES.get(d["discount_base"], "")}']
    for t in d['discount_tiers']:
        out.append(f'Pronto pago: {t["percent"]} % dentro de {t["days"]} días')
    out += _wrapped('Condiciones', terms_text(d))
    return out


def invoice_pdf(data):
    """PDF de texto de la factura de demostración (varias páginas si hace falta)."""
    return tiny_pdf(invoice_lines(data), per_page=LINES_PER_PAGE, size=10, leading=14)


def _tag(name, value, attrs=''):
    return f'<{name}{attrs}>{escape(str(value))}</{name}>'


def invoice_xml(data):
    """UBL Invoice mínimo con los mismos datos. Lo lee `einvoice.parse_invoice_xml`."""
    d = normalize(data)
    cur = ' currencyID="COP"'

    def party(name, nit):
        return ('<cac:Party><cac:PartyName>' + _tag('cbc:Name', name) + '</cac:PartyName><cac:PartyTaxScheme>' + _tag('cbc:RegistrationName', name)
                + _tag('cbc:CompanyID', nit, f' schemeID="{nit_check_digit(nit)}" schemeName="31"') + '</cac:PartyTaxScheme></cac:Party>')

    x = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" '
         'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
         'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">',
         _tag('cbc:ID', d['number'])]
    if d['cufe']:
        x.append(_tag('cbc:UUID', d['cufe']))
    x.append(_tag('cbc:IssueDate', d['issue_date'].isoformat()))
    if d['due_date']:
        x.append(_tag('cbc:DueDate', d['due_date'].isoformat()))
    x += [_tag('cbc:Note', DISCLAIMER), _tag('cbc:DocumentCurrencyCode', d['currency'])]
    if d['order_number'] or d['ordered_on']:
        x.append('<cac:OrderReference>' + _tag('cbc:ID', d['order_number'])
                 + (_tag('cbc:IssueDate', d['ordered_on'].isoformat()) if d['ordered_on'] else '') + '</cac:OrderReference>')
    x += ['<cac:AccountingSupplierParty>' + party(d['supplier_name'], d['supplier_nit']) + '</cac:AccountingSupplierParty>',
          '<cac:AccountingCustomerParty>' + party(d['buyer_name'], d['buyer_nit']) + '</cac:AccountingCustomerParty>',
          '<cac:PaymentTerms>' + _tag('cbc:Note', terms_text(d)) + '</cac:PaymentTerms>']
    if d['global_discount']:
        x.append('<cac:AllowanceCharge><cbc:ChargeIndicator>false</cbc:ChargeIndicator>' + _tag('cbc:Amount', d['global_discount'], cur) + '</cac:AllowanceCharge>')
    if d['retention']:
        x.append('<cac:WithholdingTaxTotal>' + _tag('cbc:TaxAmount', d['retention'], cur) + '</cac:WithholdingTaxTotal>')
    x.append('<cac:LegalMonetaryTotal>' + _tag('cbc:LineExtensionAmount', d['subtotal'], cur) + _tag('cbc:TaxExclusiveAmount', d['subtotal'], cur)
             + _tag('cbc:TaxInclusiveAmount', d['subtotal'] + d['tax'], cur) + _tag('cbc:PayableAmount', d['total'], cur) + '</cac:LegalMonetaryTotal>')
    for i, l in enumerate(d['lines'], 1):
        lot = ''
        if l['expected_lot'] or l['expected_expiry']:
            lot = ('<cac:ItemInstance><cac:LotIdentification>' + _tag('cbc:LotNumberID', l['expected_lot'])
                   + (_tag('cbc:ExpiryDate', l['expected_expiry'].isoformat()) if l['expected_expiry'] else '') + '</cac:LotIdentification></cac:ItemInstance>')
        x.append('<cac:InvoiceLine>' + _tag('cbc:ID', i) + _tag('cbc:InvoicedQuantity', l['quantity'], ' unitCode="94"')
                 + _tag('cbc:LineExtensionAmount', l['net'], cur)
                 + ('<cac:AllowanceCharge><cbc:ChargeIndicator>false</cbc:ChargeIndicator>' + _tag('cbc:Amount', l['discount'], cur) + '</cac:AllowanceCharge>'
                    if l['discount'] else '')
                 + '<cac:TaxTotal>' + _tag('cbc:TaxAmount', l['tax'], cur) + '<cac:TaxSubtotal>' + _tag('cbc:TaxableAmount', l['net'], cur)
                 + _tag('cbc:TaxAmount', l['tax'], cur) + '<cac:TaxCategory>' + _tag('cbc:Percent', l['tax_rate'])
                 + '<cac:TaxScheme><cbc:ID>01</cbc:ID><cbc:Name>IVA</cbc:Name></cac:TaxScheme></cac:TaxCategory></cac:TaxSubtotal></cac:TaxTotal>'
                 + '<cac:Item>' + _tag('cbc:Description', l['description'])
                 + ('<cac:SellersItemIdentification>' + _tag('cbc:ID', l['supplier_code']) + '</cac:SellersItemIdentification>' if l['supplier_code'] else '')
                 + lot + '</cac:Item><cac:Price>' + _tag('cbc:PriceAmount', l['unit_price'], cur) + '<cbc:BaseQuantity unitCode="94">1</cbc:BaseQuantity></cac:Price>'
                 + '</cac:InvoiceLine>')
    x.append('</Invoice>')
    return '\n'.join(x).encode('utf-8')


def expected_reading(data, source='pdf'):
    """Lo que deben entregar los lectores para estos datos: `pdf_invoice.parse_invoice_pdf` o `einvoice.parse_invoice_xml`."""
    d = normalize(data)
    iso = lambda day: day.isoformat() if day else ''
    out = {
        'supplier_nit': d['supplier_nit'], 'supplier_name': d['supplier_name'], 'number': d['number'], 'cufe': d['cufe'],
        'issue_date': iso(d['issue_date']), 'due_date': iso(d['due_date']), 'currency': d['currency'], 'order_number': d['order_number'],
        'ordered_on': iso(d['ordered_on']), 'global_discount': str(d['global_discount']), 'retention': str(d['retention']), 'payable': str(d['total']),
        'lines': [{'supplier_code': l['supplier_code'], 'description': l['description'], 'quantity': l['quantity'], 'unit_price': str(l['unit_price']),
                   'discount': str(l['discount']), 'tax_rate': str(l['tax_rate']), 'expected_lot': l['expected_lot'],
                   'expected_expiry': iso(l['expected_expiry'])} for l in d['lines']],
    }
    if source == 'pdf':
        out.update({'source': 'pdf', 'payment_days': d['payment_days'], 'discount_base': d['discount_base'], 'discount_tiers': d['discount_tiers'],
                    'payment_terms_text': terms_text(d), 'warnings': []})
    return out
