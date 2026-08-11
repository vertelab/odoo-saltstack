# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""
Helper model for ai.tool records to access SaltStack and Zabbix APIs.

Tools use this model instead of direct imports because ai.tool code
runs in an exec() context without access to module-level imports.
"""

import json
import logging
import ssl
import urllib.request

from odoo import fields, models

_logger = logging.getLogger(__name__)


class SaltstackAiConfig(models.Model):
    _name = 'saltstack.ai.config'
    _description = 'SaltStack AI Configuration Helper'

    # ── Salt API ────────────────────────────────────────────────────────

    def salt_call(self, client, tgt, fun, *args, timeout=120, **kwargs):
        """Call Salt REST API. Returns JSON string."""
        params = self.env['ir.config_parameter']
        api_url = params.get_param('saltstack.api_url', 'http://localhost:8377')
        api_token = self._get_salt_token()

        payload = {'client': client, 'fun': fun, 'timeout': timeout}
        if client in ('local', 'local_async', 'local_batch'):
            payload['tgt'] = tgt
        if args:
            payload['arg'] = list(args)
        if kwargs:
            payload['kwarg'] = kwargs

        data = json.dumps(payload).encode()
        # NOTE: POST must go to ROOT (/), not /run — /run returns 401 even with
        # a valid token in rest_cherrypy (verified 2026-08-09).
        req = urllib.request.Request(
            f'{api_url.rstrip("/")}/run',
            data=data,
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'X-Auth-Token': api_token,
            },
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with urllib.request.urlopen(req, timeout=timeout + 30, context=ctx) as resp:
            return json.dumps(json.loads(resp.read().decode()), indent=2, default=str)

    def _salt_login(self, api_key=None, timeout=15):
        """Exchange sharedsecret API key for a session token via /login.

        The Salt API key (saltstack.api_token with auth_method='sharedsecret',
        or the value in keykeep.credential purpose='saltstack_api') is NOT a
        session token — it must be exchanged for a token via POST /login.
        """
        params = self.env['ir.config_parameter']
        api_url = params.get_param('saltstack.api_url', 'http://localhost:8377')
        if api_key is None:
            api_key = params.get_param('saltstack.api_token', '')
        payload = {
            'username': 'saltapi',
            'password': api_key,
            'eauth': 'sharedsecret',
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f'{api_url.rstrip("/")}/login',
            data=data,
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            result = json.loads(resp.read().decode())
        try:
            return result['return'][0]['token']
        except (KeyError, IndexError, TypeError):
            _logger.error('Salt login failed: %s', result)
            raise

    def _get_salt_token(self):
        params = self.env['ir.config_parameter']
        auth_method = params.get_param('saltstack.auth_method', 'token')
        if auth_method == 'sharedsecret':
            return self._salt_login()
        if auth_method == 'keykeep' and 'keykeep.credential' in self.env:
            cred = self.env['keykeep.credential'].search([
                ('purpose', '=', 'saltstack_api'),
            ], limit=1)
            if cred and cred._get_decrypted_value():
                return self._salt_login(cred._get_decrypted_value())
        return params.get_param('saltstack.api_token', '')

    # ── Zabbix API ──────────────────────────────────────────────────────

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

    # ── Wazuh / SIEM API ──────────────────────────────────────────────

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
