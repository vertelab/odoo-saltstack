# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""Storage measurements per minion and artefact.

One row per (minion, artefact, subject) and measurement. Rows are never
overwritten: the history is what makes the trend possible. "Current" is
simply the latest row per key.

Two design rules that the whole module rests on:

1. **Dirvish is not an artefact.** Dirvish is retired (Borg + Restic
   replaced it). The artefact selection is an allowlist, so a Dirvish value
   cannot enter a sum by accident. Historical values may be parked in
   `legacy_reference`, which is never summed.

2. **Backup is never primary data.** A Borg archive of a container contains
   the container's bytes. Summing them together would double-count the
   customer. The summary therefore has two independent layers.
"""

import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

GB = 1024.0 ** 3

# Artefact → whether it belongs to the primary-data layer (True) or the
# backup layer (False). This is the single place that decides what a sum
# means; adding an artefact here is the only way to make it countable.
ARTIFACT_LAYER = {
    'lxd_container': 'primary',
    'postgres_db': 'primary',
    'filestore_s3': 'primary',
    'borg_db': 'backup',
    'borg_container': 'backup',
    'restic_filestore': 'backup',
}

# Artefacts that carry a second, non-additive value. The S3 bucket size is
# shown as a deviation against the recorded attachment size — it is NOT
# added to the primary sum, because the bucket may hold other data and the
# recorded size is the customer's data.
DEVIATION_ARTIFACTS = ('filestore_s3',)

# A measurement older than this is flagged as stale in the views. The weekly
# cron_tick task is the slowest cadence, so allow a generous margin.
STALE_AFTER_DAYS = 10


class StorageMeasurement(models.Model):
    _name = 'saltstack.storage.measurement'
    _description = 'Storage Measurement'
    _order = 'measured_at desc, id desc'
    _rec_name = 'display_name'

    # ── Identity ──────────────────────────────────────────────────────────

    artifact = fields.Selection(
        selection=[
            ('lxd_container', 'LXD container'),
            ('filestore_s3', 'Filestore (S3)'),
            ('postgres_db', 'PostgreSQL database'),
            ('borg_db', 'Borg backup — database'),
            ('borg_container', 'Borg backup — container'),
            ('restic_filestore', 'Restic backup — filestore'),
        ],
        string='Artefakt',
        required=True,
        index=True,
        help='Which artefact was measured. Dirvish is deliberately absent: '
             'it is retired and must never enter a sum.',
    )
    minion_id = fields.Many2one(
        'salt.minion',
        string='Minion',
        ondelete='cascade',
        index=True,
        help='The minion the measurement belongs to.',
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Kund',
        related='minion_id.partner_id',
        store=True,
        index=True,
        readonly=True,
        help='Customer, taken from the minion. Empty rows show up in the '
             '"not customer-linked" view instead of silently disappearing.',
    )
    subject = fields.Char(
        string='Delobjekt',
        index=True,
        help='What inside the artefact was measured: volume name, database '
             'name, bucket or archive name. Empty for single-object '
             'artefacts.',
    )

    # ── Values ────────────────────────────────────────────────────────────

    bytes_allocated = fields.Float(
        string='Allokerat (byte)',
        digits=(20, 0),
        help='Allocated size (LXD volume quota, bucket quota). Not the same '
             'as used bytes.',
    )
    bytes_used = fields.Float(
        string='Använt (byte)',
        digits=(20, 0),
        help='Actually used bytes — the value that goes into the sums.',
    )
    bytes_secondary = fields.Float(
        string='Jämförelsevärde (byte)',
        digits=(20, 0),
        help='Second, NON-additive value: the S3 bucket size for '
             'filestore_s3, the uncompressed source size for borg_db. Shown '
             'as a deviation, never summed.',
    )

    # Lagrade (store=True): pivot- och graph-vyer aggregerar i PostgreSQL,
    # och ett icke-lagrat compute-fält har ingen kolumn att summera. Samma
    # gäller sum="..." i listvyn. Beroendena är alla lagrade fält, så
    # värdet kan inte hamna i ofas.
    used_gb = fields.Float(
        string='Använt (GB)',
        compute='_compute_gb',
        store=True,
        digits=(16, 2),
    )
    allocated_gb = fields.Float(
        string='Allokerat (GB)',
        compute='_compute_gb',
        store=True,
        digits=(16, 2),
    )
    secondary_gb = fields.Float(
        string='Jämförelse (GB)',
        compute='_compute_gb',
        store=True,
        digits=(16, 2),
    )
    deviation_gb = fields.Float(
        string='Avvikelse (GB)',
        compute='_compute_gb',
        store=True,
        digits=(16, 2),
        help='bytes_used − bytes_secondary, only for artefacts where the '
             'second value is a deviation (filestore_s3).',
    )

    # ── Provenance ────────────────────────────────────────────────────────

    source = fields.Char(
        string='Källa',
        help='Where the value came from: lxd, salt, psql, borg, restic, '
             'garage.',
    )
    method = fields.Selection(
        selection=[
            ('measured', 'Uppmätt'),
            ('estimated', 'Beräknat'),
        ],
        string='Metod',
        default='measured',
        required=True,
        help='measured = straight from a measuring tool. estimated = derived '
             '(e.g. a Borg archive size inferred from repository statistics). '
             'An estimate is never presented as a measurement.',
    )
    measured_at = fields.Datetime(
        string='Mätt',
        default=fields.Datetime.now,
        required=True,
        index=True,
    )
    raw = fields.Json(
        string='Rådata',
        help='Raw output from the measurement (includes the db host for '
             'postgres_db, so a wrong database can be detected afterwards).',
    )
    legacy_reference = fields.Boolean(
        string='Historisk referens',
        default=False,
        help='True for imported values from a retired source (e.g. Dirvish). '
             'Reference only — never included in any sum.',
    )
    note = fields.Char(string='Anteckning')

    # ── Layer / freshness (computed, not stored) ──────────────────────────

    layer = fields.Selection(
        selection=[
            ('primary', 'Primärdata'),
            ('backup', 'Backup'),
        ],
        string='Lager',
        compute='_compute_layer',
        store=True,
        index=True,
    )
    is_stale = fields.Boolean(
        string='Inaktuell',
        compute='_compute_is_stale',
        search='_search_is_stale',
        help='True when the measurement is older than %s days.'
             % STALE_AFTER_DAYS,
    )
    display_name = fields.Char(
        compute='_compute_display_name',
        store=True,
    )

    # ── Computes ──────────────────────────────────────────────────────────

    @api.depends('bytes_used', 'bytes_allocated', 'bytes_secondary',
                 'artifact')
    def _compute_gb(self):
        for rec in self:
            rec.used_gb = round((rec.bytes_used or 0.0) / GB, 2)
            rec.allocated_gb = round((rec.bytes_allocated or 0.0) / GB, 2)
            rec.secondary_gb = round((rec.bytes_secondary or 0.0) / GB, 2)
            if rec.artifact in DEVIATION_ARTIFACTS and rec.bytes_secondary:
                rec.deviation_gb = round(
                    ((rec.bytes_used or 0.0) - rec.bytes_secondary) / GB, 2)
            else:
                rec.deviation_gb = 0.0

    @api.depends('artifact')
    def _compute_layer(self):
        for rec in self:
            rec.layer = ARTIFACT_LAYER.get(rec.artifact, 'primary')

    @api.depends('measured_at')
    def _compute_is_stale(self):
        limit = fields.Datetime.subtract(
            fields.Datetime.now(), days=STALE_AFTER_DAYS)
        for rec in self:
            rec.is_stale = bool(rec.measured_at and rec.measured_at < limit)

    def _search_is_stale(self, operator, value):
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise ValidationError(_('Invalid search on is_stale.'))
        limit = fields.Datetime.subtract(
            fields.Datetime.now(), days=STALE_AFTER_DAYS)
        stale = [('measured_at', '<', limit)]
        fresh = ['|', ('measured_at', '>=', limit), ('measured_at', '=', False)]
        want_stale = (operator == '=') == value
        return stale if want_stale else fresh

    @api.depends('artifact', 'subject', 'minion_id.name', 'measured_at')
    def _compute_display_name(self):
        labels = dict(self._fields['artifact'].selection)
        for rec in self:
            # artifact kan vara False på en ny, ännu oifylld post (t.ex. i en
            # onchange-snapshot från formuläret). Utan fallback hamnar False i
            # join() och ger "expected str instance, bool found".
            parts = [rec.minion_id.name or _('(ingen minion)'),
                     labels.get(rec.artifact) or rec.artifact or _('(ingen artefakt)')]
            if rec.subject:
                parts.append(rec.subject)
            rec.display_name = ' — '.join(parts)

    # ── Constraints ───────────────────────────────────────────────────────

    @api.constrains('artifact')
    def _check_artifact(self):
        """Belt and braces: the selection already blocks unknown values, but
        a direct SQL insert or a stale cache must not smuggle in a retired
        source."""
        for rec in self:
            if rec.artifact not in ARTIFACT_LAYER:
                raise ValidationError(_(
                    'Artefakten "%s" är inte tillåten. Dirvish är avvecklat '
                    'och får aldrig ingå i en lagringssumma.') % rec.artifact)

    @api.constrains('bytes_used', 'bytes_allocated', 'bytes_secondary')
    def _check_non_negative(self):
        for rec in self:
            if (rec.bytes_used or 0.0) < 0 or (rec.bytes_allocated or 0.0) < 0:
                raise ValidationError(_('Lagringsstorlek kan inte vara negativ.'))

    # ── Helpers ───────────────────────────────────────────────────────────

    def _measurement_key(self):
        """Stable key for one logical measurement target."""
        self.ensure_one()
        return (self.minion_id.id or 0, self.artifact, self.subject or '')

    @api.model
    def _upsert(self, minion, artifact, subject, bytes_used,
                bytes_allocated=None, bytes_secondary=None, source=None,
                method='measured', raw=None, note=None):
        """Record a measurement.

        A NEW row is always created — the history is the point. The views
        show the latest row per (minion, artifact, subject) as "current".
        """
        if artifact not in ARTIFACT_LAYER:
            raise ValidationError(_(
                'Okänd artefakt: %s') % artifact)
        return self.create({
            'minion_id': minion.id if minion else False,
            'artifact': artifact,
            'subject': subject or False,
            'bytes_used': bytes_used or 0.0,
            'bytes_allocated': bytes_allocated or 0.0,
            'bytes_secondary': bytes_secondary or 0.0,
            'source': source or '',
            'method': method,
            'measured_at': fields.Datetime.now(),
            'raw': raw,
            'note': note,
        })

    @api.model
    def _current(self, domain=None):
        """Latest measurement per (minion, artifact, subject).

        Returns a recordset. Implemented in Python (not SQL) because the
        per-key grouping is small and the readability is worth more than the
        microseconds on this data volume.
        """
        rows = self.search((domain or []) + [('legacy_reference', '=', False)],
                           order='measured_at desc, id desc')
        seen = set()
        current = self.browse()
        for row in rows:
            key = row._measurement_key()
            if key in seen:
                continue
            seen.add(key)
            current |= row
        return current

    # ── Summary ───────────────────────────────────────────────────────────

    @api.model
    def _summarise(self, rows):
        """Two-layer summary of a measurement set.

        - primary = lxd_container + postgres_db + filestore_s3 (recorded
          attachment size; the bucket size is a deviation, not a summand)
        - backup  = borg_db + borg_container + restic_filestore
        - total   = primary + backup, always labelled as "backup is a copy"

        Missing artefacts are reported as missing — never as 0 in a total
        that pretends to be complete.
        """
        rows = rows.filtered(lambda r: not r.legacy_reference)
        primary = rows.filtered(lambda r: r.layer == 'primary')
        backup = rows.filtered(lambda r: r.layer == 'backup')

        primary_bytes = sum(primary.mapped('bytes_used'))
        backup_bytes = sum(backup.mapped('bytes_used'))
        estimated_bytes = sum(
            rows.filtered(lambda r: r.method == 'estimated').mapped('bytes_used'))

        present = set(rows.mapped('artifact'))
        missing = sorted(set(ARTIFACT_LAYER) - present)

        deviations = {}
        for row in rows.filtered(lambda r: r.artifact in DEVIATION_ARTIFACTS):
            if not row.bytes_secondary:
                continue
            deviations.setdefault(row.artifact, 0.0)
            deviations[row.artifact] += row.deviation_gb

        return {
            'primary_gb': round(primary_bytes / GB, 2),
            'backup_gb': round(backup_bytes / GB, 2),
            'total_gb': round((primary_bytes + backup_bytes) / GB, 2),
            'estimated_gb': round(estimated_bytes / GB, 2),
            'estimated_ratio': (
                round(estimated_bytes / (primary_bytes + backup_bytes), 3)
                if (primary_bytes + backup_bytes) else 0.0),
            'artifacts_present': sorted(present),
            'artifacts_missing': missing,
            'is_complete': not missing,
            'deviations_gb': deviations,
            'row_count': len(rows),
        }

    # ── Collection entry point (cron.task) ────────────────────────────────

    @api.model
    def _cron_borg_archive_cache(self):
        """Build the Borg archive cache on the backup host.

        Runs BEFORE `storage_collect` (sequence 25 vs 30) so the collection
        reads a warm cache. Measures a bounded number of NEW archives per
        run — in steady state that is a handful per week.
        """
        Minion = self.env['salt.minion']
        borg_host = self.env['ir.config_parameter'].sudo().get_param(
            'saltstack_storage.borg_host', '') or 'tull1'
        api = self.env['saltstack.api']
        budget = int(self.env['ir.config_parameter'].sudo().get_param(
            'saltstack_storage.borg_archive_budget', '50') or 50)

        # Every LXD host that has a Borg repository on the backup host.
        results, failed = [], []
        try:
            raw = api.salt_call('local', borg_host, 'cmd.run',
                                'ls /srv/borgbackup/lxd 2>/dev/null',
                                timeout=120)
            listing = str((json.loads(raw) if isinstance(raw, str) else raw)
                          .get('return', [{}])[0].get(borg_host, '') or '')
        except Exception as exc:  # noqa: BLE001
            _logger.warning('Borg host listing failed: %s', exc)
            return {'failed': [str(exc)]}

        for lxd_host in [h.strip() for h in listing.splitlines() if h.strip()]:
            try:
                raw = api.salt_call('local', borg_host,
                                    'storage.borg_archive_cache',
                                    lxd_host, budget, timeout=7200)
                res = json.loads(raw) if isinstance(raw, str) else raw
                payload = (res.get('return') or [{}])[0].get(borg_host) or {}
                results.append(payload.get('borg_cache') or {})
            except Exception as exc:  # noqa: BLE001
                self.env.invalidate_all()
                _logger.warning('Borg cache failed for %s: %s', lxd_host, exc)
                failed.append('%s: %s' % (lxd_host, exc))
        _logger.info('Borg archive cache: %s hosts, %s failed',
                     len(results), len(failed))
        return {'hosts': results, 'failed': failed}

    @api.model
    def _cron_collect_all(self):
        """Collect measurements for every active minion.

        This is the `cron.task` entry point (`storage_collect`, cadence
        `week`). Failures are isolated per minion: one unreachable host must
        not stop the fleet.
        """
        minions = self.env['salt.minion'].search([('active', '=', True)])
        done, failed = 0, []
        for minion in minions:
            try:
                with self.env.cr.savepoint():
                    minion._collect_storage_measurements()
                done += 1
            except Exception as exc:  # noqa: BLE001 — isolate per minion
                self.env.invalidate_all()
                _logger.warning(
                    'Storage collection failed for %s: %s', minion.name, exc)
                failed.append('%s: %s' % (minion.name, exc))
        _logger.info(
            'Storage collection: %s minions measured, %s failed',
            done, len(failed))
        return {
            'measured': done,
            'failed': failed,
        }
