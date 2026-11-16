"""Staging-only first load of reconciled serials; changed replays require review."""
from odoo import api, fields, models
from odoo.exceptions import UserError
from ..serial_logic import serial_snapshot_rows
from ..sync_logic import SyncConflict


class WoSerialLot(models.Model):
    _inherit = 'stock.lot'
    wo_source_key = fields.Char(copy=False, readonly=True, index=True)
    wo_product_source_id = fields.Char(copy=False, readonly=True, index=True)


class WoSerialSync(models.Model):
    _inherit = 'xing.wo.binding'

    @api.model
    def serial_initial_snapshot(self, source_key, company_id, products, dry_run=True):
        """ONE atomic request. Initial stock must be empty; exact replay is read-only.

        Missing serials are never zeroed or discarded. This is not an ongoing
        sales/purchases synchronizer. Production application remains forbidden.
        """
        service = self._stock_service(source_key, company_id, dry_run)
        try:
            products = serial_snapshot_rows(products)
            warehouse, locations = service._wo_stock_locations(source_key)
            Quant = service.env['stock.quant'].with_context(inventory_mode=True)
            Lot = service.env['stock.lot']
            Unit = service.env.ref('uom.product_uom_unit')
            plan = []
            for row in products:
                bindings = service.search([
                    ('source_key', '=', source_key), ('company_id', '=', company_id),
                    ('kind', '=', 'product'), ('source_id', '=', row['product_source_id'])], limit=2)
                product = bindings.target_ref.exists() if len(bindings) == 1 else False
                if not product or product._name != 'product.product' or product.company_id.id != company_id:
                    raise SyncConflict('serial_product_binding_missing_or_wrong_company')
                if product.default_code != row['product_code']:
                    raise SyncConflict('serial_product_code_mismatch')
                if (not product.active or product.type != 'consu' or not product.is_storable
                        or product.uom_id != Unit):
                    raise SyncConflict('serial_product_requires_active_storable_units')
                if len(product.product_tmpl_id.product_variant_ids) != 1:
                    raise SyncConflict('serial_template_has_other_variants')
                if product.tracking not in (False, 'none', 'serial'):
                    raise SyncConflict('serial_existing_lot_tracking_requires_review')
                # Lock product/template as well as quants: protect simultaneous setup.
                service.env.cr.execute('SELECT id FROM product_template WHERE id=%s FOR UPDATE NOWAIT',
                                       [product.product_tmpl_id.id])
                product.invalidate_recordset()
                quants = Quant.search([('product_id', '=', product.id)])
                if quants:
                    service.env.cr.execute('SELECT id FROM stock_quant WHERE id IN %s FOR UPDATE NOWAIT',
                                           [tuple(quants.ids)])
                    quants.invalidate_recordset()
                if any(q.reserved_quantity or q.inventory_quantity_set or q.owner_id or q.package_id for q in quants):
                    raise SyncConflict('serial_stock_reserved_owned_or_pending_count')
                lots = Lot.search([('product_id', '=', product.id)])
                if any(l.company_id.id != company_id or l.wo_source_key != source_key
                       or l.wo_product_source_id != row['product_source_id'] for l in lots):
                    raise SyncConflict('serial_existing_unmanaged_lots_require_review')
                expected = {s['serial']: s['warehouse_source_id'] for s in row['serials']}
                replay = bool(lots)
                if replay:
                    if product.tracking != 'serial' or set(lots.mapped('name')) != set(expected):
                        raise SyncConflict('serial_changed_snapshot_requires_review')
                    for lot in lots:
                        loc = locations[expected[lot.name]]
                        physical = quants.filtered(lambda q: q.lot_id == lot and q.location_id.usage in ('internal', 'transit'))
                        if not loc or len(physical) != 1 or physical.location_id != loc or physical.quantity != 1:
                            raise SyncConflict('serial_existing_location_or_quantity_requires_review')
                    if any(q.quantity and q.location_id.usage in ('internal', 'transit')
                           and q.lot_id not in lots for q in quants):
                        raise SyncConflict('serial_existing_extra_stock_requires_review')
                elif any(q.quantity for q in quants):
                    raise SyncConflict('serial_initial_load_requires_empty_stock')
                plan.append((row, product, replay))
            if not dry_run:
                warehouse, locations = service._wo_stock_locations(source_key, create=True)
                for row, product, replay in plan:
                    if replay:
                        continue
                    product.write({'tracking': 'serial'})
                    for item in row['serials']:
                        lot = Lot.create({'name': item['serial'], 'product_id': product.id,
                                          'company_id': company_id, 'wo_source_key': source_key,
                                          'wo_product_source_id': row['product_source_id']})
                        loc = locations[item['warehouse_source_id']]
                        quant = Quant.create({'product_id': product.id, 'location_id': loc.id,
                                              'lot_id': lot.id, 'inventory_quantity': 1})
                        action = quant.action_apply_inventory()
                        if isinstance(action, dict):
                            raise SyncConflict('serial_inventory_adjustment_requires_wizard')
                        actual = Quant.search([('product_id', '=', product.id), ('lot_id', '=', lot.id),
                                               ('location_id', '=', loc.id)])
                        actual.invalidate_recordset()
                        if len(actual) != 1 or actual.quantity != 1:
                            raise SyncConflict('serial_write_verification_failed')
            results = [{'product_source_id': r['product_source_id'], 'product_code': r['product_code'],
                        'serial_count': len(r['serials']),
                        'status': 'unchanged' if replay else ('would_create' if dry_run else 'created')}
                       for r, p, replay in plan]
            return {'dry_run': dry_run, 'results': results,
                    'serial_count': sum(r['serial_count'] for r in results)}
        except SyncConflict as error:
            raise UserError(str(error)) from None
