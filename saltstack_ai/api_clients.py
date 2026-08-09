# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""
SaltAPI and ZabbixAPI — generic API client classes.

Used by ai.tool records to communicate with SaltStack and Zabbix.
Configuration is read from ir.config_parameter or keykeep.credential
depending on auth_method setting.
"""

import json
import logging
import ssl
import urllib.request

from odoo import models

_logger = logging.getLogger(__name__)


class SaltAPI:
    """Generic Salt REST API client.

    Usage:
        api = SaltAPI(env)
        result = api.call('local', '*', 'test.ping')
    """

    def __init__(self, env):
        self.env = env
        params = env['ir.config_parameter']
        self.api_url = params.get_param('saltstack.api_url', 'http://localhost:8377')
        self.auth_method = params.get_param('saltstack.auth_method', 'token')

    def _login(self, timeout=15):
        """Exchange sharedsecret API key for a session token via /login.

        Salt API-nyckeln (saltstack.api_token med auth_method='sharedsecret')
        är INTE en sessionstoken — den måste bytas mot en token via POST /login.
        """
        api_key = self.env['ir.config_parameter'].get_param(
            'saltstack.api_token', '')
        payload = {
            'username': 'saltapi',
            'password': api_key,
            'eauth': 'sharedsecret',
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f'{self.api_url.rstrip("/")}/login',
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

    def _get_token(self):
        """Resolve API token from config or keykeep."""
        if self.auth_method == 'sharedsecret':
            return self._login()
        if self.auth_method == 'keykeep':
            if 'keykeep.credential' in self.env:
                cred = self.env['keykeep.credential'].search([
                    ('purpose', '=', 'saltstack_api'),
                ], limit=1)
                if cred:
                    return cred._get_decrypted_value()
        return self.env['ir.config_parameter'].get_param(
            'saltstack.api_token', '')

    def call(self, client, tgt, fun, *args, timeout=120, **kwargs):
        """Call Salt REST API. Returns parsed JSON dict.

        Args:
            client: 'local', 'runner', 'wheel', etc.
            tgt: Minion target pattern (None for runner/wheel).
            fun: Salt function (e.g. 'test.ping', 'state.apply').
            *args: Positional arguments.
            timeout: Command timeout in seconds.
            **kwargs: Keyword arguments.

        Returns:
            dict: Parsed JSON response.
        """
        payload = {'client': client, 'fun': fun}
        if client in ('local', 'local_async', 'local_batch'):
            payload['tgt'] = tgt
        if args:
            payload['arg'] = list(args)
        if kwargs:
            payload['kwarg'] = kwargs

        data = json.dumps(payload).encode()
        # OBS: POST ska till ROOT (/), inte /run — /run ger 401 även med
        # giltig token i rest_cherrypy (verifierat 2026-08-09).
        req = urllib.request.Request(
            f'{self.api_url.rstrip("/")}/run',
            data=data,
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'X-Auth-Token': self._get_token(),
            },
        )

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with urllib.request.urlopen(req, timeout=timeout + 10, context=ctx) as resp:
            return json.loads(resp.read().decode())


class ZabbixAPI:
    """Generic Zabbix JSON-RPC API client.

    Usage:
        api = ZabbixAPI(env)
        result = api.call('problem.get', {'output': 'extend'})
    """

    def __init__(self, env):
        self.env = env
        params = env['ir.config_parameter']
        self.api_url = params.get_param('zabbix.api_url', 'https://zabbix.example.com')
        self.auth_method = params.get_param('zabbix.auth_method', 'token')

    def _get_token(self):
        """Resolve API token from config or keykeep."""
        if self.auth_method == 'keykeep':
            if 'keykeep.credential' in self.env:
                cred = self.env['keykeep.credential'].search([
                    ('purpose', '=', 'zabbix_api'),
                ], limit=1)
                if cred:
                    return cred._get_decrypted_value()
        return self.env['ir.config_parameter'].get_param(
            'zabbix.api_token', '')

    def call(self, method, params, timeout=30):
        """Call Zabbix JSON-RPC API. Returns parsed result dict.

        Args:
            method: Zabbix API method (e.g. 'problem.get').
            params: Parameters dict.
            timeout: Request timeout.

        Returns:
            dict: The 'result' field from the JSON-RPC response.
        """
        payload = {
            'jsonrpc': '2.0',
            'method': method,
            'params': params,
            'auth': self._get_token(),
            'id': 1,
        }

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f'{self.api_url}/api_jsonrpc.php',
            data=data,
            headers={'Content-Type': 'application/json-rpc'},
        )

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            result = json.loads(resp.read().decode())

        if 'error' in result:
            raise RuntimeError(
                f"Zabbix API error: {result['error']['message']} "
                f"(code: {result['error']['code']})"
            )
        return result['result']
