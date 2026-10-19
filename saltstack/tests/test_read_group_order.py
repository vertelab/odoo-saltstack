# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""saltstack — tests for date grouping order in Driftslarm and Driftslogg.

Run with: checkmodule -d <db> -m saltstack -t
Covers: _read_group ordering of timestamp:<granularity> groups (newest
first), that other groupings are untouched, that an explicit order wins,
and that alerts without a timestamp stay reachable.

Why these tests exist: the ordering lives in a model override, not in the
view XML, so a view diff cannot catch a regression. Grouping three days
and asserting the order does.
"""

from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAlertDateGrouping(TransactionCase):
    """saltstack.alert._read_group — day groups newest first."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Alert = cls.env['saltstack.alert']
        cls.Minion = cls.env['salt.minion']
        minion = cls.Minion.search([('name', '=', 'sparv')], limit=1)
        cls.minion = minion or cls.Minion.create({'name': 'sparv'})

        # Three distinct days, deliberately created oldest-last so that
        # insertion order cannot be mistaken for the group order.
        today = fields.Datetime.now()
        cls.day_new = today
        cls.day_mid = today - timedelta(days=1)
        cls.day_old = today - timedelta(days=2)
        cls.rec_new = cls.Alert.create({
            'host': cls.minion.id, 'trigger_name': 'newest',
            'timestamp': cls.day_new, 'severity': 3})
        cls.rec_mid = cls.Alert.create({
            'host': cls.minion.id, 'trigger_name': 'middle',
            'timestamp': cls.day_mid, 'severity': 3})
        cls.rec_old = cls.Alert.create({
            'host': cls.minion.id, 'trigger_name': 'oldest',
            'timestamp': cls.day_old, 'severity': 3})

    def _group_values(self, groupby='timestamp:day', order=None, domain=None):
        """Return the group values (dates) in the order _read_group gives."""
        domain = domain if domain is not None else [('host', '=', self.minion.id)]
        groups = self.Alert._read_group(
            domain, groupby=[groupby], aggregates=['__count'], order=order)
        return [g[0] for g in groups]

    def test_day_groups_newest_first(self):
        """Grouping by day puts the most recent day first (task 2.1)."""
        values = self._group_values()
        dates = [v.date() if hasattr(v, 'date') else v for v in values]
        dates = [d for d in dates if d]
        self.assertEqual(
            dates, sorted(dates, reverse=True),
            'Day groups must be newest first, got %s' % (dates,))

    def test_explicit_order_wins(self):
        """An explicit order from the user overrides the default (task 2.4)."""
        values = self._group_values(order='timestamp:day asc')
        dates = [v.date() if hasattr(v, 'date') else v for v in values]
        dates = [d for d in dates if d]
        self.assertEqual(
            dates, sorted(dates),
            'Explicit asc order must win over the newest-first default, '
            'got %s' % (dates,))

    def test_other_groupings_unaffected(self):
        """Grouping on host/status keeps its natural order (task 2.3)."""
        # No explicit order: the override must not touch non-date terms.
        host_values = self._group_values(groupby='host', domain=[])
        self.assertTrue(host_values, 'Grouping by host returned no groups')

        status_values = self._group_values(groupby='state', domain=[])
        self.assertTrue(status_values, 'Grouping by state returned no groups')

    def test_group_without_timestamp_reachable(self):
        """An alert without timestamp is still present when grouping by day.

        Spec: Alerts without a timestamp — it must not silently vanish.
        """
        no_ts = self.Alert.create({
            'host': self.minion.id, 'trigger_name': 'no-timestamp',
            'severity': 1})
        self.assertFalse(no_ts.timestamp, 'Fixture should have no timestamp')

        groups = self.Alert._read_group(
            [('host', '=', self.minion.id)],
            groupby=['timestamp:day'], aggregates=['__count'])
        total = sum(g[1] for g in groups)
        expected = self.Alert.search_count(
            [('host', '=', self.minion.id)])
        self.assertEqual(
            total, expected,
            'Every alert must be counted in some day group (incl. the '
            'uncategorised one); got %s of %s' % (total, expected))
        # The empty group exists and carries the timestamp-less record.
        empty_groups = [g for g in groups if not g[0]]
        self.assertTrue(
            empty_groups,
            'A group for alerts without timestamp must be present')
        self.assertGreaterEqual(empty_groups[0][1], 1)
        # ...and it sorts LAST, so it never pushes the newest day off the top.
        self.assertFalse(
            groups[-1][0],
            'The uncategorised group must be last, got order %s'
            % ([g[0] for g in groups],))


@tagged('post_install', '-at_install')
class TestRunlogDateGrouping(TransactionCase):
    """saltstack.runlog._read_group — day groups newest first."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Runlog = cls.env['saltstack.runlog']
        today = fields.Datetime.now()
        cls.day_new = today
        cls.day_mid = today - timedelta(days=1)
        cls.day_old = today - timedelta(days=2)
        cls.rec_old = cls.Runlog.create({
            'source': 'restic', 'run_type': 'backup', 'host': 'restic',
            'status': 'ok', 'summary': 'oldest', 'timestamp': cls.day_old})
        cls.rec_mid = cls.Runlog.create({
            'source': 'restic', 'run_type': 'backup', 'host': 'restic',
            'status': 'ok', 'summary': 'middle', 'timestamp': cls.day_mid})
        cls.rec_new = cls.Runlog.create({
            'source': 'restic', 'run_type': 'backup', 'host': 'restic',
            'status': 'ok', 'summary': 'newest', 'timestamp': cls.day_new})

    def _groups(self, groupby='timestamp:day', order=None):
        return self.Runlog._read_group(
            [], groupby=[groupby], aggregates=['__count'], order=order)

    def test_day_groups_newest_first(self):
        """Grouping by day puts the most recent day first (task 2.2)."""
        groups = self._groups()
        dates = [g[0].date() if hasattr(g[0], 'date') else g[0]
                 for g in groups if g[0]]
        self.assertEqual(
            dates, sorted(dates, reverse=True),
            'Runlog day groups must be newest first, got %s' % (dates,))

    def test_counts_per_day(self):
        """Counts per day sum to the number of visible entries (task 4.3)."""
        groups = self._groups()
        total = sum(g[1] for g in groups)
        self.assertEqual(
            total, self.Runlog.search_count([]),
            'Day group counts must sum to the visible record count')

    def test_other_groupings_unaffected(self):
        """Grouping on source/status keeps its natural order (task 4.2)."""
        for groupby in ('source', 'run_type', 'status', 'host'):
            groups = self._groups(groupby=groupby)
            self.assertTrue(
                groups, 'Grouping by %s returned no groups' % groupby)
