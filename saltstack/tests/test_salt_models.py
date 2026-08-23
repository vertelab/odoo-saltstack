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
            'host': 'test-faulty-1',
            'trigger_name': 'Test trigger',
            'resolved': False,
        })
        minion._update_open_alert_count()
        self.assertEqual(minion.open_alert_count, 1)
        self.assertEqual(minion.state, 'faulty')
        alert.action_mark_resolved()
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
        self.assertEqual(result['created'], 1)
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
