# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""saltstack — tests for the Driftslogg (saltstack.runlog) webhook model.

Run with: checkmodule -d <db> -m saltstack -t
Covers: process_webhook payload handling, status/run_type fallback, and
timestamp parsing.
"""

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestRunlogWebhook(TransactionCase):
    """saltstack.runlog.process_webhook()"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Runlog = cls.env['saltstack.runlog']

    def _publish(self, **kw):
        payload = {
            'source': 'restic',
            'run_type': 'backup',
            'host': 'restic',
            'status': 'ok',
            'summary': '16 kunder — 4 OK / 12 WARNING / 0 ERROR',
            'raw_log': 'Kund              Källa     Status\n'
                       'immich            10.4 G    OK',
            'timestamp': '2026-08-12T06:15:00Z',
        }
        payload.update(kw)
        return self.Runlog.process_webhook(payload)

    def test_create_ok(self):
        """A normal payload creates a runlog with all fields."""
        res = self._publish()
        self.assertEqual(res['status'], 'ok')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertEqual(rec.source, 'restic')
        self.assertEqual(rec.run_type, 'backup')
        self.assertEqual(rec.host, 'restic')
        self.assertEqual(rec.status, 'ok')
        self.assertIn('16 kunder', rec.summary)
        self.assertIn('immich', rec.raw_log)
        self.assertIn('source', rec.json_payload)

    def test_invalid_status_falls_back(self):
        """Unknown status values fall back to 'ok'."""
        res = self._publish(status='bogus')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertEqual(rec.status, 'ok')

    def test_invalid_run_type_falls_back(self):
        """Unknown run_type values fall back to 'other'."""
        res = self._publish(run_type='nightly-magic')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertEqual(rec.run_type, 'other')

    def test_invalid_source_falls_back(self):
        """Unknown source values fall back to 'other'."""
        res = self._publish(source='ufo')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertEqual(rec.source, 'other')

    def test_timestamp_parsed(self):
        """ISO timestamp is parsed into a naive Odoo datetime."""
        res = self._publish(timestamp='2026-08-12T06:15:00Z')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertEqual(rec.timestamp.strftime('%Y-%m-%d %H:%M'),
                         '2026-08-12 06:15')

    def test_missing_host_not_fatal(self):
        """A missing host is accepted (log entry without host is fine)."""
        res = self._publish(host='')
        self.assertEqual(res['status'], 'ok')

    def test_order_newest_first(self):
        """_order = timestamp desc — newest log comes first."""
        older = self._publish(timestamp='2026-08-10T06:15:00Z', summary='äldre')
        newer = self._publish(timestamp='2026-08-12T06:15:00Z', summary='nyare')
        # Scope the search to THIS test's records: the database also holds
        # real runlog rows (Dirvish/Restic) whose timestamps would otherwise
        # win the ordering assertion.
        ids = [older['runlog_id'], newer['runlog_id']]
        first = self.Runlog.search([('id', 'in', ids)], limit=1)
        self.assertEqual(first.id, newer['runlog_id'])
        self.assertEqual(first.summary, 'nyare')
        last = self.Runlog.search([('id', 'in', ids)], order='timestamp asc', limit=1)
        self.assertEqual(last.id, older['runlog_id'])

    # ── Minion-koppling ────────────────────────────────────────────────

    def test_minion_linked_from_host(self):
        """A published run is linked to the minion matching its host."""
        minion = self.env['salt.minion'].create({'name': 'asterisk'})
        res = self._publish(host='asterisk')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertEqual(rec.minion_id, minion)

    def test_minion_unknown_host_leaves_link_empty(self):
        """An unknown host does not break the webhook — minion_id stays empty."""
        res = self._publish(host='no-such-minion-xyz')
        self.assertEqual(res['status'], 'ok')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertFalse(rec.minion_id)
        self.assertEqual(rec.host, 'no-such-minion-xyz')

    def test_backfill_minion_id(self):
        """Backfill sets minion_id on existing records from their host."""
        minion = self.env['salt.minion'].create({'name': 'asterisk-backfill'})
        rec = self.Runlog.create({
            'host': 'asterisk-backfill',
            'source': 'other',
            'run_type': 'other',
            'status': 'ok',
        })
        self.assertFalse(rec.minion_id)
        self.Runlog._backfill_minion_id()
        self.assertEqual(rec.minion_id, minion)

    def test_backfill_is_idempotent(self):
        """A second backfill run changes nothing."""
        minion = self.env['salt.minion'].create({'name': 'asterisk-idem'})
        rec = self.Runlog.create({
            'host': 'asterisk-idem',
            'source': 'other',
            'run_type': 'other',
            'status': 'ok',
        })
        self.Runlog._backfill_minion_id()
        self.assertEqual(rec.minion_id, minion)
        # Second run: no records left with an empty minion_id for this host
        self.Runlog._backfill_minion_id()
        self.assertEqual(rec.minion_id, minion)

    # ── Manuella poster ────────────────────────────────────────────────

    def test_manual_source_and_change_type_accepted(self):
        """The manual/change selection values are valid."""
        res = self._publish(source='manual', run_type='change',
                            summary='pbx.logrotate — maxsize 500M')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertEqual(rec.source, 'manual')
        self.assertEqual(rec.run_type, 'change')

    def test_minion_runlog_count_and_actions(self):
        """A minion counts its runlogs and can open/add them."""
        minion = self.env['salt.minion'].create({'name': 'asterisk-ui'})
        self.assertEqual(minion.runlog_count, 0)
        self.Runlog.create({
            'host': 'asterisk-ui',
            'minion_id': minion.id,
            'source': 'manual',
            'run_type': 'change',
            'status': 'ok',
            'summary': 'test',
        })
        self.assertEqual(minion.runlog_count, 1)
        action = minion.action_view_runlogs()
        self.assertEqual(action['res_model'], 'saltstack.runlog')
        self.assertIn(('minion_id', '=', minion.id), action['domain'])
        add = minion.action_add_runlog()
        self.assertEqual(add['context']['default_source'], 'manual')
        self.assertEqual(add['context']['default_run_type'], 'change')
        self.assertEqual(add['context']['default_minion_id'], minion.id)
