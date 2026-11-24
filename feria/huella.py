"""Huella del escenario: conteos y sumas por tabla, para comprobar que dos generaciones con la misma semilla y la
misma fecha producen los mismos datos.

Por cada tabla del negocio: número de filas, suma de cada columna numérica y, en conjunto, una huella de todos
los valores. Quedan por fuera los pocos valores que cambian por diseño entre dos instalaciones: las contraseñas
(se generan al reiniciar), las huellas que dependen del secreto local de cada carpeta de datos y los pesos en coma
flotante del modelo de pagos (las columnas en coma flotante se comparan redondeadas en las sumas).
"""
import hashlib
import json
from decimal import Decimal
from django.apps import apps
from django.db import models

APPS = ('inventory', 'cartera', 'contable', 'agentes', 'aprendizaje', 'notificaciones')
SKIP_MODELS = {'inventory.loginattempt', 'inventory.alertread'}
VOLATILE = {('inventory.user', 'password'), ('inventory.user', 'last_login'), ('cartera.payer', 'key'), ('cartera.banktransaction', 'fingerprint'),
            ('aprendizaje.learnedmodel', 'weights'), ('aprendizaje.learnedmodel', 'meta'), ('cartera.statementimport', 'rows')}
NUMERIC = (models.IntegerField, models.DecimalField, models.FloatField)


def table_summary():
    tables, digest = {}, hashlib.sha256()
    for model in sorted(apps.get_models(), key=lambda m: m._meta.label_lower):
        label = model._meta.label_lower
        if model._meta.app_label not in APPS or label in SKIP_MODELS:
            continue
        fields = [f for f in model._meta.concrete_fields if (label, f.name) not in VOLATILE]
        names = [f.attname for f in fields]
        numeric = [i for i, f in enumerate(fields) if isinstance(f, NUMERIC) or (isinstance(f, models.ForeignKey) and isinstance(f.target_field, models.IntegerField))]
        floats = {i for i, f in enumerate(fields) if isinstance(f, models.FloatField)}
        float_names = {names[i] for i in floats}
        sums = {names[i]: Decimal(0) for i in numeric}
        count = 0
        digest.update(label.encode())
        for row in model.objects.order_by('pk').values_list(*names).iterator(chunk_size=2000):
            count += 1
            for i in numeric:
                if row[i] is not None:
                    sums[names[i]] += Decimal(str(row[i]))
            digest.update(json.dumps([v for i, v in enumerate(row) if i not in floats], default=str, sort_keys=True, ensure_ascii=False).encode())
        tables[label] = {'filas': count, 'sumas': {k: (str(round(v, 3)) if k in float_names else str(v)) for k, v in sums.items() if v}}
    return {'tablas': tables, 'huella': digest.hexdigest(), 'filas': sum(t['filas'] for t in tables.values())}


def differences(a, b):
    """Lista legible de diferencias entre dos huellas (vacía si son iguales)."""
    out = []
    ta, tb = a.get('tablas', {}), b.get('tablas', {})
    for label in sorted(set(ta) | set(tb)):
        x, y = ta.get(label), tb.get(label)
        if x is None or y is None:
            out.append(f'{label}: solo existe en una de las dos generaciones')
        elif x['filas'] != y['filas']:
            out.append(f'{label}: {x["filas"]} filas frente a {y["filas"]}')
        elif x['sumas'] != y['sumas']:
            cols = sorted(k for k in set(x['sumas']) | set(y['sumas']) if x['sumas'].get(k) != y['sumas'].get(k))
            out.append(f'{label}: sumas distintas en {", ".join(cols)}')
    if not out and a.get('huella') != b.get('huella'):
        out.append('Los conteos y las sumas coinciden, pero la huella de los valores no.')
    return out
