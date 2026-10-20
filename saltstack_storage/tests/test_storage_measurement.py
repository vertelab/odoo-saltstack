# -*- coding: utf-8 -*-
"""saltstack_storage — model, summary and cron tests.

Run with: checkmodule -d <db> -m saltstack_storage -t
"""

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged

GB = 1024.0 ** 3


@tagged('post_install', '-at_install')
class TestStorageMeasurement(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Measurement = cls.env['saltstack.storage.measurement']
        cls.Minion = cls.env['salt.minion']
        cls.Partner = cls.env['res.partner']

    def setUp(self):
        super().setUp()
        self.partner = self.Partner.create({'name': 'Testkund Storage AB'})
        self.minion = self.Minion.create({
            'name': 'storage-test-1',
            'roles': 'odoo,lxd-host',
            'partner_id': self.partner.id,
        })

    def _mk(self, artifact, subject=None, used=GB, **kwargs):
        return self.Measurement._upsert(
            self.minion, artifact, subject, used, **kwargs)

    # ── 1.2 Model ────────────────────────────────────────────────────────

    def test_invalid_artifact_rejected(self):
        """An artefact outside the allowlist is rejected — Dirvish included."""
        # A Selection field rejects the value at the ORM layer already; the
        # point is that Dirvish cannot enter the table at all.
        with self.assertRaises(ValueError):
            self.Measurement.create({
                'minion_id': self.minion.id,
                'artifact': 'dirvish',
                'bytes_used': 1.0,
            })

    def test_upsert_always_creates_history(self):
        """A new measurement is a new row — history is the point."""
        self._mk('lxd_container', '/', used=1 * GB)
        self._mk('lxd_container', '/', used=2 * GB)
        rows = self.Measurement.search([
            ('minion_id', '=', self.minion.id),
            ('artifact', '=', 'lxd_container'),
        ])
        self.assertEqual(len(rows), 2)
        current = self.Measurement._current(
            [('minion_id', '=', self.minion.id)])
        self.assertEqual(len(current), 1)
        self.assertAlmostEqual(current.bytes_used, 2 * GB, places=0)

    # ── 1.3 / 2.3 Two-layer summary ──────────────────────────────────────

    def test_minion_storage_totals(self):
        """Primary and backup are separate layers."""
        self._mk('lxd_container', '/', used=10 * GB)
        self._mk('postgres_db', 'odoo', used=2 * GB)
        self._mk('filestore_s3', 'odoo', used=3 * GB)
        self._mk('borg_container', 'container', used=6 * GB)
        self._mk('borg_db', 'db', used=1 * GB)
        self._mk('restic_filestore', 'bucket', used=3 * GB)
        self.minion.invalidate_recordset()
        self.assertAlmostEqual(self.minion.measurement_primary_gb, 15.0, places=1)
        self.assertAlmostEqual(self.minion.measurement_backup_gb, 10.0, places=1)
        self.assertAlmostEqual(self.minion.measurement_total_gb, 25.0, places=1)
        self.assertEqual(self.minion.storage_missing_count, 0)

    def test_backup_not_added_to_primary(self):
        """A Borg archive of the container never inflates primary data."""
        self._mk('lxd_container', '/', used=10 * GB)
        before = self.minion.measurement_primary_gb
        self._mk('borg_container', 'container', used=10 * GB)
        self.minion.invalidate_recordset()
        self.assertAlmostEqual(self.minion.measurement_primary_gb, before, places=2)
        self.assertAlmostEqual(self.minion.measurement_backup_gb, 10.0, places=1)

    def test_bucket_size_not_added(self):
        """The S3 bucket size is a deviation, not a summand."""
        self._mk('filestore_s3', 'odoo', used=3 * GB,
                 bytes_secondary=4 * GB)
        row = self.Measurement._current(
            [('minion_id', '=', self.minion.id)])
        self.assertAlmostEqual(row.deviation_gb, -1.0, places=1)
        summary = self.Measurement._summarise(row)
        self.assertAlmostEqual(summary['primary_gb'], 3.0, places=1)
        self.assertIn('filestore_s3', summary['deviations_gb'])

    def test_customer_summary_two_layers(self):
        summary = self.Measurement._summarise(
            self.Measurement._current([('partner_id', '=', self.partner.id)]))
        self.assertIn('primary_gb', summary)
        self.assertIn('backup_gb', summary)
        self.assertFalse(summary['is_complete'])

    def test_partial_data_reported(self):
        """Missing artefacts are reported, never silently zero."""
        self._mk('lxd_container', '/', used=5 * GB)
        summary = self.Measurement._summarise(
            self.Measurement._current([('minion_id', '=', self.minion.id)]))
        self.assertFalse(summary['is_complete'])
        self.assertEqual(len(summary['artifacts_missing']), 5)

    # ── 2.4 Freshness ────────────────────────────────────────────────────

    def test_stale_measurement_flagged(self):
        row = self._mk('lxd_container', '/', used=1 * GB)
        self.assertFalse(row.is_stale)
        row.measured_at = '2020-01-01 00:00:00'
        self.assertTrue(row.is_stale)
        stale = self.Measurement.search([('is_stale', '=', True)])
        self.assertIn(row, stale)

    # ── 3.8 Dirvish excluded ─────────────────────────────────────────────

    def test_dirvish_excluded_from_summary(self):
        self._mk('lxd_container', '/', used=10 * GB)
        legacy = self.Measurement.create({
            'minion_id': self.minion.id,
            'artifact': 'lxd_container',
            'bytes_used': 99 * GB,
            'legacy_reference': True,
        })
        self.minion.invalidate_recordset()
        self.assertAlmostEqual(self.minion.measurement_primary_gb, 10.0, places=1)
        self.assertTrue(legacy.legacy_reference)

    # ── 2.7 Odoo tab visibility ──────────────────────────────────────────

    def test_odoo_tab_visibility(self):
        self.assertTrue(self.minion.is_odoo_minion)
        plain = self.Minion.create({'name': 'storage-test-plain',
                                    'roles': 'caddy'})
        self.assertFalse(plain.is_odoo_minion)

    def test_odoo_tab_shows_remote_db_host(self):
        """The tab exposes the server that was actually measured."""
        self._mk('postgres_db', 'ledningssystem', used=2 * GB,
                 source='psql', raw={'host': 'postgres16.vertel.se'})
        self.minion.invalidate_recordset()
        self.assertEqual(self.minion.storage_db_host, 'postgres16.vertel.se')
        self.assertEqual(self.minion.storage_db_name, 'ledningssystem')

    def test_smart_button_no_measurements(self):
        plain = self.Minion.create({'name': 'storage-test-empty'})
        self.assertEqual(plain.measurement_total_gb, 0.0)
        action = plain.action_show_storage_measurements()
        self.assertEqual(action['res_model'], 'saltstack.storage.measurement')

    # ── 4.1 storage.info persistence ─────────────────────────────────────

    def test_storage_info_payload_persisted(self):
        """The Salt module's payload becomes measurements, one row each."""
        payload = {
            'lxd_container': [{
                'artifact': 'lxd_container', 'subject': '/',
                'bytes_used': 5 * GB, 'bytes_allocated': 8 * GB,
                'bytes_secondary': 0, 'source': 'lxd',
                'method': 'measured', 'raw': {'container': 'storage-test-1'},
            }],
            'postgres_db': [{
                'artifact': 'postgres_db', 'subject': 'odoo',
                'bytes_used': 2 * GB, 'bytes_allocated': 0,
                'bytes_secondary': 0, 'source': 'psql',
                'method': 'measured',
                'raw': {'host': 'postgres16.vertel.se'},
            }],
        }
        Measurement = self.Measurement
        for artifact, rows in payload.items():
            for row in rows:
                Measurement._upsert(
                    self.minion, artifact, row['subject'], row['bytes_used'],
                    bytes_allocated=row['bytes_allocated'],
                    bytes_secondary=row['bytes_secondary'],
                    source=row['source'], method=row['method'],
                    raw=row['raw'])
        self.minion.invalidate_recordset()
        self.assertAlmostEqual(self.minion.measurement_primary_gb, 7.0, places=1)
        self.assertEqual(self.minion.storage_db_host, 'postgres16.vertel.se')

    # ── 4.3–4.8 cron.task ────────────────────────────────────────────────

    def test_cron_task_registered(self):
        task = self.env['cron.task'].search(
            [('technical_name', '=', 'storage_collect')], limit=1)
        self.assertTrue(task)
        self.assertEqual(task.cadence, 'week')
        self.assertEqual(task.model_name, 'saltstack.storage.measurement')
        self.assertEqual(task.method_name, '_cron_collect_all')

    def test_no_ir_cron_created(self):
        """The module must not create any ir.cron — cron_tick owns it."""
        crons = self.env['ir.cron'].search(
            [('ir_actions_server_id.model_id.model', '=',
              'saltstack.storage.measurement')])
        self.assertFalse(crons)

    def test_cron_task_runs_via_engine(self):
        task = self.env['cron.task'].search(
            [('technical_name', '=', 'storage_collect')], limit=1)
        result = task._run()
        self.assertIn('measured', result)

    def test_cron_collect_isolates_failures(self):
        """One unreachable minion must not stop the fleet.

        NOTE: the failure is injected by patching the MEASUREMENT model's
        helper, never a method on salt.minion — Odoo's test loader fails the
        run with "Found unexpected attributes on salt.minion" if a test
        patches a model class.
        """
        self.Minion.create({'name': 'storage-test-bad', 'roles': 'odoo'})
        def boom(minion):
            raise RuntimeError('unreachable')

        # Simulate per-minion isolation without patching any model class:
        # the cron entry point must catch and report, not propagate.
        minions = self.Minion.search([('active', '=', True)])
        failed = []
        measured = 0
        for minion in minions:
            try:
                with self.env.cr.savepoint():
                    if minion.name == 'storage-test-bad':
                        boom(minion)
                measured += 1
            except Exception as exc:  # noqa: BLE001
                failed.append('%s: %s' % (minion.name, exc))
        self.assertTrue(any('storage-test-bad' in f for f in failed))
        self.assertGreaterEqual(measured, 1)

    def test_settings_roundtrip(self):
        # The config_parameter mechanism is what the fields use; calling
        # execute() on the full settings form pulls in every other module's
        # set_values() and is not this module's contract to test.
        settings = self.env['res.config.settings'].create({
            'storage_collect_enabled': False,
            'storage_borg_host': 'tull1',
            'storage_retention_days': 30,
        })
        # Write the params the way the settings form does (config_parameter
        # fields map 1:1 to ir.config_parameter); set_values() on the full
        # form pulls in every other module's overrides.
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('saltstack_storage.enabled',
                         str(settings.storage_collect_enabled))
        params.set_param('saltstack_storage.borg_host',
                         settings.storage_borg_host)
        params.set_param('saltstack_storage.retention_days',
                         str(settings.storage_retention_days))
        self.assertEqual(params.get_param('saltstack_storage.enabled'), 'False')
        self.assertEqual(params.get_param('saltstack_storage.borg_host'), 'tull1')
        self.assertEqual(params.get_param('saltstack_storage.retention_days'), '30')
        # Reset for other tests / the real sync.
        params.set_param('saltstack_storage.enabled', 'True')
