# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""Backup-snapshots per minion — the "Återskapa data" tab.

Två typer av backup hanteras:

* **Restic** (`backup_type='restic'`) — en rad per restic-snapshot för en
  kundbucket, synkad från restic-minionens
  /var/log/garage-backup/restic-status.json (25 senaste per kund = retention
  7d + 4w + 12m + 2y ≈ 25 återställbara datum).
* **Dirvish** (`backup_type='dirvish'`) — en rad per **branch med data** i en
  dirvish-vault, synkad från serverns dirvish-status.json. Endast branches
  som har `tree/` tas med: en branch utan `tree/` är en misslyckad backup
  (t.ex. disk full → error (11)) och har ingen data att återskapa.
* **Borg** (`backup_type='borg'`) — en rad per arkiv i ett Borg-repo för en
  LXD-värd, synkad via `borg list` på backupservern. Arkivnamnet är
  `<värd>-containers-<container>-<ISO-tid>` (eller `<värd>-snapshots-...`,
  `<värd>-lxd-db-...`, `<värd>-kvm-...`). Containernamnet i arkivnamnet
  matchas mot en salt.minion.

  **VIKTIGT — arkivstorlekarna hör inte till minionen.** Ett Borg-arkiv
  innehåller hela containerns rootfs; `deduplicated_size` är delad mellan
  alla arkiv i repot. Att summera arkivstorlekar per container ger fel
  siffror (se kommentaren i salt_minion.py: `fors-lxd-db-server-*` gav
  kompressionskvot 0,008 och `postgres_borg` 0,03 GB — fel med faktor 42).
  Därför sätts **size/file_count till 0** på Borg-rader: raden är en
  återställningspunkt, inte ett lagringsmått. Lagringsutrymmet mäts i
  `salt.minion.storage` som `disk_borg` (per värd, från repots total).

Varje rad bär det exakta restore-kommandot för just den tidpunkten, så att
en operatör kan kopiera det och återskapa data från valfritt datum.
"""

import json
import logging

from datetime import datetime, timezone

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class SaltMinionSnapshot(models.Model):
    _name = 'salt.minion.snapshot'
    _description = 'Backup-snapshot (Återskapa data)'
    _order = 'time desc, id desc'

    backup_type = fields.Selection(
        selection=[
            ('restic', 'Restic'),
            ('dirvish', 'Dirvish'),
            ('borg', 'Borg'),
        ],
        string='Backup-typ',
        default='restic',
        required=True,
        index=True,
        help='Vilken typ av backup raden kommer från — styr vilken maskin '
             'och vilket restore-kommando som används.',
    )
    vault = fields.Char(
        string='Vault',
        help='Dirvish-vaultens namn (samma som minionens namn). Tomt för '
             'Restic-rader.',
    )
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
        # Namnet är ändrat (till _uniq2) eftersom den gamla constrainten
        # (minion_snapshot_uniq på minion_id,snapshot_id) ligger kvar i
        # databasen med sitt gamla namn — Odoo skulle då hoppa över att skapa
        # den nya. Med ett nytt namn skapas den korrekt (additivt, ingen data
        # rörs). Den gamla constrainten är striktare men harmlös: dirvish- och
        # restic-rader har unika snapshot_id per minion ändå.
        (
            'minion_snapshot_uniq2',
            'unique (minion_id, backup_type, snapshot_id)',
            'En snapshot-rad för denna minion och backup-typ finns redan.',
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

    # ── Borg-synk ────────────────────────────────────────────────────────
    #
    # Borg backar upp LXD-containrar (och KVM-VM:er) per värd. Repona ligger
    # INTE på värden själv utan på backupservern — t.ex. ligger strand:s
    # container-arkiv i `tull1:/srv/borgbackup/lxd/strand`. Det är den fällan
    # som kostade mest tid 2026-10-26: `/srv/borg/lxd/strand` på strand är
    # TOMT medan `/srv/borg/strand` bara har host-arkiv (BORG_PATHS=/etc).
    #
    # Arkivnamnet bär containernamnet:
    #   <värd>-containers-<container>-<YYYY-MM-DDTHH:MM:SS>
    #   <värd>-snapshots-<snapshot>-<YYYY-MM-DDTHH:MM:SS>
    #   <värd>-lxd-db-server-<YYYY-MM-DDTHH:MM:SS>   (ingen container)
    #   <värd>-kvm-<vm>-<YYYY-MM-DDTHH:MM:SS>

    # Backupservern som har Borg-repon, och repokatalogen per LXD-värd.
    BORG_SERVER = 'tull1'
    BORG_REPO_BASE = '/srv/borgbackup/lxd'
    BORG_CONF = '/etc/borg/borg.conf'
    # LXD-värdar som har ett Borg-repo på backupservern. Verifierat
    # 2026-10-26: fors (1339 arkiv), strand (470), tull0 (328), tull1 (130).
    BORG_LXD_HOSTS = ('fors', 'strand', 'tull0', 'tull1')

    @api.model
    def _read_borg_archives(self, lxd_host):
        """Läs alla arkiv i en LXD-värds Borg-repo via Salt API.

        Returnerar en lista av dictar: {archive, time, id} — eller [] om
        repot saknas eller Borg inte kan läsa det.

        `BORG_PASSCOMMAND` krävs; /etc/borg/borg.conf sourcas först (den
        sätter BORG_PASSCOMMAND=cat /etc/borg/passphrase).
        """
        repo = '%s/%s' % (self.BORG_REPO_BASE, lxd_host)
        # --format med TAB som separator; {NL} avslutar varje rad så att
        # splitlines() ger en arkiv per rad (utan {NL} klistras de ihop).
        cmd = (
            'set -a; . %s; set +a; '
            'borg list --format '
            "'{archive}\\t{time}\\t{id}{NL}' %s 2>/dev/null"
        ) % (self.BORG_CONF, repo)
        try:
            result = self.env['saltstack.api'].salt_call(
                'local', self.BORG_SERVER, 'cmd.run', cmd, timeout=120,
            )
            raw = json.loads(result).get('return', [{}])[0].get(
                self.BORG_SERVER, '')
        except Exception as e:
            _logger.warning('borg list failed for %s on %s: %s',
                            lxd_host, self.BORG_SERVER, e)
            return []

        archives = []
        for line in str(raw).splitlines():
            parts = line.split('\t')
            if len(parts) != 3:
                continue
            name, btime, aid = (p.strip() for p in parts)
            if not name or not aid:
                continue
            archives.append({'archive': name, 'time': btime, 'id': aid})
        return archives

    @api.model
    def _borg_archive_minion(self, archive, lxd_host):
        """Hitta minionen som ett Borg-arkiv tillhör.

        Arkivnamnet är `<värd>-<typ>-<namn>-<tid>`. Vi plockar ut `<namn>`
        och matchar mot en salt.minion. Returnerar None för arkiv som inte
        hör till en container (lxd-db-server, lxd-db-certs) eller som saknar
        motsvarande minion.

        Prefixet `<värd>-` plockas bort först, sedan typen
        (containers/snapshots/kvm). Kvar blir `<namn>-<tid>`; tiden är
        alltid sista 19 tecknen (YYYY-MM-DDTHH:MM:SS).
        """
        prefix = '%s-' % lxd_host
        if not archive.startswith(prefix):
            return None
        rest = archive[len(prefix):]
        # Tiden är sista 19 tecknen; allt före är typ + namn.
        if len(rest) < 21:
            return None
        stem = rest[:-20]          # ta bort "-<19 tecken tid>"
        for kind in ('containers-', 'snapshots-', 'kvm-'):
            if stem.startswith(kind):
                stem = stem[len(kind):]
                break
        else:
            # t.ex. "lxd-db-server" / "lxd-db-certs" — ingen container.
            return None
        name = stem.strip()
        if not name:
            return None
        return self.env['salt.minion'].search(
            [('name', '=', name)], limit=1) or None

    @api.model
    def action_sync_borg_snapshots(self):
        """Synka Borg-arkiv till snapshot-rader.

        För varje LXD-värd som har ett repo på backupservern:
          * läs arkiven med `borg list`
          * matcha arkivnamnets container mot en salt.minion
          * skapa/uppdatera en rad per arkiv med restore-kommandot

        Idempotent: befintliga rader uppdateras, försvunna arkiv (prunade av
        retention) tas bort. Rör aldrig restic- eller dirvish-rader
        (backup_type='borg' är filtret).

        Storlek sätts medvetet till 0 — se modulens docstring: arkivstorlek
        hör till hela volymen, inte till minionen. Lagringsutrymmet mäts i
        salt.minion.storage (disk_borg).
        """
        BackupServer = self.env['salt.backup.server']
        created = updated = removed = 0
        unmatched = set()

        # Backup-servern för Borg (roll 'other' — Borg är varken restic eller
        # dirvish). Skapas inte här; saknas den används tull1 som etikett.
        srv = BackupServer.search([
            ('active', '=', True), ('name', '=', self.BORG_SERVER)], limit=1)
        if srv:
            srv_host = srv.effective_login_host()
            srv_label = srv.restore_display()
            srv_target = srv.ssh_target()
        else:
            # Ingen serverpost registrerad: slå upp backupserverns adress via
            # minionen så att SSH-kommandot ändå innehåller maskinen. Utan
            # detta blir "kopiera SSH-kommandot" identiskt med det vanliga
            # kommandot — och då är det ingen skillnad att klistra in någon
            # annanstans ifrån, vilket är hela poängen med den knappen.
            borg_minion = self.env['salt.minion'].search(
                [('name', '=', self.BORG_SERVER)], limit=1)
            host = (borg_minion.private_ip or borg_minion.public_ip) if \
                borg_minion else ''
            srv_host = host or self.BORG_SERVER
            srv_label = 'borg (%s)' % srv_host
            srv_target = ('ubuntu@%s' % host) if host else ''

        for lxd_host in self.BORG_LXD_HOSTS:
            archives = self._read_borg_archives(lxd_host)
            if not archives:
                continue

            # Gruppera per minion så att borttagningen av försvunna arkiv
            # sker per minion (annars raderas rader vi inte sett).
            per_minion = {}
            for a in archives:
                minion = self._borg_archive_minion(a['archive'], lxd_host)
                if not minion:
                    continue
                per_minion.setdefault(minion, []).append(a)

            for minion, rows in per_minion.items():
                existing = {
                    r.snapshot_id: r
                    for r in self.search([
                        ('minion_id', '=', minion.id),
                        ('backup_type', '=', 'borg'),
                    ])
                }
                keep = set()
                for a in rows:
                    aid = a['id']
                    keep.add(aid)
                    plain = 'borg extract %s/%s::%s' % (
                        self.BORG_REPO_BASE, lxd_host, a['archive'])
                    ssh_cmd = ('ssh %s "%s"' % (srv_target, plain)
                               if srv_target else plain)
                    vals = {
                        'minion_id': minion.id,
                        'backup_type': 'borg',
                        'server_id': srv.id if srv else False,
                        'server_label': srv_label,
                        'server_host': srv_host,
                        'snapshot_id': aid,
                        'short_id': aid[:12],
                        'time': self._parse_borg_time(a['time']),
                        'hostname': lxd_host,
                        'tags': 'borg',
                        'path': '%s/%s' % (self.BORG_REPO_BASE, lxd_host),
                        'restore_cmd': plain,
                        'restore_cmd_ssh': ssh_cmd,
                        # Storlek hör till volymen, inte minionen — se
                        # docstring. Lagring mäts som disk_borg.
                        'size': 0,
                        'file_count': 0,
                        'synced_at': fields.Datetime.now(),
                    }
                    rec = existing.get(aid)
                    if rec:
                        rec.write(vals)
                        updated += 1
                    else:
                        self.create(vals)
                        created += 1

                for aid, rec in existing.items():
                    if aid not in keep:
                        rec.unlink()
                        removed += 1

        _logger.info(
            'Borg snapshot sync: %d created, %d updated, %d removed',
            created, updated, removed)
        if unmatched:
            _logger.warning(
                'Borg sync: %d arkiv utan minion - ej synkade: %s',
                len(unmatched), ', '.join(sorted(unmatched)))
        return {'created': created, 'updated': updated,
                'removed': removed, 'unmatched': sorted(unmatched)}

    @api.model
    def _parse_borg_time(self, value):
        """Parsa Borgs tidssträng ("Sun, 2026-10-25 23:22:57") till UTC.

        Borg skriver tiden i lokal tid utan tidszon. Vi tolkar den som
        Europe/Stockholm (serverns tidszon) och konverterar till naive UTC,
        samma konvention som _parse_time använder för restic.
        """
        if not value:
            return False
        v = str(value).strip()
        # "Sun, 2026-10-25 23:22:57" → "2026-10-25 23:22:57"
        if ',' in v:
            v = v.split(',', 1)[1].strip()
        try:
            dt = datetime.strptime(v, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return False
        try:
            from zoneinfo import ZoneInfo
            local = dt.replace(tzinfo=ZoneInfo('Europe/Stockholm'))
            return local.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            # Utan tz-databas: använd tiden som den är (UTC-antagande).
            return dt

    # ── Dirvish-synk ─────────────────────────────────────────────────────
    #
    # Dirvish backar upp HELA maskiner (vault = maskinens namn, t.ex. tull0,
    # fors, tull1). Varje branch är en datumkatalog /srv/backup/<vault>/<datum>/.
    # En branch UTAN tree/ är en misslyckad backup (disk full → error (11)) och
    # har ingen data — sådana tas ALDRIG med ("där finns ingen data att
    # återskapa"). Bara branches med tree/ blir rader i fliken.

    # Backup-servrar som kör dirvish och deras vaults. Vaultens namn är
    # minionens namn (tull0, fors, tull1). Servern är den maskin där
    # /srv/backup ligger och restore-kommandot körs.
    DIRVISH_SERVERS = (
        # (salt-minion, dirvish-status.json-sökväg)
        'dirvishtull0',
        'dirvishtull1',
        'strand',
    )

    @api.model
    def _read_dirvish_status(self, minion_name):
        """Läs dirvish-status.json från en dirvish-server via Salt API."""
        try:
            result = self.env['saltstack.api'].salt_call(
                'local', minion_name, 'cmd.run',
                'cat /var/log/dirvish-status/dirvish-status.json',
                timeout=45,
            )
            raw = json.loads(result).get('return', [{}])[0].get(minion_name, '')
        except Exception as e:
            _logger.warning('dirvish status read failed on %s: %s',
                            minion_name, e)
            return None
        try:
            return json.loads(str(raw))
        except (ValueError, TypeError):
            _logger.warning('dirvish status JSON unparsable on %s', minion_name)
            return None

    @api.model
    def _read_dirvish_restorable(self, minion_name, vault):
        """Läs återskapsbara datum (branches MED tree/) för en vault.

        Kör dirvish-restore.sh --list på dirvish-servern — den filtrerar bort
        branches utan tree/ (tomma datumkataloger). Returnerar en lista av
        datum-strängar, nyast först.
        """
        try:
            result = self.env['saltstack.api'].salt_call(
                'local', minion_name, 'cmd.run',
                '/usr/local/bin/dirvish-restore.sh %s --list' % vault,
                timeout=60,
            )
            raw = json.loads(result).get('return', [{}])[0].get(minion_name, '')
        except Exception as e:
            _logger.warning('dirvish restore list failed on %s/%s: %s',
                            minion_name, vault, e)
            return []
        dates = []
        for line in str(raw).splitlines():
            line = line.strip()
            # Datumrader är 8 siffror (YYYYMMDD) — allt annat är rubriker.
            if len(line) == 8 and line.isdigit():
                dates.append(line)
        return dates

    @api.model
    def _dirvish_server_for_vault(self, vault, source_minion=None):
        """Hitta backup-servern (salt.backup.server) för en dirvish-vault.

        `source_minion` är den dirvish-server vaulten lästes från (ur
        DIRVISH_SERVERS). Den är auktoritativ: en vault kan heta samma som en
        server (t.ex. vaulten `strand` ligger på `dirvishtull1`, medan
        *servern* `strand` hostar vaulten `tull0`). Utan den ledtråden kan
        namnmatchningen peka fel.

        Faller tillbaka på serverns `customers`-lista, sedan på en
        dirvish-server utan kundlista.
        """
        BackupServer = self.env['salt.backup.server']
        servers = BackupServer.search([
            ('active', '=', True), ('role', '=', 'dirvish')])
        # 1. Servern vi läste vaulten från vinner alltid.
        if source_minion:
            for srv in servers:
                if srv.name == source_minion:
                    return srv
        # 2. Annars: matcha vault-namnet mot serverns kundlista.
        vault_l = (vault or '').strip().lower()
        for srv in servers:
            if vault_l in srv.customer_list():
                return srv
        # 3. Sist: en dirvish-server utan kundlista (standard).
        for srv in servers:
            if not srv.customer_list():
                return srv
        return BackupServer.browse()

    @api.model
    def action_sync_dirvish_snapshots(self):
        """Synka dirvish-branches (MED data) till snapshot-rader.

        För varje dirvish-server och varje vault på den:
          * läs återskapsbara datum via dirvish-restore.sh --list
            (filtrerar bort branches utan tree/ = tomma datumkataloger)
          * matcha vaultens namn mot en salt.minion
          * skapa/uppdatera en rad per datum med restore-kommandot

        Idempotent: befintliga rader uppdateras, försvunna datum tas bort.
        Rör aldrig restic-rader (backup_type='dirvish' är filtret).
        """
        BackupServer = self.env['salt.backup.server']
        created = updated = removed = 0
        unmatched = set()

        for minion_name in self.DIRVISH_SERVERS:
            status = self._read_dirvish_status(minion_name)
            if not status:
                continue
            vaults = status.get('vaults') or []
            for v in vaults:
                vault = (v.get('name') or '').strip()
                if not vault:
                    continue
                # Bara vaults som faktiskt är aktiva/initierade — en vault utan
                # init har ingen data alls.
                if v.get('init') != '✅':
                    continue

                minion = self.env['salt.minion'].search(
                    [('name', '=', vault)], limit=1)
                if not minion:
                    unmatched.add(vault)
                    continue

                dates = self._read_dirvish_restorable(minion_name, vault)
                if not dates:
                    continue

                srv = self._dirvish_server_for_vault(vault, minion_name)
                if srv:
                    srv_host = srv.effective_login_host()
                    helper = srv.helper_path or '/usr/local/bin/dirvish-restore.sh'
                    srv_label = srv.restore_display()
                    srv_target = srv.ssh_target()
                    cmd_prefix = srv.command_prefix()
                else:
                    srv_host = ''
                    helper = '/usr/local/bin/dirvish-restore.sh'
                    srv_label = minion_name
                    srv_target = ''
                    cmd_prefix = 'sudo '

                existing = {
                    r.snapshot_id: r
                    for r in self.search([
                        ('minion_id', '=', minion.id),
                        ('backup_type', '=', 'dirvish'),
                    ])
                }
                keep = set()

                for date in dates:
                    keep.add(date)
                    plain = '%s%s %s %s' % (
                        cmd_prefix, helper, vault, date)
                    ssh_cmd = ('ssh %s "%s"' % (srv_target, plain)
                               if srv_target else plain)
                    # Branch-datumet (YYYYMMDD) → datetime så att tidsordning
                    # och visning fungerar som för restic-raderna.
                    try:
                        t = datetime.strptime(date, '%Y%m%d')
                    except ValueError:
                        t = False
                    vals = {
                        'minion_id': minion.id,
                        'backup_type': 'dirvish',
                        'vault': vault,
                        'server_id': srv.id if srv else False,
                        'server_label': srv_label,
                        'server_host': srv_host,
                        'snapshot_id': date,
                        'short_id': date,
                        'time': t,
                        'hostname': vault,
                        'tags': 'dirvish',
                        'path': '/srv/backup/%s/%s/tree' % (vault, date),
                        'restore_cmd': plain,
                        'restore_cmd_ssh': ssh_cmd,
                        'synced_at': fields.Datetime.now(),
                    }
                    rec = existing.get(date)
                    if rec:
                        rec.write(vals)
                        updated += 1
                    else:
                        self.create(vals)
                        created += 1

                for date, rec in existing.items():
                    if date not in keep:
                        rec.unlink()
                        removed += 1

        _logger.info(
            'Dirvish snapshot sync: %d created, %d updated, %d removed',
            created, updated, removed)
        if unmatched:
            _logger.warning(
                'Dirvish sync: %d vault(s) utan minion - ej synkade: %s',
                len(unmatched), ', '.join(sorted(unmatched)))
        return {'created': created, 'updated': updated,
                'removed': removed, 'unmatched': sorted(unmatched)}
