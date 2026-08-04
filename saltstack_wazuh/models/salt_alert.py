# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

from odoo import fields, models


class SaltAlert(models.Model):
    _inherit = 'saltstack.alert'

    source = fields.Selection(
        selection_add=[('wazuh', 'Wazuh')],
    )
