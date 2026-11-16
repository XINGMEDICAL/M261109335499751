from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestWoSerialPilot(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service=self.env['xing.wo.binding']
        self.env['ir.config_parameter'].sudo().set_bool('database.is_neutralized',True)
        self.source='wo_serial_test'
        self.product=self.env['product.product'].create({
            'name':'Serial test','default_code':'HTC - 2','type':'consu','is_storable':True,
            'company_id':self.env.company.id,'uom_id':self.env.ref('uom.product_uom_unit').id})
        self.service.create({'source_key':self.source,'company_id':self.env.company.id,
                             'kind':'product','source_id':'7749','target_ref':f'product.product,{self.product.id}'})
        self.products=[{'product_source_id':'7749','product_code':'HTC - 2',
                        'balances':[{'warehouse_source_id':'Uno','warehouse_name':'Principal','quantity':'2'}],
                        'serials':[{'serial':'0001','warehouse_source_id':'Uno'},
                                   {'serial':'0002','warehouse_source_id':'Uno'}]}]
    def snapshot(self,dry_run):
        return self.service.serial_initial_snapshot(self.source,self.env.company.id,self.products,dry_run)
    def test_preview_does_not_change_tracking_or_create_lots(self):
        before=self.env['stock.lot'].search_count([])
        result=self.snapshot(True)
        self.assertEqual(result['serial_count'],2)
        self.assertFalse(self.product.tracking)
        self.assertEqual(self.env['stock.lot'].search_count([]),before)
    def test_initial_apply_and_exact_replay(self):
        self.snapshot(False)
        self.assertEqual(self.product.tracking,'serial')
        self.assertEqual(len(self.env['stock.lot'].search([('product_id','=',self.product.id)])),2)
        moves=self.env['stock.move'].search_count([('product_id','=',self.product.id)])
        self.assertEqual(self.snapshot(False)['results'][0]['status'],'unchanged')
        self.assertEqual(self.env['stock.move'].search_count([('product_id','=',self.product.id)]),moves)
    def test_existing_quantity_not_converted_blindly_to_serials(self):
        self.service.stock_snapshot(self.source,self.env.company.id,[{
            'product_source_id':'7749','product_code':'HTC - 2','source_uom':'Und.',
            'warehouse_source_id':'Uno','warehouse_name':'Principal','quantity':'2'}],False)
        with self.assertRaisesRegex(UserError,'empty_stock'),self.env.cr.savepoint():self.snapshot(False)
    def test_production_cannot_apply(self):
        self.env['ir.config_parameter'].sudo().set_bool('database.is_neutralized',False)
        with self.assertRaisesRegex(UserError,'neutralizada'):self.snapshot(False)
    def test_changed_snapshot_does_not_remove_an_existing_serial(self):
        self.snapshot(False)
        self.products[0]['serials'][0]['serial']='new'
        with self.assertRaisesRegex(UserError,'changed_snapshot'),self.env.cr.savepoint():self.snapshot(False)
