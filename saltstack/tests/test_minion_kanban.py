# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""saltstack — tests for the minion kanban card design.

Run with: checkmodule -d <db> -m saltstack -t
Covers: the three-tier card hierarchy, the fields that must NOT appear on
the card, the alert counter that must, the default grouping, and the
state-column order faulty > offline > online.

Why: the visual weight of the card is a spec contract, but it lives only in
XML and SCSS. Without these tests a future field addition could put IP
addresses back on the card, or flip the column order, without breaking
anything anyone would notice.
"""

import re

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMinionKanbanCard(TransactionCase):
    """The kanban arch obeys the element hierarchy."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.view = cls.env.ref('saltstack.view_salt_minion_kanban')
        cls.arch = cls.view.arch

    def _kanban_arch(self):
        """Return only the <kanban> block, so form/list fields don't leak in."""
        m = re.search(r'<kanban\b.*?</kanban>', self.arch, re.S)
        self.assertTrue(m, 'No <kanban> block found in the minion view')
        return m.group(0)

    def test_tier_layers_present(self):
        """The card is split into identity / attention / meta layers."""
        arch = self._kanban_arch()
        for layer in ('o_minion_card__identity',
                      'o_minion_card__attention',
                      'o_minion_card__meta'):
            self.assertIn(layer, arch,
                          'Card is missing the %s layer' % layer)

    def test_tier3_fields_not_on_card(self):
        """IP addresses and the demo-data flag stay off the card."""
        arch = self._kanban_arch()
        for fname in ('private_ip', 'public_ip', 'odoo_has_demo_data'):
            self.assertNotIn(
                'name="%s"' % fname, arch,
                '%s must not be declared on the kanban card' % fname)

    def test_alert_count_on_card(self):
        """The unresolved alert counter is on the card."""
        arch = self._kanban_arch()
        self.assertIn('name="open_alert_count"', arch,
                      'open_alert_count must be on the kanban card')

    def test_default_group_by_is_dc(self):
        """The board opens grouped by datacenter."""
        self.assertIn('default_group_by="dc"', self._kanban_arch(),
                      'Kanban must default to grouping by dc')

    def test_card_drilldown_action(self):
        """The alert counter opens the host-filtered alert list."""
        arch = self._kanban_arch()
        self.assertIn('action_show_alerts', arch,
                      'The alert counter must call action_show_alerts')

    def test_state_grouping_available(self):
        """Search view offers grouping by dc and state."""
        search = self.env.ref('saltstack.view_salt_minion_search').arch
        self.assertIn("'group_by':'dc'", search.replace(' ', ''),
                      'Search view must offer grouping by dc')
        self.assertIn("'group_by':'state'", search.replace(' ', ''),
                      'Search view must offer grouping by state')


@tagged('post_install', '-at_install')
class TestMinionStateGroupOrder(TransactionCase):
    """State columns are ordered faulty > offline > online."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Minion = cls.env['salt.minion']

    def test_group_expand_priority_order(self):
        """group_expand returns the states in priority order."""
        values = self.Minion._group_expand_state([], [])
        self.assertEqual(
            list(values), ['faulty', 'offline', 'online'],
            'State columns must be faulty > offline > online, got %s'
            % (values,))

    def test_group_expand_returns_all_states(self):
        """All three columns stay visible even when empty."""
        self.assertEqual(len(self.Minion._group_expand_state([], [])), 3)

    def test_read_group_uses_priority_order(self):
        """Grouping on state yields the priority order, not selection order.

        Goes through read_group, not _read_group: group_expand is applied by
        _read_group_fill_results, which read_group calls (in lazy mode) and
        _read_group does not. Testing _read_group would assert on an
        intermediate result that never reaches the UI.
        """
        rows = self.Minion.read_group(
            [], ['state'], ['state'])
        values = [r['state'] for r in rows]
        self.assertEqual(
            values, ['faulty', 'offline', 'online'],
            'Grouped state columns came back as %s' % (values,))
