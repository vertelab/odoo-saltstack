# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""salt.minion.storage — artefact types, two-layer sums and migration.

Run with: checkmodule -d <db> -m saltstack -t
"""

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged

GB = 1024.0 ** 3

EXPECTED_TYPES = {
    'lxd_container', 'disk', 'disk_borg', 'filestore_s3',
    'filestore_restic', 'postgres', 'postgres_borg',
}


@tagged('post_install', '-at_install')
class TestStorageTypes(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Storage = cls.env['salt.minion.storage']
        cls.Minion = cls.env['salt.minion']

    def setUp(self):
        super().setUp()
        self.minion = self.Minion.create({'name': 'storage-types-test'})

    def _mk(self, storage_type, used_gb=1.0, **kwargs):
        return self.Storage.create({
            'minion_id': self.minion.id,
            'storage_type': storage_type,
            'bytes_used': used_gb * GB,
            **kwargs,
        })

    # ── 1.1 Selection ────────────────────────────────────────────────────

    def test_selection_has_seven_types(self):
        """Exactly seven artefact types — and no Dirvish."""
        selection = dict(
            self.Storage._fields['storage_type']._description_selection(
                self.env))
        self.assertEqual(set(selection), EXPECTED_TYPES)
        self.assertEqual(len(selection), 7)
        self.assertNotIn('dirvish', selection)

    def test_dirvish_cannot_be_saved(self):
        """A retired type is rejected at the ORM layer."""
        with self.assertRaises(ValueError):
            self.Storage.create({
                'minion_id': self.minion.id,
                'storage_type': 'dirvish',
                'bytes_used': 1.0,
            })

    def test_unique_per_type(self):
        self._mk('postgres')
        with self.assertRaises(Exception):
            self._mk('postgres')

    # ── 1.2 bytes + computed size_gb ─────────────────────────────────────

    def test_size_gb_computed_from_bytes(self):
        row = self._mk('lxd_container', used_gb=25.0,
                       bytes_allocated=1100.0 * GB)
        self.assertAlmostEqual(row.size_gb, 25.0, places=2)
        self.assertAlmostEqual(row.allocated_gb, 1100.0, places=2)

    def test_size_gb_follows_bytes_on_write(self):
        row = self._mk('disk', used_gb=1.0)
        row.write({'bytes_used': 4.0 * GB})
        self.assertAlmostEqual(row.size_gb, 4.0, places=2)

    # ── 3.1 Layer classification ─────────────────────────────────────────

    def test_layer_per_type(self):
        primary = {'lxd_container', 'disk', 'postgres', 'filestore_s3'}
        backup = {'disk_borg', 'postgres_borg', 'filestore_restic'}
        for t in primary:
            self.assertEqual(self._mk(t).layer, 'primary', t)
        for t in backup:
            self.assertEqual(self._mk(t).layer, 'backup', t)

    # ── 3.1 Two-layer totals ─────────────────────────────────────────────

    def test_two_layer_totals(self):
        self._mk('lxd_container', 10.0)
        self._mk('disk', 2.0)
        self._mk('postgres', 3.0)
        self._mk('filestore_s3', 5.0)
        self._mk('disk_borg', 8.0)
        self._mk('postgres_borg', 1.0)
        self._mk('filestore_restic', 4.0)
        self.minion.invalidate_recordset()
        self.assertAlmostEqual(self.minion.storage_primary_gb, 20.0, places=1)
        self.assertAlmostEqual(self.minion.storage_backup_gb, 13.0, places=1)
        self.assertAlmostEqual(self.minion.storage_total_gb, 33.0, places=1)

    def test_backup_not_in_primary(self):
        """A Borg copy of the container never inflates primary data."""
        self._mk('lxd_container', 10.0)
        before = self.minion.storage_primary_gb
        self._mk('disk_borg', 10.0)
        self.minion.invalidate_recordset()
        self.assertAlmostEqual(self.minion.storage_primary_gb, before, places=2)
        self.assertAlmostEqual(self.minion.storage_backup_gb, 10.0, places=1)

    # ── 3.2 Legacy rows excluded ─────────────────────────────────────────

    def test_legacy_excluded_from_totals(self):
        self._mk('lxd_container', 10.0)
        self._mk('disk_borg', 99.0, legacy_reference=True, is_measured=False)
        self.minion.invalidate_recordset()
        self.assertAlmostEqual(self.minion.storage_primary_gb, 10.0, places=1)
        self.assertAlmostEqual(self.minion.storage_backup_gb, 0.0, places=1)

    # ── 2.1 / 2.2 Migration ──────────────────────────────────────────────

    def test_dirvish_becomes_legacy_reference(self):
        """A Dirvish row keeps its size and date but stops counting."""
        row = self.Storage.create({
            'minion_id': self.minion.id,
            'storage_type': 'disk_borg',
            'bytes_used': 7.0 * GB,
            'is_measured': False,
            'legacy_reference': True,
            'measured_at': '2026-01-01 00:00:00',
        })
        before = self.Storage.search_count([('minion_id', '=', self.minion.id)])
        self.Storage._migrate_legacy_types()
        after = self.Storage.search_count([('minion_id', '=', self.minion.id)])
        self.assertEqual(before, after, 'no row may be deleted')
        self.assertTrue(row.legacy_reference)
        self.assertAlmostEqual(row.size_gb, 7.0, places=2)
        self.assertEqual(str(row.measured_at)[:10], '2026-01-01')

    def test_migration_idempotent(self):
        self._mk('lxd_container', 3.0)
        self.Storage._migrate_legacy_types()
        snapshot = [
            (r.id, r.storage_type, r.legacy_reference, r.bytes_used)
            for r in self.Storage.search([('minion_id', '=', self.minion.id)])
        ]
        self.Storage._migrate_legacy_types()
        again = [
            (r.id, r.storage_type, r.legacy_reference, r.bytes_used)
            for r in self.Storage.search([('minion_id', '=', self.minion.id)])
        ]
        self.assertEqual(snapshot, again)

    # ── 4.1 Measurement writes only new types ────────────────────────────

    def test_measure_writes_new_types(self):
        """_upsert_storage normalises retired types instead of writing them."""
        self.minion._upsert_storage('lxd_host', 1.0, 'fors', 'test')
        self.minion._upsert_storage('s3', 2.0, 'bucket', 'test')
        self.minion._upsert_storage('s3_backup', 3.0, 'bucket-backup', 'test')
        self.minion._upsert_storage('dirvish', 4.0, 'dirvish', 'test')
        written = set(self.minion.storage_ids.mapped('storage_type'))
        self.assertTrue(written <= EXPECTED_TYPES, written)
        self.assertEqual(
            written,
            {'lxd_container', 'filestore_s3', 'filestore_restic', 'disk_borg'})

    def test_upsert_dirvish_becomes_legacy(self):
        self.minion._upsert_storage('dirvish', 4.0, 'dirvish', 'test')
        self.minion.invalidate_recordset()
        self.assertAlmostEqual(self.minion.storage_primary_gb, 0.0, places=2)
        self.assertAlmostEqual(self.minion.storage_backup_gb, 4.0, places=1)
