"""WO physical snapshots: standard inventory adjustments, staging only in v1."""
import hashlib
from decimal import Decimal

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..stock_logic import BODEGAS, stock_rows, validate_stock_tracking
from ..sync_logic import SyncConflict, validate_source


class WoWarehouse(models.Model):
    _inherit = 'stock.warehouse'
    wo_source_key = fields.Char(copy=False, readonly=True, index=True)


class WoLocation(models.Model):
    _inherit = 'stock.location'
    wo_source_key = fields.Char(copy=False, readonly=True, index=True)
    wo_warehouse_source_id = fields.Char(copy=False, readonly=True, index=True)


class WoStockBalance(models.Model):
    _name = 'xing.wo.stock.balance'
    _description = 'Ultimo saldo por bodega recibido de World Office'
    _rec_name = 'product_id'
    source_key = fields.Char(required=True, readonly=True, index=True)
    company_id = fields.Many2one('res.company', required=True, readonly=True, index=True)
    product_source_id = fields.Char(required=True, readonly=True)
    warehouse_source_id = fields.Char(required=True, readonly=True)
    product_id = fields.Many2one('product.product', required=True, readonly=True, ondelete='restrict')
    location_id = fields.Many2one('stock.location', required=True, readonly=True, ondelete='restrict')
    quantity = fields.Float(readonly=True, digits='Product Unit')
    received_at = fields.Datetime(readonly=True)
    _source_unique = models.Constraint(
        'UNIQUE(source_key,company_id,product_source_id,warehouse_source_id)',
        'Saldo de origen ya registrado.')


class WoStockSync(models.Model):
    _inherit = 'xing.wo.binding'

    def _stock_service(self, source_key, company_id, dry_run):
        service = self._authorise(company_id)
        if type(dry_run) is not bool:
            raise UserError('dry_run debe ser booleano.')
        if not self.env.user.has_group('stock.group_stock_manager'):
            raise UserError('Requiere permisos de administrador de Inventario.')
        # First release deliberately cannot change real production quantities.
        # Staging neutralization is verified server-side, not from the supplied URL.
        neutralized = service.env['ir.config_parameter'].sudo().get_bool('database.is_neutralized', default=False)
        if not dry_run and not neutralized:
            raise UserError('Esta version solo aplica existencias en una base neutralizada de pruebas.')
        validate_source(source_key)
        lock = int.from_bytes(hashlib.sha256(f'wo-stock:{company_id}'.encode()).digest()[:8], 'big', signed=True)
        service.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', [lock])
        return service

    def _wo_stock_root(self, source_key, create=False):
        # Odoo 20 has no shared stock.stock_location_locations XML ID.
        # Own top-level view: independent of all shipping warehouse subtrees.
        Location = self.env['stock.location'].with_context(active_test=False)
        root = Location.search([
            ('company_id', '=', self.env.company.id), ('wo_source_key', '=', source_key),
            ('wo_warehouse_source_id', '=', '_root'),
        ], limit=2)
        if len(root) > 1 or (root and (not root.active or root.usage != 'view' or root.location_id)):
            raise SyncConflict('stock_auxiliary_root_configuration_changed')
        if not root and create:
            root = Location.create({
                'name': 'World Office - ubicaciones separadas', 'usage': 'view',
                'location_id': False, 'company_id': self.env.company.id,
                'wo_source_key': source_key, 'wo_warehouse_source_id': '_root',
            })
        return root

    def _wo_stock_locations(self, source_key, create=False):
        Warehouse = self.env['stock.warehouse'].with_context(active_test=False)
        warehouse = Warehouse.search([('company_id', '=', self.env.company.id),
                                      ('wo_source_key', '=', source_key)], limit=2)
        if len(warehouse) > 1 or (warehouse and not warehouse.active):
            raise SyncConflict('stock_managed_warehouse_conflict')
        if not warehouse and create:
            if Warehouse.search([('company_id', '=', self.env.company.id), ('code', '=', 'WO')], limit=1):
                raise SyncConflict('stock_warehouse_code_WO_already_used')
            warehouse = Warehouse.create({'name': 'World Office', 'code': 'WO',
                                          'company_id': self.env.company.id, 'wo_source_key': source_key})
        Location = self.env['stock.location'].with_context(active_test=False)
        result = {}
        root = self._wo_stock_root(source_key, create=create)
        for key, (name, usage) in BODEGAS.items():
            loc = Location.search([('company_id', '=', self.env.company.id),
                                   ('wo_source_key', '=', source_key),
                                   ('wo_warehouse_source_id', '=', key)], limit=2)
            if len(loc) > 1:
                raise SyncConflict('stock_duplicate_managed_location')
            if not loc and create:
                if key == 'Uno':
                    loc = warehouse.lot_stock_id
                    loc.write({'name': 'Principal WO', 'wo_source_key': source_key,
                               'wo_warehouse_source_id': key})
                else:
                    # Quarantine and transit are outside the shipping warehouse's
                    # subtree. Its sale pickings cannot reserve these balances.
                    loc = Location.create({'name': name + ' WO', 'usage': usage,
                                           'location_id': root.id, 'company_id': self.env.company.id,
                                           'wo_source_key': source_key, 'wo_warehouse_source_id': key})
            if loc:
                if not warehouse or not loc.active or loc.usage != usage:
                    raise SyncConflict('stock_location_configuration_changed')
                if key == 'Uno' and loc != warehouse.lot_stock_id:
                    raise SyncConflict('stock_principal_location_changed')
                if key != 'Uno' and (not root or loc.location_id != root):
                    raise SyncConflict('stock_auxiliary_location_parent_changed')
                if key != 'Uno' and loc.parent_path.startswith(warehouse.view_location_id.parent_path):
                    raise SyncConflict('stock_quarantine_or_transit_inside_sale_warehouse')
            result[key] = loc
        return warehouse, result

    @api.model
    def stock_snapshot(self, source_key, company_id, rows, dry_run=True):
        """Whole snapshot in ONE transaction, atomic on apply; never invent zeros.

        Replaying an already-applied snapshot produces no new adjustments.
        A mismatch or unknown unit prevents ALL changes in this request.
        """
        service = self._stock_service(source_key, company_id, dry_run)
        try:
            rows = stock_rows(rows)
        except SyncConflict as error:
            raise UserError(str(error)) from None
        Ledger = service.env['xing.wo.stock.balance']
        previous = Ledger.search([('company_id', '=', company_id), ('source_key', '=', source_key)])
        keys = {r['source_id'] for r in rows}
        if any(p.product_source_id + ':' + p.warehouse_source_id not in keys for p in previous):
            raise UserError('stock_previously_received_pair_missing_no_implicit_zero')
        warehouse, locations = service._wo_stock_locations(source_key)
        plan, failures = [], []
        Unit = service.env.ref('uom.product_uom_unit')
        Quant = service.env['stock.quant'].with_context(inventory_mode=True)
        for row in rows:
            try:
                binding = service.search([('source_key', '=', source_key), ('company_id', '=', company_id),
                                          ('kind', '=', 'product'), ('source_id', '=', row['product_source_id'])], limit=1)
                product = binding.target_ref.exists() if binding else False
                if not product or product._name != 'product.product' or product.company_id.id != company_id:
                    raise SyncConflict('stock_product_binding_missing_or_wrong_company')
                if not product.active or not product.is_storable or product.type != 'consu':
                    raise SyncConflict('stock_product_not_active_storable_goods')
                if product.default_code != row['product_code']:
                    raise SyncConflict('stock_product_code_mismatch')
                validate_stock_tracking(product.tracking)
                if product.uom_id != Unit:
                    raise SyncConflict('stock_odoo_unit_requires_mapping')
                qty = float(Decimal(row['quantity']))
                if product.uom_id.round(qty) != qty:
                    raise SyncConflict('stock_quantity_not_representable_in_odoo_unit')
                managed_locations = [location.id for location in locations.values() if location]
                if Quant.search_count([
                    ('product_id', '=', product.id), ('company_id', '=', company_id),
                    ('location_id.usage', 'in', ['internal', 'transit']),
                    ('location_id', 'not in', managed_locations), ('quantity', '!=', 0),
                ]):
                    raise SyncConflict('stock_other_location_balance_requires_review')
                loc = locations[row['warehouse_source_id']]
                quants = Quant.search([('product_id', '=', product.id), ('location_id', '=', loc.id)]) if loc else Quant.browse()
                if quants:
                    # Coordinate with standard stock operations too; never race a count.
                    service.env.cr.execute('SELECT id FROM stock_quant WHERE id IN %s FOR UPDATE NOWAIT', [tuple(quants.ids)])
                    quants.invalidate_recordset()
                if any(q.lot_id or q.package_id or q.owner_id or q.reserved_quantity or q.inventory_quantity_set for q in quants):
                    raise SyncConflict('stock_reserved_tracked_owned_or_pending_count')
                if len(quants) > 1:
                    raise SyncConflict('stock_multiple_quants_require_review')
                prior = previous.filtered(lambda p: p.product_source_id == row['product_source_id'] and
                                            p.warehouse_source_id == row['warehouse_source_id'])
                if prior and (prior.product_id != product or prior.location_id != loc):
                    raise SyncConflict('stock_persistent_binding_changed')
                current = sum(quants.mapped('quantity'))
                plan.append((row, product, loc, quants, prior, qty, current))
            except SyncConflict as error:
                failures.append({'source_id': row['source_id'], 'status': 'error', 'error': str(error)})
        if failures:
            if not dry_run:
                raise UserError('stock_preflight_failed_no_changes: ' + ', '.join(sorted({r['error'] for r in failures})))
            return {'dry_run': True, 'counts': {'error': len(failures)}, 'results': failures,
                    'locations_ready': bool(warehouse)}
        if not dry_run:
            warehouse, locations = service._wo_stock_locations(source_key, create=True)
        results = []
        for row, product, loc, quants, prior, qty, current in plan:
            changed = qty != current
            if not dry_run:
                loc = locations[row['warehouse_source_id']]
                if changed:
                    if not quants:
                        quants = Quant.create({'product_id': product.id, 'location_id': loc.id,
                                               'inventory_quantity': qty})
                    else:
                        quants.write({'inventory_quantity': qty})
                    action = quants.action_apply_inventory()
                    if isinstance(action, dict):
                        raise UserError('stock_inventory_adjustment_requires_wizard_no_changes')
                    quants.invalidate_recordset(['quantity'])
                    if sum(quants.mapped('quantity')) != qty:
                        raise UserError('stock_write_verification_failed_no_changes')
                values = {'quantity': qty, 'received_at': fields.Datetime.now()}
                if prior:
                    prior.write(values)
                else:
                    Ledger.create(dict(values, source_key=source_key, company_id=company_id,
                                       product_source_id=row['product_source_id'],
                                       warehouse_source_id=row['warehouse_source_id'],
                                       product_id=product.id, location_id=loc.id))
            results.append({'source_id': row['source_id'], 'status': ('would_adjust' if dry_run else 'adjusted') if changed else 'unchanged',
                            'source_quantity': row['quantity'], 'odoo_quantity': current,
                            'warehouse_source_id': row['warehouse_source_id'],
                            'unit_mapping_pending': row['source_uom'] == 'Cj*25'})
        counts = {}
        for result in results:
            counts[result['status']] = counts.get(result['status'], 0) + 1
        return {'dry_run': dry_run, 'counts': counts, 'results': results,
                'locations_ready': bool(warehouse), 'negative_rows': sum(Decimal(r['quantity']) < 0 for r in rows)}
