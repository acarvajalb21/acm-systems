"""Entrada de facturas de compra para precargar el registro.

Acepta el XML de una factura electrónica (UBL Invoice), el AttachedDocument que lo envuelve o el ZIP que contiene
ambos. Los PDF se delegan al lector local del formato «Factura de demostración» (`pdf_invoice.py`). Solo extrae
datos: la factura queda «Por revisar» y una persona confirma cada campo antes de validarla.
"""
import io
import re
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET
from django.core.exceptions import ValidationError

NS = {
    'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
    'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
}
INVOICE_TAG = '{urn:oasis:names:specification:ubl:schema:xsd:Invoice-2}Invoice'
ATTACHED_TAG = '{urn:oasis:names:specification:ubl:schema:xsd:AttachedDocument-2}AttachedDocument'
MAX_XML = 5 * 1024 * 1024
MAX_ZIP_MEMBERS = 20
MAX_UNZIPPED = 40 * 1024 * 1024


def _safe_parse(raw):
    if len(raw) > MAX_XML:
        raise ValidationError('El XML supera 5 MB.')
    head = raw[:4096].upper()
    # Una factura electrónica no usa DTD; rechazarlas evita la expansión de entidades.
    if b'<!DOCTYPE' in head or b'<!ENTITY' in raw.upper():
        raise ValidationError('El XML contiene declaraciones no admitidas.')
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        raise ValidationError('El archivo no es un XML válido.')


def _text(node, path):
    found = node.find(path, NS) if node is not None else None
    return found.text.strip() if found is not None and found.text else ''


def _decimal(value, default='0'):
    try:
        return Decimal(value or default)
    except InvalidOperation:
        raise ValidationError(f'Valor numérico inválido en el XML: {value}')


def _date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        raise ValidationError(f'Fecha inválida en el XML: {value}')


def _invoice_root(raw):
    root = _safe_parse(raw)
    if root.tag == INVOICE_TAG:
        return root
    if root.tag == ATTACHED_TAG:
        embedded = root.find('cac:Attachment/cac:ExternalReference/cbc:Description', NS)
        if embedded is not None and embedded.text and 'Invoice' in embedded.text:
            inner = _safe_parse(embedded.text.strip().encode('utf-8'))
            if inner.tag == INVOICE_TAG:
                return inner
    raise ValidationError('El XML no corresponde a una factura electrónica de venta (UBL Invoice).')


def parse_invoice_xml(raw):
    inv = _invoice_root(raw)
    party = inv.find('cac:AccountingSupplierParty/cac:Party', NS)
    nit = _text(party, 'cac:PartyTaxScheme/cbc:CompanyID') or _text(party, 'cac:PartyLegalEntity/cbc:CompanyID')
    name = (_text(party, 'cac:PartyTaxScheme/cbc:RegistrationName') or _text(party, 'cac:PartyLegalEntity/cbc:RegistrationName')
            or _text(party, 'cac:PartyName/cbc:Name'))
    lines = []
    for line in inv.findall('cac:InvoiceLine', NS):
        qty = _decimal(_text(line, 'cbc:InvoicedQuantity'))
        if qty <= 0 or qty != qty.to_integral_value():
            raise ValidationError('El sistema registra cantidades enteras; revise la línea ' + (_text(line, 'cbc:ID') or '?') + '.')
        discount = sum((_decimal(_text(ac, 'cbc:Amount')) for ac in line.findall('cac:AllowanceCharge', NS)
                        if _text(ac, 'cbc:ChargeIndicator').lower() == 'false'), Decimal(0))
        percent = _text(line, 'cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory/cbc:Percent') or _text(line, 'cac:TaxTotal/cac:TaxSubtotal/cbc:Percent')
        price = _decimal(_text(line, 'cac:Price/cbc:PriceAmount'))
        base_qty = _decimal(_text(line, 'cac:Price/cbc:BaseQuantity'), '1') or Decimal(1)
        item = line.find('cac:Item', NS)
        lines.append({
            'supplier_code': _text(item, 'cac:SellersItemIdentification/cbc:ID') or _text(item, 'cac:StandardItemIdentification/cbc:ID'),
            'description': _text(item, 'cbc:Description')[:200],
            'quantity': int(qty),
            'unit_price': str((price / base_qty).quantize(Decimal('0.01'))),
            'discount': str(discount.quantize(Decimal('0.01'))),
            'tax_rate': str(_decimal(percent).quantize(Decimal('0.01'))),
            'expected_lot': _text(item, 'cac:ItemInstance/cac:LotIdentification/cbc:LotNumberID')[:80],
            'expected_expiry': _text(item, 'cac:ItemInstance/cac:LotIdentification/cbc:ExpiryDate'),
        })
    if not lines:
        raise ValidationError('La factura no tiene líneas de producto.')
    issue = _date(_text(inv, 'cbc:IssueDate'))
    due = _date(_text(inv, 'cbc:DueDate') or _text(inv, 'cac:PaymentMeans/cbc:PaymentDueDate'))
    allowance = sum((_decimal(_text(ac, 'cbc:Amount')) for ac in inv.findall('cac:AllowanceCharge', NS)
                     if _text(ac, 'cbc:ChargeIndicator').lower() == 'false'), Decimal(0))
    withholding = sum((_decimal(_text(w, 'cbc:TaxAmount')) for w in inv.findall('cac:WithholdingTaxTotal', NS)), Decimal(0))
    return {
        'supplier_nit': re.sub(r'\D', '', nit),
        'supplier_name': name[:160],
        'number': _text(inv, 'cbc:ID')[:40],
        'cufe': _text(inv, 'cbc:UUID')[:120],
        'issue_date': issue.isoformat() if issue else '',
        'due_date': due.isoformat() if due else '',
        'currency': _text(inv, 'cbc:DocumentCurrencyCode') or 'COP',
        'order_number': _text(inv, 'cac:OrderReference/cbc:ID')[:40],
        'ordered_on': _text(inv, 'cac:OrderReference/cbc:IssueDate'),
        'global_discount': str(allowance.quantize(Decimal('0.01'))),
        'retention': str(withholding.quantize(Decimal('0.01'))),
        'payable': _text(inv, 'cac:LegalMonetaryTotal/cbc:PayableAmount'),
        'lines': lines,
    }


def read_upload(name, raw):
    """Devuelve (datos, archivos) donde archivos son pares (nombre, bytes) para adjuntar."""
    lower = name.lower()
    if lower.endswith('.pdf'):
        from .pdf_invoice import parse_invoice_pdf
        return parse_invoice_pdf(raw), [(name, raw)]
    if lower.endswith('.xml'):
        return parse_invoice_xml(raw), [(name, raw)]
    if lower.endswith('.zip'):
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            raise ValidationError('El ZIP está dañado o no es válido.')
        members = [m for m in archive.infolist() if not m.is_dir()]
        if len(members) > MAX_ZIP_MEMBERS or sum(m.file_size for m in members) > MAX_UNZIPPED:
            raise ValidationError('El ZIP contiene demasiados archivos o es demasiado grande.')
        files, data = [], None
        for m in members:
            base = m.filename.replace('\\', '/').rsplit('/', 1)[-1]
            if not base.lower().endswith(('.xml', '.pdf')):
                continue
            content = archive.read(m)
            if base.lower().endswith('.xml') and data is None:
                try:
                    data = parse_invoice_xml(content)
                except ValidationError:
                    continue
            files.append((base, content))
        if data is None:
            raise ValidationError('El ZIP no contiene el XML de una factura electrónica.')
        return data, files
    raise ValidationError('Cargue la factura de demostración en PDF, el XML de la factura electrónica o el ZIP que los contiene.')
