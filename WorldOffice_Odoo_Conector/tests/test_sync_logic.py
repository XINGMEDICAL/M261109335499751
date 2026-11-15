import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'xing_wo_sync' / 'sync_logic.py'
spec = importlib.util.spec_from_file_location('logic', path)
logic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logic)


def product(**overrides):
    data = dict(source_id='1', code='P01', name='Producto', classification='Producto',
                category='PRODUCTOS NO FABRICADOS POR LA EMPRESA')
    return dict(data, **overrides)


def asset(**overrides):
    return dict(dict(source_id='1', code='AF01', name='Equipo', classification='Activo Fijo', category='ACTIVOS FIJOS'), **overrides)


class ValidationTests(unittest.TestCase):
    def test_product_scope(self):
        self.assertEqual(logic.normalise('product', product())['code'], 'P01')

    def test_service_scope(self):
        data = product(classification='Servicio', category='SERVICIOS')
        self.assertEqual(logic.normalise('product', data)['classification'], 'Servicio')

    def test_rejects_unagreed_category(self):
        with self.assertRaisesRegex(logic.SyncConflict, 'outside_agreed_scope'):
            logic.normalise('product', product(category='ACTIVOS FIJOS'))

    def test_preserves_document_zeroes(self):
        row = dict(source_id=5, name='Nombre', identification='001234')
        self.assertEqual(logic.normalise('employee', row)['identification'], '001234')

    def test_rejects_empty_employee_identity(self):
        with self.assertRaises(logic.SyncConflict):
            logic.normalise('employee', dict(source_id=1, name='Nombre', identification=' '))

    def test_rejects_empty_name(self):
        with self.assertRaises(logic.SyncConflict):
            logic.normalise('product', product(name=' '))

    def test_rejects_boolean_id(self):
        with self.assertRaises(logic.SyncConflict):
            logic.normalise('product', product(source_id=True))

    def test_rejects_null_and_invalid_source(self):
        for value in (None, '', 'nombre con espacios', 'a/b', 'a\x00b'):
            with self.subTest(value=value), self.assertRaises(logic.SyncConflict):
                logic.validate_source(value)

    def test_rejects_duplicate_id(self):
        with self.assertRaisesRegex(logic.SyncConflict, 'duplicate_source_source_id'):
            logic.validate_rows('product', [product(), product(code='P02')])

    def test_rejects_duplicate_code(self):
        with self.assertRaisesRegex(logic.SyncConflict, 'duplicate_source_code'):
            logic.validate_rows('product', [product(), product(source_id='2')])

    def test_rejects_duplicate_document(self):
        rows = [dict(source_id=i, name='Nombre', identification='001') for i in (1, 2)]
        with self.assertRaisesRegex(logic.SyncConflict, 'duplicate_source_identification'):
            logic.validate_rows('employee', rows)

    def test_asset_serial_optional(self):
        result = logic.normalise('asset', asset())
        self.assertEqual(result['serial'], '')

    def test_duplicate_serial_blocks_both_assets(self):
        rows = [asset(source_id=i, code=f'AF{i}', serial='S01') for i in (1, 2)]
        with self.assertRaisesRegex(logic.SyncConflict, 'duplicate_source_serial'):
            logic.validate_rows('asset', rows)

    def test_missing_serials_not_duplicate(self):
        rows = [asset(source_id=i, code=f'AF{i}') for i in (1, 2)]
        self.assertEqual(len(logic.validate_rows('asset', rows)), 2)

    def test_diff_preserves_unmanaged_fields(self):
        self.assertEqual(logic.diff_values({'name':'A','description_sale':'catalogo'}, {'name':'B'}), {'name':'B'})

    def test_repeat_same_values_is_noop(self):
        row = {'name':'A','wo_identification':'001'}
        self.assertEqual(logic.diff_values(dict(row), dict(row)), {})

    def test_no_salary_or_email_in_employee_payload(self):
        data = dict(source_id=1, name='Nombre', identification='001', wage=500, email='private@example.com')
        self.assertEqual(set(logic.normalise('employee', data)), {'source_id','name','identification'})

    def test_no_price_or_inventory_in_product_payload(self):
        self.assertNotIn('list_price', logic.normalise('product', product(list_price=500)))

    def test_rejects_missing_asset_code(self):
        with self.assertRaises(logic.SyncConflict):
            logic.normalise('asset', asset(code=''))

    def test_asset_classified_as_product_is_included(self):
        result = logic.normalise('asset', asset(classification='Producto'))
        self.assertEqual(result['classification'], 'Producto')

    def test_nonasset_group_not_equipment(self):
        with self.assertRaisesRegex(logic.SyncConflict, 'asset_outside_agreed_scope'):
            logic.normalise('asset', asset(category='SERVICIOS'))

    def test_other_classification_in_asset_group_blocked(self):
        with self.assertRaisesRegex(logic.SyncConflict, 'asset_outside_agreed_scope'):
            logic.normalise('asset', asset(classification='Servicio'))

    def test_same_name_different_asset_codes_valid(self):
        self.assertEqual(len(logic.validate_rows('asset', [asset(), asset(source_id='2', code='AF02')])), 2)

    def test_rejects_unknown_kind(self):
        with self.assertRaises(logic.SyncConflict):
            logic.normalise('payroll', dict(source_id=1, name='Nomina'))


if __name__ == '__main__':
    unittest.main()
