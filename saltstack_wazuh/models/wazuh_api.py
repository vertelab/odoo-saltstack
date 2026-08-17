# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""
Wazuh/SIEM API client — owned by the saltstack_wazuh bridge.

Bridge modules own their domain's API client. ai.tool records use this
model (instead of direct imports) because tool code runs in an exec()
context without access to module-level imports.
"""

import json
import logging
import ssl
import urllib.request

from odoo import models

_logger = logging.getLogger(__name__)


class WazuhApi(models.Model):
    _name = 'wazuh.api'
    _description = 'Wazuh API Client'

    def wazuh_call(self, endpoint, params_dict=None, timeout=30):
        """Call Wazuh API. Authenticates per call, returns JSON string.

        Flow (Wazuh API v4):
          1. POST {url}/api/v1/auth  {jsonrpc, method: auth, auth_context: token}
             → data.token
          2. GET  {url}/api/v1/{endpoint}?<params>  Authorization: Bearer <token>

        Config (ir.config_parameter, set by saltstack_wazuh):
          siem.api_url, siem.api_token, siem.auth_method (token|keykeep)
        """
        params = self.env['ir.config_parameter']
        api_url = params.get_param('siem.api_url', '')
        api_token = self._get_siem_token()
        if not api_url:
            return json.dumps(
                {'error': 'siem.api_url is not configured — set it in '
                          'Settings → Saltstack → Wazuh'},
                indent=2)

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        # 1. Login → session-token
        login_payload = json.dumps({
            'jsonrpc': '2.0',
            'method': 'auth',
            'params': {'auth_context': api_token},
            'id': 1,
        }).encode()
        req = urllib.request.Request(
            f'{api_url.rstrip("/")}/api/v1/auth',
            data=login_payload,
            headers={'Content-Type': 'application/json'},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                login = json.loads(resp.read().decode())
        except Exception as e:
            return json.dumps(
                {'error': f'Wazuh login failed: {e}'}, indent=2)

        data = login.get('data') or {}
        token = data.get('token', '')
        if not token:
            return json.dumps(
                {'error': 'Wazuh login failed (no token)',
                 'response': login}, indent=2)

        # 2. Anropa endpoint
        url = f'{api_url.rstrip("/")}/api/v1/{endpoint}'
        if params_dict:
            query = '&'.join(
                f'{k}={v}' for k, v in params_dict.items() if v not in (None, ''))
            if query:
                url += '?' + query
        req = urllib.request.Request(
            url, headers={'Authorization': f'Bearer {token}'})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                result = json.loads(resp.read().decode())
            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            return json.dumps(
                {'error': f'Wazuh {endpoint} failed: {e}'}, indent=2)

    def _get_siem_token(self):
        params = self.env['ir.config_parameter']
        auth_method = params.get_param('siem.auth_method', 'token')
        if auth_method == 'keykeep' and 'keykeep.credential' in self.env:
            cred = self.env['keykeep.credential'].search([
                ('purpose', '=', 'siem_api'),
            ], limit=1)
            if cred:
                return cred._get_decrypted_value()
        return params.get_param('siem.api_token', '')
