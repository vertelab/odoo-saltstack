# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import ValidationError


@tagged('post_install', '-at_install')
class TestSaltMinion(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.SaltMinion = cls.env['salt.minion']

    def test_create_minion(self):
        minion = self.SaltMinion.create({
            'name': 'test-minion-1',
            'ip': '192.168.99.99',
            'dc': 'ska',
            'roles': 'odoo,caddy',
        })
        self.assertEqual(minion.name, 'test-minion-1')
        self.assertEqual(minion.dc, 'ska')
        self.assertTrue(minion.active)

    def test_minion_name_required(self):
        with self.assertRaises(Exception):
            self.SaltMinion.create({'ip': '192.168.99.99'})

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
