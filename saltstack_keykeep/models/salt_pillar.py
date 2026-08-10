# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""Extend salt.pillar with Keykeep sync."""

import json
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

    def _pillar_originals(self, timeout=90):
        """Fetch UNMASKED pillar values from all minions (bulk pillar.raw).

        pillar.items masks secrets — pillar.raw does not.
        Returns a flat {key_path: value} dict and prefers the SaltStack
        master's (global) value when the same key exists on several minions.
        """
        api = self.env['saltstack.ai.config']
        result = api.salt_call('local', '*', 'pillar.raw', timeout=timeout)
        data = json.loads(result)
        returned = (data.get('return') or [{}])[0]
        if not isinstance(returned, dict):
            return {}

        def _flatten(prefix, d):
            for key, value in sorted(d.items()):
                full = f'{prefix}.{key}' if prefix else key
                if isinstance(value, dict):
                    yield full, value
                    yield from _flatten(full, value)
                elif isinstance(value, list):
                    yield full, value
                else:
                    yield full, value

        originals = {}
        for minion_name, pillar_data in sorted(returned.items()):
            if not isinstance(pillar_data, dict):
                continue
            for key, value in _flatten('', pillar_data):
                if key == '_errors':
                    continue
                if key not in originals or minion_name == 'SaltStack':
                    originals[key] = value
        return originals

    def action_sync_to_keykeep(self):
        """Sync pillar secrets to keykeep credentials.

        Fetches ORIGINAL values via the Salt API pillar.raw (not masked,
        unlike pillar.items) and stores them encrypted in keykeep.credential.
        For each secret record:
        - finds/creates a keykeep.subscription based on the pillar namespace
        - creates/updates a keykeep.credential with the encrypted original value
        - links the pillar record to the subscription

        Runs with sudo() — the button should work even for users who only
        have read access on salt.pillar (internal users are read-only).
        """
        self = self.sudo()
        records = self.filtered(lambda r: r.data_type == 'secret')
        if not records:
            return self._keykeep_notify(0, _('No secrets to sync'))

        try:
            originals = self._pillar_originals()
        except Exception as e:
            raise UserError(_('Failed to fetch pillar.raw: %s') % str(e))

        synced = 0
        skipped = 0
        for rec in records:
            original = originals.get(rec.key)
            if original is None:
                skipped += 1
                continue
            sub = rec._get_or_create_keykeep_subscription()
            cred = rec._get_or_create_keykeep_credential(sub, original)
            # Rotate/update if the value changed (write encrypts key_value)
            try:
                if cred.key_value != str(original):
                    cred.write({'key_value': str(original)})
            except Exception:
                _logger.exception(
                    'Could not update keykeep.credential for %s', rec.key)
                continue
            rec.keykeep_subscription_id = sub.id
            synced += 1

        message = _('%d hemligheter synkade till Keykeep') % synced
        if skipped:
            message += _('\n%d without original value (missing in pillar.raw)') % skipped
        return self._keykeep_notify(synced, message)

    def _keykeep_notify(self, count, message=None):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Keykeep Sync'),
                'message': message or _('%d secrets synced to Keykeep') % count,
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
                'notes': _(
                    'Auto-synced from Salt pillar namespace: %s'
                ) % namespace,
            })
        return sub

    def _get_or_create_keykeep_credential(self, subscription, original_value):
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
                'key_value': str(original_value),
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
