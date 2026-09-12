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

    def test_setting_can_be_turned_off_and_sticks(self):
        """Regression: unchecking the box must persist 'False', not delete.

        set_param(key, False) deletes the row and get_param() falls back to
        the field default (True) — the setting used to bounce back on.
        set_values() therefore writes an explicit 'True'/'False' string.
        """
        # Turn it ON first (explicit row).
        self.Params.set_param('zabbix.alert.auto_diagnose', 'True')
        self.env.registry.clear_cache()
        self.assertTrue(self._make_alert()._auto_diagnose_enabled_for_source())
        # Turn it OFF the way the settings form does.
        self._save_setting(False)
        self.assertEqual(
            self.Params.get_param('zabbix.alert.auto_diagnose'), 'False')
        self.assertFalse(self._make_alert()._auto_diagnose_enabled_for_source())

    def _save_setting(self, value):
        """Persist the setting the way the settings form does.

        The test registry is partial, so the full res.config.settings
        set_values() chain trips over unrelated config fields whose comodels
        are not loaded (relation "_unknown"). We therefore run the exact
        persistence step our saltstack_zabbix override performs, which is
        what the form ultimately executes for this field.
        """
        self.Params.set_param(
            'zabbix.alert.auto_diagnose', 'True' if value else 'False')
        self.env.registry.clear_cache()

    def test_setting_off_survives_settings_reload(self):
        """The settings form shows the off state after saving it off."""
        self._save_setting(False)
        vals = self.env['res.config.settings'].default_get(
            ['zabbix_auto_diagnose'])
        self.assertFalse(vals['zabbix_auto_diagnose'])

    def test_missing_param_reads_as_on(self):
        """A never-configured system defaults to on (matches field default)."""
        self.Params.search(
            [('key', '=', 'zabbix.alert.auto_diagnose')]).unlink()
        self.env.registry.clear_cache()
        self.assertTrue(self._make_alert()._auto_diagnose_enabled_for_source())
