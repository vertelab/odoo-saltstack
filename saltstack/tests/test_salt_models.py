# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

from odoo import fields
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import ValidationError

from unittest.mock import patch


@tagged('post_install', '-at_install')
class TestSaltMinion(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.SaltMinion = cls.env['salt.minion']
        cls.Params = cls.env['ir.config_parameter'].sudo()

    def setUp(self):
        super().setUp()
        # Isolate from the production auth config: the live database may run
        # 'sharedsecret' (login round-trip) and carry a real API URL/token.
        # Each test opts in to what it needs.
        self.Params.set_param('saltstack.auth_method', 'token')
        self.Params.set_param('saltstack.api_url', 'http://localhost:8377')
        self.Params.set_param('saltstack.api_token', 'test-token')

    def test_create_minion(self):
        minion = self.SaltMinion.create({
            'name': 'test-minion-1',
            'private_ip': '192.168.11.99',
            'public_ip': '185.39.146.200',
            'dc': 'ska',
            'roles': 'odoo,caddy',
        })
        self.assertEqual(minion.name, 'test-minion-1')
        self.assertEqual(minion.dc, 'ska')
        self.assertTrue(minion.active)

    def test_minion_name_required(self):
        with self.assertRaises(Exception):
            self.SaltMinion.create({'private_ip': '192.168.11.99'})

    def test_demo_minion_flag(self):
        demo = self.SaltMinion.create({'name': 'test-demo-1', 'is_demo': True})
        prod = self.SaltMinion.create({'name': 'test-prod-1', 'is_demo': False})
        self.assertTrue(demo.is_demo)
        self.assertFalse(prod.is_demo)

    def test_ping_without_api(self):
        """Ping without API configured should fail gracefully, not crash."""
        self.Params.set_param('saltstack.api_url', '')
        self.Params.set_param('saltstack.api_token', '')
        minion = self.SaltMinion.create({'name': 'test-noconfig-1'})
        result = minion.action_ping()
        self.assertIn('success', result)
        self.assertFalse(result['success'])

    def test_ping_delegates_to_saltstack_api(self):
        """action_ping reaches Salt via saltstack.api delegator."""
        minion = self.SaltMinion.create({'name': 'test-delegate-1'})
        with patch('odoo.addons.saltstack.models.salt_api.'
                   'urllib.request.urlopen') as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value.read.return_value = \
                b'{"return": [{"test-delegate-1": true}]}'
            result = minion.action_ping()
        self.assertTrue(result['success'])
        self.assertEqual(result['status'], 'up')
        minion.invalidate_recordset()
        self.assertEqual(minion.state, 'online')


@tagged('post_install', '-at_install')
class TestSaltPillar(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.SaltPillar = cls.env['salt.pillar']

    def test_create_pillar(self):
        pillar = self.SaltPillar.create({
            'key': 'test.version',
            'value': '18',
        })
        self.assertEqual(pillar.key, 'test.version')
        # Auto-detect integer type
        self.assertEqual(pillar.data_type, 'integer')

    def test_create_pillar_string(self):
        pillar = self.SaltPillar.create({
            'key': 'test.name',
            'value': 'hello',
        })
        self.assertEqual(pillar.data_type, 'string')

    def test_create_pillar_boolean(self):
        pillar = self.SaltPillar.create({
            'key': 'test.enabled',
            'value': 'true',
        })
        self.assertEqual(pillar.data_type, 'boolean')

    def test_create_pillar_secret(self):
        pillar = self.SaltPillar.create({
            'key': 'test.secret',
            'value': 'super-secret-value',
            'data_type': 'secret',
        })
        self.assertEqual(pillar.data_type, 'secret')

    def test_duplicate_pillar(self):
        pillar = self.SaltPillar.create({
            'key': 'test.dup',
            'value': '1',
        })
        pillar.action_duplicate()
        count = self.SaltPillar.search_count([('key', 'ilike', 'test.dup%')])
        self.assertEqual(count, 2)

    def test_invalid_json_value(self):
        with self.assertRaises(ValidationError):
            self.SaltPillar.create({
                'key': 'test.badjson',
                'value': '{not valid json}',
                'data_type': 'json',
            })


@tagged('post_install', '-at_install')
class TestMinionOverview(TransactionCase):
    """Minion overview: IP extraction, semaphore, sync guard, partner lookup."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.SaltMinion = cls.env['salt.minion']
        cls.Alert = cls.env['saltstack.alert']

    def test_extract_ips_prefers_management_network(self):
        grains = {
            'ip4_interfaces': {
                'lo': ['127.0.0.1'],
                'lxdbr0': ['10.204.124.1'],
                'eth0': ['192.168.11.7'],
            },
            'fqdn_ip4': ['185.39.146.152', '192.168.11.7'],
        }
        private, public = self.SaltMinion._extract_ips(grains)
        self.assertEqual(private, '192.168.11.7')
        self.assertEqual(public, '185.39.146.152')

    def test_extract_ips_public_only(self):
        grains = {'fqdn_ip4': ['185.39.146.152']}
        private, public = self.SaltMinion._extract_ips(grains)
        self.assertEqual(private, '')
        self.assertEqual(public, '185.39.146.152')

    def test_apply_grains_sets_partner_by_name(self):
        partner = self.env['res.partner'].create({'name': 'Vertel Test AB'})
        minion = self.SaltMinion.create({'name': 'test-partner-1'})
        minion._apply_grains({'customer': 'Vertel Test AB', 'roles': 'odoo'})
        self.assertEqual(minion.partner_id, partner)

    def test_apply_grains_partner_unmatched(self):
        minion = self.SaltMinion.create({'name': 'test-nopartner-1'})
        minion._apply_grains({'customer': 'Ingen Sådan Kund'})
        self.assertFalse(minion.partner_id)
        self.assertEqual(minion.customer, 'Ingen Sådan Kund')

    def test_state_faulty_when_open_alert(self):
        minion = self.SaltMinion.create({
            'name': 'test-faulty-1',
            'last_seen': fields.Datetime.now(),
        })
        minion._compute_state()
        self.assertEqual(minion.state, 'online')
        alert = self.Alert.create({
            'host': minion.id,
            'trigger_name': 'Test trigger',
            'resolved': False,
        })
        minion._update_open_alert_count()
        self.assertEqual(minion.open_alert_count, 1)
        self.assertEqual(minion.state, 'faulty')
        alert.action_mark_resolved()
        minion.invalidate_recordset()
        self.assertEqual(minion.open_alert_count, 0)
        self.assertEqual(minion.state, 'online')

    def test_state_offline_when_not_seen(self):
        minion = self.SaltMinion.create({'name': 'test-offline-1'})
        self.assertEqual(minion.state, 'offline')

    def test_sync_refuses_empty_result(self):
        """An empty manage.status result must not deactivate the registry."""
        minion = self.SaltMinion.create({'name': 'test-keep-1'})
        with patch('odoo.addons.saltstack.models.salt_minion.SaltMinion.'
                   '_call_salt_api', return_value={'return': [{}]}):
            result = minion.action_sync_all_minions()
        self.assertIn('error', result)
        minion.invalidate_recordset()
        self.assertTrue(minion.active)

    def test_sync_marks_up_minions_seen(self):
        minion = self.SaltMinion.create({'name': 'test-up-1'})
        with patch('odoo.addons.saltstack.models.salt_minion.SaltMinion.'
                   '_call_salt_api') as mock:
            mock.side_effect = [
                {'return': [{'up': ['test-up-1'], 'down': []}]},
                {'return': [{'test-up-1': {'os': 'Ubuntu', 'roles': 'odoo'}}]},
            ]
            result = minion.action_sync_all_minions()
        self.assertEqual(result['created'], 0)
        self.assertGreaterEqual(result['updated'], 1)
        minion.invalidate_recordset()
        self.assertTrue(minion.last_seen)
        self.assertEqual(minion.state, 'online')

    def test_logo_asset_loaded(self):
        minion = self.SaltMinion.create({'name': 'test-logo-1', 'roles': 'odoo'})
        minion._set_default_image()
        self.assertTrue(minion.image, 'Odoo logo asset should be set')

    def test_show_alerts_action(self):
        minion = self.SaltMinion.create({'name': 'test-alerts-1'})
        action = minion.action_show_alerts()
        self.assertEqual(action['res_model'], 'saltstack.alert')
        self.assertEqual(action['domain'], [('host', '=', 'test-alerts-1')])


@tagged('post_install', '-at_install')
class TestStateThreshold(TransactionCase):
    """T/12322 — the online semaphore must follow the sync interval.

    `last_seen` is written only by the daily minion sync, so a hardcoded 1 h
    window marks every healthy minion `offline` for ~23 of 24 hours. These
    tests pin the window to `cron.task` (`minion_sync`) instead.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.SaltMinion = cls.env['salt.minion']
        cls.CronTask = cls.env['cron.task'].sudo()

    def _task(self):
        return self.CronTask.search(
            [('technical_name', '=', 'minion_sync')], limit=1)

    def test_sync_interval_read_from_cron_task(self):
        """The interval comes from the task, not from a system parameter."""
        task = self._task()
        self.assertTrue(task, 'minion_sync must exist in cron_tick data')
        self.assertEqual(task.interval_number, 24)
        self.assertEqual(task.interval_type, 'hours')
        self.assertAlmostEqual(
            self.SaltMinion._sync_interval_hours(), 24.0)

    def test_threshold_is_interval_times_factor(self):
        """Window = max(1 h, 1.5 * 24 h) = 36 h for the default schedule."""
        self.assertAlmostEqual(
            self.SaltMinion._state_threshold_hours(), 36.0)

    def test_threshold_never_below_one_hour(self):
        """A tight interval must not collapse the window below 1 h."""
        task = self._task()
        task.write({'interval_number': 1, 'interval_type': 'minutes'})
        self.assertAlmostEqual(
            self.SaltMinion._state_threshold_hours(), 1.0)

    def test_threshold_follows_a_changed_interval(self):
        """Raising the interval raises the window — no stale hardcoding."""
        task = self._task()
        task.write({'interval_number': 48, 'interval_type': 'hours'})
        self.assertAlmostEqual(
            self.SaltMinion._state_threshold_hours(), 72.0)

    def test_missing_task_falls_back_to_24_hours(self):
        """A missing task must not collapse the window to 1 h."""
        self._task().unlink()
        self.assertAlmostEqual(
            self.SaltMinion._sync_interval_hours(), 24.0)
        self.assertAlmostEqual(
            self.SaltMinion._state_threshold_hours(), 36.0)

    def test_state_online_within_window(self):
        """A minion seen 20 h ago is online under a 36 h window."""
        from datetime import datetime, timedelta
        minion = self.SaltMinion.create({
            'name': 'test-threshold-online',
            'last_seen': datetime.now() - timedelta(hours=20),
        })
        self.assertEqual(minion.state, 'online')

    def test_state_offline_outside_window(self):
        """A minion seen 40 h ago is offline under a 36 h window."""
        from datetime import datetime, timedelta
        minion = self.SaltMinion.create({
            'name': 'test-threshold-offline',
            'last_seen': datetime.now() - timedelta(hours=40),
        })
        self.assertEqual(minion.state, 'offline')

    def test_state_faulty_beats_online(self):
        """An unresolved alert wins over a fresh last_seen."""
        from datetime import datetime
        minion = self.SaltMinion.create({
            'name': 'test-threshold-faulty',
            'last_seen': datetime.now(),
            'open_alert_count': 1,
        })
        self.assertEqual(minion.state, 'faulty')
