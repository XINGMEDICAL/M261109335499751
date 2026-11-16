"""Initial, explicitly reconciled serial inventories. Never infer missing serials."""
from collections import Counter
from decimal import Decimal
from .stock_logic import stock_rows
from .sync_logic import SyncConflict, validate_source


def serial_snapshot_rows(products):
    if not isinstance(products, list) or not 1 <= len(products) <= 20:
        raise SyncConflict('serial_product_count_invalid')
    result, seen = [], set()
    for raw in products:
        if not isinstance(raw, dict):
            raise SyncConflict('serial_product_invalid')
        pid = validate_source(raw.get('product_source_id'))
        if pid in seen:
            raise SyncConflict('serial_duplicate_product_id')
        seen.add(pid)
        balances = raw.get('balances')
        if not isinstance(balances, list):
            raise SyncConflict('serial_balances_required')
        rows = stock_rows([dict(b, product_source_id=pid,
                                product_code=raw.get('product_code'), source_uom='Und.')
                           for b in balances if isinstance(b, dict)])
        if len(rows) != len(balances):
            raise SyncConflict('serial_balance_invalid')
        expected = {r['warehouse_source_id']: Decimal(r['quantity']) for r in rows}
        if any(q < 0 or q != q.to_integral_value() for q in expected.values()):
            raise SyncConflict('serial_quantity_requires_nonnegative_integers')
        serials = raw.get('serials')
        if not isinstance(serials, list) or not 1 <= len(serials) <= 5000:
            raise SyncConflict('serial_detail_required')
        names, counts, detail = set(), Counter(), []
        for item in serials:
            if not isinstance(item, dict):
                raise SyncConflict('serial_detail_invalid')
            name = item.get('serial')
            # Preserve identifiers literally: do not trim, split or generate them.
            if (not isinstance(name, str) or not name or len(name) > 128
                    or name != name.strip() or any(ord(c) < 32 for c in name)
                    or ';' in name or ',' in name):
                raise SyncConflict('serial_identifier_requires_review')
            if name in names:
                raise SyncConflict('serial_duplicate_in_product')
            names.add(name)
            warehouse = item.get('warehouse_source_id')
            if warehouse not in expected:
                raise SyncConflict('serial_warehouse_without_official_balance')
            counts[warehouse] += 1
            detail.append({'serial': name, 'warehouse_source_id': warehouse})
        if any(counts[k] != q for k, q in expected.items()):
            raise SyncConflict('serial_count_differs_from_official_stock')
        result.append({'product_source_id': pid, 'product_code': rows[0]['product_code'],
                       'balances': rows, 'serials': detail})
    return result
