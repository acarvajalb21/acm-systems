"""Lectura de códigos escaneados: GS1 (DataMatrix, GS1-128, QR GS1, Digital Link), EAN/UPC y QR de factura electrónica.

Las cajas de producto pueden llevar un GS1 DataMatrix con el GTIN (01), el vencimiento (17), el lote (10) y a veces un
serial (21). Los lectores de mano envían el separador FNC1 como el carácter GS (ASCII 29); algunos lo reemplazan por
otro carácter configurable. Aquí se aceptan GS, «|», «^» y «~», que no forman parte del conjunto de caracteres GS1.
"""
import calendar
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse, parse_qs, unquote

GS = '\x1d'
SEPARATORS = (GS, '|', '^', '~')
FIXED = {'00': 18, '01': 14, '02': 14, '11': 6, '12': 6, '13': 6, '15': 6, '16': 6, '17': 6, '20': 2}
VARIABLE = {'10': 20, '21': 20, '22': 20, '240': 30, '241': 30, '30': 8, '37': 8, '710': 20, '711': 20, '712': 20, '713': 20}
SYMBOLOGY = re.compile(r'^\](?:d2|C1|Q3|e0|J1|d1|Q1)')


@dataclass
class Scan:
    kind: str                      # 'gs1' | 'gtin' | 'invoice' | 'unknown'
    raw: str = ''
    gtin: str = ''
    lot: str = ''
    expiry: date = None
    serial: str = ''
    invoice: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def check_digit(digits):
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return str((10 - total % 10) % 10)


def normalize_gtin(value):
    """EAN-8, UPC-A (12), EAN-13 o GTIN-14 → GTIN-14 con dígito verificador válido; None si no lo es."""
    digits = re.sub(r'\D', '', value or '')
    if len(digits) not in (8, 12, 13, 14):
        return None
    digits = digits.zfill(14)
    return digits if check_digit(digits[:-1]) == digits[-1] else None


def gs1_date(yymmdd):
    """AAMMDD de GS1; día 00 = último día del mes. Siglo según la regla de ventana de GS1 (−49 a +50 años)."""
    if not re.fullmatch(r'\d{6}', yymmdd or ''):
        return None
    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:])
    current = date.today().year
    year = (current // 100) * 100 + yy
    if year - current > 50:
        year -= 100
    elif current - year > 49:
        year += 100
    if not 1 <= mm <= 12:
        return None
    if dd == 0:
        dd = calendar.monthrange(year, mm)[1]
    try:
        return date(year, mm, dd)
    except ValueError:
        return None


def _element_string(text):
    """Recorre una cadena GS1 sin paréntesis. Devuelve {AI: valor} y advertencias."""
    for sep in SEPARATORS[1:]:
        text = text.replace(sep, GS)
    text = text.strip(GS)
    out, warnings, i = {}, [], 0
    while i < len(text):
        if text[i] == GS:
            i += 1
            continue
        ai = next((a for a in sorted(list(FIXED) + list(VARIABLE), key=len, reverse=True) if text.startswith(a, i)), None)
        if ai is None:
            warnings.append(f'Parte no reconocida: {text[i:i + 12]}')
            break
        i += len(ai)
        if ai in FIXED:
            out[ai] = text[i:i + FIXED[ai]]
            i += FIXED[ai]
        else:
            end = text.find(GS, i)
            end = len(text) if end == -1 else end
            out[ai] = text[i:end]
            i = end
    return out, warnings


def _parenthesized(text):
    return {m.group(1): m.group(2) for m in re.finditer(r'\((\d{2,4})\)([^(]*)', text)}


def _digital_link(text):
    url = urlparse(text)
    parts = [p for p in url.path.split('/') if p]
    out = {}
    for k in range(len(parts) - 1):
        if parts[k] in ('01', '10', '21', '17', '22'):
            out[parts[k]] = unquote(parts[k + 1])
    for k, v in parse_qs(url.query).items():
        if k in ('17', '10', '21'):
            out[k] = v[0]
    return out


def _split_lot_expiry(values, warnings):
    """Sin separador, un lote al final puede traer pegado «17AAMMDD». Si aún no hay vencimiento y el final es una fecha
    válida, se separa (es el orden más común cuando el lector no transmite el separador)."""
    lot = values.get('10', '')
    if lot and '17' not in values:
        m = re.search(r'17(\d{6})$', lot)
        if m and gs1_date(m.group(1)) and len(lot) > 8:
            values['10'], values['17'] = lot[:m.start()], m.group(1)
            warnings.append('El lector no envió el separador entre lote y vencimiento; se separaron por la fecha.')
    if len(values.get('10', '')) > 20:
        warnings.append('El lote parece demasiado largo: confirme que el lector envía el separador GS.')


QR_KEYS = {'numfac': 'number', 'fecfac': 'date', 'nitfac': 'nit', 'docadq': 'buyer', 'valfac': 'subtotal', 'valiva': 'tax',
             'valtolfac': 'total', 'valtotfac': 'total', 'cufe': 'cufe'}


def parse_invoice_qr(text):
    """QR de la representación gráfica de una factura electrónica (pares «Clave: valor» y/o enlace con documentkey)."""
    data = {}
    for key, value in re.findall(r'([A-Za-z]{3,12})\s*[:=]\s*([^\s\r\n]+)', text):
        name = QR_KEYS.get(key.lower())
        if name and name not in data:
            data[name] = value.strip()
    if 'cufe' not in data:
        m = re.search(r'documentkey=([0-9a-fA-F]{64,128})', text) or re.search(r'\b([0-9a-fA-F]{96})\b', text)
        if m:
            data['cufe'] = m.group(1)
    if data.get('cufe'):
        data['cufe'] = data['cufe'].lower()
    if data.get('nit'):
        data['nit'] = re.sub(r'\D', '', data['nit'].split('-')[0])
    return data if (data.get('cufe') or (data.get('number') and data.get('nit'))) else {}


def parse(raw):
    """Interpreta un código escaneado o escrito."""
    text = (raw or '').replace('\r', '\n').strip()
    scan = Scan('unknown', raw=text[:300])
    if not text:
        return scan
    if re.search(r'documentkey=|\b(?:CUFE|NumFac)\b', text, re.I):
        info = parse_invoice_qr(text)
        if info:
            scan.kind, scan.invoice = 'invoice', info
            return scan
    text = SYMBOLOGY.sub('', text.strip())
    values, warnings = {}, []
    if text.lower().startswith(('http://', 'https://')):
        values = _digital_link(text)
    elif text.startswith('('):
        values = _parenthesized(text)
    elif re.fullmatch(r'\d{8}|\d{12,14}', text):
        gtin = normalize_gtin(text)
        if gtin:
            scan.kind, scan.gtin = 'gtin', gtin
        else:
            scan.warnings.append('El dígito verificador del código no es válido.')
        return scan
    elif text.lstrip(GS).startswith(('01', '02')):
        values, warnings = _element_string(text)
    else:
        info = parse_invoice_qr(text)
        if info:
            scan.kind, scan.invoice = 'invoice', info
        return scan
    _split_lot_expiry(values, warnings)
    gtin = normalize_gtin(values.get('01') or values.get('02') or '')
    if not gtin:
        scan.warnings = warnings + ['No se encontró un GTIN válido en el código.']
        return scan
    scan.kind, scan.gtin = 'gs1', gtin
    scan.lot = (values.get('10') or '').strip().upper()[:80]
    scan.serial = (values.get('21') or '').strip()[:40]
    scan.expiry = gs1_date(values.get('17', ''))
    if values.get('17') and scan.expiry is None:
        warnings.append('La fecha de vencimiento del código no es válida.')
    scan.warnings = warnings
    return scan
