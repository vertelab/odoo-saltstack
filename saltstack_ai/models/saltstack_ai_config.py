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

        payload = {
            'client': client,
            'tgt': tgt,
            'fun': fun,
            'arg': list(args),
            'kwarg': kwargs,
        }

        data = json.dumps(payload).encode()
        # OBS: POST ska till ROOT (/), inte /run — /run ger 401 även med
        # giltig token i rest_cherrypy (verifierat 2026-08-09).
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

        with urllib.request.urlopen(req, timeout=timeout + 10, context=ctx) as resp:
            return json.dumps(json.loads(resp.read().decode()), indent=2, default=str)

    def _salt_login(self, timeout=15):
        """Exchange sharedsecret API key for a session token via /login.

        Salt API-nyckeln (saltstack.api_token med auth_method='sharedsecret')
        är INTE en sessionstoken — den måste bytas mot en token via POST /login.
        """
        params = self.env['ir.config_parameter']
        api_url = params.get_param('saltstack.api_url', 'http://localhost:8377')
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
            if cred:
                return cred._get_decrypted_value()
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
