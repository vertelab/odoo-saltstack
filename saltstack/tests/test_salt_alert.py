# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""saltstack — tests for drift alert chatter, auto-fix prompt and simulation.

Run with: checkmodule -d <db> -m saltstack -t
Covers: add-operator-use-cases — chatter/writeback methods,
auto-fix taxonomy in the diagnosis prompt, and end-to-end simulation.
"""

from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAlertChatter(TransactionCase):
    """Chatter/writeback on saltstack.alert + salt.minion records."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Alert = cls.env['saltstack.alert']
        cls.Minion = cls.env['salt.minion']

    def _make_alert(self, host='sparv', category='process', severity=12):
        return self.Alert.create({
            'host': host,
            'source': 'zabbix',
            'category': category,
            'severity': severity,
            'trigger_name': 'Odoo HTTP endpoint not responding',
            'description': 'Simulerat larm',
            'raw_log': 'odoo.service: main process exited',
        })

    def test_post_diagnosis_start_chatter(self):
        """_post_diagnosis_start() posts "AI-diagnos påbörjad" on the alert."""
        alert = self._make_alert()
        before = len(alert.message_ids)
        alert._post_diagnosis_start()
        self.assertEqual(len(alert.message_ids), before + 1)
        # mail.message _order = 'id DESC' → message_ids[0] is the newest
        body = alert.message_ids[0].body or ''
        self.assertIn('AI-diagnos påbörjad', body)
        self.assertIn('process', body)

    def test_post_diagnosis_result_chatter(self):
        """_post_diagnosis_result() posts result (truncated ≤3000) + action."""
        alert = self._make_alert()
        before = len(alert.message_ids)
        alert._post_diagnosis_result('Rotorsak: OOM-kill', 'systemctl start odoo')
        self.assertEqual(len(alert.message_ids), before + 1)
        body = alert.message_ids[0].body or ''
        self.assertIn('Diagnos klar', body)
        self.assertIn('Rotorsak: OOM-kill', body)
        self.assertIn('systemctl start odoo', body)

    def test_post_diagnosis_result_truncates(self):
        """Long results are truncated to 3000 chars to protect the chatter."""
        alert = self._make_alert()
        long_result = 'x' * 5000
        alert._post_diagnosis_result(long_result, '')
        body = alert.message_ids[0].body or ''
        self.assertLess(len(body), 4000)

    def test_post_minion_chatter_finds_minion(self):
        """_post_minion_chatter() posts a summary incl. alert link on minion."""
        minion = self.Minion.create({'name': 'sparv'})
        alert = self._make_alert(host='sparv')
        before = len(minion.message_ids)
        alert._post_minion_chatter('Odoo var nere, startades om.')
        self.assertEqual(len(minion.message_ids), before + 1)
        body = minion.message_ids[0].body or ''
        self.assertIn('Driftlarm', body)
        self.assertIn('alert #%d' % alert.id, body)
        # Klickbar länk till alerten (task T14 / GAP 3)
        self.assertIn('model=saltstack.alert', body)
        self.assertIn('id=%d' % alert.id, body)

    def test_post_minion_chatter_no_minion_not_fatal(self):
        """No matching minion → logged, not fatal."""
        alert = self._make_alert(host='finns-inte')
        # Ska inte kasta; minion saknas och bara loggas
        alert._post_minion_chatter('Sammanfattning')

    def test_post_action_plan_channel(self):
        """_post_action_plan() posts to the Driftlarm channel."""
        alert = self._make_alert(severity=12)
        alert._post_action_plan('Åtgärdsplan: starta om odoo')
        channel = self.env['discuss.channel'].search(
            [('name', '=', 'Driftlarm')], limit=1)
        self.assertTrue(channel)
        body = '\n'.join(m.body or '' for m in channel.message_ids)
        self.assertIn('Åtgärdsplan: starta om odoo', body)


@tagged('post_install', '-at_install')
class TestDiagnosisPrompt(TransactionCase):
    """Auto-fix taxonomy + Odoo ORM reference in the diagnosis prompt."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Alert = cls.env['saltstack.alert']

    def _make_alert(self, category='process'):
        return self.Alert.create({
            'host': 'sparv',
            'source': 'zabbix',
            'category': category,
            'severity': 12,
            'trigger_name': 'Odoo HTTP endpoint not responding',
            'description': 'Simulerat larm',
            'raw_log': 'odoo.service: main process exited',
        })

    def test_prompt_has_auto_fix_rules(self):
        """Prompt contains the auto-fix taxonomy (ACT / DOCUMENT ONLY)."""
        prompt = self._make_alert()._build_diagnosis_prompt()
        self.assertIn('Auto-fix rules', prompt)
        self.assertIn('SAFE TO AUTO-FIX', prompt)
        self.assertIn('DOCUMENT ONLY', prompt)

    def test_prompt_has_orm_reference(self):
        """Prompt references odoo_search for minion + describe_model guidance."""
        prompt = self._make_alert()._build_diagnosis_prompt()
        self.assertIn("odoo_search(model='salt.minion'", prompt)
        self.assertIn('describe_model', prompt)

    def test_prompt_category_instruction(self):
        """Category-specific instruction is injected (kernel → dmesg)."""
        prompt = self._make_alert(category='kernel')._build_diagnosis_prompt()
        self.assertIn('dmesg', prompt)


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
