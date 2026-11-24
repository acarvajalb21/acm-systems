"""Lector del PDF «Factura de demostración» (el formato sintético que genera `demo_documents.py`).

Solo reconoce ese formato: un PDF de texto con un campo etiquetado por línea. Entrega los datos a la carga de
facturas con la misma forma que el lector de XML, más las condiciones de pago y una lista de advertencias. La
lectura no registra nada: la persona revisa cada campo contra el documento y confirma antes de guardar.
"""
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
import pdfplumber
from django.core.exceptions import ValidationError
from .demo_documents import TITLE, BASES
from .models import nit_check_digit

MAX_PDF_SIZE = 20 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_TEXT_LENGTH = 250_000
CENT = Decimal('0.01')
CONTINUES = ' (continúa)'
HEADER = {'Proveedor': 'supplier_name', 'NIT del proveedor': 'supplier_nit', 'Número de factura': 'number', 'Código único': 'cufe',
          'Fecha de emisión': 'issue_date', 'Fecha de vencimiento': 'due_date', 'Orden de compra': 'order_number', 'Fecha del pedido': 'ordered_on',
          'Moneda': 'currency'}
LINE = {'Código': 'supplier_code', 'Descripción': 'description', 'Cantidad': 'quantity', 'Precio unitario': 'unit_price', 'Descuento': 'discount',
        'IVA': 'tax_rate', 'Lote': 'expected_lot', 'Vencimiento': 'expected_expiry'}
TOTALS = {'Subtotal': 'subtotal', 'Descuento global': 'global_discount', 'IVA total': 'tax', 'Retenciones': 'retention', 'Total a pagar': 'payable'}
TERMS = {'Plazo de pago': 'payment_days', 'Base del descuento': 'discount_base', 'Condiciones': 'payment_terms_text'}
REQUIRED = {'supplier_name': 'la razón social del proveedor', 'supplier_nit': 'el NIT del proveedor', 'number': 'el número de la factura',
            'issue_date': 'la fecha de emisión', 'due_date': 'la fecha de vencimiento', 'payable': 'el total a pagar'}


def _amount(text):
    """'$ 1.234.567,50' → Decimal('1234567.50'); None si no es un valor."""
    raw = re.sub(r'[^\d,.-]', '', text or '')
    if not raw:
        return None
    try:
        return Decimal(raw.replace('.', '').replace(',', '.')).quantize(CENT)
    except InvalidOperation:
        return None


def _date(text):
    try:
        return datetime.strptime((text or '').strip(), '%d/%m/%Y').date().isoformat()
    except ValueError:
        return ''


def _fields(lines):
    """(sección, etiqueta, valor) por cada línea «Etiqueta: valor»; las líneas «(continúa)» se unen a la anterior."""
    section, out = 'header', []
    for raw in lines:
        text = ' '.join(raw.split())
        if not text:
            continue
        if text == 'DETALLE' or re.fullmatch(r'Línea \d+', text):
            section = 'line'
            if text != 'DETALLE':
                out.append(('line', '#', text))
            continue
        if text == 'TOTALES':
            section = 'totals'
            continue
        if text == 'CONDICIONES DE PAGO':
            section = 'terms'
            continue
        label, sep, value = text.partition(':')
        if not sep:
            continue
        label, value = label.strip(), value.strip()
        if label.endswith(CONTINUES):
            base = label[:-len(CONTINUES)]
            for i in range(len(out) - 1, -1, -1):
                if out[i][1] == base:
                    out[i] = (out[i][0], base, (out[i][2] + ' ' + value).strip())
                    break
            continue
        out.append((section, label, value))
    return out


def parse_invoice_text(pages):
    """pages: texto de cada página. Devuelve los datos propuestos y sus advertencias."""
    lines = [l for page in pages for l in (page or '').splitlines()]
    if not any(' '.join(l.split()) == TITLE for l in lines[:6]):
        raise ValidationError('Este lector solo reconoce el formato «Factura de demostración». Cargue el XML de la factura o regístrela manualmente.')
    data = {'source': 'pdf', 'supplier_nit': '', 'supplier_name': '', 'number': '', 'cufe': '', 'issue_date': '', 'due_date': '', 'currency': '',
            'order_number': '', 'ordered_on': '', 'global_discount': '0.00', 'retention': '0.00', 'payable': '', 'lines': [], 'payment_days': None,
            'discount_base': '', 'discount_tiers': [], 'payment_terms_text': '', 'warnings': []}
    warn = data['warnings'].append
    printed, current = {}, None
    for section, label, value in _fields(lines):
        if section == 'header' and label in HEADER:
            key = HEADER[label]
            if key == 'supplier_nit':
                digits, _, dv = value.partition('-')
                data[key] = re.sub(r'\D', '', digits)
                if data[key] and dv.strip() and dv.strip() != nit_check_digit(data[key]):
                    warn('El dígito de verificación del NIT del proveedor no corresponde; revíselo contra el documento.')
            elif key in ('issue_date', 'due_date', 'ordered_on'):
                data[key] = _date(value)
                if value and not data[key]:
                    warn(f'No se pudo leer «{label}»; complétela a mano.')
            else:
                data[key] = value[:160] if key == 'supplier_name' else value.replace(' ', '')[:120] if key == 'cufe' else value[:40]
        elif section == 'line':
            if label == '#':
                current = {'supplier_code': '', 'description': '', 'quantity': None, 'unit_price': None, 'discount': '0.00', 'tax_rate': '',
                           'expected_lot': '', 'expected_expiry': ''}
                data['lines'].append(current)
            elif current is not None and label in LINE:
                key = LINE[label]
                if key == 'quantity':
                    current[key] = int(value) if value.isdigit() else None
                elif key in ('unit_price', 'discount'):
                    amount = _amount(value)
                    current[key] = str(amount) if amount is not None else (None if key == 'unit_price' else '0.00')
                elif key == 'tax_rate':
                    rate = _amount(value.replace('%', ''))
                    current[key] = str(rate) if rate is not None else ''
                elif key == 'expected_expiry':
                    current[key] = _date(value)
                elif key == 'expected_lot':
                    current[key] = value.upper()[:80]
                else:
                    current[key] = value[:200] if key == 'description' else value[:60]
        elif section == 'totals' and label in TOTALS:
            amount = _amount(value)
            if amount is not None:
                printed[TOTALS[label]] = amount
        elif section == 'terms':
            if label == 'Pronto pago':
                m = re.fullmatch(r'([\d.,]+) % dentro de (\d+) días', value)
                if m:
                    data['discount_tiers'].append({'days': int(m.group(2)), 'percent': m.group(1).replace(',', '.')})
                else:
                    warn('No se pudo leer un tramo de pronto pago; revise las condiciones de pago.')
            elif label == 'Plazo de pago':
                m = re.match(r'(\d+)', value)
                data['payment_days'] = int(m.group(1)) if m else None
            elif label == 'Base del descuento':
                data['discount_base'] = next((k for k, v in BASES.items() if v == value), '')
            elif label == 'Condiciones':
                data['payment_terms_text'] = value
    for key in ('global_discount', 'retention'):
        data[key] = str(printed.get(key, Decimal('0.00')))
    data['payable'] = str(printed['payable']) if 'payable' in printed else ''
    data['discount_tiers'].sort(key=lambda t: t['days'])
    # Las líneas incompletas no se proponen: la persona las agrega desde el documento.
    complete = [l for l in data['lines'] if l['quantity'] and l['unit_price'] is not None]
    if len(complete) != len(data['lines']):
        warn(f'{len(data["lines"]) - len(complete)} línea(s) no se pudieron leer completas; agréguelas a mano.')
    data['lines'] = complete
    if not complete:
        raise ValidationError('La factura de demostración no tiene líneas de producto legibles.')
    for key, name in REQUIRED.items():
        if not data[key]:
            warn(f'No se pudo leer {name}; complétela a mano.')
    if data['currency'] and data['currency'] != 'COP':
        warn('La moneda del documento no es COP; confirme los valores.')
    computed = sum((Decimal(l['quantity']) * Decimal(l['unit_price']) - Decimal(l['discount'])) * (1 + Decimal(l['tax_rate'] or 0) / 100) for l in complete)
    computed = (computed - Decimal(data['global_discount']) - Decimal(data['retention'])).quantize(CENT)
    if data['payable'] and abs(computed - Decimal(data['payable'])) > Decimal('1'):
        warn('El total impreso no coincide con la suma de las líneas leídas; revise cantidades, valores e impuestos.')
    return data


def parse_invoice_pdf(raw):
    if len(raw) > MAX_PDF_SIZE:
        raise ValidationError('El archivo PDF pesa más de 20 MB; cargue una factura por archivo.')
    if not raw.lstrip()[:5].startswith(b'%PDF'):
        raise ValidationError('El contenido del archivo no es un PDF, aunque tenga esa extensión.')
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            if not pdf.pages:
                raise ValidationError('El PDF está vacío: no tiene ninguna página.')
            if len(pdf.pages) > MAX_PDF_PAGES:
                raise ValidationError('El PDF tiene más de 20 páginas; cargue una factura por archivo.')
            pages, size = [], 0
            for page in pdf.pages:
                text = page.extract_text() or ''
                size += len(text)
                if size > MAX_TEXT_LENGTH:
                    raise ValidationError('El PDF tiene demasiado texto para ser una factura.')
                pages.append(text)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError('El PDF no se pudo abrir (dañado o protegido). Registre la factura a mano.') from exc
    if not any(p.strip() for p in pages):
        raise ValidationError('El PDF no tiene texto legible (parece una imagen). Registre sus datos manualmente.')
    return parse_invoice_text(pages)
