# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ── Salt API ─────────────────────────────────────────────────────

    saltstack_api_url = fields.Char(
        string='Salt API URL',
        config_parameter='saltstack.api_url',
        default='http://localhost:8377',
        help='Base URL of the Salt REST API (e.g. http://192.168.11.22:8377)',
    )
    saltstack_api_token = fields.Char(
        string='Salt API Token',
        config_parameter='saltstack.api_token',
        help='Authentication token for the Salt API',
    )
    saltstack_auth_method = fields.Selection(
        selection='_selection_saltstack_auth_method',
        string='Salt Auth Method',
        config_parameter='saltstack.auth_method',
        default='token',
        help='How to authenticate with the Salt API. '
             '"Shared Secret" uses the API key via /login. '
             '"Keykeep Managed" is available when the saltstack_keykeep '
             'module is installed.',
    )

    def _selection_saltstack_auth_method(self):
        """Return auth method options. Keykeep only when module installed."""
        selection = [
            ('token', 'API Token'),
            ('sharedsecret', 'Shared Secret (API key)'),
        ]
        if 'keykeep.credential' in self.env:
            selection.append(('keykeep', 'Keykeep Managed'))
        return selection

    # ── Sync scheduling ────────────────────────────────────────────────

    sync_interval_number = fields.Integer(
        string='Sync interval',
        config_parameter='saltstack.sync_interval_number',
        default=24,
        help='How often the minion and pillar sync runs (in hours).',
    )

    # ── Driftlarm webhook (shared) ─────────────────────────────────────

    alert_webhook_url = fields.Char(
        string='Webhook URL',
        config_parameter='saltstack.alert.webhook_url',
        help='URL that Wazuh/Zabbix call to send drift alerts '
             '(e.g. http://luke18:8069/saltstack/alert).',
    )
    alert_webhook_token = fields.Char(
        string='API key',
        config_parameter='saltstack.alert.webhook_token',
        help='API key (Bearer token) that Wazuh/Zabbix send in the '
             'Authorization header to the webhook URL.',
    )
    alert_webhook_enabled = fields.Boolean(
        string='Webhook enabled',
        config_parameter='saltstack.alert.webhook_enabled',
        default=True,
        help='Enable/disable reception of drift alerts.',
    )
    alert_correlation_window = fields.Integer(
        string='Correlation window (s)',
        config_parameter='saltstack.alert.correlation_window',
        default=120,
        help='Time window in seconds for Zabbix correlation (±).',
    )
    alert_auto_diagnose = fields.Boolean(
        string='Auto diagnosis',
        config_parameter='saltstack.alert.auto_diagnose',
        default=True,
        help='Start AI diagnosis automatically when an alert arrives.',
    )
    alert_coworker_id = fields.Many2one(
        'ai.coworker',
        string='AI Coworker',
        config_parameter='saltstack.alert.coworker_id',
        help='AI coworker that performs diagnosis on drift alerts. '
             'Default is the one bundled with the module (Infrastructure Operator).',
    )

    def get_values(self):
        res = super().get_values()
        self._ensure_webhook_config()
        coworker_id = self.env['ir.config_parameter'].get_param(
            'saltstack.alert.coworker_id', False)
        if coworker_id and 'ai.coworker' in self.env:
            res['alert_coworker_id'] = int(coworker_id)
        return res

    def set_values(self):
        super().set_values()
        if self.alert_coworker_id:
            self.env['ir.config_parameter'].set_param(
                'saltstack.alert.coworker_id', self.alert_coworker_id.id)

    def get_values(self):
        res = super().get_values()
        res['sync_interval_number'] = int(self.env['ir.config_parameter'].get_param(
            'saltstack.sync_interval_number', 24))
        return res

    def set_values(self):
        super().set_values()
        self.env['ir.config_parameter'].set_param(
            'saltstack.sync_interval_number', self.sync_interval_number or 24)
        self._update_sync_cron()

    def _update_sync_cron(self):
        """Create/update the sync cron with configured interval."""
        cron = self.env['ir.cron'].search([
            ('cron_name', '=', 'Saltstack: synk minions & pillars'),
        ], limit=1)
        if not cron:
            model = self.env['ir.model']._get('salt.minion')
            cron = self.env['ir.cron'].create({
                'cron_name': 'Saltstack: synk minions & pillars',
                'model_id': model.id,
                'state': 'code',
                'code': 'model.action_cron_sync_all()',
                'interval_number': self.sync_interval_number or 24,
                'interval_type': 'hours',
                'active': True,
                'user_id': self.env.ref('base.user_root').id,
            })
        else:
            cron.interval_number = self.sync_interval_number or 24

    # ── Test ───────────────────────────────────────────────────────────

    def action_generate_webhook_token(self):
        """Generate a new random API key for the Driftlarm webhook."""
        import secrets
        token = secrets.token_urlsafe(32)
        self.env['ir.config_parameter'].set_param(
            'saltstack.alert.webhook_token', token)
        self.alert_webhook_token = token
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Ny API-nyckel genererad',
                'message': 'Use the new key as the Bearer token for '
                           'webhook calls.',
                'type': 'success',
                'sticky': False,
            },
        }

    def action_fill_webhook_url(self):
        """Fill the webhook URL from web.base.url."""
        base = self.env['ir.config_parameter'].get_param(
            'web.base.url', 'http://localhost:8069')
        url = '%s/saltstack/alert' % base.rstrip('/')
        self.env['ir.config_parameter'].set_param(
            'saltstack.alert.webhook_url', url)
        self.alert_webhook_url = url
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Webhook-URL uppdaterad',
                'message': url,
                'type': 'success',
                'sticky': False,
            },
        }

    def _ensure_webhook_config(self):
        """Ensure webhook URL + API key exist (idempotent).

        The post_init_hook only runs at first install — on upgrade of an
        existing system these values would be missing. Called from
        get_values() so they are created the first time Settings opens.
        """
        import secrets
        params = self.env['ir.config_parameter']
        if not params.get_param('saltstack.alert.webhook_token'):
            params.set_param('saltstack.alert.webhook_token',
                             secrets.token_urlsafe(32))
        if not params.get_param('saltstack.alert.webhook_url'):
            base = params.get_param('web.base.url', 'http://localhost:8069')
            params.set_param(
                'saltstack.alert.webhook_url',
                '%s/saltstack/alert' % base.rstrip('/'))

    def action_test_salt(self):
        """Test Salt API connection. Returns a popup notification."""
        import json
        params = self.env['ir.config_parameter']
        url = params.get_param('saltstack.api_url', '')

        if not url:
            return self._salt_test_result(
                False, 'Salt API URL is not configured.\n'
                        'Set the URL in Settings → Saltstack → Salt Master.')

        try:
            # Reuse the fixed client (sharedsecret login + correct endpoint).
            # Pings the SaltStack master (always online) instead of a hardcoded minion.
            result = json.loads(self.env['saltstack.ai.config'].salt_call(
                'local', 'SaltStack', 'test.ping', timeout=10))

            returned = result.get('return', [{}])
            if returned and isinstance(returned[0], dict):
                minions = returned[0]
                all_ok = all(v is True for v in minions.values())
                names = ', '.join(minions.keys())
                if all_ok:
                    return self._salt_test_result(
                        True,
                        f'Salt API reachable. Ping OK against: {names}')
                return self._salt_test_result(
                    True,
                    f'Salt API reachable. Response: {str(minions)[:200]}')
            return self._salt_test_result(
                True, 'Salt API reachable.\nResponse: %s' % str(result)[:200])

        except Exception as e:
            return self._salt_test_result(
                False,
                f'Could not reach Salt API at {url}.\n'
                f'Error: {str(e)}\n\nTroubleshooting:\n'
                f'- Check that the URL is correct (e.g. http://192.168.11.22:8377)\n'
                f'- Check that the Salt API is running (salt-api.service)\n'
                f'- Check the token (Salt API external_auth)\n'
                f'- Check network access (port 8377)')

    def _salt_test_result(self, ok, message):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Salt-test: ' + ('OK' if ok else 'MISSLYCKADES'),
                'message': message,
                'type': 'success' if ok else 'danger',
                'sticky': True,
            },
        }
