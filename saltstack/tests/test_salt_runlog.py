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
        self._publish(timestamp='2026-08-10T06:15:00Z', summary='äldre')
        self._publish(timestamp='2026-08-12T06:15:00Z', summary='nyare')
        first = self.Runlog.search([], limit=1)
        self.assertEqual(first.summary, 'nyare')
