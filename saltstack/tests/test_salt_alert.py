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
        cls.Minion = cls.env['salt.minion']

    def _minion(self, name='sparv'):
        """Return a salt.minion record for the given name (create once)."""
        minion = self.Minion.search([('name', '=', name)], limit=1)
        return minion or self.Minion.create({'name': name})

    def test_process_webhook_creates_alert(self):
        """A minimal payload creates a saltstack.alert record.

        Bridge hooks (_correlate_zabbix / diagnosis) are patched away so the
        test asserts ground behaviour regardless of whether the bridge
        modules are installed in the same run.
        """
        minion = self._minion('sparv')
        AlertModel = self.Alert.__class__
        with patch.object(AlertModel, '_correlate_zabbix',
                          lambda self: None), \
                patch.object(AlertModel, '_schedule_diagnosis',
                             lambda self: None), \
                patch.object(AlertModel, '_start_diagnosis',
                             lambda self: None):
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
        self.assertEqual(alert.host, minion)
        self.assertEqual(alert.severity, 12)
        # Ground defaults: no correlation on the base model itself.
        self.assertFalse(getattr(alert, 'correlated_zabbix_alert', False))

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
        """The webhook calls _correlate_zabbix when a bridge defines it.

        The base source gate _auto_diagnose_enabled_for_source always exists
        and is source-agnostic (True) — diagnosis dispatch itself is guarded
        by the AI-only hook _auto_diagnose_enabled.
        """
        AlertModel = self.Alert.__class__
        called = []

        def fake_correlate(self):
            called.append('correlate')

        # _correlate_zabbix exists (saltstack_zabbix installed)
        with patch.object(AlertModel, '_correlate_zabbix', fake_correlate):
            result = self.Alert.process_webhook({
                'host': 'sparv',
                'category': 'process',
                'severity': 5,
            })

        self.assertIn('correlate', called)
        self.assertEqual(result['status'], 'ok')
        # The base source gate always exists and is source-agnostic (True).
        self.assertTrue(AlertModel._auto_diagnose_enabled_for_source(
            self.Alert.browse(result['alert_id'])))
        self.assertFalse(result['correlated_zabbix_alert'])


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


@tagged('post_install', '-at_install')
class TestAlertDedup(TransactionCase):
    """Alert dedup: same host + normalized trigger does not create new records."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Alert = cls.env['saltstack.alert']

    def _payload(self, host='sparv', trigger='Odoo: HTTP endpoint not responding (HTTP 0)'):
        return {
            'host': host,
            'source': 'zabbix',
            'category': 'process',
            'severity': 12,
            'trigger_name': trigger,
            'description': 'test',
        }

    def test_normalize_strips_trailing_parenthetical(self):
        self.assertEqual(
            self.Alert._normalize_trigger('Odoo: HTTP endpoint not responding (HTTP 0)'),
            'odoo: http endpoint not responding')
        self.assertEqual(
            self.Alert._normalize_trigger('Odoo: HTTP endpoint not responding (HTTP 200)'),
            'odoo: http endpoint not responding')

    def test_same_trigger_deduplicated(self):
        r1 = self.Alert.process_webhook(self._payload())
        self.assertFalse(r1['deduplicated'])
        r2 = self.Alert.process_webhook(self._payload())
        self.assertTrue(r2['deduplicated'])
        self.assertEqual(r2['alert_id'], r1['alert_id'])
        alert = self.Alert.browse(r1['alert_id'])
        self.assertEqual(alert.occurrences, 2)
        self.assertTrue(alert.last_occurrence)

    def test_flapping_suffix_deduplicated(self):
        r1 = self.Alert.process_webhook(self._payload(trigger='Odoo: HTTP endpoint not responding (HTTP 0)'))
        r2 = self.Alert.process_webhook(self._payload(trigger='Odoo: HTTP endpoint not responding (HTTP 200)'))
        self.assertTrue(r2['deduplicated'])
        self.assertEqual(r2['alert_id'], r1['alert_id'])

    def test_different_trigger_creates_new(self):
        r1 = self.Alert.process_webhook(self._payload(trigger='Odoo: HTTP endpoint not responding (HTTP 0)'))
        r2 = self.Alert.process_webhook(self._payload(trigger='Linux: Load average is too high'))
        self.assertFalse(r2['deduplicated'])
        self.assertNotEqual(r2['alert_id'], r1['alert_id'])

    def test_resolved_previous_allows_new(self):
        r1 = self.Alert.process_webhook(self._payload())
        self.Alert.browse(r1['alert_id']).action_mark_resolved()
        r2 = self.Alert.process_webhook(self._payload())
        self.assertFalse(r2['deduplicated'])
        self.assertNotEqual(r2['alert_id'], r1['alert_id'])

    def test_dedup_skips_diagnosis_flag(self):
        r1 = self.Alert.process_webhook(self._payload())
        r2 = self.Alert.process_webhook(self._payload())
        self.assertFalse(r2['diagnosis_started'])
