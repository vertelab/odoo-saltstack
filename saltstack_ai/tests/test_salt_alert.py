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

    def _minion(self, name='sparv'):
        """Return a salt.minion record for the given name (create once)."""
        minion = self.Minion.search([('name', '=', name)], limit=1)
        return minion or self.Minion.create({'name': name})

    def _make_alert(self, host='sparv', category='process', severity=12):
        return self.Alert.create({
            'host': self._minion(host).id,
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
        cls.Minion = cls.env['salt.minion']

    def _minion(self, name='sparv'):
        """Return a salt.minion record for the given name (create once)."""
        minion = self.Minion.search([('name', '=', name)], limit=1)
        return minion or self.Minion.create({'name': name})

    def _make_alert(self, category='process'):
        return self.Alert.create({
            'host': self._minion('sparv').id,
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

    def setUp(self):
        super().setUp()
        # The live database may have auto diagnosis switched off; these tests
        # exercise the dispatch path, so force the gate on.
        self.env['ir.config_parameter'].sudo().set_param(
            'saltstack.alert.auto_diagnose', 'True')
        self.env.registry.clear_cache()

    def _patch_dispatch(self):
        """Patch both diagnosis entry points (async + sync fallback).

        process_webhook prefers _schedule_diagnosis when it exists
        (saltstack_ai installed) and falls back to _start_diagnosis. Patching
        both lets the test assert on the diagnosis step regardless of which
        dispatch path is taken.
        """
        AlertModel = self.Alert.__class__
        sched = patch.object(AlertModel, '_schedule_diagnosis')
        start = patch.object(AlertModel, '_start_diagnosis')
        return sched, start

    def test_webhook_starts_diagnosis_when_auto_diagnose(self):
        """auto_diagnose default on → webhook dispatches the diagnosis."""
        sched, start = self._patch_dispatch()
        with sched as mock_sched, start as mock_start:
            result = self.Alert.process_webhook({
                'host': 'sparv',
                'category': 'process',
                'severity': 12,
                'trigger_name': 'Test',
            })
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(mock_sched.call_count + mock_start.call_count, 1)

    def test_webhook_skips_diagnosis_when_disabled(self):
        """auto_diagnose off → webhook does NOT dispatch the diagnosis."""
        AlertModel = self.Alert.__class__
        sched, start = self._patch_dispatch()
        with patch.object(AlertModel, '_auto_diagnose_enabled',
                          return_value=False), sched as mock_sched, \
                start as mock_start:
            result = self.Alert.process_webhook({
                'host': 'sparv',
                'category': 'process',
                'severity': 5,
            })
        self.assertEqual(result['status'], 'ok')
        mock_sched.assert_not_called()
        mock_start.assert_not_called()

    def test_webhook_skips_diagnosis_when_source_disabled(self):
        """Source-specific gate off → webhook does NOT dispatch the diagnosis."""
        AlertModel = self.Alert.__class__
        sched, start = self._patch_dispatch()
        with patch.object(AlertModel, '_auto_diagnose_enabled_for_source',
                          return_value=False), sched as mock_sched, \
                start as mock_start:
            result = self.Alert.process_webhook({
                'host': 'sparv',
                'source': 'zabbix',
                'category': 'process',
                'severity': 12,
                'trigger_name': 'Kall-gate-test',
            })
        self.assertEqual(result['status'], 'ok')
        mock_sched.assert_not_called()
        mock_start.assert_not_called()

    def test_webhook_starts_diagnosis_when_source_enabled(self):
        """Source-specific gate on (default) → diagnosis still dispatched."""
        sched, start = self._patch_dispatch()
        with sched as mock_sched, start as mock_start:
            result = self.Alert.process_webhook({
                'host': 'sparv',
                'source': 'zabbix',
                'category': 'process',
                'severity': 12,
                'trigger_name': 'Kall-gate-test-2',
            })
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(mock_sched.call_count + mock_start.call_count, 1)

    def test_schedule_does_not_queue_when_gate_off(self):
        """Gate off → _schedule_diagnosis must NOT mark the alert 'pending'.

        Regression: the pending-diagnosis cron picks up every alert with
        diagnosis_state='pending' and runs the AI regardless of the gate, so
        queueing a gated alert made it diagnose anyway.
        """
        alert = self.Alert.create({
            'host': self.env['salt.minion'].create({'name': 'gate-q'}).id,
            'source': 'zabbix',
            'category': 'process',
            'severity': 12,
            'trigger_name': 'Gate queue check',
        })
        AlertModel = self.Alert.__class__
        with patch.object(AlertModel, '_auto_diagnose_enabled',
                          return_value=False):
            queued = alert._schedule_diagnosis()
        self.assertFalse(queued)
        self.assertNotEqual(alert.diagnosis_state, 'pending')

    def test_cron_skips_gated_pending_alert(self):
        """Cron must not run diagnosis on a pending alert when the gate is off.

        Covers alerts queued before the setting was turned off.
        """
        alert = self.Alert.create({
            'host': self.env['salt.minion'].create({'name': 'gate-cron'}).id,
            'source': 'zabbix',
            'category': 'process',
            'severity': 12,
            'trigger_name': 'Gate cron check',
            'diagnosis_state': 'pending',
        })
        AlertModel = self.Alert.__class__
        with patch.object(AlertModel, '_auto_diagnose_enabled',
                          return_value=False), \
                patch.object(AlertModel, '_start_diagnosis') as mock_start:
            self.Alert._run_pending_diagnoses(limit=50)
        mock_start.assert_not_called()
        self.assertNotEqual(alert.diagnosis_state, 'pending')
