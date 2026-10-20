# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""Salt minion storage usage — one row per artefact.

The type is the ARTEFACT, not where the data happens to live. That is what
makes the Storage tab answer "what is this minion's storage made of".

Dirvish is retired (Borg + Restic replaced it) and is deliberately NOT a
type: a type that exists but must not be used is a trap. Existing Dirvish
rows are migrated to `legacy_reference` so the history survives without ever
entering a sum.
"""

import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

GB = 1024.0 ** 3

# Artefact → layer. This is the single place that decides what a sum means.
# primary = the customer's live data; backup = a COPY of the same bytes and
# therefore never added to the primary sum.
STORAGE_LAYER = {
    'lxd_container': 'primary',
    'disk': 'primary',
    'postgres': 'primary',
    'filestore_s3': 'primary',
    'disk_borg': 'backup',
    'postgres_borg': 'backup',
    'filestore_restic': 'backup',
}

# Old type → new type. Used by the migration and by _measure_storage() so a
# stale caller cannot reintroduce a retired type.
LEGACY_TYPE_MAP = {
    'lxd_host': 'lxd_container',
    'dirvish': 'disk_borg',
    's3': 'filestore_s3',
    's3_backup': 'filestore_restic',
}


class SaltMinionStorage(models.Model):
    _name = 'salt.minion.storage'
    _description = 'Salt Minion Storage Usage'
    _order = 'storage_type, id'

    minion_id = fields.Many2one(
        'salt.minion',
        string='Minion',
        required=True,
        ondelete='cascade',
        index=True,
    )
    storage_type = fields.Selection(
        selection=[
            ('lxd_container', 'LXD-container'),
            ('disk', 'Disk'),
            ('disk_borg', 'Disk-backup (borg)'),
            ('filestore_s3', 'Filestore (S3)'),
            ('filestore_restic', 'Filestore-backup (restic)'),
            ('postgres', 'Postgres'),
            ('postgres_borg', 'Postgres-backup'),
        ],
        string='Typ',
        required=True,
        help='Which artefact this row measures. Dirvish is deliberately '
             'absent: it is retired and must never enter a sum.',
    )
    layer = fields.Selection(
        selection=[
            ('primary', 'Primärdata'),
            ('backup', 'Backup'),
        ],
        string='Lager',
        compute='_compute_layer',
        store=True,
        index=True,
        readonly=True,
        help='primary = the customer\'s live data. backup = a copy of the '
             'same bytes, never added to the primary sum.',
    )
    provider = fields.Char(
        string='Källa',
        help='Source of the measurement, e.g. the LXD host name (fors), '
             'the database name, or the Garage bucket name.',
    )

    # ── Values ────────────────────────────────────────────────────────────

    bytes_used = fields.Float(
        string='Använt (byte)',
        digits=(20, 0),
        help='Actually used bytes — the number that goes into the sums.',
    )
    bytes_allocated = fields.Float(
        string='Allokerat (byte)',
        digits=(20, 0),
        help='Allocated size (container quota, disk size). Not the same as '
             'used bytes.',
    )
    size_gb = fields.Float(
        string='Storlek (GB)',
        digits=(12, 2),
        compute='_compute_size_gb',
        store=True,
        help='Derived from bytes_used, kept so existing list columns and '
             'exports keep working.',
    )
    allocated_gb = fields.Float(
        string='Allokerat (GB)',
        digits=(12, 2),
        compute='_compute_size_gb',
    )

    # ── Provenance ────────────────────────────────────────────────────────

    is_measured = fields.Boolean(
        string='Uppmätt',
        default=True,
        help='True = the number was actually measured on disk (du/df, '
             'psql, borg, restic). False = estimate.',
    )
    method = fields.Char(
        string='Metod',
        help='How the size was measured or estimated.',
    )
    measured_at = fields.Datetime(
        string='Mätt',
    )
    legacy_reference = fields.Boolean(
        string='Historisk referens',
        default=False,
        index=True,
        help='True for imported values from a retired source (e.g. Dirvish). '
             'Reference only — never included in any sum.',
    )
    note = fields.Char(string='Anteckning')

    _sql_constraints = [
        (
            'minion_type_uniq',
            'unique (minion_id, storage_type)',
            'A storage row of this type already exists for the minion.',
        ),
    ]

    # ── Computes ──────────────────────────────────────────────────────────

    @api.depends('bytes_used', 'bytes_allocated')
    def _compute_size_gb(self):
        for rec in self:
            rec.size_gb = round((rec.bytes_used or 0.0) / GB, 2)
            rec.allocated_gb = round((rec.bytes_allocated or 0.0) / GB, 2)

    @api.depends('storage_type')
    def _compute_layer(self):
        for rec in self:
            rec.layer = STORAGE_LAYER.get(rec.storage_type, 'primary')

    # ── Migration ─────────────────────────────────────────────────────────

    def _auto_init(self):
        """Run the type migration on install AND on every upgrade.

        Odoo has no per-module upgrade hook, so `_auto_init` is the hook that
        actually fires on `--update`. The migration is idempotent, which is
        what makes this safe to run on every upgrade.
        """
        res = super()._auto_init()
        try:
            self._migrate_legacy_types()
        except Exception as exc:  # noqa: BLE001 — never block an upgrade
            _logger.warning('Storage type migration failed: %s', exc)
        return res

    @api.model
    def _migrate_legacy_types(self):
        """Move retired types to their new artefact, keeping the history.

        Idempotent: rows already marked `legacy_reference` are left alone, so
        running this twice changes nothing.

        Dirvish rows become `disk_borg` with `legacy_reference=True` — the
        type is gone from the selection but the number survives as a
        reference that no sum touches.
        """
        for old_type, new_type in LEGACY_TYPE_MAP.items():
            rows = self.search([
                ('storage_type', '=', old_type),
                ('legacy_reference', '=', False),
            ])
            if not rows:
                continue
            vals = {'storage_type': new_type}
            if old_type == 'dirvish':
                vals['legacy_reference'] = True
                vals['is_measured'] = False
            rows.write(vals)
            _logger.info(
                'Storage migration: %s rows %s → %s%s',
                len(rows), old_type, new_type,
                ' (legacy reference)' if old_type == 'dirvish' else '')
        return True

    # ── Helpers ───────────────────────────────────────────────────────────

    @api.model
    def _normalise_type(self, storage_type):
        """Map a retired type to its replacement, or return it unchanged."""
        return LEGACY_TYPE_MAP.get(storage_type, storage_type)
