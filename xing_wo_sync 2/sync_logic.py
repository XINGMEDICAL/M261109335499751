"""Validacion independiente del ORM; no transforma documentos ni codigos."""
import re
from collections import Counter


class SyncConflict(ValueError):
    """El mensaje es un codigo fijo, nunca datos personales."""


MODELS = {'product': 'product.product', 'employee': 'hr.employee', 'asset': 'maintenance.equipment'}
CATEGORIES = {'Producto': 'PRODUCTOS NO FABRICADOS POR LA EMPRESA', 'Servicio': 'SERVICIOS'}


def text(value, required=False, maximum=4096):
    if value is None:
        value = ''
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise SyncConflict('invalid_text')
    value = str(value).strip()
    if (required and not value) or len(value) > maximum or '\x00' in value:
        raise SyncConflict('missing_or_invalid_text')
    return value


def validate_source(value):
    value = text(value, True, 100)
    if not re.fullmatch(r'[a-zA-Z0-9_.:-]+', value):
        raise SyncConflict('invalid_source_key')
    return value


def normalise(kind, row):
    if kind not in MODELS or not isinstance(row, dict):
        raise SyncConflict('invalid_kind_or_row')
    result = {'source_id': validate_source(row.get('source_id')), 'name': text(row.get('name'), True)}
    if kind == 'product':
        classification = text(row.get('classification'), True, 40)
        category = text(row.get('category'), True, 200)
        if CATEGORIES.get(classification) != category:
            raise SyncConflict('product_outside_agreed_scope')
        result.update(code=text(row.get('code'), True, 128), classification=classification, category=category)
    elif kind == 'employee':
        result['identification'] = text(row.get('identification'), True, 128)
    else:
        classification = text(row.get('classification'), True, 40)
        category = text(row.get('category'), True, 200)
        if category != 'ACTIVOS FIJOS' or classification not in ('Activo Fijo', 'Producto'):
            raise SyncConflict('asset_outside_agreed_scope')
        result.update(code=text(row.get('code'), True, 128), serial=text(row.get('serial'), maximum=128),
                      classification=classification, category=category)
    return result


def validate_rows(kind, rows):
    """Se valida el conjunto COMPLETO antes de dividirlo en lotes."""
    if not isinstance(rows, list):
        raise SyncConflict('rows_must_be_list')
    result = [normalise(kind, row) for row in rows]
    unique_fields = ['source_id', 'identification' if kind == 'employee' else 'code']
    if kind == 'asset':
        unique_fields.append('serial')
    for field in unique_fields:
        counts = Counter(row[field] for row in result if row.get(field))
        if any(value > 1 for value in counts.values()):
            raise SyncConflict('duplicate_source_' + field)
    return result


def diff_values(current, desired):
    return {key: value for key, value in desired.items() if current.get(key) != value}
