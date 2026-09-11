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
    server_id = fields.Many2one(
        'salt.backup.server',
        string='Backup-server',
        ondelete='set null',
        help='Maskinen där restore-kommandot körs (ur backup-server-registret).',
    )
    server_label = fields.Char(
        string='Körs på',
        help='Visningstext med maskinen, t.ex. "restic (192.168.11.112)". '
             'Sätts vid synken så att den alltid finns — även om servern inte '
             'är registrerad än.',
    )
    server_host = fields.Char(
        string='Inloggningsadress',
        help='Adress att SSH:a till för att köra kommandot.',
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
        help='Kommando att köra PÅ backup-servern (anges i kolumnen Server).',
    )
    restore_cmd_ssh = fields.Char(
        string='SSH-kommando',
        help='Fullt kommando som kan klistras in varifrån som helst med '
             'SSH-åtkomst — det innehåller självt vilken maskin det körs på.',
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

    def action_copy_restore_cmd_ssh(self):
        """Copy the full SSH one-liner (states which machine to log into)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'saltstack_copy_value',
            'params': {'value': self.restore_cmd_ssh or self.restore_cmd or ''},
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

        Prioritet:
        1. ``snapshot_customer`` - det explicita fältet. Behovs när
           bucketnamnet skiljer sig från minionen (t.ex. bucketen
           "sparv" på minionen "sparv-test") och när grain
           ``customer`` skrivs över av ``odoo.grains`` (= minion-id).
        2. Exakt minionnamn.
        3. ``customer``-grain (ilike).

        Loggar en varning när inget matchar - annars försvinner
        kunder tyst ur "Återskapa data".
        """
        customer = (customer or '').strip().lower()
        if not customer:
            return self.env['salt.minion']
        Minion = self.env['salt.minion']
        m = Minion.search([('snapshot_customer', '=', customer)], limit=1)
        if m:
            return m
        m = Minion.search([('name', '=', customer)], limit=1)
        if m:
            return m
        m = Minion.search([('customer', 'ilike', customer)], limit=1)
        if m:
            return m
        _logger.warning(
            'Restic-snapshot: ingen minion matchar kund %r - sätt '
            '"Backup-kund (bucket)" på rätt minion för att '
            'få fliken "Återskapa data".', customer)
        return Minion

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

        Varje rad får `server_id` + `server_host` så att det framgår **vilken
        maskin** restore-kommandot körs på, samt två kommandon:
        `restore_cmd` (att köra på backup-servern) och `restore_cmd_ssh`
        (fullt SSH-kommando, självförklarande).
        """
        data = self._read_restic_status()
        if not data:
            return {'error': 'kunde inte läsa restic-status.json', 'created': 0,
                    'updated': 0, 'removed': 0}

        BackupServer = self.env['salt.backup.server']
        fb_name, fb_host, fb_helper, _fb_base = BackupServer._fallback_server_info()

        created = updated = removed = 0
        unmatched = []
        for row in data.get('customers', []):
            customer = (row.get('customer') or '').strip()
            minion = self._find_minion(customer)
            if not minion:
                unmatched.append(customer)
                continue

            # Vilken maskin kör restore-kommandot för denna kund?
            srv = BackupServer._find_for_customer(customer)
            if srv:
                srv_host = srv.effective_login_host()
                helper = srv.helper_path or fb_helper
                srv_label = srv.restore_display()
                srv_target = srv.ssh_target()
                cmd_prefix = srv.command_prefix()
            else:
                srv_host = fb_host
                helper = fb_helper
                srv_label = '%s (%s)' % (fb_name, fb_host) if fb_host else fb_name
                srv_target = 'ubuntu@%s' % fb_host if fb_host else ''
                cmd_prefix = 'sudo '

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
                plain = '%s%s %s %s' % (cmd_prefix, helper, customer, sid)
                if srv_target:
                    ssh_cmd = 'ssh %s "%s"' % (srv_target, plain)
                else:
                    ssh_cmd = plain
                vals = {
                    'minion_id': minion.id,
                    'server_id': srv.id if srv else False,
                    'server_label': srv_label,
                    'server_host': srv_host,
                    'snapshot_id': sid,
                    'short_id': snap.get('short_id') or sid[:12],
                    'time': self._parse_time(snap.get('time')),
                    'hostname': snap.get('hostname', ''),
                    'tags': ', '.join(snap.get('tags') or []),
                    'path': ', '.join(snap.get('paths') or []),
                    'restore_cmd': plain,
                    'restore_cmd_ssh': ssh_cmd,
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
        if unmatched:
            _logger.warning(
                'Snapshot sync: %d kund(er) utan minion - ej synkade: %s',
                len(unmatched), ', '.join(sorted(unmatched)))
        return {'created': created, 'updated': updated,
                'removed': removed, 'unmatched': sorted(unmatched)}
