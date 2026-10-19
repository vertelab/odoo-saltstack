# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""saltstack — tests for the SaltStack top-level menu order.

Run with: checkmodule -d <db> -m saltstack -t
Covers: Minions before Pillars before Konfiguration, and that the order
survives a module upgrade without duplicating or renaming menu items.

Why: the order is a spec contract (saltstack-base "SaltStack menu order"),
but it is expressed only as ``sequence`` numbers in XML. Nothing else would
catch a regression if a future view edit reordered the menu.
"""

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSaltStackMenuOrder(TransactionCase):
    """The SaltStack root menu shows Minions first."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Menu = cls.env['ir.ui.menu']
        cls.root = cls.env.ref('saltstack.menu_saltstack_root')

    def _children(self):
        """Visible children of the SaltStack root, in display order."""
        return self.Menu.search(
            [('parent_id', '=', self.root.id)], order='sequence, id')

    def test_minions_is_first(self):
        """Minions is the topmost item under the SaltStack root."""
        children = self._children()
        self.assertTrue(children, 'SaltStack root has no menu items')
        self.assertEqual(
            children[0].name, 'Minions',
            'Minions must be the first menu item, got %s'
            % ([c.name for c in children],))

    def test_minions_before_pillars(self):
        """Minions sorts before Pillars (the spec's core requirement)."""
        names = [c.name for c in self._children()]
        self.assertIn('Minions', names)
        self.assertIn('Pillars', names)
        self.assertLess(
            names.index('Minions'), names.index('Pillars'),
            'Minions must come before Pillars, got %s' % (names,))

    def test_configuration_is_last(self):
        """Konfiguration stays after the working views."""
        names = [c.name for c in self._children()]
        self.assertIn('Konfiguration', names)
        self.assertEqual(
            names[-1], 'Konfiguration',
            'Konfiguration must be last, got %s' % (names,))

    def test_sequence_values_leave_room(self):
        """Minions/Pillars use 10/20 so a future item can be inserted."""
        minions = self.Menu.search(
            [('parent_id', '=', self.root.id), ('name', '=', 'Minions')],
            limit=1)
        pillars = self.Menu.search(
            [('parent_id', '=', self.root.id), ('name', '=', 'Pillars')],
            limit=1)
        self.assertEqual(minions.sequence, 10)
        self.assertEqual(pillars.sequence, 20)

    def test_no_duplicate_items(self):
        """The upgrade must not have duplicated any menu item."""
        names = [c.name for c in self._children()]
        self.assertEqual(
            len(names), len(set(names)),
            'Duplicate menu items under SaltStack root: %s' % (names,))
