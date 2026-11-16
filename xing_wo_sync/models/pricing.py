from decimal import Decimal

from odoo import api, fields, models
from ..sync_logic import SyncConflict, diff_values


class ManagedPricelist(models.Model):
    _inherit = 'product.pricelist'

    wo_source_key = fields.Char(index=True, copy=False, readonly=True)
    wo_price_column = fields.Char(index=True, copy=False, readonly=True)


class PriceSync(models.Model):
    _inherit = 'xing.wo.binding'

    def _price_binding(self, source_key, kind, source_id):
        return self.search([
            ('source_key', '=', source_key), ('company_id', '=', self.env.company.id),
            ('kind', '=', kind), ('source_id', '=', source_id),
        ], limit=1)

    def _bind_price_target(self, source_key, kind, source_id, target):
        self.create({
            'source_key': source_key, 'company_id': self.env.company.id,
            'kind': kind, 'source_id': source_id, 'target_ref': f'{target._name},{target.id}',
        })

    def _get_wo_pricelist(self, source_key, row):
        binding = self._price_binding(source_key, 'pricelist', row['list_key'])
        Pricelist = self.env['product.pricelist'].with_context(active_test=False)
        target = binding.target_ref.exists() if binding else Pricelist.search([
            ('wo_source_key', '=', source_key), ('wo_price_column', '=', row['list_key']),
            ('company_id', '=', self.env.company.id),
        ], limit=2)
        if len(target) > 1:
            raise SyncConflict('duplicate_managed_pricelist')
        if binding and not target:
            raise SyncConflict('pricelist_binding_target_missing')
        if target and (target._name != 'product.pricelist' or target.company_id != self.env.company
                       or target.currency_id.name != 'COP' or not target.active
                       or target.wo_source_key != source_key or target.wo_price_column != row['list_key']):
            raise SyncConflict('pricelist_configuration_requires_review')
        return binding, target

    def _upsert_pricing(self, source_key, kind, row, dry_run):
        if kind == 'pricelist':
            currency = self.env['res.currency'].with_context(active_test=False).search([('name', '=', 'COP')], limit=2)
            if len(currency) != 1 or not currency.active:
                raise SyncConflict('COP_currency_not_active')
            binding, target = self._get_wo_pricelist(source_key, row)
            if not target:
                if self.env['product.pricelist'].with_context(active_test=False).search([
                    ('name', '=', row['name']), ('company_id', '=', self.env.company.id),
                ], limit=1):
                    raise SyncConflict('pricelist_name_exists_without_verified_identity')
                if dry_run:
                    return {'source_id': row['source_id'], 'status': 'would_create'}
                target = self.env['product.pricelist'].create({
                    'name': row['name'], 'company_id': self.env.company.id,
                    'currency_id': currency.id, 'wo_source_key': source_key, 'wo_price_column': row['list_key'],
                })
                status = 'created'
            else:
                status = 'unchanged' if binding else ('would_link' if dry_run else 'linked')
            if not dry_run and not binding:
                self._bind_price_target(source_key, kind, row['source_id'], target)
            return {'source_id': row['source_id'], 'status': status, 'target_id': target.id}

        # Cero/vacio = sin configuracion. No escribir $0, no borrar precio previo,
        # no bloquear una cotizacion ni cambiar el precio manual de una venta.
        binding = self._price_binding(source_key, 'price', row['source_id'])
        if row['amount'] is None or Decimal(row['amount']) == 0:
            return {'source_id': row['source_id'],
                    'status': 'review_unconfigured' if binding else 'unconfigured',
                    'target_id': binding.target_ref.id if binding and binding.target_ref.exists() else False}

        product_binding = self._price_binding(source_key, 'product', row['product_source_id'])
        product = product_binding.target_ref.exists() if product_binding else self.env['product.product'].browse()
        if not product_binding and dry_run:
            product = self.env['product.product'].with_context(active_test=False).search([
                ('default_code', '=', row['product_code']), ('company_id', '=', self.env.company.id),
            ], limit=2)
        if len(product) > 1:
            raise SyncConflict('ambiguous_product_for_price')
        if product_binding and not product:
            raise SyncConflict('price_product_binding_missing_target')
        if product and (product._name != 'product.product' or product.company_id != self.env.company or not product.active):
            raise SyncConflict('price_product_wrong_company_or_archived')
        if product and product.default_code != row['product_code']:
            raise SyncConflict('price_product_code_mismatch')
        _, pricelist = self._get_wo_pricelist(source_key, row)
        if not product or not pricelist:
            if not dry_run:
                raise SyncConflict('sync_product_and_pricelist_before_price')
            return {'source_id': row['source_id'], 'status': 'would_create', 'depends_on_new_records': True}

        target = binding.target_ref.exists() if binding else self.env['product.pricelist.item'].browse()
        if binding and not target:
            raise SyncConflict('price_binding_target_missing')
        if target and (target._name != 'product.pricelist.item' or target.product_id != product or target.pricelist_id != pricelist):
            raise SyncConflict('price_binding_wrong_target')
        # No tomar control de reglas manuales ni reglas con cantidades/fechas.
        overlapping = self.env['product.pricelist.item'].search([
            ('pricelist_id', '=', pricelist.id), ('id', '!=', target.id if target else 0),
            '|', ('product_id', '=', product.id), ('product_tmpl_id', '=', product.product_tmpl_id.id),
        ], limit=1)
        if overlapping:
            raise SyncConflict('overlapping_pricelist_rule_requires_review')
        if target and (target.compute_price != 'fixed' or target.min_quantity != 0
                       or target.date_start or target.date_end or target.uom_id):
            raise SyncConflict('managed_rule_changed_requires_review')
        amount = float(Decimal(row['amount']))
        if target:
            changed = target.fixed_price != amount
            if changed and not dry_run:
                target.write({'fixed_price': amount})
                self._verify(target, {'fixed_price': amount})
            status = ('would_update' if dry_run else 'updated') if changed else 'unchanged'
        else:
            if dry_run:
                return {'source_id': row['source_id'], 'status': 'would_create'}
            vals = {
                'pricelist_id': pricelist.id, 'product_id': product.id,
                'product_tmpl_id': product.product_tmpl_id.id,
                'applied_on': '0_product_variant', 'compute_price': 'fixed',
                'fixed_price': amount, 'min_quantity': 0,
            }
            target = self.env['product.pricelist.item'].create(vals)
            self._verify(target, {'fixed_price': amount, 'product_id': product.id, 'pricelist_id': pricelist.id})
            self._bind_price_target(source_key, kind, row['source_id'], target)
            status = 'created'
        return {'source_id': row['source_id'], 'status': status, 'target_id': target.id}

    @api.model
    def price_source_rows(self, source_key, company_id):
        """Identidades de reglas existentes para detectar un precio que paso a cero."""
        from ..sync_logic import validate_source
        service = self._authorise(company_id)
        source_key = validate_source(source_key)
        result = []
        for binding in service.search([
            ('source_key', '=', source_key), ('company_id', '=', company_id), ('kind', '=', 'price'),
        ]):
            product_source, _, key = binding.source_id.rpartition(':')
            target = binding.target_ref.exists()
            if target and target._name == 'product.pricelist.item':
                result.append({'source_id': binding.source_id, 'product_source_id': product_source,
                               'product_code': target.product_id.default_code or '', 'list_key': key,
                               'amount': '0', 'currency': 'COP'})
        return result
