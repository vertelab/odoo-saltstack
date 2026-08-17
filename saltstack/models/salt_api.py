# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""
Ground Salt API client for the SaltStack module.

Single client model for all Salt REST API communication in the
saltstack module series. Base module owns communication with the Salt
master; bridge modules (_ai, _zabbix, _wazuh) own their own API clients
and tools, and inherit from this base.

ai.tool records use this model (instead of direct imports) because tool
code runs in an exec() context without access to module-level imports.
"""

import json
import logging
import ssl
import urllib.request

from odoo import models

_logger = logging.getLogger(__name__)


class SaltstackApi(models.Model):
    _name = 'saltstack.api'
    _description = 'SaltStack API Client'

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
            f'{api_url.rstrip("/")}/',
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
