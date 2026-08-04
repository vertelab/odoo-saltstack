# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ── SIEM / Wazuh ──────────────────────────────────────────────────

    siem_api_url = fields.Char(
        string='Wazuh API URL',
        config_parameter='siem.api_url',
        default='https://siem.vertel.se',
        help='Base URL of the Wazuh API (e.g. https://siem.vertel.se)',
    )
    siem_api_token = fields.Char(
        string='Wazuh API Token',
        config_parameter='siem.api_token',
        help='Authentication token for the Wazuh API',
    )
    siem_manager_minion = fields.Char(
        string='Wazuh Manager Minion',
        config_parameter='siem.manager_minion',
        default='siem.vertel.se',
        help='Salt minion-ID for the Wazuh Manager server '
             '(used by SIEM tools to run agent_control etc.)',
    )
    siem_auth_method = fields.Selection(
        selection='_selection_siem_auth_method',
        string='Wazuh Auth Method',
        config_parameter='siem.auth_method',
        default='token',
        help='How to authenticate with the Wazuh API. '
             '"Keykeep Managed" is available when the saltstack_wazuh_keykeep '
             'module is installed.',
    )

    def _selection_siem_auth_method(self):
        """Base selection — keykeep added by saltstack_wazuh_keykeep module."""
        return [('token', 'API Token')]

    def action_test_wazuh(self):
        """Test Wazuh API connection. Returns a popup notification."""
        params = self.env['ir.config_parameter']
        url = params.get_param('siem.api_url', '')
        token = params.get_param('siem.api_token', '')

        if not url:
            return self._wazuh_test_result(
                False, 'Wazuh API URL är inte konfigurerad.\n'
                        'Sätt URL:en i Inställningar → Saltstack → Wazuh.')

        try:
            import json
            import ssl
            import urllib.request

            payload = {
                'jsonrpc': '2.0',
                'method': 'auth',
                'params': {'auth_context': token},
                'id': 1,
            }
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                f'{url}/api/v1/auth',
                data=data,
                headers={'Content-Type': 'application/json'},
            )
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                result = json.loads(resp.read().decode())

            if 'data' in result and result.get('data', {}).get('token'):
                return self._wazuh_test_result(
                    True, 'Wazuh API autentisering lyckades.\n'
                          'Token erhölls — agenthantering fungerar.')
            return self._wazuh_test_result(
                False, 'Wazuh API svarade men token erhölls inte.\n'
                        f'Svar: {str(result)[:200]}')

        except Exception as e:
            return self._wazuh_test_result(
                False,
                f'Kunde inte nå Wazuh API på {url}.\n'
                f'Fel: {str(e)}\n\nFelsökning:\n'
                f'- Kontrollera att URL:en är rätt (t.ex. https://siem.vertel.se)\n'
                f'- Kontrollera API-token (Wazuh Dashboard → Security → API)\n'
                f'- Kontrollera nätverksåtkomst')

    def _wazuh_test_result(self, ok, message):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Wazuh-test: ' + ('OK' if ok else 'MISSLYCKADES'),
                'message': message,
                'type': 'success' if ok else 'danger',
                'sticky': True,
            },
        }
