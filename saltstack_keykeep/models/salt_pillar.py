# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""Extend salt.pillar with Keykeep sync."""

import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SaltPillar(models.Model):
    _inherit = 'salt.pillar'

    keykeep_subscription_id = fields.Many2one(
        'keykeep.subscription',
        string='Keykeep Subscription',
        help='Linked keykeep subscription for synced secrets',
    )

    def action_sync_to_keykeep(self):
        """Sync pillar secrets to keykeep credentials.

        For each pillar record with data_type='secret':
        - Creates or finds a keykeep.subscription based on pillar namespace
        - Creates or updates a keykeep.credential with encrypted value
        - Links the pillar record to the subscription
        """
        synced = 0
        for rec in self:
            if rec.data_type != 'secret' or not rec.value:
                continue

            sub = rec._get_or_create_keykeep_subscription()
            cred = rec._get_or_create_keykeep_credential(sub)

            # Rotate: create new version if value changed
            if cred.key_value != rec.value:
                try:
                    cred._rotate(rec.value)
                except AttributeError:
                    # _rotate not available — update directly
                    cred.write({'key_value': rec.value})

            rec.keykeep_subscription_id = sub.id
            synced += 1

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Keykeep Sync'),
                'message': _('%d secrets synced to Keykeep') % synced,
                'type': 'success',
            },
        }

    def _get_or_create_keykeep_subscription(self):
        """Find or create a keykeep.subscription for this pillar's namespace."""
        namespace = 'default'
        if self.pillar_file:
            namespace = self.pillar_file.split('/')[0].replace('.sls', '')

        sub = self.env['keykeep.subscription'].search([
            ('name', '=', f'Salt Pillar: {namespace}'),
        ], limit=1)

        if not sub:
            sub = self.env['keykeep.subscription'].create({
                'name': f'Salt Pillar: {namespace}',
                'description': _(
                    'Auto-synced from Salt pillar namespace: %s'
                ) % namespace,
            })
        return sub

    def _get_or_create_keykeep_credential(self, subscription):
        """Find or create a keykeep.credential for this pillar key."""
        purpose = f'pillar:{self.key}'
        cred = self.env['keykeep.credential'].search([
            ('subscription_id', '=', subscription.id),
            ('purpose', '=', purpose),
        ], limit=1)

        if not cred:
            cred = self.env['keykeep.credential'].create({
                'name': self.key,
                'subscription_id': subscription.id,
                'credential_type': 'api_key',
                'environment': 'production',
                'purpose': purpose,
                'key_value': self.value,
                'notes': _(
                    'Auto-synced from Salt pillar: %s\nFile: %s\nMinion: %s'
                ) % (self.key, self.pillar_file or 'N/A',
                     self.minion_target or 'N/A'),
            })
        return cred

    def action_sync_bifrost_to_keykeep(self):
        """Sync Bifrost provider secrets to Keykeep.

        Reads Bifrost pillar from Salt API and creates keykeep.credential
        records for each LLM provider.
        """
        if not self.env['ir.config_parameter'].get_param('saltstack.api_url'):
            raise UserError(_('Salt API URL not configured'))

        try:
            api = self.env['saltstack.ai.config']
            import json
            result = api.salt_call('local', 'bifrost', 'pillar.get', 'bifrost')
            data = json.loads(result)
            bifrost = data.get('return', [{}])[0].get('bifrost', {})
        except Exception as e:
            raise UserError(_('Failed to fetch Bifrost pillar: %s') % str(e))

        sub = self.env['keykeep.subscription'].search([
            ('name', '=', 'Bifrost AI Gateway'),
        ], limit=1)
        if not sub:
            sub = self.env['keykeep.subscription'].create({
                'name': 'Bifrost AI Gateway',
                'description': 'LLM provider API keys for Bifrost gateway',
            })

        providers = bifrost.get('providers', {})
        created = 0
        for provider_name, provider_cfg in providers.items():
            for key_cfg in provider_cfg.get('keys', []):
                env_var = key_cfg.get('env', '')
                purpose = f'bifrost:{provider_name}'

                cred = self.env['keykeep.credential'].search([
                    ('subscription_id', '=', sub.id),
                    ('purpose', '=', purpose),
                ], limit=1)

                if not cred:
                    self.env['keykeep.credential'].create({
                        'name': f'Bifrost — {provider_name}',
                        'subscription_id': sub.id,
                        'credential_type': 'api_key',
                        'environment': 'production',
                        'purpose': purpose,
                        'username': env_var,
                        'notes': _(
                            'Bifrost provider: %s\nEnvironment variable: %s'
                        ) % (provider_name, env_var),
                    })
                    created += 1

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Bifrost Sync'),
                'message': _('%d providers synced to Keykeep') % created,
                'type': 'success',
            },
        }
