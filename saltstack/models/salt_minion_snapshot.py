# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""Restic snapshots per minion — the "Återskapa data" tab.

Each row is one restic snapshot for a customer bucket, synced from the
restic minion's /var/log/garage-backup/restic-status.json (which lists the
25 most recent snapshots per customer — matching the retention policy
7d + 4w + 12m + 2y ≈ 25 restorable dates).

The row carries the exact restore command for that snapshot so an operator
can copy it and restore data from any given date.
"""

import json
import logging

from datetime import datetime, timezone

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class SaltMinionSnapshot(models.Model):
    _name = 'salt.minion.snapshot'
    _description = 'Restic-snapshot (Återskapa data)'
    _order = 'time desc, id desc'

    minion_id = fields.Many2one(
        'salt.minion',
        string='Minion',
        required=True,
        ondelete='cascade',
        index=True,
    )
    snapshot_id = fields.Char(
        string='Snapshot-ID',
        required=True,
        help='Fullständigt restic snapshot-id.',
    )
    short_id = fields.Char(
        string='Kort-ID',
        help='Kort restic snapshot-id (12 tecken) — tillräckligt för restore.',
    )
    time = fields.Datetime(
        string='Datum (lokal)',
        help='Snapshot-tid (lagrad som UTC, visas i användarens tidszon).',
    )
    hostname = fields.Char(string='Host')
    tags = fields.Char(string='Taggar')
    path = fields.Char(string='Sökväg')
    size = fields.Float(
        string='Storlek (B)',
        digits=(16, 0),
        help='Storlek för senaste snapshot (hämtas billigt via restic stats). '
             'Äldre snapshots saknar storlek i 0.16.x-metadata.',
    )
    file_count = fields.Integer(
        string='Filer',
        help='Antal filer i senaste snapshot.',
    )
    restore_cmd = fields.Char(
        string='Restore-kommando',
        help='Exakt kommando för att återskapa data ur just detta snapshot.',
    )
    synced_at = fields.Datetime(
        string='Synkad',
        help='När raden senast synkades från restic-status.json.',
    )

    _sql_constraints = [
        (
            'minion_snapshot_uniq',
            'unique (minion_id, snapshot_id)',
            'En snapshot-rad för denna minion finns redan.',
        ),
    ]

    def name_get(self):
        res = []
        for rec in self:
            label = rec.time or rec.short_id or rec.snapshot_id
            res.append((rec.id, str(label)))
        return res

    # ── Actions ──────────────────────────────────────────────────────────

    def action_copy_restore_cmd(self):
        """Copy the exact restore command for this snapshot to the clipboard."""
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'saltstack_copy_value',
            'params': {'value': self.restore_cmd or ''},
        }

    # ── Synk ─────────────────────────────────────────────────────────────

    @api.model
    def _read_restic_status(self):
        """Read /var/log/garage-backup/restic-status.json from the restic
        minion via the Salt API (same pattern as salt_minion._measure_s3)."""
        minion = self.env['salt.minion'].search([
            ('active', '=', True),
            ('name', '=', 'restic'),
        ], limit=1)
        if not minion:
            _logger.warning('No restic minion record found — snapshot sync skipped')
            return None
        try:
            result = self.env['saltstack.api'].salt_call(
                'local', 'restic', 'cmd.run',
                'cat /var/log/garage-backup/restic-status.json',
                timeout=45,
            )
            raw = json.loads(result).get('return', [{}])[0].get('restic', '')
        except Exception as e:
            _logger.warning('restic status read failed: %s', e)
            return None
        try:
            return json.loads(str(raw))
        except (ValueError, TypeError):
            _logger.warning('restic status JSON unparsable')
            return None

    @api.model
    def _find_minion(self, customer):
        """Match a restic customer to a salt.minion record.

        Matches on the minion name first, then on the customer grain —
        the same convention used by _measure_s3.
        """
        customer = (customer or '').strip().lower()
        if not customer:
            return self.env['salt.minion']
        m = self.env['salt.minion'].search(
            [('name', '=', customer)], limit=1)
        if m:
            return m
        return self.env['salt.minion'].search(
            [('customer', 'ilike', customer)], limit=1)

    @api.model
    def _parse_time(self, value):
        """Parse a restic ISO-8601 timestamp into naive UTC for Odoo.

        Handles 'Z', '+02:00' and naive values. Returns a naive UTC datetime
        (Odoo convention) so the view renders in the user's timezone.
        """
        if not value:
            return False
        v = str(value).strip()
        try:
            if v.endswith('Z'):
                v = v[:-1] + '+00:00'
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except (ValueError, TypeError):
            return False

    @api.model
    def action_sync_snapshots(self):
        """Upsert snapshot rows for all customers from restic-status.json.

        Called by the scheduled cron and by the per-minion button. Idempotent:
        existing rows are updated by (minion_id, snapshot_id), stale rows
        (snapshots expired by retention) are removed, and the list is capped
        at 25 per minion.
        """
        data = self._read_restic_status()
        if not data:
            return {'error': 'kunde inte läsa restic-status.json', 'created': 0,
                    'updated': 0, 'removed': 0}

        created = updated = removed = 0
        for row in data.get('customers', []):
            minion = self._find_minion(row.get('customer', ''))
            if not minion:
                continue
            snaps = row.get('snapshots') or []
            # Senaste snapshot (max tid) får storlek/filer från kundraden.
            latest_time = max(
                (s.get('time', '') for s in snaps), default='')

            existing = {
                r.snapshot_id: r
                for r in self.search([('minion_id', '=', minion.id)])
            }
            keep = set()

            for snap in snaps[:25]:
                sid = snap.get('id') or ''
                if not sid:
                    continue
                keep.add(sid)
                restore_cmd = 'restic restore %s --target /restore/%s -r %s' % (
                    sid, row.get('customer', ''), row.get('restore_repo', '?'))
                vals = {
                    'minion_id': minion.id,
                    'snapshot_id': sid,
                    'short_id': snap.get('short_id') or sid[:12],
                    'time': self._parse_time(snap.get('time')),
                    'hostname': snap.get('hostname', ''),
                    'tags': ', '.join(snap.get('tags') or []),
                    'path': ', '.join(snap.get('paths') or []),
                    'restore_cmd': restore_cmd,
                    'synced_at': fields.Datetime.now(),
                }
                if snap.get('time') == latest_time:
                    vals['size'] = row.get('size') or 0
                    vals['file_count'] = row.get('files') or 0
                else:
                    vals['size'] = 0
                    vals['file_count'] = 0

                rec = existing.get(sid)
                if rec:
                    rec.write(vals)
                    updated += 1
                else:
                    self.create(vals)
                    created += 1

            # Ta bort rader vars snapshot försvunnit (retention).
            for sid, rec in existing.items():
                if sid not in keep:
                    rec.unlink()
                    removed += 1

        _logger.info('Snapshot sync: %d created, %d updated, %d removed',
                     created, updated, removed)
        return {'created': created, 'updated': updated, 'removed': removed}