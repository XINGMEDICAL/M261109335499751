from copy import deepcopy
import hashlib
import logging

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from ..sync_logic import MODELS, SyncConflict, diff_values, validate_rows, validate_source

_logger = logging.getLogger(__name__)


class WorldOfficeBinding(models.Model):
    _name = 'xing.wo.binding'
    _description = 'Vinculo World Office - Odoo'
    _rec_name = 'source_id'

    source_key = fields.Char(required=True, readonly=True, index=True)
    source_id = fields.Char(required=True, readonly=True, index=True)
    kind = fields.Selection([(k, k) for k in MODELS], required=True, readonly=True)
    company_id = fields.Many2one('res.company', required=True, readonly=True, index=True)
    target_ref = fields.Reference(
        selection=[(name, kind) for kind, name in MODELS.items()],
        required=True, readonly=True,
    )
    _unique_source = models.Constraint(
        'UNIQUE(source_key, company_id, kind, source_id)', 'Identificador de origen ya vinculado.'
    )
    _unique_target = models.Constraint(
        'UNIQUE(source_key, company_id, kind, target_ref)', 'Destino ya vinculado a otro registro del mismo origen.'
    )

    def _authorise(self, company_id=None):
        if not self.env.user.has_group('base.group_system'):
            raise AccessError('Se requiere un usuario administrador autorizado para el conector.')
        if company_id is not None:
            if type(company_id) is not int or company_id not in self.env.user.company_ids.ids:
                raise AccessError('Empresa fuera del acceso del usuario.')
            company = self.env['res.company'].browse(company_id)
            return self.with_context(
                allowed_company_ids=[company_id], active_test=False,
                tracking_disable=True, mail_create_nosubscribe=True, mail_notrack=True,
            ).with_company(company)
        return self

    @api.model
    def connector_info(self):
        self._authorise()
        return {
            'protocol': 2, 'version': '20.0.1.3.1', 'max_batch': 50,
            'kinds': list(MODELS),
            'companies': self.env.user.company_ids.read(['id', 'name']),
            'employee_fields': ['name', 'wo_identification'],
            'asset_fields': ['name', 'wo_asset_code', 'serial_no'],
            'archives_automatically': False,
        }

    @api.model
    def sync_batch(self, source_key, company_id, kind, rows, dry_run=True):
        """Cada peticion es atomica; cada fila tiene un savepoint.

        Una repeticion tras perder la respuesta encuentra el vinculo ya confirmado.
        No hay commit manual: JSON-2 confirma la transaccion completa de la peticion.
        """
        service = self._authorise(company_id)
        if type(dry_run) is not bool:
            raise UserError('dry_run debe ser booleano.')
        source_key = validate_source(source_key)
        if kind not in MODELS or not isinstance(rows, list) or not 1 <= len(rows) <= 50:
            raise UserError('Tipo o tamano de lote invalido (1 a 50).')
        try:
            rows = validate_rows(kind, rows)
        except SyncConflict as error:
            raise UserError(str(error)) from None
        lock = int.from_bytes(
            hashlib.sha256(f'xing_wo_sync:{company_id}:{kind}'.encode()).digest()[:8],
            byteorder='big', signed=True,
        )
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', [lock])
        results = []
        for row in rows:
            try:
                with self.env.cr.savepoint():
                    result = service._upsert(source_key, kind, row, dry_run)
                results.append(result)
            except Exception as error:
                # No emitir nombre, documento, claves, payload ni mensaje SQL en logs.
                code = str(error) if isinstance(error, SyncConflict) else 'odoo_operation_failed'
                _logger.warning('WO sync: kind=%s failure_type=%s', kind, type(error).__name__)
                results.append({'source_id': row['source_id'], 'status': 'error', 'error': code})
        counts = {}
        for result in results:
            counts[result['status']] = counts.get(result['status'], 0) + 1
        return {'dry_run': dry_run, 'kind': kind, 'counts': counts, 'results': results}

    def _domain_company(self, kind):
        if kind == 'product':
            return [('company_id', 'in', [False, self.env.company.id])]
        return [('company_id', '=', self.env.company.id)]

    def _candidates(self, kind, row):
        Model = self.env[MODELS[kind]].with_context(active_test=False)
        domain = self._domain_company(kind)
        if kind == 'product':
            domain += [('default_code', '=', row['code'])]
        elif kind == 'employee':
            if 'identification_id' in Model._fields:
                domain += ['|', ('wo_identification', '=', row['identification']),
                           ('identification_id', '=', row['identification'])]
            else:
                domain += [('wo_identification', '=', row['identification'])]
        else:
            if row['serial']:
                domain += ['|', ('wo_asset_code', '=', row['code']), ('serial_no', '=', row['serial'])]
            else:
                domain += [('wo_asset_code', '=', row['code'])]
        return Model.search(domain, limit=2)

    def _find_target(self, source_key, kind, row):
        Binding = self.with_context(active_test=False)
        binding = Binding.search([
            ('source_key', '=', source_key), ('company_id', '=', self.env.company.id),
            ('kind', '=', kind), ('source_id', '=', row['source_id']),
        ], limit=2)
        if len(binding) > 1:
            raise SyncConflict('duplicate_binding')
        candidates = self._candidates(kind, row)
        if len(candidates) > 1:
            raise SyncConflict('ambiguous_existing_records')
        if binding:
            target = binding.target_ref.exists()
            if not target or target._name != MODELS[kind]:
                raise SyncConflict('binding_target_missing_or_wrong_model')
            if candidates and candidates != target:
                raise SyncConflict('identity_now_matches_another_record')
        else:
            target = candidates
        if target:
            if not target.company_id or target.company_id != self.env.company:
                raise SyncConflict('shared_or_other_company_record_requires_review')
            if 'active' in target._fields and not target.active:
                raise SyncConflict('archived_record_requires_review')
            other = Binding.search([
                ('source_key', '=', source_key), ('company_id', '=', self.env.company.id),
                ('kind', '=', kind), ('target_ref', '=', f'{target._name},{target.id}'),
                ('source_id', '!=', row['source_id']),
            ], limit=1)
            if other:
                raise SyncConflict('target_already_bound_to_another_source_record')
        elif kind in ('employee', 'asset'):
            # Solo bloquear homonimos SIN identidad verificable. Dos equipos o
            # personas con el mismo nombre y claves distintas son registros validos.
            Model = self.env[MODELS[kind]].with_context(active_test=False)
            unknown_domain = self._domain_company(kind) + [('name', '=', row['name'])]
            if kind == 'employee':
                unknown_domain += [('wo_identification', '=', False)]
                if 'identification_id' in Model._fields:
                    unknown_domain += [('identification_id', '=', False)]
            else:
                unknown_domain += [('wo_asset_code', '=', False)]
            if Model.search(unknown_domain, limit=1):
                raise SyncConflict('name_exists_without_verified_identity')
        return binding, target

    def _product_category(self, name, dry_run):
        Category = self.env['product.category']
        category = Category.search([('name', '=', name)], limit=2)
        if len(category) > 1:
            raise SyncConflict('duplicate_product_category')
        if not category and not dry_run:
            category = Category.create({'name': name})
        return category

    def _desired(self, kind, row, target, dry_run):
        vals = {'name': row['name']}
        if kind == 'product':
            is_service = row['classification'] == 'Servicio'
            desired_type = 'service' if is_service else 'consu'
            if target and (target.type != desired_type or target.is_storable != (not is_service)):
                raise SyncConflict('product_type_or_tracking_change_requires_review')
            vals.update(default_code=row['code'], type=desired_type, is_storable=not is_service)
            category = self._product_category(row['category'], dry_run)
            if category:
                vals['categ_id'] = category.id
        elif kind == 'employee':
            vals['wo_identification'] = row['identification']
        else:
            vals['wo_asset_code'] = row['code']
            # Un serial vacio no borra uno cargado manualmente en Odoo.
            if row['serial']:
                vals['serial_no'] = row['serial']
        if not target:
            vals['company_id'] = self.env.company.id
        return vals

    def _upsert(self, source_key, kind, row, dry_run):
        if kind in ('pricelist', 'price'):
            return self._upsert_pricing(source_key, kind, row, dry_run)
        binding, target = self._find_target(source_key, kind, row)
        desired = self._desired(kind, row, target, dry_run)
        category_missing = (kind == 'product' and 'categ_id' not in desired)
        if target:
            current = {}
            for key in desired:
                value = target[key]
                current[key] = value.id if target._fields[key].type == 'many2one' else value
            changes = diff_values(current, desired)
            if category_missing:
                # No hay ID de categoria en simulacion, pero SI se requeriria cambiarla.
                change_fields = sorted(set(changes) | {'categ_id'})
            else:
                change_fields = sorted(changes)
            if dry_run:
                status = 'would_update' if change_fields else ('would_link' if not binding else 'unchanged')
            else:
                if changes:
                    target.write(deepcopy(changes))
                    self._verify(target, desired)
                status = 'updated' if changes else ('linked' if not binding else 'unchanged')
        else:
            change_fields = sorted(desired)
            status = 'would_create' if dry_run else 'created'
            if not dry_run:
                target = self.env[MODELS[kind]].create(deepcopy(desired))
                self._verify(target, desired)
        if not dry_run and not binding:
            self.create({
                'source_key': source_key, 'source_id': row['source_id'], 'kind': kind,
                'company_id': self.env.company.id, 'target_ref': f'{target._name},{target.id}',
            })
        return {
            'source_id': row['source_id'], 'status': status,
            'target_id': target.id if target else False, 'fields': change_fields,
            'category_missing': category_missing,
        }

    def _verify(self, target, desired):
        target.invalidate_recordset(list(desired))
        for key, expected in desired.items():
            actual = target[key]
            if target._fields[key].type == 'many2one':
                actual = actual.id
            if actual != expected:
                # Reportar solo el nombre tecnico del campo, nunca su valor.
                raise SyncConflict(f'write_verification_failed_{key}')
