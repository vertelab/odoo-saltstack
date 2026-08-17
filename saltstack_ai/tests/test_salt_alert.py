# -*- coding: utf-8 -*-
"""saltstack_ai — tests for AI diagnosis on saltstack.alert.

Run with: checkmodule -d <db> -m saltstack_ai -t
Covers: chatter/writeback methods, auto-fix taxonomy in the diagnosis
prompt, and the guarded webhook flow (diagnosis runs when this bridge
is installed).
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
        self.assertIn('model=saltstack.alert', body)
        self.assertIn('id=%d' % alert.id, body)

    def test_post_minion_chatter_no_minion_not_fatal(self):
        """No matching minion → logged, not fatal."""
        alert = self._make_alert(host='finns-inte')
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
class TestWebhookRunsDiagnosis(TransactionCase):
    """With saltstack_ai installed, process_webhook starts AI diagnosis."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Alert = cls.env['saltstack.alert']

    def test_webhook_starts_diagnosis_when_auto_diagnose(self):
        """auto_diagnose default on → webhook calls _start_diagnosis."""
        AlertModel = self.Alert.__class__
        with patch.object(AlertModel, '_start_diagnosis') as mock_start:
            result = self.Alert.process_webhook({
                'host': 'sparv',
                'category': 'process',
                'severity': 12,
                'trigger_name': 'Test',
            })
        self.assertEqual(result['status'], 'ok')
        mock_start.assert_called_once()

    def test_webhook_skips_diagnosis_when_disabled(self):
        """auto_diagnose off → webhook does NOT call _start_diagnosis."""
        AlertModel = self.Alert.__class__
        with patch.object(AlertModel, '_auto_diagnose_enabled',
                          return_value=False), \
                patch.object(AlertModel, '_start_diagnosis') as mock_start:
            result = self.Alert.process_webhook({
                'host': 'sparv',
                'category': 'process',
                'severity': 5,
            })
        self.assertEqual(result['status'], 'ok')
        mock_start.assert_not_called()
