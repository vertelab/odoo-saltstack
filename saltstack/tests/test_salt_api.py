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

    def test_salt_call_builds_payload_and_posts_to_root(self):
        """salt_call POSTs to {api_url}/ (root), not /run, with correct payload."""
        captured = {}

        def fake_urlopen(req, timeout=0, context=None):
            captured['url'] = req.full_url
            captured['method'] = req.get_method()
            captured['headers'] = dict(req.headers)
            captured['body'] = json.loads(req.data.decode())
            return self._mock_response({'return': [{'minion-1': True}]})

        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            result = self.Api.salt_call(
                'local', 'minion-1', 'test.ping', timeout=10)

        self.assertEqual(captured['url'], 'http://localhost:8377/')
        self.assertEqual(captured['method'], 'POST')
        # urllib lowercases header names (Content-type, X-auth-token)
        self.assertEqual(captured['headers'].get('Content-type'),
                         'application/json')
        self.assertTrue(captured['headers'].get('X-auth-token'))
        self.assertEqual(captured['body'], {
            'client': 'local',
            'fun': 'test.ping',
            'timeout': 10,
            'tgt': 'minion-1',
        })
        # Returns formatted JSON string
        parsed = json.loads(result)
        self.assertEqual(parsed['return'][0]['minion-1'], True)

    def test_salt_call_runner_has_no_tgt(self):
        """Runner commands omit tgt."""
        captured = {}

        def fake_urlopen(req, timeout=0, context=None):
            captured['body'] = json.loads(req.data.decode())
            return self._mock_response({'return': [['minion-1']]})

        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            self.Api.salt_call('runner', None, 'minions.list', timeout=10)

        self.assertNotIn('tgt', captured['body'])
        self.assertEqual(captured['body']['client'], 'runner')

    def test_salt_call_args_and_kwargs(self):
        """Positional args and kwargs are included in payload."""
        captured = {}

        def fake_urlopen(req, timeout=0, context=None):
            captured['body'] = json.loads(req.data.decode())
            return self._mock_response({'return': [{}]})

        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            self.Api.salt_call(
                'local', 'gw*', 'state.apply', 'caddy.service',
                timeout=600, test=True)

        self.assertEqual(captured['body']['arg'], ['caddy.service'])
        self.assertEqual(captured['body']['kwarg'], {'test': True})
        self.assertEqual(captured['body']['tgt'], 'gw*')
