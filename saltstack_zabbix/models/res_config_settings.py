# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ── Zabbix API ───────────────────────────────────────────────────

    zabbix_api_url = fields.Char(
        string='Zabbix API URL',
        config_parameter='zabbix.api_url',
        default='https://zabbix.example.com',
        help='Base URL of the Zabbix JSON-RPC API '
             '(e.g. https://zabbix.vertel.se)',
    )
    zabbix_api_token = fields.Char(
        string='Zabbix API Token',
        config_parameter='zabbix.api_token',
        help='Authentication token for the Zabbix API',
    )
    zabbix_auth_method = fields.Selection(
        selection='_selection_zabbix_auth_method',
        string='Zabbix Auth Method',
        config_parameter='zabbix.auth_method',
        default='token',
        help='How to authenticate with the Zabbix API. '
             '"Keykeep Managed" is available when the saltstack_zabbix_keykeep '
             'module is installed.',
    )

    def _selection_zabbix_auth_method(self):
        """Base selection — keykeep added by saltstack_zabbix_keykeep module."""
        return [('token', 'API Token')]

    def action_test_zabbix(self):
        """Test Zabbix API connection. Returns a popup notification."""
        params = self.env['ir.config_parameter']
        url = params.get_param('zabbix.api_url', '')
        token = params.get_param('zabbix.api_token', '')

        if not url:
            return self._zabbix_test_result(
                False, 'Zabbix API URL är inte konfigurerad.\n'
                        'Sätt URL:en i Inställningar → Saltstack → Zabbix.')

        if not token and params.get_param('zabbix.auth_method', 'token') != 'keykeep':
            return self._zabbix_test_result(
                False, 'Zabbix API Token saknas.\n'
                        'Sätt token i Inställningar → Saltstack → Zabbix.')

        try:
            import json
            import ssl
            import urllib.request

            payload = {
                'jsonrpc': '2.0',
                'method': 'apiinfo.version',
                'params': {},
                'id': 1,
            }
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                f'{url}/api_jsonrpc.php',
                data=data,
                headers={'Content-Type': 'application/json-rpc'},
            )
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                result = json.loads(resp.read().decode())

            if 'error' in result:
                return self._zabbix_test_result(
                    False, f'Zabbix API-fel: {result["error"].get("message", "okänt")}')

            # Test auth with a real call
            payload['method'] = 'apiinfo.version'
            version = result.get('result', '?')
            return self._zabbix_test_result(
                True, f'Zabbix API nåbar.\nVersion: {version}')

        except Exception as e:
            return self._zabbix_test_result(
                False,
                f'Kunde inte nå Zabbix API på {url}.\n'
                f'Fel: {str(e)}\n\nFelsökning:\n'
                f'- Kontrollera att URL:en är rätt (t.ex. https://zabbix.vertel.se)\n'
                f'- Kontrollera nätverksåtkomst (port 443)\n'
                f'- Kontrollera att api_jsonrpc.php svarar')

    def _zabbix_test_result(self, ok, message):
        """Return a popup notification."""
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Zabbix-test: ' + ('OK' if ok else 'MISSLYCKADES'),
                'message': message,
                'type': 'success' if ok else 'danger',
                'sticky': True,
            },
        }
