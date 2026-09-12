# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSaltstackApi(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Api = cls.env['saltstack.api']
        cls.Params = cls.env['ir.config_parameter'].sudo()

    def setUp(self):
        super().setUp()
        # Pin the auth method: the production database may run
        # 'sharedsecret' (which triggers a /login round-trip). Tests that
        # want the login path opt in explicitly; the rest use 'token' so the
        # API key is used as-is.
        self.Params.set_param('saltstack.auth_method', 'token')
        self.Params.set_param('saltstack.api_url', 'http://localhost:8377')

    def _mock_response(self, payload):
        """Build a mock urllib response object."""
        class FakeResp:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        return FakeResp(json.dumps(payload).encode())

    def _mock_urlopen(self, captured, result_payload, token='tok-1'):
        """urlopen side_effect that answers /login and the API call.

        salt_call may perform a login round-trip (sharedsecret/keykeep)
        before the actual call. This helper answers both so a test can
        assert on the API call without caring which auth path ran.
        """
        def fake_urlopen(req, timeout=0, context=None):
            url = req.full_url
            if url.endswith('/login'):
                captured['login'] = json.loads(req.data.decode())
                return self._mock_response(
                    {'return': [{'token': token, 'eauth': 'sharedsecret'}]})
            captured['url'] = url
            captured['method'] = req.get_method()
            captured['headers'] = dict(req.headers)
            captured['body'] = json.loads(req.data.decode())
            return self._mock_response(result_payload)
        return fake_urlopen

    def test_salt_call_builds_payload_and_posts_to_root(self):
        """salt_call POSTs to {api_url}/ (root), not /run, with correct payload."""
        self.Params.set_param('saltstack.api_token', 'test-token-123')
        captured = {}

        with patch('urllib.request.urlopen',
                   side_effect=self._mock_urlopen(
                       captured, {'return': [{'minion-1': True}]})):
            result = self.Api.salt_call(
                'local', 'minion-1', 'test.ping', timeout=10)

        self.assertEqual(captured['url'], 'http://localhost:8377/')
        self.assertEqual(captured['method'], 'POST')
        # urllib lowercases header names (Content-type, X-auth-token)
        self.assertEqual(captured['headers'].get('Content-type'),
                         'application/json')
        self.assertEqual(captured['headers'].get('X-auth-token'),
                         'test-token-123')
        self.assertEqual(captured['body'], {
            'client': 'local',
            'fun': 'test.ping',
            'timeout': 10,
            'tgt': 'minion-1',
        })
        # Returns formatted JSON string
        parsed = json.loads(result)
        self.assertEqual(parsed['return'][0]['minion-1'], True)

    def test_salt_call_sharedsecret_logs_in_first(self):
        """auth_method=sharedsecret exchanges the API key for a token."""
        self.Params.set_param('saltstack.auth_method', 'sharedsecret')
        self.Params.set_param('saltstack.api_token', 'shared-key-abc')
        captured = {}

        with patch('urllib.request.urlopen',
                   side_effect=self._mock_urlopen(
                       captured, {'return': [{'minion-1': True}]},
                       token='session-tok-9')):
            self.Api.salt_call('local', 'minion-1', 'test.ping', timeout=10)

        # /login was called with the shared secret…
        self.assertEqual(captured['login'], {
            'username': 'saltapi',
            'password': 'shared-key-abc',
            'eauth': 'sharedsecret',
        })
        # …and the API call used the returned session token.
        self.assertEqual(captured['headers'].get('X-auth-token'),
                         'session-tok-9')

    def test_salt_call_runner_has_no_tgt(self):
        """Runner commands omit tgt."""
        captured = {}

        with patch('urllib.request.urlopen',
                   side_effect=self._mock_urlopen(
                       captured, {'return': [['minion-1']]})):
            self.Api.salt_call('runner', None, 'minions.list', timeout=10)

        self.assertNotIn('tgt', captured['body'])
        self.assertEqual(captured['body']['client'], 'runner')

    def test_salt_call_args_and_kwargs(self):
        """Positional args and kwargs are included in payload."""
        captured = {}

        with patch('urllib.request.urlopen',
                   side_effect=self._mock_urlopen(captured, {'return': [{}]})):
            self.Api.salt_call(
                'local', 'gw*', 'state.apply', 'caddy.service',
                timeout=600, test=True)

        self.assertEqual(captured['body']['arg'], ['caddy.service'])
        self.assertEqual(captured['body']['kwarg'], {'test': True})
        self.assertEqual(captured['body']['tgt'], 'gw*')
