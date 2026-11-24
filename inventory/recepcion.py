"""Recepción de mercancía con lector de códigos.

Flujo: se ubica la factura (por su QR de factura electrónica, su número o la lista de pendientes), se escanean las
cajas y la plataforma reconoce la referencia por su código, toma el lote y el vencimiento del propio código y los va
sumando por línea de la factura. Al confirmar se registra la entrada con `services.receive_invoice`, que conserva
todas sus reglas (no recibir vencidos, no exceder lo facturado, explicar diferencias y recepciones parciales).
"""
from collections import defaultdict, OrderedDict
from datetime import date
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.utils import timezone
from . import codes, services as S
from .models import Invoice, InvoiceLine, ProductBarcode, ReceivingSession, ReceivingScan, Supplier, Product, Batch

MAX_BOXES = 500


def receivable_invoices():
    """Facturas validadas con mercancía por recibir, la más antigua primero."""
    return (Invoice.objects.filter(review=Invoice.Review.VALIDATED, reception_required=True, lines__received__lt=F('lines__quantity'))
            .select_related('supplier').distinct().order_by('issue_date', 'pk'))


def find_invoice(text):
    """Busca la factura por el QR de la factura electrónica, el CUFE o el número. Devuelve (factura o None, datos leídos, mensaje)."""
    text = (text or '').strip()
    scan = codes.parse(text)
    info = scan.invoice if scan.kind == 'invoice' else {}
    qs = Invoice.objects.exclude(review=Invoice.Review.VOID).select_related('supplier')
    inv = None
    if info.get('cufe'):
        inv = qs.filter(cufe__iexact=info['cufe']).first()
    if inv is None and info.get('number'):
        inv = qs.filter(number__iexact=info['number'], supplier__nit=info.get('nit', '')).first() or qs.filter(number__iexact=info['number']).first()
    if inv is None and not info and text and len(text) <= 40:
        inv = qs.filter(number__iexact=text).first()
    if inv is not None:
        return inv, info, ''
    if info:
        supplier = Supplier.objects.filter(nit=info.get('nit', '')).first()
        who = supplier.name if supplier else (f'NIT {info["nit"]}' if info.get('nit') else 'el proveedor')
        return None, info, (f'La factura {info.get("number", "")} de {who} todavía no está registrada en la plataforma. '
                            'Compras debe cargarla y validarla antes de recibir la mercancía.')
    if scan.kind in ('gs1', 'gtin'):
        return None, {}, 'Ese código es de una caja de referencia. Primero elija la factura que llegó y luego escanee las cajas.'
    return None, {}, 'No se encontró una factura con ese código o número.'


def open_session(user, invoice):
    S.require(user, S.RECEIVERS)
    if invoice.review != Invoice.Review.VALIDATED:
        raise ValidationError('La factura todavía no está validada por compras.' if invoice.review == Invoice.Review.PENDING else 'La factura está anulada.')
    if not invoice.reception_required:
        raise ValidationError('Esta factura no requiere recepción física.')
    if invoice.received_units >= invoice.expected_units:
        raise ValidationError('La mercancía de esta factura ya se recibió completa.')
    session = ReceivingSession.objects.filter(invoice=invoice, status=ReceivingSession.Status.OPEN).first()
    if session:
        return session
    try:
        with transaction.atomic():
            session = ReceivingSession.objects.create(invoice=invoice, created_by=user, location=_default_location())
    except IntegrityError:
        session = ReceivingSession.objects.get(invoice=invoice, status=ReceivingSession.Status.OPEN)
    S.event(user, 'Recepción con escáner iniciada', invoice.pk, f'{invoice.number} · {invoice.supplier.name}')
    return session


def _default_location():
    loc = Batch.objects.order_by().values('location').annotate(n=Sum('physical')).order_by('-n').values_list('location', flat=True).first()
    return loc or 'Bodega principal'


def _open(session_id, user):
    S.require(user, S.RECEIVERS)
    session = ReceivingSession.objects.select_for_update().select_related('invoice__supplier').get(pk=session_id)
    if session.status != ReceivingSession.Status.OPEN:
        raise ValidationError('Esta recepción ya se cerró.')
    return session


def _scanned(session):
    return dict(session.scans.values_list('line_id').annotate(q=Sum('quantity')))


def _line_for(session, product):
    lines = [l for l in session.invoice.lines.select_related('product').order_by('pk') if l.product_id == product.pk]
    if not lines:
        return None
    done = _scanned(session)
    return next((l for l in lines if done.get(l.pk, 0) < l.pending), lines[0])


def scan(user, session_id, raw, boxes=1, lot='', expiry=None):
    """Registra una caja escaneada. Devuelve un dict con el resultado para la pantalla:
    kind = added | duplicate | unknown | needs_details | invoice | error."""
    boxes = max(1, min(int(boxes or 1), MAX_BOXES))
    parsed = codes.parse(raw)
    with transaction.atomic():
        session = _open(session_id, user)
        if parsed.kind == 'invoice':
            same = parsed.invoice.get('cufe') and parsed.invoice['cufe'] == (session.invoice.cufe or '').lower()
            return {'kind': 'invoice', 'ok': bool(same),
                    'message': 'Es el QR de esta misma factura.' if same else 'Ese QR es de otra factura. Termine esta recepción o vuelva a la lista.'}
        if parsed.kind not in ('gs1', 'gtin'):
            return {'kind': 'error', 'ok': False, 'message': ('No se reconoció el código. ' + ' '.join(parsed.warnings)).strip()
                    + ' Escanéelo de nuevo o, si la etiqueta no se lee, registre la caja en «Agregar a mano».'}
        code = ProductBarcode.objects.select_related('product').filter(gtin=parsed.gtin).first()
        if code is None:
            products = sorted({l.product for l in session.invoice.lines.select_related('product')}, key=lambda p: p.name)
            return {'kind': 'unknown', 'ok': False, 'gtin': parsed.gtin, 'lot': parsed.lot, 'expiry': parsed.expiry, 'serial': parsed.serial,
                    'products': products, 'message': 'Este código todavía no está asociado a una referencia. Indique cuál es (solo se hace una vez).'}
        line = _line_for(session, code.product)
        if line is None:
            return {'kind': 'error', 'ok': False, 'message': f'{code.product.name} no está en la factura {session.invoice.number}.'}
        lot = (parsed.lot or (lot or '')).strip().upper()
        expiry = parsed.expiry or expiry
        if not lot or not expiry:
            return {'kind': 'needs_details', 'ok': False, 'gtin': parsed.gtin, 'line': line, 'serial': parsed.serial, 'boxes': code.units * boxes,
                    'lot': lot or line.expected_lot, 'expiry': expiry or line.expected_expiry,
                    'message': f'{code.product.name}: el código no trae lote y vencimiento. Escríbalos una vez y siga escaneando.'}
        return _add(session, user, line, parsed.gtin, lot, expiry, parsed.serial, code.units * boxes, 'scan', parsed.warnings)


def _add(session, user, line, gtin, lot, expiry, serial, quantity, source, warnings=()):
    today = timezone.localdate()
    if expiry < today:
        return {'kind': 'error', 'ok': False, 'message': f'Lote {lot} vencido el {expiry:%d/%m/%Y}: no se recibe. Sepárelo y avise a compras.'}
    if serial and ReceivingScan.objects.filter(gtin=gtin, serial=serial).filter(session__status__in=('open', 'confirmed')).exists():
        return {'kind': 'duplicate', 'ok': False, 'message': f'Esta caja (serial {serial}) ya se escaneó. No se sumó otra vez.'}
    done = _scanned(session).get(line.pk, 0)
    if done + quantity > line.pending:
        left = line.pending - done
        return {'kind': 'error', 'ok': False,
                'message': f'{line.product.name}: llegan más de las facturadas (faltan {left}, se intentó sumar {quantity}). Sepárela y avise a compras.'}
    try:
        with transaction.atomic():
            ReceivingScan.objects.create(session=session, line=line, gtin=gtin, lot=lot[:80], expiry=expiry, serial=serial, quantity=quantity,
                                         source=source, created_by=user)
    except IntegrityError:
        return {'kind': 'duplicate', 'ok': False, 'message': f'Esta caja (serial {serial}) ya se escaneó. No se sumó otra vez.'}
    notes = []
    if line.expected_lot and lot != line.expected_lot:
        notes.append(f'el lote {lot} no es el facturado ({line.expected_lot})')
    if line.expected_expiry and expiry != line.expected_expiry:
        notes.append(f'el vencimiento {expiry:%d/%m/%Y} no es el facturado ({line.expected_expiry:%d/%m/%Y})')
    message = f'{line.product.name} · lote {lot} · vence {expiry:%d/%m/%Y} · +{quantity}'
    if notes:
        message += '. Atención: ' + ' y '.join(notes) + '.'
    return {'kind': 'added', 'ok': True, 'message': message, 'warn': bool(notes) or bool(warnings), 'extra': ' '.join(warnings)}


def link_code(user, session_id, gtin, product_id, units):
    """Asocia un código nuevo a una referencia de la factura. Queda para todas las recepciones siguientes."""
    gtin = codes.normalize_gtin(gtin)
    if not gtin:
        raise ValidationError('El código no es un GTIN válido.')
    with transaction.atomic():
        session = _open(session_id, user)
        product = Product.objects.get(pk=product_id)
        if not session.invoice.lines.filter(product=product).exists():
            raise ValidationError('Elija una referencia de esta factura.')
        units = int(units or 1)
        if not 1 <= units <= 1000:
            raise ValidationError('Las unidades por caja deben estar entre 1 y 1.000.')
        code, created = ProductBarcode.objects.get_or_create(gtin=gtin, defaults={'product': product, 'units': units, 'created_by': user})
        if not created and code.product_id != product.pk:
            raise ValidationError(f'Ese código ya está asociado a {code.product.name}.')
        S.event(user, 'Código de barras asociado', product.pk, f'{gtin} → {product.name} · {units} por caja')
    return code


def manual(user, session_id, line_id, lot, expiry, quantity, gtin='', serial=''):
    """Registra cajas sin código legible o códigos sin lote/vencimiento."""
    lot = (lot or '').strip().upper()
    if not lot or not expiry:
        raise ValidationError('Indique lote y vencimiento.')
    quantity = int(quantity or 0)
    if quantity < 1:
        raise ValidationError('La cantidad debe ser mayor que cero.')
    with transaction.atomic():
        session = _open(session_id, user)
        line = InvoiceLine.objects.select_related('product').get(pk=line_id, invoice=session.invoice)
        return _add(session, user, line, codes.normalize_gtin(gtin) or '', lot, expiry, serial[:40], quantity, 'scan' if gtin else 'manual')


def remove_scan(user, session_id, scan_id):
    with transaction.atomic():
        session = _open(session_id, user)
        deleted, _ = ReceivingScan.objects.filter(pk=scan_id, session=session).delete()
    if not deleted:
        raise ValidationError('Ese registro ya no está en la recepción.')


def summary(session):
    """Por línea de la factura: facturado, pendiente, escaneado y lotes contados; diferencias a revisar."""
    scans = list(session.scans.select_related('line__product', 'created_by'))
    by_line = defaultdict(list)
    for s in scans:
        by_line[s.line_id].append(s)
    rows = []
    for line in session.invoice.lines.select_related('product').order_by('pk'):
        items = by_line.get(line.pk, [])
        lots = OrderedDict()
        for s in sorted(items, key=lambda s: (s.lot, s.expiry)):
            key = (s.lot, s.expiry)
            lots[key] = lots.get(key, 0) + s.quantity
        counted = sum(s.quantity for s in items)
        differs = lambda lot, exp: bool((line.expected_lot and lot != line.expected_lot) or (line.expected_expiry and exp != line.expected_expiry))
        lot_rows = [{'lot': l, 'expiry': e, 'quantity': q, 'different': differs(l, e)} for (l, e), q in lots.items()]
        state = 'done' if counted >= line.pending and line.pending else 'none' if not counted else 'partial'
        if line.pending == 0:
            state = 'received'
        rows.append({'line': line, 'counted': counted, 'pending': line.pending, 'missing': max(line.pending - counted, 0), 'lots': lot_rows, 'state': state,
                     'different': any(x['different'] for x in lot_rows), 'share': int(min(counted / line.pending, 1) * 100) if line.pending else 100})
    counted = sum(r['counted'] for r in rows)
    pending = sum(r['pending'] for r in rows)
    return {'rows': rows, 'scans': scans[:40], 'counted': counted, 'pending': pending, 'missing': max(pending - counted, 0),
            'complete': pending > 0 and counted >= pending, 'partial': 0 < counted < pending, 'different': any(r['different'] for r in rows),
            'boxes': len(scans), 'share': int(min(counted / pending, 1) * 100) if pending else 100,
            'received_lines': sum(1 for r in rows if r['state'] == 'received')}


def confirm(user, session_id, received_on, location, note, token):
    """Registra la entrada por lotes con las reglas de la recepción y cierra la sesión."""
    location = (location or '').strip()
    if not location:
        raise ValidationError('Indique dónde se guarda la mercancía (bodega, pasillo o estantería).')
    with transaction.atomic():
        session = _open(session_id, user)
        agg = OrderedDict()
        for s in session.scans.order_by('pk'):
            key = (s.line_id, s.lot, s.expiry)
            agg[key] = agg.get(key, 0) + s.quantity
        if not agg:
            raise ValidationError('Escanee al menos una caja antes de confirmar.')
        rows = [{'line': line, 'quantity': q, 'lot': lot, 'expiry': exp, 'location': location} for (line, lot, exp), q in agg.items()]
        receipt = S.receive_invoice(user, session.invoice_id, rows, received_on, note or '', token)
        session.status, session.receipt, session.location = ReceivingSession.Status.CONFIRMED, receipt, location[:120]
        session.closed_by, session.closed_at = user, timezone.now()
        session.save(update_fields=['status', 'receipt', 'location', 'closed_by', 'closed_at'])
    return session


def discard(user, session_id):
    with transaction.atomic():
        session = _open(session_id, user)
        session.status, session.closed_by, session.closed_at = ReceivingSession.Status.DISCARDED, user, timezone.now()
        session.save(update_fields=['status', 'closed_by', 'closed_at'])
        S.event(user, 'Recepción con escáner descartada', session.invoice_id, session.invoice.number)
    return session
