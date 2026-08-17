# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""
Zabbix JSON-RPC API client — owned by the saltstack_zabbix bridge.

Bridge modules own their domain's API client and tools. ai.tool records
use this model (instead of direct imports) because tool code runs in an
exec() context without access to module-level imports.
"""

import json
import logging
import ssl
import urllib.request

from odoo import models

_logger = logging.getLogger(__name__)


class ZabbixApi(models.Model):
    _name = 'zabbix.api'
    _description = 'Zabbix API Client'

    def zabbix_call(self, method, params_dict, timeout=30):
        """Call Zabbix JSON-RPC API. Returns JSON string."""
        params = self.env['ir.config_parameter']
        api_url = params.get_param('zabbix.api_url', 'https://zabbix.example.com')
        api_token = self._get_zabbix_token()

        payload = {
            'jsonrpc': '2.0',
            'method': method,
            'params': params_dict,
            'auth': api_token,
            'id': 1,
        }

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f'{api_url}/api_jsonrpc.php',
            data=data,
            headers={'Content-Type': 'application/json-rpc'},
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            result = json.loads(resp.read().decode())

        if 'error' in result:
            return json.dumps({'error': result['error']}, indent=2)
        return json.dumps(result.get('result', {}), indent=2, default=str)

    def _get_zabbix_token(self):
        params = self.env['ir.config_parameter']
        auth_method = params.get_param('zabbix.auth_method', 'token')
        if auth_method == 'keykeep' and 'keykeep.credential' in self.env:
            cred = self.env['keykeep.credential'].search([
                ('purpose', '=', 'zabbix_api'),
            ], limit=1)
            if cred:
                return cred._get_decrypted_value()
        return params.get_param('zabbix.api_token', '')
