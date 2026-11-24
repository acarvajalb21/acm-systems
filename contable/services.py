"""Cola de envíos al sistema contable: intención persistida, clave de idempotencia estable y recuperación."""
import hashlib
import json
import logging
import uuid
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .client import Rejected, Uncertain, Unavailable, ConnectorError, get_client
from .models import Submission, CatalogItem

log = logging.getLogger(__name__)
PATHS = {Submission.Kind.INVOICE: '/v1/invoices', Submission.Kind.VOUCHER: '/v1/vouchers'}
PREFIX = {Submission.Kind.INVOICE: 'FV', Submission.Kind.VOUCHER: 'RC'}
LIVE = (Submission.Status.PENDING, Submission.Status.UNCERTAIN)


def environment():
    return settings.CONTABLE['mode']


def new_key(kind):
    # Clave alfanumérica de 30 caracteres: el límite que la cola se impone para cualquier conector.
    return PREFIX[kind] + uuid.uuid4().hex[:28].upper()


def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def enqueue(user, kind, target_id, payload):
    """Se llama dentro de la transacción de negocio: la intención y el cambio de estado se confirman juntos."""
    return Submission.objects.create(kind=kind, target_id=str(target_id), idempotency_key=new_key(kind), environment=environment(),
                                     payload=payload, payload_hash=digest(payload), created_by=user)


def after_commit(submission):
    pk = submission.pk
    transaction.on_commit(lambda: process(pk))


def _handler(kind):
    from cartera import services as C
    return {Submission.Kind.INVOICE: C.on_invoice_result, Submission.Kind.VOUCHER: C.on_voucher_result}[kind]


def _finish(sub, status, **fields):
    for k, v in fields.items():
        setattr(sub, k, v)
    sub.status = status
    sub.save()
    try:
        _handler(sub.kind)(sub)
    except Exception as exc:  # el resultado del envío ya quedó guardado; se puede volver a registrar
        log.exception('No se registró el resultado del sistema contable en la aplicación')
        Submission.objects.filter(pk=sub.pk).update(last_error=f'Resultado guardado; falta registrarlo en la aplicación: {exc}'[:500])
    return sub


def process(pk):
    with transaction.atomic():
        sub = Submission.objects.select_for_update().get(pk=pk)
        if sub.status not in LIVE:
            return sub
        sub.status, sub.attempts, sub.sent_at = Submission.Status.SENDING, sub.attempts + 1, timezone.now()
        sub.save(update_fields=['status', 'attempts', 'sent_at', 'updated_at'])
    try:
        data = get_client().request('POST', PATHS[sub.kind], sub.payload, idempotency_key=sub.idempotency_key)
    except Rejected as e:
        if 'duplicated_document' in e.codes:
            found = locate(sub)
            if found:
                return _finish(sub, Submission.Status.ACCEPTED, response=found, contable_id=str(found.get('id', '')),
                               contable_name=str(found.get('name', '')), last_error='Documento existente localizado en el sistema contable.')
            return _finish(sub, Submission.Status.REVIEW, errors=e.errors, last_error='El sistema contable indica que el documento ya existe y no se pudo localizar.')
        return _finish(sub, Submission.Status.REJECTED, errors=e.errors, last_error=str(e)[:500])
    except Unavailable as e:
        # No se llegó a enviar: queda pendiente para un nuevo intento con la misma clave.
        return _finish(sub, Submission.Status.PENDING, last_error=str(e)[:500])
    except Uncertain as e:
        return _finish(sub, Submission.Status.UNCERTAIN, last_error=str(e)[:500])
    except Exception as e:
        log.exception('Error inesperado al enviar al sistema contable')
        return _finish(sub, Submission.Status.UNCERTAIN, last_error=f'Error inesperado: {e}'[:500])
    return _finish(sub, Submission.Status.ACCEPTED, response=data, contable_id=str(data.get('id', '')), contable_name=str(data.get('name', '')),
                   errors=[], last_error='')


def locate(sub):
    """Busca una factura ya creada por cliente, fecha y total. Solo devuelve un resultado único."""
    if sub.kind != Submission.Kind.INVOICE:
        return None
    p = sub.payload
    try:
        data = get_client().request('GET', '/v1/invoices', params={'customer_identification': p['customer']['identification'], 'date_start': p['date']})
    except ConnectorError:
        return None
    total = round(sum(float(x['value']) for x in p['payments']), 2)
    taken = set(Submission.objects.exclude(pk=sub.pk).exclude(contable_id='').values_list('contable_id', flat=True))
    hits = [r for r in data.get('results', []) if r.get('date') == p['date'] and round(float(r.get('total', 0)), 2) == total and str(r.get('id')) not in taken]
    return hits[0] if len(hits) == 1 else None


def mark_stale():
    limit = timezone.now() - timedelta(seconds=settings.CONTABLE['timeout'] + 60)
    stale = Submission.objects.filter(status=Submission.Status.SENDING, sent_at__lt=limit)
    count = 0
    for sub in stale:
        sub.last_error = 'El envío no terminó (servidor reiniciado o sin respuesta). Se reintentará con la misma clave.'
        _finish(sub, Submission.Status.UNCERTAIN)
        count += 1
    return count


def process_pending():
    mark_stale()
    done = []
    for pk in Submission.objects.filter(status__in=LIVE).order_by('created_at').values_list('pk', flat=True):
        done.append(process(pk))
    return done


def rerun_handler(pk):
    sub = Submission.objects.get(pk=pk)
    Submission.objects.filter(pk=pk).update(last_error='')
    _handler(sub.kind)(sub)


def fetch(path, params=None):
    return get_client().request('GET', path, params=params)


CATALOG_SOURCES = [
    ('document_fv', '/v1/document-types', {'type': 'FV'}),
    ('document_rc', '/v1/document-types', {'type': 'RC'}),
    ('payment_fv', '/v1/payment-types', {'document_type': 'FV'}),
    ('payment_rc', '/v1/payment-types', {'document_type': 'RC'}),
    ('seller', '/v1/users', None),
    ('tax', '/v1/taxes', None),
    ('warehouse', '/v1/warehouses', None),
]


def _rows(data):
    return data.get('results', []) if isinstance(data, dict) else data or []


def sync_catalogs():
    now, summary = timezone.now(), {}
    for kind, path, params in CATALOG_SOURCES:
        try:
            rows = _rows(fetch(path, params))
        except ConnectorError as e:
            summary[kind] = f'error: {e}'
            continue
        seen = set()
        for r in rows:
            sid = str(r.get('id'))
            seen.add(sid)
            name = r.get('name') or ' '.join(x for x in (r.get('first_name'), r.get('last_name')) if x) or r.get('username') or sid
            extra = {k: r[k] for k in ('due_date', 'percentage', 'type') if k in r}
            CatalogItem.objects.update_or_create(kind=kind, contable_id=sid, defaults={'code': str(r.get('code', '') or ''), 'name': name[:160],
                                                                                    'active': bool(r.get('active', True)), 'extra': extra, 'fetched_at': now})
        CatalogItem.objects.filter(kind=kind).exclude(contable_id__in=seen).update(active=False)
        summary[kind] = len(rows)
    return summary


def ensure_catalogs():
    if not CatalogItem.objects.exists():
        sync_catalogs()


def catalog(kind):
    ensure_catalogs()
    return CatalogItem.objects.filter(kind=kind, active=True)
