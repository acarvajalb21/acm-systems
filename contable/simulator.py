"""Simulador local del sistema contable.

Atiende las operaciones que usa la plataforma: catálogos, facturas de venta, recibos de caja, consulta de
saldo, PDF y correo. Respeta la idempotencia por clave. Sus documentos se guardan en `SimulatedDocument`,
llevan un código con prefijo «SIM» y un PDF marcado «Documento de demostración sin validez fiscal»: no se
comunican con ninguna entidad ni servicio externo.

Reglas para ensayar fallos:
- Un cliente cuya identificación empieza por 999 no existe en el sistema contable (rechazo).
- `Simulator.fail_next` permite ensayar fallos: 'timeout' (sin crear), 'lost_response'
  (crea el documento y pierde la respuesta) y 'server_error'.
"""
import hashlib
import uuid
from base64 import b64encode
from decimal import Decimal, ROUND_HALF_UP
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .client import Rejected, Uncertain
from .models import SimulatedDocument

CENT = Decimal('0.01')

CATALOGS = {
    ('/v1/document-types', 'FV'): [{'id': 101, 'code': '1', 'name': 'Factura electrónica de venta', 'type': 'FV', 'active': True}],
    ('/v1/document-types', 'RC'): [{'id': 102, 'code': '1', 'name': 'Recibo de caja', 'type': 'RC', 'active': True}],
    ('/v1/payment-types', 'FV'): [
        {'id': 201, 'name': 'Efectivo', 'type': 'Cartera', 'active': True, 'due_date': False},
        {'id': 202, 'name': 'Tarjeta débito o crédito', 'type': 'Cartera', 'active': True, 'due_date': False},
        {'id': 203, 'name': 'Transferencia bancaria', 'type': 'Cartera', 'active': True, 'due_date': False},
        {'id': 204, 'name': 'Crédito', 'type': 'Cartera', 'active': True, 'due_date': True},
    ],
    ('/v1/payment-types', 'RC'): [
        {'id': 301, 'name': 'Banco · cuenta corriente principal', 'type': 'Cartera', 'active': True, 'due_date': False},
        {'id': 302, 'name': 'Caja general', 'type': 'Cartera', 'active': True, 'due_date': False},
    ],
    ('/v1/users', None): {'results': [{'id': 401, 'username': 'ventas', 'first_name': 'Vendedor', 'last_name': 'principal', 'active': True}]},
    ('/v1/taxes', None): [{'id': 501, 'name': 'IVA 19 %', 'type': 'IVA', 'percentage': 19, 'active': True},
                          {'id': 502, 'name': 'IVA 5 %', 'type': 'IVA', 'percentage': 5, 'active': True}],
    ('/v1/warehouses', None): [{'id': 601, 'name': 'Bodega principal', 'active': True}],
}


def money(value):
    return Decimal(str(value)).quantize(CENT, ROUND_HALF_UP)


def item_total(item, taxes):
    base = money(Decimal(str(item['quantity'])) * Decimal(str(item.get('taxed_price') or item['price'])) - Decimal(str(item.get('discount') or 0)))
    tax = sum((money(base * Decimal(str(taxes[t['id']])) / 100) for t in item.get('taxes', []) if t['id'] in taxes), Decimal(0))
    return money(base + tax)


def reject(code, message, *params):
    raise Rejected(message, [{'Code': code, 'Message': message, 'Params': list(params)}], 400)


def _esc(text):
    return text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def _day_first(iso):
    """«2026-11-20» → «20/11/2026»; cualquier otro texto se deja igual."""
    parts = str(iso or '')[:10].split('-')
    return '/'.join(reversed(parts)) if len(parts) == 3 and all(p.isdigit() for p in parts) else str(iso or '')


def _cop(value):
    """Pesos con separador de miles y centavos solo cuando existen: $1.234.567 o $1.234.567,50."""
    value = Decimal(value or 0).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    text = f'{value:,.0f}' if value == value.to_integral_value() else f'{value:,.2f}'
    return '$' + text.replace(',', '_').replace('.', ',').replace('_', '.')


def tiny_pdf(lines, per_page=40, size=12, leading=18):
    """PDF de texto plano (Helvetica), una línea por elemento; pasa de página cada `per_page` líneas."""
    lines = list(lines) or ['']
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)]
    kids = ' '.join(f'{4 + 2 * i} 0 R' for i in range(len(pages)))
    objs = [b'<< /Type /Catalog /Pages 2 0 R >>', f'<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>'.encode(),
            b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>']
    for i, chunk in enumerate(pages):
        stream = f'BT /F1 {size} Tf 56 780 Td {leading} TL ' + ' '.join(f'({_esc(l)}) Tj T*' for l in chunk) + ' ET'
        body = stream.encode('cp1252', 'replace')
        objs.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>'.encode())
        objs.append(b'<< /Length ' + str(len(body)).encode() + b' >>\nstream\n' + body + b'\nendstream')
    out, offsets = bytearray(b'%PDF-1.4\n'), []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(out)
    out += f'xref\n0 {len(objs) + 1}\n0000000000 65535 f \n'.encode() + b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets)
    out += f'trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return bytes(out)


class Simulator:
    environment = 'simulator'
    fail_next = None

    def request(self, method, path, body=None, idempotency_key=None, params=None):
        params = params or {}
        failure, Simulator.fail_next = Simulator.fail_next, None
        if failure == 'timeout' and method != 'GET':
            raise Uncertain('Simulador: tiempo de espera agotado antes de crear el documento.')
        if failure == 'server_error':
            raise Uncertain('Simulador: error temporal del servicio.')
        if method == 'GET':
            return self._get(path, params)
        with transaction.atomic():
            if path == '/v1/invoices':
                data = self._invoice(body, idempotency_key)
            elif path == '/v1/vouchers':
                data = self._voucher(body, idempotency_key)
            elif path.startswith('/v1/invoices/') and path.endswith('/mail'):
                data = self._mail(path.split('/')[3], body)
            else:
                reject('not_found', f'Ruta no disponible en el simulador: {path}')
        if failure == 'lost_response':
            raise Uncertain('Simulador: el documento se creó, pero la respuesta se perdió.')
        return data

    # ------------------------------------------------------------- consultas
    def _get(self, path, params):
        key = (path, params.get('type') or params.get('document_type'))
        if key in CATALOGS:
            return CATALOGS[key]
        if (path, None) in CATALOGS:
            return CATALOGS[(path, None)]
        parts = path.strip('/').split('/')
        if parts[:2] == ['v1', 'invoices'] and len(parts) == 2:
            qs = SimulatedDocument.objects.filter(kind='invoice')
            if params.get('customer_identification'):
                qs = qs.filter(customer=params['customer_identification'])
            if params.get('date_start'):
                qs = qs.filter(payload__request__date__gte=params['date_start'])
            results = [self._invoice_view(d) for d in qs.order_by('number')]
            return {'pagination': {'page': 1, 'page_size': len(results), 'total_results': len(results)}, 'results': results}
        if parts[:2] == ['v1', 'invoices'] and len(parts) >= 3:
            doc = SimulatedDocument.objects.filter(kind='invoice', contable_id=parts[2]).first()
            if doc is None:
                reject('not_found', 'La factura no existe.')
            if len(parts) == 4 and parts[3] == 'pdf':
                req = doc.payload['request']
                pdf = tiny_pdf(['Documento de demostración sin validez fiscal', 'Generado por el simulador local del sistema contable.', '',
                                settings.COMPANY_NAME, f'Factura {doc.name}', f'Identificación del cliente: {req["customer"]["identification"]}',
                                f'Fecha: {_day_first(req["date"])}', f'Total: {_cop(doc.total)}'])
                return {'id': doc.contable_id, 'cufe': doc.payload['response']['stamp'].get('cufe', ''), 'base64': b64encode(pdf).decode()}
            return self._invoice_view(doc)
        reject('not_found', f'Consulta no disponible en el simulador: {path}')

    def _invoice_view(self, doc):
        data = dict(doc.payload['response'])
        data['balance'] = float(doc.balance)
        return data

    def _previous(self, kind, key):
        if key:
            doc = SimulatedDocument.objects.filter(kind=kind, idempotency_key=key).first()
            if doc:
                return self._invoice_view(doc) if kind == 'invoice' else doc.payload['response']
        return None

    def _next_number(self, kind):
        last = SimulatedDocument.objects.select_for_update().filter(kind=kind).order_by('-number').first()
        return (last.number if last else 1000) + 1

    # -------------------------------------------------------------- facturas
    def _invoice(self, body, key):
        previous = self._previous('invoice', key)
        if previous:
            return previous
        doc_ids = {d['id'] for d in CATALOGS[('/v1/document-types', 'FV')]}
        payments = {p['id']: p for p in CATALOGS[('/v1/payment-types', 'FV')]}
        taxes = {t['id']: t['percentage'] for t in CATALOGS[('/v1/taxes', None)]}
        sellers = {u['id'] for u in CATALOGS[('/v1/users', None)]['results']}
        if (body.get('document') or {}).get('id') not in doc_ids:
            reject('invalid_reference', 'El tipo de comprobante no existe o no está activo.', 'document.id')
        if str(body.get('date', '')) < timezone.localdate().isoformat():
            reject('invalid_date', 'La fecha de una factura electrónica no puede ser anterior a la fecha actual.', 'date')
        ident = str((body.get('customer') or {}).get('identification', ''))
        if not ident or ident.startswith('999'):
            reject('invalid_reference', 'El cliente no existe o no está activo en el sistema contable.', 'customer.identification')
        if body.get('seller') not in sellers:
            reject('invalid_reference', 'El vendedor no existe en el sistema contable.', 'seller')
        items = body.get('items') or []
        if not items or len(items) > 500:
            reject('invalid_array', 'La factura debe tener entre 1 y 500 ítems.', 'items')
        for it in items:
            if not it.get('code') or Decimal(str(it.get('quantity', 0))) <= 0:
                reject('invalid_reference', 'Cada ítem requiere un producto activo y una cantidad positiva.', 'items.code')
        total = sum((item_total(it, taxes) for it in items), Decimal(0))
        pays = body.get('payments') or []
        if any(p.get('id') not in payments for p in pays):
            reject('invalid_array', 'Forma de pago inválida para facturas.', 'payments.id')
        due = [p for p in pays if payments[p['id']]['due_date']]
        if len(due) > 1:
            reject('invalid_array', 'Solo se permite una forma de pago con fecha de vencimiento.', 'payments')
        if any(not p.get('due_date') for p in due):
            reject('parameter_required', 'La forma de pago requiere fecha de vencimiento.', 'payments.due_date')
        if money(sum(Decimal(str(p.get('value', 0))) for p in pays)) != total:
            reject('invalid_total_payments', f'La suma de las formas de pago no coincide con el total de la factura ({total}).', 'payments')
        number = self._next_number('invoice')
        contable_id = str(uuid.uuid4())
        stamped = bool((body.get('stamp') or {}).get('send'))
        cufe = ('SIM' + hashlib.sha384(f'{contable_id}{total}'.encode()).hexdigest())[:96] if stamped else ''
        credit = money(sum(Decimal(str(p['value'])) for p in due))
        response = {'id': contable_id, 'document': {'id': body['document']['id']}, 'prefix': 'FE', 'number': number, 'name': f'FE-{number}',
                    'date': body['date'], 'customer': {'identification': ident, 'branch_office': (body.get('customer') or {}).get('branch_office', 0)},
                    'seller': body['seller'], 'total': float(total), 'balance': float(credit), 'observations': body.get('observations', ''),
                    'items': items, 'payments': pays,
                    'stamp': {'status': 'Accepted' if stamped else 'Draft', 'cufe': cufe, 'observations': 'Simulador local: sin validez fiscal', 'errors': None},
                    'mail': {'status': 'NotSent', 'observations': ''},
                    'metadata': {'created': timezone.now().isoformat(), 'stock_updated': True}}
        SimulatedDocument.objects.create(kind='invoice', contable_id=contable_id, prefix='FE', number=number, name=f'FE-{number}', idempotency_key=key or '',
                                         customer=ident, payload={'request': body, 'response': response}, total=total, balance=credit)
        return dict(response)

    def _mail(self, contable_id, body):
        doc = SimulatedDocument.objects.select_for_update().filter(kind='invoice', contable_id=contable_id).first()
        if doc is None:
            reject('not_found', 'La factura no existe.')
        to = (body or {}).get('mail_to', '')
        if '@' not in to:
            reject('invalid_email', 'El correo no tiene un formato válido.', 'mail_to')
        doc.mail = doc.mail + [{'to': to, 'copy_to': (body or {}).get('copy_to', ''), 'at': timezone.now().isoformat()}]
        doc.save(update_fields=['mail'])
        return {'id': contable_id, 'status': 'Simulado: no se envió ningún correo.'}

    # ----------------------------------------------------- recibos de caja
    def _voucher(self, body, key):
        previous = self._previous('voucher', key)
        if previous:
            return previous
        if (body.get('document') or {}).get('id') not in {d['id'] for d in CATALOGS[('/v1/document-types', 'RC')]}:
            reject('invalid_reference', 'El tipo de recibo no existe o no está activo.', 'document.id')
        if body.get('type') != 'DebtPayment':
            reject('invalid_type', 'El simulador solo admite recibos de abono a deuda (DebtPayment).', 'type')
        ident = str((body.get('customer') or {}).get('identification', ''))
        payment = body.get('payment') or {}
        if payment.get('id') not in {p['id'] for p in CATALOGS[('/v1/payment-types', 'RC')]}:
            reject('invalid_payment', 'La forma de pago no es válida para recibos de caja.', 'payment.id')
        items, applied = body.get('items') or [], Decimal(0)
        if not items:
            reject('parameter_required', 'El recibo debe indicar al menos un vencimiento.', 'items')
        docs = []
        for it in items:
            due = it.get('due') or {}
            doc = SimulatedDocument.objects.select_for_update().filter(kind='invoice', prefix=due.get('prefix'), number=due.get('consecutive')).first()
            if doc is None or doc.customer != ident:
                reject('invalid_reference', f'El vencimiento {due.get("prefix")}-{due.get("consecutive")} no existe para este cliente.', 'items.due')
            value = money(it.get('value', 0))
            if value <= 0 or value > doc.balance:
                reject('invalid_value', f'El valor aplicado a {doc.name} supera su saldo ({doc.balance}).', 'items.value')
            docs.append((doc, value))
            applied += value
        if money(payment.get('value', 0)) != money(applied):
            reject('invalid_value', 'El valor del pago no coincide con la suma aplicada.', 'payment.value')
        for doc, value in docs:
            doc.balance = money(doc.balance - value)
            doc.save(update_fields=['balance'])
        number = self._next_number('voucher')
        contable_id = str(uuid.uuid4())
        response = {'id': contable_id, 'document': body['document'], 'number': number, 'name': f'RC-{number}', 'date': body.get('date'),
                    'type': 'DebtPayment', 'customer': body.get('customer'), 'items': items, 'payment': payment,
                    'metadata': {'created': timezone.now().isoformat()}}
        SimulatedDocument.objects.create(kind='voucher', contable_id=contable_id, prefix='RC', number=number, name=f'RC-{number}', idempotency_key=key or '',
                                         customer=ident, payload={'request': body, 'response': response}, total=applied, balance=0)
        return response
