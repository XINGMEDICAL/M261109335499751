from odoo import fields, models


class Equipment(models.Model):
    _inherit = 'maintenance.equipment'

    wo_asset_code = fields.Char(string='Codigo activo World Office', index=True, copy=False)
