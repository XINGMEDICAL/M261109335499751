"""Strict snapshot validation. No inference of zero for absent WO rows."""
from decimal import Decimal, InvalidOperation
from .sync_logic import SyncConflict, text, validate_source

BODEGAS = {'Uno': ('Principal', 'internal'), 'Dos': ('Cuarentena', 'internal'),
           'Tres': ('Traslados', 'transit')}


def stock_rows(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
        raise SyncConflict('stock_snapshot_size_invalid')
    result, seen = [], set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise SyncConflict('stock_row_invalid')
        product = validate_source(raw.get('product_source_id'))
        warehouse = text(raw.get('warehouse_source_id'), True, 30)
        if warehouse not in BODEGAS or raw.get('warehouse_name') != BODEGAS[warehouse][0]:
            raise SyncConflict('stock_unknown_or_renamed_warehouse')
        key = (product, warehouse)
        if key in seen:
            raise SyncConflict('stock_duplicate_product_warehouse')
        seen.add(key)
        amount = raw.get('quantity')
        if isinstance(amount, bool) or amount is None or str(amount).strip() == '':
            raise SyncConflict('stock_quantity_missing')
        try:
            qty = Decimal(str(amount))
        except InvalidOperation:
            raise SyncConflict('stock_quantity_invalid') from None
        if not qty.is_finite() or abs(qty) > Decimal('999999999999'):
            raise SyncConflict('stock_quantity_invalid')
        unit = text(raw.get('source_uom'), True, 30)
        # The source has a single Cj*25 item with zero stock. Its nonzero balances
        # require a separately confirmed unit/price mapping, never guess x25.
        if unit != 'Und.' and not (unit == 'Cj*25' and qty == 0):
            raise SyncConflict('stock_source_unit_requires_mapping')
        result.append({'product_source_id': product, 'warehouse_source_id': warehouse,
                       'warehouse_name': BODEGAS[warehouse][0],
                       'product_code': text(raw.get('product_code'), True, 128),
                       'source_uom': unit, 'quantity': str(qty),
                       'source_id': product + ':' + warehouse})
    return result
