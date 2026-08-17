# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""saltstack — tests for ground saltstack.alert behavior.

Run with: checkmodule -d <db> -m saltstack -t
Covers: webhook-processing with guarded bridge hooks (base runs without
saltstack_zabbix / saltstack_ai), notification, and fault simulation.
"""

from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAlertWebhookGround(TransactionCase):
    """Base webhook flow without any bridge module installed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Alert = cls.env['saltstack.alert']

    def test_process_webhook_creates_alert(self):
        """A minimal payload creates a saltstack.alert record."""
        result = self.Alert.process_webhook({
            'host': 'sparv',
            'source': 'zabbix',
            'category': 'process',
            'severity': 12,
            'trigger_name': 'Odoo HTTP endpoint not responding',
            'raw_log': 'odoo.service: main process exited',
        })
        self.assertEqual(result['status'], 'ok')
        alert = self.Alert.browse(result['alert_id'])
        self.assertEqual(alert.host, 'sparv')
        self.assertEqual(alert.severity, 12)
        # Bridge fields absent on base-only → guarded defaults
        self.assertFalse(result['correlated_zabbix_alert'])
        self.assertFalse(result['diagnosis_started'])
        self.assertEqual(result['coworker_session_id'], '')

    def test_process_webhook_missing_host(self):
        result = self.Alert.process_webhook({'category': 'process'})
        self.assertEqual(result['status'], 'error')
        self.assertIn('Missing host', result['error'])

    def test_process_webhook_invalid_category_defaults(self):
        """Unknown categories fall back to 'other' (webhook contract)."""
        result = self.Alert.process_webhook({
            'host': 'sparv',
            'category': 'bogus-category',
        })
        self.assertEqual(result['status'], 'ok')
        alert = self.Alert.browse(result['alert_id'])
        self.assertEqual(alert.category, 'other')

    def test_critical_alert_notifies_channel(self):
        """Severity ≥ 12 posts to the Driftlarm channel (ground notification)."""
        result = self.Alert.process_webhook({
            'host': 'sparv',
            'category': 'process',
            'severity': 15,
            'trigger_name': 'Test critical',
        })
        self.assertEqual(result['status'], 'ok')
        channel = self.env['discuss.channel'].search(
            [('name', '=', 'Driftlarm')], limit=1)
        self.assertTrue(channel)
        body = '\n'.join(m.body or '' for m in channel.message_ids)
        self.assertIn('Test critical', body)

    def test_bridge_hooks_called_when_present(self):
        """When a bridge defines _correlate_zabbix/_start_diagnosis, the
        webhook calls them (guarded). Simulated via stub methods on the model.
        Fields are NOT set on the base-only model — we assert the guard calls
        the hooks and falls back to safe defaults in the result dict.
        """
        AlertModel = self.Alert.__class__
        called = []

        def fake_correlate(self):
            called.append('correlate')

        def fake_auto_enabled(self):
            return True

        def fake_start(self):
            called.append('diagnosis')

        with patch.object(AlertModel, '_correlate_zabbix', fake_correlate), \
                patch.object(AlertModel, '_auto_diagnose_enabled',
                             fake_auto_enabled), \
                patch.object(AlertModel, '_start_diagnosis', fake_start):
            result = self.Alert.process_webhook({
                'host': 'sparv',
                'category': 'process',
                'severity': 5,
            })

        self.assertIn('correlate', called)
        self.assertIn('diagnosis', called)
        self.assertEqual(result['status'], 'ok')
        # Hooks ran but their fields are not present on the base-only model
        self.assertFalse(result['correlated_zabbix_alert'])
        self.assertFalse(result['diagnosis_started'])
        self.assertEqual(result['coworker_session_id'], '')


@tagged('post_install', '-at_install')
class TestSimulateFullChain(TransactionCase):
    """End-to-end simulation server action on salt.minion."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Minion = cls.env['salt.minion']

    def _make_minion(self, name='sparv'):
        return self.Minion.create({'name': name})

    def test_simulate_full_chain_unknown_type(self):
        """Unknown fault_type returns success=False without any API calls."""
        minion = self._make_minion()
        with patch.object(type(minion), '_call_salt_api') as mock_api:
            result = minion.action_simulate_full_chain('bogus')
        mock_api.assert_not_called()
        self.assertFalse(result['success'])
        self.assertIn('Unknown fault_type', result['error'])

    def test_simulate_full_chain_payload_map(self):
        """Each fault type maps to source/category/severity/trigger_name."""
        minion = self._make_minion()
        AlertModel = self.env['saltstack.alert'].__class__
        expected = {
            'stop_odoo': {
                'source': 'zabbix', 'category': 'process', 'severity': 12,
                'trigger_name': 'Odoo HTTP endpoint not responding',
            },
            'grow_log': {
                'source': 'zabbix', 'category': 'system', 'severity': 12,
                'trigger_name': 'No free disk space',
            },
            'wazuh_bruteforce': {
                'source': 'wazuh', 'category': 'system', 'severity': 12,
                'trigger_name': 'SSH brute force detected',
            },
        }
        # Mock both the Salt API (fault injection) and the webhook processing
        # so the test never touches the network nor starts an AI diagnosis.
        with patch.object(
                type(minion), '_call_salt_api',
                return_value={'success': True, 'result': 'ok'}), \
                patch.object(
                    AlertModel, 'process_webhook',
                    return_value={'status': 'ok', 'alert_id': 1,
                                  'diagnosis_started': True}) as mock_wh:
            for fault_type, exp in expected.items():
                minion.action_simulate_full_chain(fault_type)
                payload = mock_wh.call_args[0][0]
                self.assertEqual(payload['source'], exp['source'])
                self.assertEqual(payload['category'], exp['category'])
                self.assertEqual(payload['severity'], exp['severity'])
                self.assertEqual(
                    payload['trigger_name'], exp['trigger_name'])
