from odoo.tests import TransactionCase, tagged
from odoo.exceptions import AccessError


@tagged('post_install', '-at_install')
class TestWorldOfficeSync(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env['xing.wo.binding']
        self.company = self.env.company
        self.source = 'wo_integration_test'
        self.product_row = dict(source_id='101', code='WO-TEST-101', name='Test producto',
                                classification='Producto', category='PRODUCTOS NO FABRICADOS POR LA EMPRESA')

    def batch(self, kind, rows, dry=False, company=None):
        return self.service.sync_batch(self.source, (company or self.company).id, kind, rows, dry)

    def test_dry_run_creates_nothing(self):
        before = self.env['product.product'].search_count([('default_code','=','WO-TEST-101')])
        result = self.batch('product', [self.product_row], True)
        self.assertEqual(result['counts'], {'would_create':1})
        self.assertEqual(self.env['product.product'].search_count([('default_code','=','WO-TEST-101')]), before)
        self.assertFalse(self.service.search([('source_key','=',self.source)]))

    def test_repeat_does_not_duplicate(self):
        first = self.batch('product', [self.product_row])
        second = self.batch('product', [self.product_row])
        self.assertEqual(first['counts'], {'created':1})
        self.assertEqual(second['counts'], {'unchanged':1})
        self.assertEqual(first['results'][0]['target_id'], second['results'][0]['target_id'])

    def test_existing_shared_product_is_not_changed(self):
        product = self.env['product.product'].create({'name':'Shared','default_code':'WO-TEST-101','company_id':False})
        result = self.batch('product', [self.product_row])
        self.assertEqual(result['results'][0]['error'], 'shared_or_other_company_record_requires_review')
        self.assertEqual(product.name, 'Shared')

    def test_record_and_binding_rollback_together(self):
        # Fuerza fallo del vinculo; el producto creado en su savepoint no debe persistir.
        from unittest.mock import patch
        BindingClass = type(self.service)
        with patch.object(BindingClass, 'create', side_effect=RuntimeError('test failure')):
            result = self.batch('product', [self.product_row])
        self.assertEqual(result['counts'], {'error':1})
        self.assertFalse(self.env['product.product'].search([('default_code','=','WO-TEST-101')]))

    def test_employee_repeat_and_no_archive(self):
        row = dict(source_id='102', name='Test empleado', identification='WO-TEST-DOC')
        first = self.batch('employee', [row])
        self.assertEqual(first['counts'], {'created':1})
        employee = self.env['hr.employee'].browse(first['results'][0]['target_id'])
        self.assertEqual(employee.wo_identification, 'WO-TEST-DOC')
        self.assertEqual(self.batch('employee', [row])['counts'], {'unchanged':1})
        employee.active = False
        result = self.batch('employee', [row])
        self.assertEqual(result['results'][0]['error'], 'archived_record_requires_review')
        self.assertFalse(employee.active)

    def test_equipment_is_not_accounting_asset(self):
        row = dict(source_id='103', name='Equipo test', code='AF-TEST-103', serial='S-TEST-103', classification='Activo Fijo', category='ACTIVOS FIJOS')
        result = self.batch('asset', [row])
        self.assertEqual(result['counts'], {'created':1})
        equipment = self.env['maintenance.equipment'].browse(result['results'][0]['target_id'])
        self.assertEqual(equipment.wo_asset_code, 'AF-TEST-103')
        self.assertEqual(self.batch('asset', [row])['counts'], {'unchanged':1})

    def test_regular_user_cannot_sync(self):
        user = self.env['res.users'].create({'name':'WO Test User','login':'wo_sync_test_noadmin',
                                             'company_id':self.company.id,'company_ids':[(6,0,self.company.ids)]})
        with self.assertRaises(AccessError):
            self.service.with_user(user).sync_batch(self.source,self.company.id,'product',[self.product_row],True)

    def test_two_equipment_same_name_different_ids(self):
        rows = [dict(source_id=str(i), name='Equipo mismo nombre', code=f'AF-TEST-{i}',
                     serial='', classification='Activo Fijo', category='ACTIVOS FIJOS') for i in (104,105)]
        result = self.batch('asset', rows)
        self.assertEqual(result['counts'], {'created':2})
        self.assertNotEqual(result['results'][0]['target_id'], result['results'][1]['target_id'])
        self.assertEqual(self.batch('asset', rows)['counts'], {'unchanged':2})

    def test_two_employees_same_name_different_documents(self):
        rows = [dict(source_id=str(i), name='Test homonimo', identification=f'WO-TEST-{i}') for i in (106,107)]
        result = self.batch('employee', rows)
        self.assertEqual(result['counts'], {'created':2})
        self.assertEqual(self.batch('employee', rows)['counts'], {'unchanged':2})
