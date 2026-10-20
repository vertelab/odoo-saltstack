# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""salt.minion extensions for storage measurement.

Adds three things to the base minion registry:

1. The measurement relation + two-layer totals (smart button).
2. The Odoo tab data: database size, filestore size, the database server
   that was ACTUALLY measured and the demo-data flag.
3. The collection hook, called from the minion sync in a try/except so a
   broken measurement can never affect status or registration.
"""

import json
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class SaltMinion(models.Model):
    _inherit = 'salt.minion'

    # ── Measurements ──────────────────────────────────────────────────────

    storage_measurement_ids = fields.One2many(
        'saltstack.storage.measurement',
        'minion_id',
        string='Lagringsmätningar',
    )
    measurement_primary_gb = fields.Float(
        string='Primärdata (GB)',
        compute='_compute_storage_layers',
        digits=(16, 2),
        help='Container + databas + filestore. The customer\'s live data.',
    )
    measurement_backup_gb = fields.Float(
        string='Backup (GB)',
        compute='_compute_storage_layers',
        digits=(16, 2),
        help='Borg + Restic. A COPY of the primary data — never added to it '
             'when the customer\'s own size is reported.',
    )
    measurement_total_gb = fields.Float(
        string='Totalt (GB)',
        compute='_compute_storage_layers',
        digits=(16, 2),
    )
    storage_measured_at = fields.Datetime(
        string='Senast mätt',
        compute='_compute_storage_layers',
    )
    storage_missing_count = fields.Integer(
        string='Saknade artefakter',
        compute='_compute_storage_layers',
        help='Number of artefacts with no measurement. A sum with missing '
             'artefacts is never presented as complete.',
    )
    storage_summary_title = fields.Char(
        string='Lagringssammanfattning',
        compute='_compute_storage_layers',
        help='Tooltip for the storage badge on the kanban card. Built here '
             'instead of in t-attf-title: OWL compiles t-attf-* to plain '
             'JavaScript, where the QWeb concatenation operator is a bitwise '
             'NOT and breaks the template.',
    )

    # ── Odoo tab ──────────────────────────────────────────────────────────

    storage_db_bytes = fields.Float(
        string='Databas (byte)',
        compute='_compute_storage_layers',
        digits=(20, 0),
    )
    storage_filestore_bytes = fields.Float(
        string='Filestore (byte)',
        compute='_compute_storage_layers',
        digits=(20, 0),
    )
    storage_bucket_bytes = fields.Float(
        string='S3-bucket (byte)',
        compute='_compute_storage_layers',
        digits=(20, 0),
        help='Actual bucket size. Shown as a deviation against the recorded '
             'attachment size — not added to the primary sum.',
    )
    storage_db_host = fields.Char(
        string='Mätt databasserver',
        compute='_compute_storage_layers',
        help='The database server the measurement actually hit. A machine '
             'can run a local PostgreSQL with stale data while Odoo uses a '
             'remote one — this field makes the difference visible.',
    )
    storage_db_name = fields.Char(
        string='Databas',
        compute='_compute_storage_layers',
    )
    storage_odoo_measured_at = fields.Datetime(
        string='Odoo-mätning',
        compute='_compute_storage_layers',
    )
    is_odoo_minion = fields.Boolean(
        string='Odoo-minion',
        compute='_compute_is_odoo_minion',
        help='True when the minion has the odoo role. Drives the visibility '
             'of the Odoo tab.',
    )

    # ── Computes ──────────────────────────────────────────────────────────

    @api.depends('roles', 'odoo_version')
    def _compute_is_odoo_minion(self):
        for rec in self:
            roles = [r.strip().lower()
                     for r in (rec.roles or '').split(',') if r.strip()]
            rec.is_odoo_minion = 'odoo' in roles or bool(rec.odoo_version)

    @api.depends('storage_measurement_ids',
                 'storage_measurement_ids.bytes_used',
                 'storage_measurement_ids.artifact',
                 'storage_measurement_ids.method',
                 'storage_measurement_ids.measured_at')
    def _compute_storage_layers(self):
        Measurement = self.env['saltstack.storage.measurement']
        for rec in self:
            rows = Measurement._current([('minion_id', '=', rec.id)])
            summary = Measurement._summarise(rows)
            rec.measurement_primary_gb = summary['primary_gb']
            rec.measurement_backup_gb = summary['backup_gb']
            rec.measurement_total_gb = summary['total_gb']
            rec.storage_missing_count = len(summary['artifacts_missing'])
            missing_n = rec.storage_missing_count
            rec.storage_summary_title = (
                'Saknar mätning för %d artefakt(er) — summan är ofullständig'
                % missing_n
                if missing_n
                else 'Primärdata: container + databas + filestore')
            dates = [d for d in rows.mapped('measured_at') if d]
            rec.storage_measured_at = max(dates) if dates else False

            # Odoo tab values — latest row per artefact/subject.
            db_rows = rows.filtered(lambda r: r.artifact == 'postgres_db')
            fs_rows = rows.filtered(lambda r: r.artifact == 'filestore_s3')
            db_row = db_rows[:1]
            fs_row = fs_rows[:1]
            rec.storage_db_bytes = db_row.bytes_used or 0.0
            rec.storage_filestore_bytes = fs_row.bytes_used or 0.0
            rec.storage_bucket_bytes = fs_row.bytes_secondary or 0.0
            rec.storage_db_name = db_row.subject or False
            raw = (db_row.raw or {}) if db_row else {}
            rec.storage_db_host = raw.get('host') if isinstance(raw, dict) else False
            odoo_dates = [d for d in (db_rows | fs_rows).mapped('measured_at') if d]
            rec.storage_odoo_measured_at = max(odoo_dates) if odoo_dates else False

    # ── Smart button ──────────────────────────────────────────────────────

    def action_show_storage_measurements(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Lagringsmätningar — %s') % self.name,
            'res_model': 'saltstack.storage.measurement',
            'view_mode': 'list,form,pivot',
            'domain': [('minion_id', '=', self.id)],
            'context': {'default_minion_id': self.id},
        }

    # ── Collection ────────────────────────────────────────────────────────

    def _collect_storage_measurements(self):
        """Measure every artefact for this minion.

        Delegates to the Salt execution module `storage.info`, which returns
        all six artefacts in one dict. The module owns the measurement logic
        (one place to fix a bug, one transport); this method only persists
        the result.

        Failures are isolated per artefact by the module itself. A host that
        cannot be reached raises here, and the caller (cron / sync) isolates
        it per minion.
        """
        self.ensure_one()
        Measurement = self.env['saltstack.storage.measurement']
        api = self.env['saltstack.api']
        container = self.name

        archive_stats = int(self.env['ir.config_parameter'].sudo().get_param(
            'saltstack_storage.borg_archive_stats', '1') or 1)
        try:
            # salt_call returns a JSON STRING — decode it. Using the raw
            # string as a dict silently produced "'str' object has no
            # attribute 'get'" for every minion.
            raw = api.salt_call(
                'local', self.name, 'storage.info', container,
                archive_stats, timeout=900)
            result = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:  # noqa: BLE001 — host unreachable
            _logger.warning(
                'storage.info failed for %s: %s', self.name, exc)
            return ['storage.info: %s' % exc]

        payload = (result.get('return') or [{}])[0].get(self.name) or {}
        if not isinstance(payload, dict):
            _logger.warning(
                'storage.info for %s returned %r', self.name, payload)
            return ['storage.info: unexpected payload']

        # Odoo key figures travel in the same payload. They are NOT storage
        # artefacts — they belong on the minion, where the Odoo tab reads
        # them. Writing them here keeps one collection, two destinations.
        self._apply_odoo_stats(payload.get('odoo_stats') or [])

        saved = 0
        for artifact, rows in payload.items():
            if artifact in ('odoo_stats', 'lxd_resources'):
                continue
            for row in rows or []:
                try:
                    Measurement._upsert(
                        self,
                        artifact,
                        row.get('subject'),
                        row.get('bytes_used') or 0,
                        bytes_allocated=row.get('bytes_allocated') or 0,
                        bytes_secondary=row.get('bytes_secondary') or 0,
                        source=row.get('source'),
                        method=row.get('method') or 'measured',
                        raw=row.get('raw'),
                    )
                    saved += 1
                except Exception as exc:  # noqa: BLE001 — one row
                    self.env.invalidate_all()
                    _logger.warning(
                        'Could not save %s/%s for %s: %s',
                        artifact, row.get('subject'), self.name, exc)
        _logger.info('storage.info: %s measurements saved for %s',
                     saved, self.name)
        return []

    def _apply_odoo_stats(self, rows):
        """Write the Odoo key figures onto the minion.

        Same fields the old `_collect_odoo_stats()` filled — one collection,
        two destinations (measurements + minion fields).
        """
        self.ensure_one()
        if not rows:
            return
        dbs, users, coworkers, tokens = [], 0, 0, 0
        last_login = None
        version = None
        for row in rows:
            db = row.get('database') or ''
            users += int(row.get('users_active') or 0)
            coworkers += int(row.get('coworkers') or 0)
            tokens += int(row.get('tokens_m') or 0)
            dbs.append('%s (demo)' % db if row.get('demo') else db)
            version = version or row.get('version') or None
            val = (row.get('last_login') or '').split('.')[0]
            if val and (not last_login or val > last_login):
                last_login = val
        vals = {
            'odoo_databases': ', '.join(dbs),
            'odoo_user_count': users,
            'odoo_coworker_count': coworkers,
            'odoo_coworker_tokens_m': tokens,
            'odoo_stats_synced': fields.Datetime.now(),
        }
        if version:
            vals['odoo_version'] = version
        if last_login:
            try:
                vals['odoo_last_login'] = fields.Datetime.to_datetime(last_login)
            except (ValueError, TypeError):
                pass
        self.write(vals)
