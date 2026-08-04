# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

from odoo import models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    def _selection_zabbix_auth_method(self):
        """Add Keykeep Managed option when keykeep module is present."""
        selection = super()._selection_zabbix_auth_method()
        if 'keykeep.credential' in self.env:
            selection.append(('keykeep', 'Keykeep Managed'))
        return selection
