from odoo import fields, models


class Employee(models.Model):
    _inherit = 'hr.employee'

    wo_identification = fields.Char(
        string='Identificacion World Office', index=True, copy=False,
        groups='hr.group_hr_user',
        help='Documento de origen. No sustituye las versiones ni contratos laborales de Odoo.',
    )
