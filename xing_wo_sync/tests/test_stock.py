from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestWorldOfficeStock(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_bool('database.is_neutralized', True)
        self.service = self.env['xing.wo.binding']
        self.source = 'wo_stock_integration'
        self.company = self.env.company
        self.product = self.env['product.product'].create({
            'name':'WO stock test', 'default_code':'WO-STOCK-TEST', 'type':'consu',
            'is_storable':True, 'company_id':self.company.id,
            'uom_id':self.env.ref('uom.product_uom_unit').id})
        self.service.create({'source_key':self.source,'company_id':self.company.id,
                             'kind':'product','source_id':'5814',
                             'target_ref':f'product.product,{self.product.id}'})
        self.row = dict(product_source_id='5814',product_code='WO-STOCK-TEST',warehouse_source_id='Uno',
                        warehouse_name='Principal',source_uom='Und.',quantity='-2')

    def snapshot(self, rows=None, dry=False):
        return self.service.stock_snapshot(self.source,self.company.id,rows or [self.row],dry)

    def test_preview_does_not_create_locations_ledger_or_stock(self):
        warehouses=self.env['stock.warehouse'].search_count([])
        result=self.snapshot(dry=True)
        self.assertEqual(result['counts'],{'would_adjust':1})
        self.assertEqual(self.env['stock.warehouse'].search_count([]),warehouses)
        self.assertFalse(self.env['xing.wo.stock.balance'].search([('source_key','=',self.source)]))

    def test_negative_snapshot_and_replay_without_duplicate_adjustment(self):
        result=self.snapshot()
        self.assertEqual(result['counts'],{'adjusted':1})
        ledger=self.env['xing.wo.stock.balance'].search([('source_key','=',self.source)])
        self.assertEqual(ledger.quantity,-2)
        moves=self.env['stock.move'].search_count([('product_id','=',self.product.id)])
        self.assertEqual(self.snapshot()['counts'],{'unchanged':1})
        self.assertEqual(self.env['stock.move'].search_count([('product_id','=',self.product.id)]),moves)
        qty=sum(self.env['stock.quant'].search([('product_id','=',self.product.id),('location_id','=',ledger.location_id.id)]).mapped('quantity'))
        self.assertEqual(qty,-2)

    def test_quarantine_and_transit_outside_sale_stock(self):
        self.snapshot()
        warehouse,locations=self.service._wo_stock_locations(self.source)
        self.assertEqual(locations['Tres'].usage,'transit')
        self.assertEqual(locations['Dos'].usage,'internal')
        self.assertEqual(locations['Uno'],warehouse.lot_stock_id)
        for key in ('Dos','Tres'):
            self.assertFalse(locations[key].parent_path.startswith(warehouse.view_location_id.parent_path))

    def test_invalid_product_in_same_snapshot_rolls_back_all(self):
        from odoo.exceptions import UserError
        with self.assertRaises(UserError),self.env.cr.savepoint():
            self.snapshot([self.row,dict(self.row,product_source_id='999')])
        self.assertFalse(self.env['xing.wo.stock.balance'].search([('source_key','=',self.source)]))

    def test_production_is_blocked_server_side(self):
        from odoo.exceptions import UserError
        self.env['ir.config_parameter'].sudo().set_bool('database.is_neutralized', False)
        with self.assertRaises(UserError):self.snapshot()
