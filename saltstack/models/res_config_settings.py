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
             '"Keykeep Managed" is available when the saltstack_keykeep '
             'module is installed.',
    )

    def _selection_saltstack_auth_method(self):
        """Return auth method options. Keykeep only when module installed."""
        selection = [('token', 'API Token')]
        if 'keykeep.credential' in self.env:
            selection.append(('keykeep', 'Keykeep Managed'))
        return selection

    # ── Sync scheduling ────────────────────────────────────────────────

    sync_interval_number = fields.Integer(
        string='Synk-intervall',
        config_parameter='saltstack.sync_interval_number',
        default=24,
        help='Hur ofta minion- och pillar-synkningen körs (i timmar).',
    )

    # ── Driftlarm webhook (gemensam) ──────────────────────────────────

    alert_webhook_url = fields.Char(
        string='Webhook URL',
        config_parameter='saltstack.alert.webhook_url',
        help='URL som Wazuh/Zabbix anropar för att skicka driftlarm '
             '(t.ex. http://luke18:8069/saltstack/webhook).',
    )
    alert_webhook_token = fields.Char(
        string='API-nyckel',
        config_parameter='saltstack.alert.webhook_token',
        help='API-nyckel (Bearer token) som Wazuh/Zabbix skickar i '
             'Authorization-headern till webhook-URL:en.',
    )
    alert_webhook_enabled = fields.Boolean(
        string='Webhook aktiverad',
        config_parameter='saltstack.alert.webhook_enabled',
        default=True,
        help='Aktivera/avaktivera mottagning av driftlarm.',
    )
    alert_correlation_window = fields.Integer(
        string='Korrelationsfönster (s)',
        config_parameter='saltstack.alert.correlation_window',
        default=120,
        help='Tidsfönster i sekunder för Zabbix-korrelation (±).',
    )
    alert_auto_diagnose = fields.Boolean(
        string='Auto-diagnos',
        config_parameter='saltstack.alert.auto_diagnose',
        default=True,
        help='Starta AI-diagnos automatiskt vid larm.',
    )
    alert_coworker_id = fields.Many2one(
        'ai.coworker',
        string='AI Medarbetare',
        config_parameter='saltstack.alert.coworker_id',
        help='AI Medarbetare som utför diagnos vid driftlarm. '
             'Default är den som följer med modulen (Infrastructure Operator).',
    )

    def get_values(self):
        res = super().get_values()
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

    def action_test_salt(self):
        """Test Salt API connection. Returns a popup notification."""
        params = self.env['ir.config_parameter']
        url = params.get_param('saltstack.api_url', '')
        token = params.get_param('saltstack.api_token', '')

        if not url:
            return self._salt_test_result(
                False, 'Salt API URL är inte konfigurerad.\n'
                        'Sätt URL:en i Inställningar → Saltstack → Salt Master.')

        try:
            import json
            import ssl
            import urllib.request

            payload = {
                'client': 'local',
                'tgt': 'luke18',  # pinga en snabb lokal minion, inte alla
                'fun': 'test.ping',
                'timeout': 10,
            }
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                f'{url.rstrip("/")}/',
                data=data,
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'X-Auth-Token': token,
                },
            )
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
                result = json.loads(resp.read().decode())

            returned = result.get('return', [{}])
            if returned and isinstance(returned[0], dict):
                minions = returned[0]
                all_ok = all(v is True for v in minions.values())
                names = ', '.join(minions.keys())
                if all_ok:
                    return self._salt_test_result(
                        True,
                        f'Salt API nåbar. Ping OK mot: {names}')
                return self._salt_test_result(
                    True,
                    f'Salt API nåbar. Svar: {str(minions)[:200]}')
            return self._salt_test_result(
                True, 'Salt API nåbar.\nSvar: %s' % str(result)[:200])

        except Exception as e:
            return self._salt_test_result(
                False,
                f'Kunde inte nå Salt API på {url}.\n'
                f'Fel: {str(e)}\n\nFelsökning:\n'
                f'- Kontrollera att URL:en är rätt (t.ex. http://192.168.11.22:8377)\n'
                f'- Kontrollera att Salt API körs (salt-api.service)\n'
                f'- Kontrollera token (Salt API external_auth)\n'
                f'- Kontrollera nätverksåtkomst (port 8377)')

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
