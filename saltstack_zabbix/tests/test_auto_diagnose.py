# -*- coding: utf-8 -*-
"""saltstack_zabbix — tests for the Zabbix-specific auto-diagnosis gate.

Run with: checkmodule -d <db> -m saltstack_zabbix -t
Covers: the 'zabbix.alert.auto_diagnose' setting and the
_auto_diagnose_enabled_for_source() override on saltstack.alert.
"""

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestZabbixAutoDiagnose(TransactionCase):
    """Source-specific gate: Zabbix alerts can opt out of AI diagnosis."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Alert = cls.env['saltstack.alert']
        cls.Minion = cls.env['salt.minion']
        cls.Params = cls.env['ir.config_parameter'].sudo()

    def setUp(self):
        super().setUp()
        # Start from the documented default (enabled) for every test.
        self.Params.set_param('zabbix.alert.auto_diagnose', 'True')

    def _make_alert(self, source='zabbix'):
        minion = self.Minion.search([('name', '=', 'sparv')], limit=1) \
            or self.Minion.create({'name': 'sparv'})
        return self.Alert.create({
            'host': minion.id,
            'source': source,
            'category': 'process',
            'severity': 12,
            'trigger_name': 'Odoo HTTP endpoint not responding',
        })

    def test_enabled_by_default(self):
        """Default setting is True → Zabbix alerts are diagnosed."""
        self.assertTrue(self._make_alert()._auto_diagnose_enabled_for_source())

    def test_disabled_for_zabbix(self):
        """Setting off → Zabbix alerts are NOT diagnosed."""
        self.Params.set_param('zabbix.alert.auto_diagnose', 'False')
        self.assertFalse(self._make_alert()._auto_diagnose_enabled_for_source())

    def test_other_sources_unaffected(self):
        """Other sources (e.g. wazuh) are never gated by the Zabbix setting."""
        self.Params.set_param('zabbix.alert.auto_diagnose', 'False')
        self.assertTrue(
            self._make_alert(source='wazuh')._auto_diagnose_enabled_for_source())

    def test_setting_field_registered(self):
        """The setting is exposed as a config_parameter-backed field."""
        field = self.env['res.config.settings']._fields['zabbix_auto_diagnose']
        self.assertEqual(field.config_parameter, 'zabbix.alert.auto_diagnose')
        self.assertTrue(field.default)
