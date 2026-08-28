# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""Extend salt.minion with keykeep_encryption_key deployment."""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class KeykeepCredentialMinion(models.Model):
    """Link keykeep.credential to the salt.minion it belongs to."""
    _inherit = 'keykeep.credential'

    minion_id = fields.Many2one(
        'salt.minion',
        string='Minion',
        ondelete='set null',
        help='Salt minion this credential belongs to (saltstack_keykeep).',
    )


class SaltMinionKeykeep(models.Model):
    _inherit = 'salt.minion'

    keykeep_key_count = fields.Integer(
        string='Keykeep-nycklar',
        compute='_compute_keykeep_key_count',
        help='Number of Keykeep credentials linked to this minion '
             '(saltstack_keykeep).',
    )

    @api.depends('name')
    def _compute_keykeep_key_count(self):
        for rec in self:
            rec.keykeep_key_count = rec._keykeep_minion_credential_count()

    def _keykeep_minion_credential_count(self):
        """Number of keykeep.credential records linked to this minion."""
        self.ensure_one()
        return self.env['keykeep.credential'].search_count(
            [('minion_id', '=', self.id)])

    def action_view_keykeep_keys(self):
        """Smart button: open the Keykeep credentials for this minion."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Keykeep-nycklar — %s' % self.name,
            'res_model': 'keykeep.credential',
            'view_mode': 'list,form',
            'domain': [('minion_id', '=', self.id)],
        }

    def action_fetch_keykeep_master_key(self):
        """Read keykeep_encryption_key from the minion's odoo.conf and vault
        it as a keykeep.credential linked to this minion.

        The master key is generated on the minion by the salt state 'keykeep'
        (Deploy keykeep key) and lives in odoo.conf. This action fetches it
        into Keykeep (encrypted at rest with the ledningssystem master key,
        audit-logged) so it is one of the minion's Keykeep keys.
        """
        self.ensure_one()
        if not self.env.user.has_group('saltstack.group_saltstack_admin'):
            raise UserError(_('Only Saltstack administrators can fetch the '
                              'Keykeep master key.'))
        try:
            result = self._call_salt_api(
                'local', self.name, 'cmd.run',
                "grep -E '^keykeep_encryption_key' /etc/odoo/odoo.conf",
                timeout=60,
            )
        except Exception as e:
            raise UserError(_('Failed to read odoo.conf on %s: %s') % (self.name, e))
        raw = result.get('return', [{}])[0].get(self.name, '')
        key = None
        for line in str(raw).splitlines():
            line = line.strip()
            if line.startswith('keykeep_encryption_key'):
                key = line.split('=', 1)[1].strip()
                break
        if not key:
            raise UserError(
                _('No keykeep_encryption_key found in /etc/odoo/odoo.conf on '
                  '%s. Run "Deploy keykeep key" first.') % self.name)

        sub = self.env['keykeep.subscription'].sudo().search([
            ('name', '=', 'Salt Pillar: %s' % self.name),
        ], limit=1)
        if not sub:
            sub = self.env['keykeep.subscription'].sudo().create({
                'name': 'Salt Pillar: %s' % self.name,
                'notes': _('Auto-created from saltstack_keykeep for minion %s')
                          % self.name,
            })
        cred = self.env['keykeep.credential'].sudo().search([
            ('minion_id', '=', self.id),
            ('purpose', '=', 'pillar:keykeep.keykeep_encryption_key'),
        ], limit=1)
        vals = {
            'name': 'Keykeep masternyckel',
            'subscription_id': sub.id,
            'credential_type': 'api_key',
            'environment': 'production',
            'purpose': 'pillar:keykeep.keykeep_encryption_key',
            'minion_id': self.id,
            'notes': _('Keykeep master key (Fernet) from odoo.conf on %s. '
                       'Synced via "Hämta Keykeep masternyckel".')
                      % self.name,
        }
        if cred:
            cred.write({**vals, 'key_value': key})
            action = cred
        else:
            action = self.env['keykeep.credential'].sudo().create({
                **vals, 'key_value': key})
        return {
            'type': 'ir.actions.act_window',
            'name': 'Keykeep masternyckel — %s' % self.name,
            'res_model': 'keykeep.credential',
            'res_id': action.id,
            'view_mode': 'form',
        }

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
