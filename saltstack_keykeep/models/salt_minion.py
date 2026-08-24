# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""Extend salt.minion with keykeep_encryption_key deployment."""

import logging

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SaltMinionKeykeep(models.Model):
    _inherit = 'salt.minion'

    def action_deploy_keykeep_encryption_key(self):
        """Generate + deploy keykeep_encryption_key via the salt state 'keykeep'.

        Triggers `state.apply keykeep` on this minion through the Salt REST
        API (saltstack.api). The state generates the Fernet key idempotently
        (only if missing), writes it to odoo.conf and restarts Odoo.
        """
        self.ensure_one()
        api = self.env['saltstack.api']
        try:
            result = api.salt_call(
                'local', self.name, 'state.apply', 'keykeep', timeout=180)
        except Exception as e:
            raise UserError(_('Failed to run salt state "keykeep": %s') % str(e))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Keykeep Key Deploy'),
                'message': result,
                'type': 'success',
            },
        }
