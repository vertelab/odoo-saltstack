# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

import json
import logging
import re
import time

import psycopg2.errors

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


def _is_rfc1918(ip):
    """True for private/link-local/loopback IPv4 ranges."""
    try:
        parts = [int(p) for p in ip.split('.')]
    except (ValueError, AttributeError):
        return True
    if len(parts) != 4:
        return True
    a, b, c, _d = parts
    if a == 10:
        return True
    if a == 127:
        return True
    if a == 169 and b == 254:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    if a == 100 and 64 <= b <= 127:
        return True  # CGNAT
    if a == 0:
        return True
    return False


# Internal networks, most authoritative first. The first address found in the
# earliest matching prefix becomes ``private_ip``.
#
# 2026-09-14 (T/11478): this used to be a hardcoded ``192.168.11.`` prefix
# check. Every host outside that single /24 — the GleSYS fleet on
# 185.39.146.0/28, the Hetzner gateways, and WireGuard nodes on 172.16.11.x —
# therefore got an EMPTY private_ip and showed up as "Ej anslutna / IP
# saknas" in the "Kopiera hosts-lista" button, despite being online with a
# perfectly good address. The list below makes the lookup explicit and
# ordered, and is the single place to extend when a new internal network is
# added.
_PRIVATE_NET_PREFIXES = (
    '192.168.11.',   # DJDATA management LAN (colocation) — most authoritative
    '192.168.12.',   # dc-sto DHCP LAN (kind)
    '172.16.10.',    # WireGuard gw0 side
    '172.16.11.',    # WireGuard gw1 side
    '10.0.0.',       # Hetzner private network (gw0/gw1/drake/ring)
    '10.0.1.',       # Hetzner private network (drake)
    '10.20.20.',     # gate local LAN
    '10.128.10.',    # zenbook local container LAN
    '10.145.182.',   # luke container LAN (luke18/luke18a)
    '10.204.124.',   # jakob18.lxd container LAN
    '10.23.23.',     # zenbook local container LAN
    '192.168.1.',    # misc home/office LANs
)


class SaltMinion(models.Model):
    _name = 'salt.minion'
    _description = 'Salt Minion Registry'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'is_demo, dc, name'
    _rec_names_search = ['name', 'customer', 'host_machine', 'private_ip']

    # ── Identity ────────────────────────────────────────────────────────

    name = fields.Char(
        string='Minion ID',
        required=True,
        index=True,
    )
    private_ip = fields.Char(
        string='Private IP',
        help='Private address — prefers the 192.168.11.0/24 management '
             'network, filled from grains at sync.',
    )
    public_ip = fields.Char(
        string='Public IP',
        help='First non-RFC1918 address; empty for containers.',
    )
    os = fields.Char(
        string='Operating System',
    )
    os_version = fields.Char(
        string='OS Version',
    )

    # ── Topology ─────────────────────────────────────────────────────────

    dc = fields.Selection(
        # 'lab' = maskiner utanför datacenter (laptops, arbetsstationer,
        # testburkar) — t.ex. jakob18 på jangofett (192.168.1.x/WireGuard).
        # Utan den kraschade hela minion-synken på ett okänt dc-grain
        # (2026-10-19). Nya värden ska läggas här OCH valideras i
        # _sanitize_dc() — annars rullas hela synktransaktionen tillbaka.
        selection=[('ska', 'SKA'), ('sto', 'STO'), ('lab', 'Lab')],
        string='Datacenter',
    )
    roles = fields.Char(
        string='Roles',
        help='Comma-separated roles (e.g. "caddy,odoo,postgres")',
    )

    # ── Customer ─────────────────────────────────────────────────────────

    customer = fields.Char(
        string='Kund (grain)',
        help='Customer name if this is a customer container (grain mirror).',
    )
    snapshot_customer = fields.Char(
        string='Backup-kund (bucket)',
        help='Kundnamn i backup-rapporten (restic-status.json) när det '
             'skiljer sig från minionens namn — t.ex. bucketen '
             '"sparv" på minionen "sparv-test". Används av '
             '"Återskapa data" och S3-mätningen för att hitta '
             'rätt minion. Lämnas tom när namnet stämmer.',
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Kund',
        ondelete='set null',
        help='Linked res.partner, looked up by name from the customer grain '
             '(or from the minion name when the grain is missing) at sync.',
    )
    odoo_version = fields.Char(
        string='Odoo Version',
    )
    odoo_has_demo_data = fields.Boolean(
        string='Odoo Demo Data',
        help='True when the minion runs Odoo and its main database contains '
             'demo data. Checked at sync.',
    )

    # ── Container ────────────────────────────────────────────────────────

    is_container = fields.Boolean(
        string='LXD Container',
    )
    host_machine = fields.Char(
        string='LXD Host',
        help='Physical machine hosting this container (e.g. "fors", "strand")',
    )

    # ── Status ───────────────────────────────────────────────────────────

    state = fields.Selection(
        selection=[
            ('online', 'Online'),
            ('offline', 'Offline'),
            ('faulty', 'Faulty'),
        ],
        string='Status',
        compute='_compute_state',
        store=True,
        default='offline',
        help='Minion-status semaphore: faulty (olösta larm) > online '
             '(svarar) > offline (svarar ej).',
    )
    open_alert_count = fields.Integer(
        string='Open Alerts',
        default=0,
        help='Number of unresolved saltstack.alert records for this host. '
             'Maintained by the alert model and the minion sync.',
    )
    runlog_count = fields.Integer(
        string='Driftslogg',
        compute='_compute_runlog_count',
        help='Number of saltstack.runlog records linked to this minion '
             '(automatic runs and manual change notes).',
    )
    runlog_ids = fields.One2many(
        'saltstack.runlog',
        'minion_id',
        string='Run log entries',
        help='Run reports and manual change notes for this minion.',
    )
    image = fields.Image(
        string='Logo',
        max_width=256,
        max_height=256,
        help='Logo for the minion. Filled automatically at sync — the '
             'customer logo when a partner is linked, otherwise a '
             'technology logo from the roles. Can be replaced manually in '
             'the form; a manual image is never overwritten by sync.',
    )
    image_manual = fields.Boolean(
        string='Manuellt satt logotyp',
        default=False,
        help='Set when someone uploads a logo by hand. Sync then leaves the '
             'image alone — without this flag an automatically assigned '
             'logo would look manual and freeze forever, so a container '
             'that gains a role would never get a new logo.',
    )

    # ── Topology / extern (uppdateras vid sync + server action) ──────────

    external_domain = fields.Char(
        string='Extern domän',
        help='External/public domain for this minion, e.g. '
             'svenskfast.azzar.org. Filled from grains at sync, can be '
             'set manually or via the "Uppdatera grunduppgifter" action.',
    )
    has_gateway = fields.Boolean(
        string='Gateway (GW)',
        help='True when this minion hosts/exposes a gateway in front of the '
             'service (role "gateway").',
    )

    # ── Odoo statistics (per Odoo-minion, via "Uppdatera grunduppgifter") ──

    odoo_databases = fields.Char(
        string='Odoo-databaser',
        help='Comma-separated Odoo databases, with "(demo)" after the name '
             'when demo data is present.',
    )
    odoo_user_count = fields.Integer(
        string='Användare',
        help='Total number of active users across the minion Odoo databases.',
    )
    odoo_last_login = fields.Datetime(
        string='Senast inloggad',
        help='Latest login across the minion Odoo databases.',
    )
    odoo_coworker_count = fields.Integer(
        string='AI-medarbetare',
        help='Number of active ai.coworker records across the minion Odoo '
             'databases.',
    )
    odoo_coworker_tokens_m = fields.Integer(
        string='Systemtokens (M)',
        help='Monthly systemtoken budget in millions (1M base per coworker, '
             'plus extra budgeted via monthly_cap_mtokens).',
    )
    odoo_stats_synced = fields.Datetime(
        string='Statistik uppdaterad',
        help='When the Odoo statistics were last collected.',
    )

    # ── Storage (mätt via "Mät storage") ────────────────────────────────

    storage_ids = fields.One2many(
        'salt.minion.storage',
        'minion_id',
        string='Storage',
    )
    snapshot_ids = fields.One2many(
        'salt.minion.snapshot',
        'minion_id',
        string='Restic-snapshots (Återskapa data)',
        domain=[('backup_type', '=', 'restic')],
        help='Senaste restic-snapshots för kundens backup-bucket — synkade '
             'från restic-status.json. Används för att återskapa data ur ett '
             'tidigare datum.',
    )
    dirvish_snapshot_ids = fields.One2many(
        'salt.minion.snapshot',
        'minion_id',
        string='Dirvish-snapshots (Återskapa data)',
        domain=[('backup_type', '=', 'dirvish')],
        help='Dirvish-branches MED data (tree/) för denna maskins vault — '
             'synkade från dirvish-status.json + dirvish-restore.sh --list. '
             'Datumkataloger utan data (misslyckade backuper) tas aldrig med.',
    )
    restore_target_info = fields.Char(
        string='Kör restore-kommandon på',
        compute='_compute_restore_target_info',
        help='Maskinen där restore-kommandona körs — visas högst upp på '
             '"Återskapa data"-fliken så att det alltid framgår var man '
             'klistrar in kommandot.',
    )
    storage_primary_gb = fields.Float(
        string='Primärdata (GB)',
        compute='_compute_storage_total',
        digits=(12, 2),
        help='Container + disk + Postgres + filestore — the customer\'s '
             'live data.',
    )
    storage_backup_gb = fields.Float(
        string='Backup (GB)',
        compute='_compute_storage_total',
        digits=(12, 2),
        help='Borg + restic. A COPY of the primary data — never added to it '
             'when the customer\'s own size is reported.',
    )
    storage_total_gb = fields.Float(
        string='Totalt (GB)',
        compute='_compute_storage_total',
        digits=(12, 2),
        help='Primary + backup, always labelled so the copy is not mistaken '
             'for live data.',
    )
    lxd_host = fields.Char(
        string='LXD-host (cache)',
        help="LXD host this minion's container runs on — detected by the "
             'storage measurement and cached here to avoid re-probing.',
    )

    @api.depends('storage_ids.bytes_used',
                 'storage_ids.layer',
                 'storage_ids.legacy_reference',
                 'storage_ids.storage_type')
    def _compute_storage_total(self):
        """Two independent layers — backup is never added to primary data.

        A Borg archive of a container contains the container's bytes.
        Summing them together would double-count the customer.
        """
        for rec in self:
            rows = rec.storage_ids.filtered(lambda r: not r.legacy_reference)
            primary = rows.filtered(lambda r: r.layer == 'primary')
            backup = rows.filtered(lambda r: r.layer == 'backup')
            rec.storage_primary_gb = round(
                sum(primary.mapped('bytes_used')) / 1024.0 ** 3, 2)
            rec.storage_backup_gb = round(
                sum(backup.mapped('bytes_used')) / 1024.0 ** 3, 2)
            rec.storage_total_gb = round(
                rec.storage_primary_gb + rec.storage_backup_gb, 2)

    @api.depends('snapshot_ids', 'snapshot_ids.server_label',
                 'dirvish_snapshot_ids', 'dirvish_snapshot_ids.server_label')
    def _compute_restore_target_info(self):
        """Vilken maskin restore-kommandona körs på.

        Läser backup-servern ur snapshot-raderna (satt vid synken). Saknas
        rader faller vi tillbaka på backup-server-registret/config-parametern
        så att texten alltid är korrekt — även för en ny kund innan första
        synken hunnit köra.

        Har minionen både restic- och dirvish-rader visas båda maskinerna
        (de kan vara olika), tydligt märkta per typ så att det inte går att
        blanda ihop dem.
        """
        Server = self.env['salt.backup.server']
        for rec in self:
            parts = []
            labels = []
            for snap in rec.snapshot_ids:
                if snap.server_label and snap.server_label not in labels:
                    labels.append(snap.server_label)
            if labels:
                parts.append('Restic: %s' % ' / '.join(labels))
            labels = []
            for snap in rec.dirvish_snapshot_ids:
                if snap.server_label and snap.server_label not in labels:
                    labels.append(snap.server_label)
            if labels:
                parts.append('Dirvish: %s' % ' / '.join(labels))
            if not parts:
                fb_name, fb_host, _h, _b = Server._fallback_server_info()
                if fb_name:
                    parts.append(
                        '%s (%s)' % (fb_name, fb_host) if fb_host else fb_name)
            rec.restore_target_info = ' — '.join(parts) or False
    is_demo = fields.Boolean(
        string='Demo / Test',
        default=False,
        help='Mark as test/demo minion. Excluded from production operations.',
    )
    last_seen = fields.Datetime(
        string='Last Seen',
    )
    last_sync = fields.Datetime(
        string='Last Grains Sync',
    )
    grains_json = fields.Text(
        string='Full Grains (JSON)',
    )
    notes = fields.Text(
        string='Notes',
    )

    # ── Related ──────────────────────────────────────────────────────────

    pillar_ids = fields.Many2many(
        'salt.pillar',
        'salt_minion_pillar_rel',
        'minion_id', 'pillar_id',
        string='Pillar Data',
    )
    active = fields.Boolean(
        string='Active',
        default=True,
    )

    # ── Computed ─────────────────────────────────────────────────────────

    @api.depends('last_seen', 'open_alert_count')
    def _compute_state(self):
        """Semaphore: faulty (unresolved alerts) > online > offline."""
        from datetime import datetime, timedelta
        threshold = datetime.now() - timedelta(hours=1)
        for rec in self:
            if rec.open_alert_count > 0:
                rec.state = 'faulty'
            elif rec.last_seen and rec.last_seen > threshold:
                rec.state = 'online'
            else:
                rec.state = 'offline'

    def _update_open_alert_count(self):
        """Recompute the stored open alert count from alert records.

        Called by the alert model on create/resolve and by the dedicated
        backfill cron. Writing the stored field triggers the stored state
        recomputation.

        Batched (2026-09-24, T/11607): previously this issued one search_count
        per minion (179 queries) inside the minion-sync transaction. That forced
        a flush of the pending last_seen writes mid-transaction and collided
        with concurrent writers (driftlarm webhook, auto-resolve cron), aborting
        the whole sync with:

            psycopg2.errors.SerializationFailure: could not serialize access
            due to concurrent update

        Now: one read_group for all hosts, and each write is wrapped in a
        savepoint so a concurrent update on a single minion cannot roll back
        the entire run.
        """
        if not self:
            return True

        # host is a Many2one to salt.minion, so group on the id and map back.
        groups = self.env['saltstack.alert'].read_group(
            [('resolved', '=', False), ('host', 'in', self.ids)],
            ['host'],
            ['host'],
        )
        counts = {
            group['host'][0]: group['host_count']
            for group in groups
            if group.get('host')
        }

        for rec in self:
            count = counts.get(rec.id, 0)
            if rec.open_alert_count == count:
                continue
            try:
                with self.env.cr.savepoint():
                    rec.open_alert_count = count
            except psycopg2.errors.SerializationFailure:
                _logger.warning(
                    'open_alert_count: concurrent update on %s, skipped this '
                    'round (will be corrected by the backfill cron)', rec.name)
                continue
        return True

    # ── Logo handling ────────────────────────────────────────────────────

    _ROLE_LOGO_PRIORITY = [
        ('salt-master', 'saltstack.png'),
        ('salt', 'saltstack.png'),
        ('lxd-host', 'lxd.png'),
        ('lxd', 'lxd.png'),
        ('odoo', 'odoo.png'),
        ('postgres', 'postgres.png'),
        ('caddy', 'caddy.png'),
        ('gateway', 'caddy.png'),
        ('mail', 'mail.png'),
        ('postfix', 'mail.png'),
        ('dovecot', 'mail.png'),
        ('garage', 'garage.png'),
        ('s3', 'garage.png'),
        ('zabbix', 'siem.png'),
        ('wazuh', 'siem.png'),
    ]
    _LOGO_FALLBACK = 'server.png'

    def write(self, vals):
        """Flag a hand-uploaded logo as manual.

        Sync writes the image through _write_auto_image(), which passes the
        'saltstack_auto_image' context key. Any other write that carries an
        image is a human in the form, and must be protected from the next
        sync.
        """
        if ('image' in vals
                and not self.env.context.get('saltstack_auto_image')
                and 'image_manual' not in vals):
            vals = dict(vals, image_manual=bool(vals.get('image')))
        return super().write(vals)

    def _set_default_image(self):
        """Assign a logo using the precedence order below.

            1. A manually uploaded image — never touched.
            2. The linked customer's logo (partner_id.image_128).
            3. A technology logo taken from the roles.
            4. server.png as a last resort.

        The customer logo wins over the technology logo: on a kanban grouped
        by datacenter you are looking for the customer, not the stack, and
        ten Odoo containers with the same icon are impossible to tell apart.
        The roles are printed on the card anyway.
        """
        for rec in self:
            if rec.image_manual:
                continue  # a human chose this — leave it alone
            partner_logo = rec.partner_id.image_128
            if partner_logo:
                rec._write_auto_image(partner_logo)
                continue
            roles = [r.strip().lower() for r in (rec.roles or '').split(',') if r.strip()]
            asset = next(
                (logo for role, logo in self._ROLE_LOGO_PRIORITY if role in roles),
                self._LOGO_FALLBACK,
            )
            rec._write_auto_image(self._logo_from_asset(asset))

    def _write_auto_image(self, image):
        """Write an automatically chosen logo without marking it manual."""
        self.ensure_one()
        self.with_context(saltstack_auto_image=True).write(
            {'image': image, 'image_manual': False})

    def _logo_from_asset(self, filename):
        """Return base64 PNG of a bundled logo asset from this module."""
        import base64 as b64
        import os as _os
        path = _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
            'static', 'src', 'img', 'logos', filename,
        )
        try:
            with open(path, 'rb') as f:
                return b64.b64encode(f.read())
        except OSError:
            _logger.warning('Logo asset missing: %s', path)
            return None

    # ── Salt API Helpers ─────────────────────────────────────────────────

    def _call_salt_api(self, client, tgt, fun, *args, timeout=120, **kwargs):
        """Call the Salt REST API via saltstack.api. Returns parsed dict or raises."""
        import json as _json
        result = self.env['saltstack.api'].salt_call(
            client, tgt, fun, *args, timeout=timeout, **kwargs)
        return _json.loads(result)

    # ── IP extraction ────────────────────────────────────────────────────

    @staticmethod
    def _extract_ips(grains):
        """Return (private_ip, public_ip) from grains.

        private_ip: first address matching an internal network in
        ``_PRIVATE_NET_PREFIXES``, resolved in PREFIX order (192.168.11.x
        wins over 172.16.10.x even when the WireGuard address comes first in
        the grain list), falling back to the first other RFC1918 address so
        that every host with *any* internal address gets one.
        public_ip: first non-RFC1918 address.

        2026-09-14 (T/11478): the fallback to "any other RFC1918 address" is
        what makes the GleSYS fleet (185.39.146.x + 10.0.1.x / 10.0.0.x) and
        the WireGuard nodes (172.16.11.x) land in ``private_ip`` instead of
        being blank. See ``_PRIVATE_NET_PREFIXES`` for the background.
        """
        candidates = []  # (iface, ip)
        ip4_interfaces = grains.get('ip4_interfaces') or {}
        for iface, addrs in ip4_interfaces.items():
            if iface == 'lo':
                continue
            for ip in addrs or []:
                candidates.append((iface, ip))
        for src in ('fqdn_ip4', 'ipv4'):
            for ip in grains.get(src) or []:
                candidates.append(('', ip))

        # Keep only valid dotted-quad strings (and drop loopback).
        ips = []
        for _iface, ip in candidates:
            if not isinstance(ip, str) or '.' not in ip:
                continue
            if ip.startswith('127.'):
                continue
            if ip not in ips:
                ips.append(ip)

        # Pass 1 — the ordered internal networks. The OUTER loop is the
        # prefix, so the earliest prefix always wins regardless of the order
        # the addresses appear in the grains.
        private = None
        for prefix in _PRIVATE_NET_PREFIXES:
            for ip in ips:
                if ip.startswith(prefix):
                    private = ip
                    break
            if private:
                break

        # Pass 2 — any other RFC1918 address (e.g. an unusual container LAN).
        fallback_private = None
        if not private:
            for ip in ips:
                if _is_rfc1918(ip):
                    fallback_private = ip
                    break

        # Pass 3 — first public address.
        public = None
        for ip in ips:
            if not _is_rfc1918(ip):
                public = ip
                break

        return (private or fallback_private or ''), (public or '')

    # ── Demo data detection ──────────────────────────────────────────────

    def _update_demo_data(self, grains=None):
        """Check whether an Odoo minion's main database contains demo data.

        Main database is resolved from the Odoo setting (saltstack.odoo_main_db)
        or the grain 'odoo_main_db'. The check is best-effort: failures are
        logged, never fatal.
        """
        self.ensure_one()
        roles = [r.strip().lower() for r in (self.roles or '').split(',') if r.strip()]
        if 'odoo' not in roles:
            self.odoo_has_demo_data = False
            return False

        main_db = self.env['ir.config_parameter'].get_param(
            'saltstack.odoo_main_db', '')
        if not main_db and grains and isinstance(grains, dict):
            main_db = grains.get('odoo_main_db') or ''
        if not main_db:
            _logger.info(
                'Demo data check skipped for %s: no saltstack.odoo_main_db '
                'setting or odoo_main_db grain', self.name)
            return False

        query = (
            "SELECT count(*) FROM res_company WHERE name ILIKE "
            "'%(san francisco)%' OR name ILIKE '%(chicago)%' OR name ILIKE '%(demo)%'"
        )
        cmd = 'psql -d %s -tAc "%s"' % (main_db, query)
        try:
            result = self._call_salt_api('local', self.name, 'cmd.run', cmd, timeout=60)
            raw = result.get('return', [{}])[0].get(self.name, '')
            count = int(str(raw).strip() or 0)
            self.odoo_has_demo_data = count > 0
            _logger.info('Demo data check for %s (%s): %s', self.name, main_db, count)
            return bool(count)
        except Exception as e:
            _logger.warning('Demo data check failed for %s: %s', self.name, e)
            return False

    # ── DC inference ─────────────────────────────────────────────────────

    _DC_BY_HOST = {
        # host_machine -> dc. Fylls på med den faktiska mappningen (open
        # question i design.md). Grain 'dc' är auktoritativ när den finns.
        # 'fors': 'ska',
    }

    def _infer_dc(self, host_machine, private_ip):
        """Best-effort DC inference when the grain lacks 'dc'."""
        if host_machine and host_machine in self._DC_BY_HOST:
            return self._DC_BY_HOST[host_machine]
        if private_ip:
            # Management network 192.168.11.x — ranges per DC unknown yet.
            pass
        return False

    # ── Actions ──────────────────────────────────────────────────────────

    def action_ping(self):
        """Ping this minion via Salt API and update status."""
        self.ensure_one()
        try:
            self._call_salt_api('local', self.name, 'test.ping')
            self.last_seen = fields.Datetime.now()  # state recomputes
            return {'success': True, 'minion': self.name, 'status': 'up'}
        except Exception as e:
            _logger.warning('Ping failed for %s: %s', self.name, e)
            return {'success': False, 'minion': self.name, 'error': str(e)}

    def action_copy_private_ip(self):
        """Copy the minion IP to the clipboard (list-view button).

        Falls back to public_ip so hosts that only have a public address
        (GleSYS/Hetzner) still copy something useful instead of an empty
        string. See T/11478.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'saltstack_copy_value',
            'params': {'value': self.private_ip or self.public_ip or ''},
        }

    def action_copy_hosts_list(self):
        """Copy a /etc/hosts-formatted list of all minions to the clipboard.

        Collective list-view header action: builds one hosts list from the
        private_ip of every active salt.minion record (no live Salt call),
        ready to paste into /etc/hosts.

        2026-09-14 (T/11478): hosts whose only address is public (the GleSYS
        fleet on 185.39.146.0/28, the Hetzner gateways) used to be dumped in
        the "Ej anslutna / IP saknas" section even though they are online.
        They now get their own section, and the trailing section only lists
        hosts with genuinely no address at all.
        """
        records = self.search([('active', '=', True)])
        lines = []
        lines.append('# Hosts Update — genererad av SaltStack (salt.minion)')
        lines.append('# Klistra in i din /etc/hosts (ersätt hela filen — listan är komplett).')
        lines.append('#')
        lines.append('# ── Maskiner med privat IP ────────────────────────────')
        entries = []
        for rec in records:
            if rec.private_ip:
                entries.append((rec.private_ip, rec.name))
        for ip, name in sorted(entries):
            lines.append('%-18s%s' % (ip, name))
        public_entries = [
            (rec.public_ip, rec.name) for rec in records
            if not rec.private_ip and rec.public_ip
        ]
        if public_entries:
            lines.append('#')
            lines.append('# ── Maskiner med endast publik IP (nås ej på LAN) ─────')
            for ip, name in sorted(public_entries):
                lines.append('%-18s%s' % (ip, name))
        no_ip = [
            rec.name for rec in records
            if not rec.private_ip and not rec.public_ip
        ]
        if no_ip:
            lines.append('#')
            lines.append('# ── IP saknas (ej synkad / offline) ───────────────────')
            for name in sorted(no_ip):
                lines.append('# %s' % name)
        return {
            'type': 'ir.actions.client',
            'tag': 'saltstack_copy_value',
            'params': {'value': '\n'.join(lines)},
        }

    def action_open_external_domain(self):
        """Open the external domain in a new tab (list-view button)."""
        self.ensure_one()
        if not self.external_domain:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Extern domän saknas'),
                    'message': _('No external domain set for %s.') % self.name,
                    'type': 'warning',
                },
            }
        url = self.external_domain
        if '://' not in url:
            url = 'https://' + url
        return {
            'type': 'ir.actions.act_url',
            'url': url,
            'target': 'new',
        }

    def action_show_alerts(self):
        """Open the alert list filtered to this host (smart button)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Drift Alerts — %s' % self.name,
            'res_model': 'saltstack.alert',
            'view_mode': 'list,form',
            'domain': [('host', '=', self.name)],
            'context': {'search_default_unresolved': 1},
        }

    @api.depends('runlog_ids')
    def _compute_runlog_count(self):
        for rec in self:
            rec.runlog_count = len(rec.runlog_ids)

    def action_view_runlogs(self):
        """Open the Driftslogg filtered to this minion (smart button)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Driftslogg — %s' % self.name,
            'res_model': 'saltstack.runlog',
            'view_mode': 'list,form',
            'domain': [('minion_id', '=', self.id)],
            'context': {'default_host': self.name,
                        'default_minion_id': self.id},
        }

    def action_add_runlog(self):
        """Open a prefilled Driftslogg form for a manual change note."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Ny driftslogg — %s' % self.name,
            'res_model': 'saltstack.runlog',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_host': self.name,
                'default_minion_id': self.id,
                'default_source': 'manual',
                'default_run_type': 'change',
                'default_status': 'ok',
                'default_timestamp': fields.Datetime.now(),
            },
        }

    def _search_partner_by_name(self, name):
        """Best-effort res.partner lookup from a minion/customer name.

        Tries an exact name match first, then a case-insensitive contains
        match — accepted only when it resolves to a single partner.
        """
        if not name:
            return self.env['res.partner']
        Partner = self.env['res.partner']
        exact = Partner.search([('name', '=', name)], limit=2)
        if len(exact) == 1:
            return exact
        if len(exact) > 1:
            return self.env['res.partner']  # ambiguous — leave unset
        # Contains-match, accept only when unambiguous.
        contains = Partner.search([('name', 'ilike', '%' + name + '%')], limit=2)
        if len(contains) == 1:
            return contains
        return self.env['res.partner']

    def _sanitize_dc(self, dc):
        """Return ``dc`` only when it is a valid selection value, else False.

        The ``dc`` grain is free-form text (any host can set ``dc: lab`` in
        /etc/salt/grains), while ``salt.minion.dc`` is a Selection of 'ska' /
        'sto'. Writing an unknown value raised
        ``ValueError: Wrong value for salt.minion.dc`` INSIDE the sync
        transaction, which rolled back the whole minion sync — no new minions
        were ever created (elevata/elevata-test, 2026-10-19; the culprit was
        jakob18 with ``dc: lab``).

        Unknown values are logged and dropped instead of aborting the sync.
        """
        if not dc:
            return False
        valid = self._fields['dc']._description_selection(self.env)
        if dc in dict(valid):
            return dc
        _logger.warning(
            'Ignoring unknown dc grain %r (valid: %s)',
            dc, ', '.join(v for v, _l in valid))
        return False

    def _apply_grains(self, grains):
        """Write grains data onto this record (no API call)."""
        self.ensure_one()
        if not grains or not isinstance(grains, dict):
            _logger.warning('No grains for %s (got %r)', self.name, grains)
            return False

        private_ip, public_ip = self._extract_ips(grains)

        roles = grains.get('roles', '')
        if isinstance(roles, list):
            roles = ','.join(roles)

        customer = grains.get('customer') or ''
        partner_id = self.partner_id.id
        if not partner_id and customer:
            partner = self.env['res.partner'].search(
                [('name', '=', customer)], limit=1)
            if partner:
                partner_id = partner.id
        # Fallback: infer the partner from the minion name when no customer
        # grain is present (e.g. lxd-web-svenskfast / azzar-at).
        if not partner_id:
            partner = self._search_partner_by_name(customer or self.name)
            if partner:
                partner_id = partner.id

        # 2026-09-13: LXD-containrar har INTE grainet 'lxc' (alltid tomt).
        # Ratt kalla ar 'virtual' == 'container' (verifierat: 71 av 95).
        # Fallback pa 'lxc' for aldre/udda minioner.
        _virt = str(grains.get('virtual') or '').strip().lower()
        is_container = _virt == 'container' or bool(grains.get('lxc'))
        if is_container:
            public_ip = ''

        dc = grains.get('dc') or self._infer_dc(grains.get('host'), private_ip)
        dc = self._sanitize_dc(dc)
        if not dc:
            dc = False

        role_list = [r.strip().lower() for r in roles.split(',') if r.strip()]
        has_gateway = 'gateway' in role_list or bool(grains.get('gw'))
        # `domain`-grainet sätts av onboard.grains (onboard.grains pillar
        # domain=...) och är den källa som finns på alla containrar.
        # `external_domain` finns bara på en del — läs den först, fall tillbaka
        # på `domain` så att t.ex. e-postmappningen (domän -> minion) fungerar.
        external_domain = (grains.get('external_domain')
                           or grains.get('domain') or '')

        self.write({
            'private_ip': private_ip,
            'public_ip': public_ip,
            'os': str(grains.get('os', '')).title(),
            'os_version': str(grains.get('osrelease', '')),
            'dc': dc,
            'roles': roles,
            'customer': customer,
            'partner_id': partner_id,
            'odoo_version': grains.get('odoo_version') or grains.get('odoo_full_version'),
            'is_container': is_container,
            'host_machine': grains.get('host'),
            'external_domain': external_domain,
            'has_gateway': has_gateway,
            'grains_json': json.dumps(grains, indent=2, default=str),
            'last_sync': fields.Datetime.now(),
        })
        self._set_default_image()
        self._update_demo_data(grains)
        return True

    def action_sync_from_grains(self):
        """Fetch grains from Salt API and populate this record."""
        self.ensure_one()
        try:
            result = self._call_salt_api('local', self.name, 'grains.items')
        except Exception as e:
            _logger.warning('Grains sync failed for %s: %s', self.name, e)
            return False
        grains = result.get('return', [{}])[0].get(self.name, {})
        return self._apply_grains(grains)

    def register_minion(self, name):
        """Create/refresh ONE minion record from its own grains.

        Called by ``/saltstack/minion`` right after ``container_create_core.sh``
        has accepted the Salt key, so a new container shows up in the kanban
        immediately instead of waiting for the 24-hour sync cron.

        Idempotent: an existing record is reactivated and refreshed. Returns a
        small dict so the caller can log the outcome.
        """
        name = (name or '').strip()
        if not name:
            return {'status': 'error', 'error': 'Missing minion id'}

        # Fetch grains for this single minion (cheap — one minion, not all).
        try:
            result = self._call_salt_api('local', name, 'grains.items', timeout=60)
            grains = result.get('return', [{}])[0].get(name) or {}
        except Exception as e:
            _logger.warning('register_minion: grains fetch failed for %s: %s',
                            name, e)
            grains = {}

        if not grains:
            # Salt key accepted but the minion has not answered yet. Create the
            # bare record so it is visible; the sync cron fills the grains in.
            _logger.warning(
                'register_minion: no grains for %s yet — creating bare record',
                name)

        existing = self.search([('name', '=', name)], limit=1)
        created = False
        if existing:
            if not existing.active:
                existing.active = True
            rec = existing
        else:
            rec = self.create({'name': name})
            created = True

        if grains:
            rec._apply_grains(grains)
            rec.last_seen = fields.Datetime.now()

        rec.message_post(body=(
            'Minion registrerad via onboarding-hook (%s).'
            % ('ny post' if created else 'befintlig post uppdaterad')))

        return {
            'status': 'ok',
            'minion': rec.name,
            'id': rec.id,
            'created': created,
            'grains': bool(grains),
        }

    # ── Odoo statistics ────────────────────────────────────────────────

    def _collect_odoo_stats(self):
        """Collect Odoo statistics from this minion via Salt API.

        Delegates to the Salt execution module `storage.odoo_stats`, which
        reads the same `odoo.conf` and the same database listing that the
        storage measurement already uses. The old implementation ran its own
        bash script over `cmd.run` — two code paths reading the same files,
        with two chances to drift apart.

        Best-effort: any failure leaves the fields untouched.
        """
        self.ensure_one()
        roles = [r.strip().lower() for r in (self.roles or '').split(',')
                 if r.strip()]
        if 'odoo' not in roles and not self.odoo_version:
            return False
        try:
            result = self._call_salt_api(
                'local', self.name, 'storage.odoo_stats', timeout=180,
            )
        except Exception as e:
            _logger.warning('Odoo stats collection failed for %s: %s',
                            self.name, e)
            return False

        rows = (result.get('return') or [{}])[0].get(self.name) or {}
        if isinstance(rows, dict):
            rows = rows.get('odoo_stats') or []
        if not rows:
            _logger.info('No Odoo databases reported for %s', self.name)
            return False

        dbs = []
        users_total = 0
        coworkers_total = 0
        tokens_total = 0
        last_login = None
        version = None
        for row in rows:
            db = row.get('database') or ''
            users_total += int(row.get('users_active') or 0)
            coworkers_total += int(row.get('coworkers') or 0)
            tokens_total += int(row.get('tokens_m') or 0)
            dbs.append('%s (demo)' % db if row.get('demo') else db)
            version = version or row.get('version') or None
            last_val = (row.get('last_login') or '').split('.')[0]
            if last_val and (not last_login or last_val > last_login):
                last_login = last_val

        vals = {
            'odoo_databases': ', '.join(dbs),
            'odoo_user_count': users_total,
            'odoo_coworker_count': coworkers_total,
            'odoo_coworker_tokens_m': tokens_total,
            'odoo_stats_synced': fields.Datetime.now(),
        }
        if version:
            vals['odoo_version'] = version
        if last_login:
            try:
                vals['odoo_last_login'] = fields.Datetime.to_datetime(last_login)
            except (ValueError, TypeError):
                _logger.warning('Could not parse last login %r for %s',
                                last_login, self.name)
        self.write(vals)
        return True

    def action_update_basic_info(self):
        """Server action (gear): update basic info for the selected minions.

        Primarily fills missed/unfilled identity fields (grains sync when
        stale) and always refreshes Odoo statistics (databases, users, last
        login, AI coworkers) for Odoo minions.
        """
        results = []
        for rec in self:
            touched = []
            # 1. Grains sync when stale or missing identity fields.
            stale = (not rec.last_sync
                     or rec.last_sync < fields.Datetime.subtract(
                         fields.Datetime.now(), hours=24)
                     or not rec.roles)
            if stale:
                try:
                    if rec.action_sync_from_grains():
                        touched.append('grains')
                except Exception as e:
                    _logger.warning('Grains sync failed for %s: %s', rec.name, e)
            # 2. Odoo statistics (always for Odoo minions).
            roles = [r.strip().lower() for r in (rec.roles or '').split(',') if r.strip()]
            if 'odoo' in roles or rec.odoo_version:
                try:
                    if rec._collect_odoo_stats():
                        touched.append('odoo-stats')
                except Exception as e:
                    _logger.warning('Odoo stats failed for %s: %s', rec.name, e)
            results.append('%s: %s' % (rec.name, ', '.join(touched) or 'oförändrad'))

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Grunduppgifter uppdaterade'),
                'message': '\n'.join(results),
                'type': 'success',
            },
        }

    # ── Storage measurement ──────────────────────────────────────────────

    _STORAGE_LXD_SCRIPT = r'''
base=$(mount | awk '$3 ~ /lxd/ && $1 !~ /snap|tmpfs|nsfs/ {print $3; exit}')
if [ -z "$base" ] && [ -d /var/snap/lxd/common/lxd/storage-pools ]; then
  base=/var/snap/lxd/common/lxd/storage-pools
fi
found=""
if [ -n "$base" ]; then
  for d in "$base/containers/{minion}" "$base"/*/containers/{minion}; do
    if [ -d "$d" ]; then found="$d"; break; fi
  done
fi
if [ -z "$found" ]; then echo "NONE"; else du -sb "$found" | awk '{print $1}'; fi
'''

    _STORAGE_LXD_HOST_TOTAL_SCRIPT = r'''
base=$(mount | awk '$3 ~ /lxd/ && $1 !~ /snap|tmpfs|nsfs/ {print $3; exit}')
if [ -z "$base" ] && [ -d /var/snap/lxd/common/lxd/storage-pools ]; then
  base=/var/snap/lxd/common/lxd/storage-pools
fi
# The pool name is an extra level on some hosts (strand: storage-pools/default/
# containers), so try both the bare and the globbed path.
found=""
if [ -n "$base" ]; then
  for d in "$base/containers" "$base"/*/containers; do
    if [ -d "$d" ]; then found="$d"; break; fi
  done
fi
if [ -n "$found" ]; then
  du -sb "$found" | awk '{print $1}'
else
  echo "NONE"
fi
'''

    def _lxd_host_candidates(self):
        """Salt minions that are REAL LXD hosts (not containers).

        A real LXD host has the lxd role and runs the lxc CLI AND is not
        itself a container (grains virtual != container — containers with a
        shared lxd socket would otherwise pass an 'lxc list' check). The
        host list is detected once, cached in ir.config_parameter, and
        refreshed when empty.
        """
        param = 'saltstorage.lxd_hosts'
        cached = self.env['ir.config_parameter'].get_param(param, '')
        if cached:
            return [h for h in cached.split(',') if h]
        api = self.env['saltstack.api']
        online = []
        try:
            res = api.salt_call('runner', None, 'manage.status', timeout=30)
            online = json.loads(res).get('return', [{}])[0].get('up', []) or []
        except Exception as e:
            _logger.warning('manage.status failed for LXD detection: %s', e)
        candidates = []
        for rec in self.env['salt.minion'].search([('active', '=', True)]):
            roles = [r.strip().lower() for r in (rec.roles or '').split(',') if r.strip()]
            if 'lxd' not in roles or rec.is_container:
                continue
            # Skip minions that are themselves containers (virtual grain).
            try:
                virtual = json.loads(rec.grains_json or '{}').get('virtual', '')
            except (ValueError, TypeError):
                virtual = rec.grains_json or ''
            if str(virtual).strip().lower() == 'container':
                continue
            if online and rec.name not in online:
                continue
            candidates.append(rec.name)
        hosts = []
        for name in candidates:
            try:
                res = api.salt_call(
                    'local', name, 'cmd.run',
                    "lxc list >/dev/null 2>&1 && echo LXD_OK || echo NOLXD",
                    timeout=6,
                )
                ret = str(json.loads(res).get('return', [{}])[0].get(name, ''))
                if 'LXD_OK' in ret:
                    hosts.append(name)
            except Exception:
                continue
        if hosts:
            self.env['ir.config_parameter'].set_param(param, ','.join(hosts))
        _logger.info('Detected %d real LXD hosts: %s', len(hosts), hosts)
        return hosts

    def _lxd_container_map(self, api, refresh=False):
        """Return {ip: host} for every container across all real LXD hosts.

        One `lxc list` call per host (not per minion). Keyed on IP because
        container NAMES collide across hosts (ledningssystem exists on both
        fors and ring). Cached in ir.config_parameter with a TTL.

        Only hosts that actually HAVE containers are cached — empty hosts
        (drake, gw0, gw1, heron, hoary, tahr, utv18, xerus, zenbook) are
        dropped so probing does not waste time.
        """
        param = 'saltstorage.lxd_container_map'
        if not refresh:
            raw = self.env['ir.config_parameter'].get_param(param, '')
            if raw:
                try:
                    data = json.loads(raw)
                    age = time.time() - float(data.get('ts', 0))
                    if age < 86400:  # 24 h TTL
                        return data.get('map', {}), data.get('hosts', [])
                except (ValueError, TypeError):
                    pass
        hosts = self._lxd_host_candidates()
        cmap, live_hosts = {}, []
        for host in hosts:
            try:
                res = api.salt_call(
                    'local', host, 'cmd.run',
                    "lxc list --format csv -c n4 2>/dev/null",
                    timeout=20,
                )
                out = str(json.loads(res).get('return', [{}])[0].get(host, ''))
            except Exception as e:
                _logger.warning('lxc list on %s failed: %s', host, e)
                continue
            found = 0
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                # CSV: "name","ip1 (eth0)\nip2 (eth1)" — strip quotes first.
                parts = line.split(',', 1)
                name = parts[0].strip().strip('"')
                ips = parts[1] if len(parts) > 1 else ''
                for m in re.finditer(r'(\d+\.\d+\.\d+\.\d+)', ips):
                    cmap[m.group(1)] = host
                    found += 1
                if name and not ips.strip():
                    # No IP yet (starting container) — skip, not usable as key.
                    pass
            if found:
                live_hosts.append(host)
        if cmap:
            self.env['ir.config_parameter'].set_param(param, json.dumps({
                'ts': time.time(), 'map': cmap, 'hosts': live_hosts,
            }))
            # Keep the legacy host cache in sync (used elsewhere).
            self.env['ir.config_parameter'].set_param(
                'saltstorage.lxd_hosts', ','.join(live_hosts))
        _logger.info('LXD container map: %d containers on %d hosts (%s)',
                     len(cmap), len(live_hosts), ','.join(live_hosts))
        return cmap, live_hosts

    def _measure_lxd(self, api):
        """Return (host, bytes) for this minion's container on an LXD host.

        Uses the cached lxd_host when known; otherwise probes each known
        LXD host for the container (short timeout), measures with du on the
        found host and caches the host on the minion.
        """
        # 2026-09-13: use the cached container map (one `lxc list` per host,
        # keyed on IP) instead of probing every host for this minion's name.
        # Name collisions exist (ledningssystem on both fors and ring).
        hosts = self._lxd_host_candidates()
        if self.lxd_host:
            hosts = [self.lxd_host] + [h for h in hosts if h != self.lxd_host]
        cmap, _live = self._lxd_container_map(api)
        mapped = cmap.get((self.private_ip or '').strip())
        if mapped:
            hosts = [mapped] + [h for h in hosts if h != mapped]
        for host in hosts:
            if mapped and host != mapped:
                continue
            if not mapped:
                # Fallback: no IP match — probe the host for the name.
                try:
                    res = api.salt_call(
                        'local', host, 'cmd.run',
                        'lxc list --format csv -c n 2>/dev/null',
                        timeout=6,
                    )
                    out = str(json.loads(res).get('return', [{}])[0].get(host, ''))
                except Exception as e:
                    _logger.warning('LXD host probe %s failed: %s', host, e)
                    continue
                if self.name not in out.splitlines():
                    continue
            try:
                res2 = api.salt_call(
                    'local', host, 'cmd.run',
                    self._STORAGE_LXD_SCRIPT.replace('{minion}', self.name),
                    timeout=120,
                )
                raw = str(json.loads(res2).get('return', [{}])[0].get(host, '')).strip()
            except Exception as e:
                _logger.warning('LXD usage measure on %s failed: %s', host, e)
                continue
            if raw and raw != 'NONE' and raw.isdigit():
                if not self.lxd_host:
                    self.lxd_host = host
                return host, int(raw)
        return None, None

    def _dirvish_hosts(self):
        """Salt minions that run dirvish/backup.

        Three servers run dirvish: dirvishtull0, dirvishtull1 and strand.
        They do NOT share a role — dirvishtull0/1 have ``backup`` while
        strand has ``storage``. Matching on ``backup`` alone silently
        dropped strand (and its T2 vaults) from every lookup.
        """
        return self.env['salt.minion'].search([
            ('active', '=', True),
            '|',
            ('roles', 'ilike', '%backup%'),
            ('name', 'in', ['strand']),
        ]).mapped('name')

    def _dirvish_vault_map(self, api, force=False):
        """Map vault name -> backup host, by listing /srv/backup on each host.

        The vault name is NOT always the LXD host name (strand's vault is
        called ``tull0``) and the vaults are spread over several backup
        servers, so a vault must be looked up rather than assumed. The map
        is cached in ``ir.config_parameter`` (``saltstorage.dirvish_vaults``)
        for ``saltstorage.dirvish_vaults_ttl`` seconds (default 24 h).

        Returns a dict ``{vault_name: host}``; empty on failure.
        """
        param = 'saltstorage.dirvish_vaults'
        icp = self.env['ir.config_parameter']
        if not force:
            try:
                ttl = int(icp.get_param('saltstorage.dirvish_vaults_ttl', '86400'))
            except ValueError:
                ttl = 86400
            cached = icp.get_param(param, '')
            if cached:
                try:
                    data = json.loads(cached)
                    if time.time() - float(data.get('_at', 0)) < ttl:
                        return {k: v for k, v in data.items() if not k.startswith('_')}
                except (ValueError, TypeError):
                    pass

        hosts = self._dirvish_hosts()
        if not hosts:
            return {}
        vaults = {}
        for host in hosts:
            try:
                res = api.salt_call(
                    'local', host, 'cmd.run',
                    'ls -1 /srv/backup 2>/dev/null || true', timeout=30)
                raw = str(json.loads(res).get('return', [{}])[0].get(host, ''))
            except Exception as e:
                _logger.warning('Dirvish vault listing failed on %s: %s', host, e)
                continue
            for name in raw.split():
                # First host wins; a vault should only exist on one server.
                vaults.setdefault(name, host)
        if vaults:
            payload = dict(vaults)
            payload['_at'] = time.time()
            icp.set_param(param, json.dumps(payload))
            _logger.info('Dirvish vault map cached: %d vaults on %s',
                         len(vaults), hosts)
        return vaults

    def _dirvish_vault_size(self, api, dhost, vault):
        """Read a vault's size from the nightly ``dirvish-du`` report.

        ``dirvish-du`` (cron 23:04 on every backup server) writes the vault
        size to ``/srv/backup/<vault>/dirvish/du``, e.g. ``517G\t.``. That
        file is the cheapest source available — reading it costs nothing,
        whereas running our own ``du`` over a multi-hundred-GB vault tree
        takes minutes and hammers the backup disk.

        The file is only trusted when it is non-empty and younger than
        ``saltstorage.dirvish_du_max_age`` hours (default 48). ``dirvish-du``
        can hang for days on the large vaults, and a stale size must not be
        presented as current.

        Returns the size in bytes as an int, or None when unavailable.
        """
        path = '/srv/backup/%s/dirvish/du' % vault
        try:
            max_age = int(self.env['ir.config_parameter'].get_param(
                'saltstorage.dirvish_du_max_age', '48'))
        except ValueError:
            max_age = 48
        # -s: only report a non-empty file; -m: mtime in epoch seconds.
        # Wrapped in bash -c: a bare ';' makes Salt treat the parts as
        # separate cmd.run arguments ("run() missing 1 required positional
        # argument: 'cmd'").
        inner = ('f=%s; [ -s "$f" ] && echo "$(cat $f)|$(stat -c %%Y "$f")"'
                 % path)
        cmd = "bash -c '%s'" % inner
        try:
            res = api.salt_call('local', dhost, 'cmd.run', cmd, timeout=20)
            raw = str(json.loads(res).get('return', [{}])[0].get(dhost, '')).strip()
        except Exception as e:
            _logger.warning('Dirvish du read failed for %s on %s: %s', vault, dhost, e)
            return None
        if not raw:
            _logger.info('Dirvish du file empty or missing for %s on %s', vault, dhost)
            return None
        size_part, _, mtime_part = raw.partition('|')
        if mtime_part.isdigit():
            age_h = (time.time() - int(mtime_part)) / 3600.0
            if age_h > max_age:
                _logger.info('Dirvish du for %s on %s is stale (%.1f h > %d h)',
                             vault, dhost, age_h, max_age)
                return None
        # Format: "517G\t." (du -sh) — may also be "1.5T" or plain bytes.
        m = re.match(r'^([0-9.]+)\s*([KMGTP]?)', size_part.split('\t')[0].strip())
        if not m:
            return None
        try:
            value = float(m.group(1))
        except ValueError:
            return None
        unit = (m.group(2) or '').upper()
        factor = {'': 1, 'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3, 'T': 1024 ** 4,
                  'P': 1024 ** 5}.get(unit, 1)
        return int(value * factor)

    def _dirvish_ratio(self, api, lxd_host):
        """Dirvish/LXD ratio for a host.

        Dirvish backs up whole hosts (e.g. fors, strand) — there is no
        per-container backup tree. The per-minion Dirvish share is estimated
        as minion_LXD_usage × ratio, where
        ratio = dirvish_vault_size(host) / lxd_total(host).

        The vault is looked up in the vault map (name != host in general)
        and its size is read from the nightly ``dirvish-du`` report — no
        ``du`` is ever started from here.

        Returns the cached ratio when available, otherwise the configurable
        default (`saltstorage.dirvish_ratio_default`, default 2.0) so the
        Dirvish row appears immediately.
        """
        if not lxd_host:
            return None
        param = 'saltstorage.dirvish_ratio.%s' % lxd_host
        icp = self.env['ir.config_parameter']
        cached = icp.get_param(param, '')
        if cached:
            try:
                return float(cached)
            except ValueError:
                pass
        default = float(icp.get_param('saltstorage.dirvish_ratio_default', '2.0'))

        vaults = self._dirvish_vault_map(api)
        dhost = vaults.get(lxd_host)
        if not dhost:
            _logger.info('No dirvish vault found for %s (vault map: %d entries)',
                         lxd_host, len(vaults))
            return default

        dirvish_bytes = self._dirvish_vault_size(api, dhost, lxd_host)
        if not dirvish_bytes:
            _logger.info('No dirvish du value for %s on %s', lxd_host, dhost)
            return default

        lxd_bytes = self._lxd_host_total(api, lxd_host)
        if not lxd_bytes:
            return default
        ratio = dirvish_bytes / float(lxd_bytes)
        icp.set_param(param, '%.4f' % ratio)
        _logger.info('Dirvish ratio cached for %s: %.4f (dirvish=%s, lxd=%s)',
                     lxd_host, ratio, dirvish_bytes, lxd_bytes)
        return ratio

    def _lxd_host_total(self, api, lxd_host):
        """Total LXD usage on a host (bytes), or None."""
        try:
            res = api.salt_call(
                'local', lxd_host, 'cmd.run',
                self._STORAGE_LXD_HOST_TOTAL_SCRIPT, timeout=300)
            raw = str(json.loads(res).get('return', [{}])[0].get(lxd_host, '')).strip()
        except Exception:
            return None
        return int(raw) if raw.isdigit() and int(raw) > 0 else None

    def _backup_customer(self):
        """Kundnamn i backup-rapporten (restic-status.json).

        Prioritet: explicit ``snapshot_customer`` -> ``customer``-grain ->
        minionens namn. Det explicita fältet behövs eftersom
        ``odoo.grains`` tvingar ``customer = <minion-id>`` på
        Odoo-värdar, och eftersom bucketnamnet kan skilja sig från
        minionen (t.ex. bucketen "sparv" på minionen "sparv-test").
        """
        self.ensure_one()
        return (
            self.snapshot_customer or self.customer or self.name or ''
        ).strip().lower()

    def _measure_s3(self, api):
        """Return (src_gb, backup_gb, customer) from the restic status report.

        The restic minion publishes /var/log/garage-backup/restic-status.json
        with per-customer src bucket size and restic backup bucket size.
        """
        restic_minion = self.env['salt.minion'].search([
            ('active', '=', True),
            ('name', '=', 'restic'),
        ], limit=1)
        if not restic_minion:
            return None, None, None
        try:
            res = api.salt_call(
                'local', 'restic', 'cmd.run',
                'cat /var/log/garage-backup/restic-status.json',
                timeout=45,
            )
            raw = json.loads(res).get('return', [{}])[0].get('restic', '')
        except Exception as e:
            _logger.warning('restic status read failed: %s', e)
            return None, None, None
        try:
            data = json.loads(str(raw))
        except (ValueError, TypeError):
            return None, None, None
        customer = self._backup_customer()
        for row in data.get('customers') or []:
            if str(row.get('customer', '')).strip().lower() == customer:
                src = row.get('size') or 0
                backup = row.get('backup_size') or 0
                return (src / 1024.0 ** 3, backup / 1024.0 ** 3,
                        row.get('customer'))
        return None, None, None

    def _upsert_storage(self, storage_type, size_gb, provider, method,
                        is_measured=True, bytes_used=None,
                        bytes_allocated=None):
        """Create or update one storage row for this minion.

        `storage_type` is normalised through the storage model's
        LEGACY_TYPE_MAP, so a stale caller cannot reintroduce a retired type
        (e.g. `dirvish`). `bytes_used` wins over `size_gb` when both are
        given — bytes are the source of truth.
        """
        self.ensure_one()
        Storage = self.env['salt.minion.storage']
        storage_type = Storage._normalise_type(storage_type)
        if bytes_used is None:
            if size_gb is None:
                return
            bytes_used = size_gb * 1024.0 ** 3
        row = Storage.search([
            ('minion_id', '=', self.id),
            ('storage_type', '=', storage_type),
        ], limit=1)
        vals = {
            'bytes_used': bytes_used,
            'bytes_allocated': bytes_allocated or 0.0,
            'provider': provider or '',
            'method': method or '',
            'is_measured': is_measured,
            'measured_at': fields.Datetime.now(),
        }
        if row:
            row.write(vals)
        else:
            Storage.create({
                'minion_id': self.id,
                'storage_type': storage_type,
                **vals,
            })

    # Mapping from the Salt module's artefact keys to the storage types on
    # the minion. `lxd_resources` is NOT storage (CPU/RAM) and is skipped.
    _STORAGE_TYPE_FROM_ARTIFACT = {
        'lxd_container': 'lxd_container',
        'postgres_db': 'postgres',
        'filestore_s3': 'filestore_s3',
        # NOTE: borg_db/borg_container are NOT mapped here. Their raw archive
        # sizes belong to the whole PostgreSQL volume / LXD host, not to one
        # minion. `postgres_borg` is derived from a key figure and
        # `disk_borg` from the repository total — both handled below.

        'restic_filestore': 'filestore_restic',
    }

    @staticmethod
    def _ratio_from_rows(rows):
        """compressed/original from already-fetched Borg rows.

        Preferred over a fresh API call: the rows are already in hand, and a
        second `storage.borg` call inside the same collection can race with
        the repository lock.
        """
        best = None
        for row in rows:
            original = float(row.get('bytes_secondary') or 0)
            compressed = float(row.get('bytes_used') or 0)
            if original and compressed and (best is None or original > best[1]):
                best = (compressed, original)
        return (best[0] / best[1]) if best else 0.0

    def _postgres_backup_share(self):
        """Disk cost of this database's share of the PostgreSQL backup.

        THE MODEL
            The databases live in the `postgres16` CONTAINER, so that is the
            archive that holds them. (`fors-lxd-db-server-*` holds the
            server's configuration — 60 MB per archive — not the data. Using
            it gave a compression ratio of 0.008 and a `postgres_borg` of
            0.03 GB, off by a factor of 42.)

                share   = pg_database_size / sum(all databases on the server)
                cost    = share * sum(deduplicated_size of postgres16 archives)

            `deduplicated_size` is the only figure that answers "how much
            disk does the backup take": each nightly archive is a full copy
            of the container, so summing `original` or `compressed` counts
            the same bytes 16 times.

        KNOWN UNDERCOUNT
            Databases that have not yet had a retention pass add more bytes
            than they will in steady state, and the share is computed from
            today's sizes while the backup accumulated over time. The result
            is therefore an ESTIMATE, and is marked as such.

        Returns (cost_bytes, share, total_backup_bytes) or (0, 0, 0).
        """
        self.ensure_one()
        api = self.env['saltstack.api']
        borg_host = self.env['ir.config_parameter'].sudo().get_param(
            'saltstack_storage.borg_host', '') or 'tull1'
        lxd_host = self.lxd_host or self.host_machine
        if not lxd_host:
            return 0.0, 0.0, 0.0

        # The PostgreSQL container on this host. Its name is configured so a
        # renamed container does not silently break the calculation.
        pg_container = self.env['ir.config_parameter'].sudo().get_param(
            'saltstack_storage.postgres_container', '') or 'postgres16'

        try:
            raw = api.salt_call('local', borg_host, 'storage.borg_totals',
                                lxd_host, pg_container, timeout=900)
            res = json.loads(raw) if isinstance(raw, str) else raw
            payload = (res.get('return') or [{}])[0].get(borg_host) or {}
            rows = payload.get('borg_total') or []
        except Exception as exc:  # noqa: BLE001
            _logger.warning('Borg postgres totals failed: %s', exc)
            return 0.0, 0.0, 0.0

        total_backup = float((rows[0] if rows else {}).get('bytes_used') or 0)
        if not total_backup:
            return 0.0, 0.0, 0.0

        pg_row = self.storage_ids.filtered(
            lambda r: r.storage_type == 'postgres')
        own = float(pg_row[0].bytes_used or 0) if pg_row else 0.0
        if not own:
            return 0.0, 0.0, 0.0

        # Denominator: EVERY database on the server, asked directly.
        # Summing our own measured rows is wrong — only minions that have
        # been collected appear there, so a single measured database gave a
        # share of 100 %.
        total_db = 0.0
        try:
            raw = api.salt_call('local', pg_container, 'storage.pg_total_size',
                                timeout=300)
            res = json.loads(raw) if isinstance(raw, str) else raw
            payload = (res.get('return') or [{}])[0].get(pg_container) or {}
            total_db = float(payload.get('total_bytes') or 0)
        except Exception as exc:  # noqa: BLE001
            _logger.warning('PostgreSQL total size failed: %s', exc)
        if not total_db:
            return 0.0, 0.0, 0.0

        share = own / total_db
        return total_backup * share, share, total_backup

    def _measure_storage(self):
        """Measure all storage types for this minion.

        Delegates to the Salt execution module `storage.info`, which already
        knows how to measure every artefact (and where the decoy-PostgreSQL
        trap is). This method only maps the artefacts onto the seven storage
        types and persists them.

        The old implementation measured only three types with its own
        `_measure_lxd`/`_measure_s3` helpers — `postgres`, `postgres_borg`,
        `disk` and `disk_borg` were never written, so the Storage tab showed
        a single row.
        """
        self.ensure_one()
        api = self.env['saltstack.api']

        def call(target, container, minion=None, archive_stats=1):
            # storage.info(container, archive_stats, minion) — the argument
            # ORDER matters. Passing the minion second silently landed it in
            # archive_stats, so borg_totals never saw the container name.
            raw = api.salt_call('local', target, 'storage.info',
                                container, archive_stats, minion or container,
                                timeout=900)
            res = json.loads(raw) if isinstance(raw, str) else raw
            return (res.get('return') or [{}])[0].get(target) or {}

        # The artefacts live on DIFFERENT hosts:
        #   - the container's own footprint + Odoo DB/filestore: on the
        #     container itself
        #   - the LXD resources: on the LXD host, with the container as arg
        #   - the Borg archives: on the backup host, keyed by the LXD host
        #     that owns the container
        # Calling storage.info only on the container found the first group
        # and silently missed Borg, which is why the backup types were empty.
        def merge(dest, src):
            """Merge src into dest, keeping non-empty values.

            A plain `dict.update` lets the LXD host's empty lists overwrite
            the container's real data — the container answers for
            postgres_db/filestore_s3, the host answers for borg/lxd_resources.
            """
            for key, value in (src or {}).items():
                if isinstance(value, list):
                    if value:
                        dest.setdefault(key, []).extend(value)
                elif value:
                    dest[key] = value

        payload = {}
        try:
            merge(payload, call(self.name, self.name))
        except Exception as exc:  # noqa: BLE001
            _logger.warning('storage.info failed for %s: %s', self.name, exc)

        lxd_host = self.lxd_host or self.host_machine
        if lxd_host:
            try:
                # LXD host: CPU/RAM and the container's own footprint.
                merge(payload, call(lxd_host, self.name, self.name))
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    'storage.info failed for LXD host %s (%s): %s',
                    lxd_host, self.name, exc)

        borg_host = self.env['ir.config_parameter'].sudo().get_param(
            'saltstack_storage.borg_host', '') or 'tull1'
        if lxd_host:
            try:
                # Borg lives on the BACKUP host, keyed by the LXD host that
                # owns the container — not on the LXD host itself.
                borg_payload = call(borg_host, lxd_host, self.name)
                for key in ('borg_db', 'borg_container'):
                    rows = borg_payload.get(key) or []
                    mine = [r for r in rows
                            if self.name in (r.get('subject') or '')]
                    if mine:
                        payload.setdefault(key, []).extend(mine)
                # (c) The minion's disk_borg is the SUM of every Borg archive
                # on its LXD host — not the newest archive per volume.
                if borg_payload.get('borg_total'):
                    payload['borg_total'] = borg_payload['borg_total']
                # The DB-archive rows are needed for the backup key figure
                # even when none of them belongs to this minion by name: the
                # ratio is a property of the whole PostgreSQL volume.
                # `storage.borg` filters by container name, so ask it for the
                # unfiltered archive list explicitly.
                try:
                    raw_all = api.salt_call(
                        'local', borg_host, 'storage.borg', lxd_host, 0,
                        timeout=900)
                    res_all = (json.loads(raw_all)
                               if isinstance(raw_all, str) else raw_all)
                    all_payload = ((res_all.get('return') or [{}])[0]
                                   .get(borg_host) or {})
                    if all_payload.get('borg_db'):
                        payload['_borg_db_all'] = all_payload['borg_db']
                except Exception as exc:  # noqa: BLE001
                    _logger.warning('Borg DB rows for ratio failed: %s', exc)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    'storage.info failed for Borg host %s: %s',
                    borg_host, exc)

        # Restic repositories live on the restic minion, keyed by customer
        # name. Asked separately because the measurement needs that host's
        # rclone credentials and repository password.
        restic_host = self.env['ir.config_parameter'].sudo().get_param(
            'saltstack_storage.restic_host', '') or 'restic'
        # The bucket is named after the CUSTOMER, but the `customer` grain on
        # an Odoo host is forced to the owning customer (e.g. "vertel"), not
        # the container name. Try the configured customer first, then the
        # minion name — the bucket is usually named after the container.
        candidates = []
        for name in (self.snapshot_customer, self._backup_customer(),
                     self.name):
            name = (name or '').strip().lower()
            if name and name not in candidates:
                candidates.append(name)

        for customer in candidates:
            try:
                raw = api.salt_call('local', restic_host,
                                    'storage.restic_stats', customer,
                                    timeout=600)
                res = json.loads(raw) if isinstance(raw, str) else raw
                rows = ((res.get('return') or [{}])[0].get(restic_host) or {}) \
                    .get('restic_filestore') or []
                if rows:
                    payload['restic_filestore'] = rows
                    break
            except Exception as exc:  # noqa: BLE001
                _logger.warning('Restic stats failed for %s: %s',
                                customer, exc)

        if not isinstance(payload, dict) or not payload:
            _logger.warning('storage.info for %s returned no payload',
                            self.name)
            return False

        # Several artefacts can map to the same type (e.g. two Borg archives
        # → postgres_borg). The type is unique per minion, so the values are
        # SUMMED rather than overwritten — otherwise the last row wins and
        # the storage looks smaller than it is.
        totals = {}
        for artifact, rows in payload.items():
            storage_type = self._STORAGE_TYPE_FROM_ARTIFACT.get(artifact)
            if not storage_type:
                continue
            for row in rows or []:
                used = float(row.get('bytes_used') or 0)
                alloc = float(row.get('bytes_allocated') or 0)
                if not used and not alloc:
                    continue
                agg = totals.setdefault(storage_type, {
                    'used': 0.0, 'alloc': 0.0,
                    'measured': True, 'methods': [], 'providers': [],
                })
                agg['used'] += used
                agg['alloc'] += alloc
                if row.get('method') != 'measured':
                    agg['measured'] = False
                if row.get('method'):
                    agg['methods'].append(row['method'])
                if row.get('source'):
                    agg['providers'].append(row['source'])

        # (b) postgres_borg as a KEY FIGURE, not a raw archive size.
        #
        # A Borg DB archive covers the whole PostgreSQL volume, not one
        # customer's database. Attributing that archive to this minion would
        # over-report it massively. Instead:
        #
        #   ratio = sum(all DB backups) / sum(all databases)   <- key figure
        #   postgres_borg = ratio * this database's size
        #
        # The ratio is computed from the fleet-wide totals, so it is stable
        # and comparable across customers.
        db_size = (totals.get('postgres') or {}).get('used') or 0.0
        if db_size:
            cost, share, total_backup = self._postgres_backup_share()
            if cost:
                totals['postgres_borg'] = {
                    'used': cost,
                    'alloc': 0.0,
                    'measured': False,
                    'methods': ['estimated'],
                    'providers': ['andel %.1f%% av %.2f GB' % (
                        share * 100, total_backup / 1024.0 ** 3)],
                }

        # (c) disk_borg = the minion's OWN Borg archives, deduplicated.
        #
        # NOT the repository total: on fors that covers 68 containers, so
        # `ledningssystem` was credited with 314 GB and 1129 archives that
        # belonged to the whole host.
        #
        # `deduplicated_size` is the only honest figure — 19 daily archives
        # of one container are 435 GB original but 13 GB deduplicated, since
        # each archive is a full copy of the same bytes.
        borg_total = (payload.get('borg_total') or [{}])[0]
        if borg_total.get('bytes_used'):
            source = borg_total.get('source') or 'archive-cache'
            totals['disk_borg'] = {
                'used': float(borg_total['bytes_used']),
                'alloc': 0.0,
                'measured': borg_total.get('method') == 'measured',
                'methods': [borg_total.get('method') or 'measured'],
                'providers': ['borg %s (%s arkiv)' % (
                    source, borg_total.get('archives', 0))],
            }

        for storage_type, agg in totals.items():
            self._upsert_storage(
                storage_type, None,
                ', '.join(sorted(set(agg['providers'])))[:120],
                ', '.join(sorted(set(agg['methods'])))[:120],
                is_measured=agg['measured'],
                bytes_used=agg['used'],
                bytes_allocated=agg['alloc'],
            )
        _logger.info('storage: %s types written for %s',
                     len(totals), self.name)
        return bool(totals)

    def action_measure_storage(self):
        """Server action (gear/button): measure storage for the selected minions."""
        results = []
        for rec in self:
            try:
                rec._measure_storage()
                total = rec.storage_total_gb
                results.append('%s: %.1f GB total' % (rec.name, total))
            except Exception as e:
                _logger.exception('Storage measurement failed for %s', rec.name)
                results.append('%s: fel (%s)' % (rec.name, e))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Storage mätt'),
                'message': '\n'.join(results),
                'type': 'success',
            },
        }

    def action_sync_all_minions(self):
        """List all minions via Salt API and create/update records.

        Protected sync (2026-08-23): an empty/erroneous manage.status result
        refuses to run the deactivation pass, so a broken Salt API can never
        wipe the registry. Sets last_seen for up minions so the semaphore
        reflects the sync, and backfills open_alert_count.
        """
        try:
            result = self._call_salt_api('runner', None, 'manage.status')
        except Exception as e:
            _logger.warning('Minion list failed: %s', e)
            return {'error': str(e)}

        returned = result.get('return', [{}])[0]
        if not isinstance(returned, dict):
            returned = {}
        up = returned.get('up') or []
        down = returned.get('down') or []
        all_minions = list(up) + list(down) + list(returned.get('unaccepted') or [])

        # Protect the registry: never deactivate everything on an empty result.
        if not all_minions:
            _logger.error(
                'manage.status returned no minions (raw=%s); refusing to '
                'sync/deactivate', result)
            return {'error': 'manage.status returned no minions; registry untouched'}

        # Bulk-grains: ett anrop mot alla minioner → {minion_id: grains}
        bulk_grains = {}
        try:
            # Timeout 120 s: a bulk grains.items against ~115 minions takes
            # ~55 s in production (measured 2026-10-19). At the old 45 s the
            # call timed out silently, bulk_grains stayed {} and every new
            # minion was created with ONLY a name (no IP, no customer).
            bulk_result = self._call_salt_api('local', '*', 'grains.items', timeout=120)
            bulk_grains = bulk_result.get('return', [{}])[0] or {}
        except Exception as e:
            _logger.warning('Bulk grains fetch failed: %s', e)

        now = fields.Datetime.now()
        created = 0
        updated = 0
        deactivated = 0
        for minion_id in all_minions:
            grains = bulk_grains.get(minion_id, {})
            existing = self.search([('name', '=', minion_id)], limit=1)
            if existing:
                if not existing.active:
                    existing.active = True
                    updated += 1
                existing._apply_grains(grains)
                if minion_id in up:
                    existing.last_seen = now  # semaphore: online (if no alerts)
                updated += 1
            else:
                rec = self.create({'name': minion_id})
                rec._apply_grains(grains)
                if minion_id in up:
                    rec.last_seen = now
                created += 1

        # Deactivate minions no longer present on the Salt master
        # (only reached when manage.status returned a healthy list).
        known = self.search([('active', '=', True)])
        for rec in known:
            if rec.name not in all_minions:
                rec.active = False
                deactivated += 1

        # NOTE (2026-09-24, T/11607): the open_alert_count backfill used to run
        # here. It issued one search_count per minion, which forced a flush of
        # the pending last_seen writes mid-transaction and collided with
        # concurrent writers -> SerializationFailure -> the ENTIRE sync was
        # rolled back, freezing last_seen for every minion.
        #
        # It now runs in its own cron (action_cron_backfill_alert_counts,
        # data/alert_count_cron.xml) so this transaction only touches
        # salt_minion and stays short.

        return {
            'created': created,
            'updated': updated,
            'deactivated': deactivated,
            'total': len(all_minions),
        }

    def action_cron_backfill_alert_counts(self):
        """Scheduled backfill of open_alert_count for all minions.

        Split out of the minion sync (T/11607) so the sync transaction stays
        short. The field is normally maintained by saltstack.alert on
        create/resolve; this job corrects drift and covers the first run after
        an upgrade.
        """
        minions = self.search([('active', '=', True)])
        minions._update_open_alert_count()
        return {'minions': len(minions)}

    def action_cron_sync_all(self):
        """Scheduled sync: refresh minions and pillars from Salt Master.

        Called by the sync cron job. Idempotent.
        """
        minion_result = self.action_sync_all_minions()
        try:
            pillar_result = self.env['salt.pillar'].action_sync_from_salt()
        except Exception as e:
            pillar_result = {'error': str(e)}
        return {
            'minions': minion_result,
            'pillars': pillar_result,
        }

    # ── Fault Injection (server actions) ────────────────────────────────

    def action_simulate_full_chain(self, fault_type='stop_odoo'):
        """Inject a fault AND trigger the full alert chain.

        fault_type: 'stop_odoo' | 'grow_log' | 'wazuh_bruteforce'

        1. Injects the real fault on the minion
        2. Builds a webhook payload matching what Zabbix/Wazuh would send
        3. Calls process_webhook() internally to create the alert + start AI diagnosis
        4. Returns the alert ID so the operator can follow the chain
        """
        self.ensure_one()

        # Step 1: Inject fault
        fault_map = {
            'stop_odoo': {
                'method': 'action_fault_stop_odoo',
                'source': 'zabbix',
                'category': 'process',
                'severity': 12,
                'trigger_name': 'Odoo HTTP endpoint not responding',
                'description': 'Simulerat larm: odoo stoppad via server action på %s' % self.name,
            },
            'grow_log': {
                'method': 'action_fault_grow_log',
                'source': 'zabbix',
                'category': 'system',
                'severity': 12,
                'trigger_name': 'No free disk space',
                'description': 'Simulerat larm: diskfyllning (grow.log) via server action på %s' % self.name,
            },
            'wazuh_bruteforce': {
                'method': 'action_fault_wazuh_bruteforce',
                'source': 'wazuh',
                'category': 'system',
                'severity': 12,
                'trigger_name': 'SSH brute force detected',
                'description': 'Simulerat larm: Wazuh brute-force via server action på %s' % self.name,
                'wazuh_rule_id': '5710',
            },
        }
        fault = fault_map.get(fault_type)
        if not fault:
            return {'success': False, 'error': 'Unknown fault_type: %s' % fault_type}

        # Run fault injection
        fault_result = getattr(self, fault['method'])()

        # Step 2: Build webhook payload
        payload = {
            'host': self.name,
            'source': fault['source'],
            'category': fault['category'],
            'severity': fault['severity'],
            'trigger_name': fault['trigger_name'],
            'description': fault['description'],
            'raw_log': str(fault_result.get('result', '')),
        }
        if 'wazuh_rule_id' in fault:
            payload['wazuh_rule_id'] = fault['wazuh_rule_id']

        # Step 3: Trigger alert chain internally
        alert_model = self.env['saltstack.alert']
        webhook_result = alert_model.process_webhook(payload)

        # Step 4: Post summary
        alert_id = webhook_result.get('alert_id', '?')
        self.message_post(
            body=(
                f'🧪 <b>Simulerad full larmkedja</b><br/>'
                f'Typ: {fault_type}<br/>'
                f'Alert: #{alert_id}<br/>'
                f'Diagnos startad: {webhook_result.get("diagnosis_started", False)}<br/>'
                f'Deduplicerad: {webhook_result.get("deduplicated", False)}<br/>'
                f'<b>Följ alert #{alert_id} för att se diagnos och åtgärd.</b>'
            ),
            message_type='notification',
        )

        return {
            'success': True,
            'fault_type': fault_type,
            'fault_result': fault_result,
            'alert_id': alert_id,
            'diagnosis_started': webhook_result.get('diagnosis_started', False),
            'deduplicated': webhook_result.get('deduplicated', False),
        }

    def action_fault_stop_odoo(self):
        """Stop the Odoo service on this minion. Zabbix will alert."""
        self.ensure_one()
        try:
            result = self._call_salt_api(
                'local', self.name, 'cmd.run', 'systemctl stop odoo', timeout=60)
            self.message_post(
                body=('🛑 <b>Odoo stopped</b> — fault injection on %s.<br/>'
                      'Zabbix should alert "Odoo HTTP endpoint not responding".'
                      % self.name),
                message_type='notification')
            return {'success': True, 'minion': self.name, 'result': result}
        except Exception as e:
            _logger.warning('Fault stop odoo failed for %s: %s', self.name, e)
            return {'success': False, 'error': str(e)}

    def action_fault_grow_log(self):
        """Grow a log file until disk fills. Zabbix will alert.

        Creates /var/log/odoo/grow.log (2000MB default) so an agent can
        find and remove it during remediation.
        """
        self.ensure_one()
        try:
            result = self._call_salt_api(
                'local', self.name, 'cmd.run',
                'dd if=/dev/zero of=/var/log/odoo/grow.log bs=1M count=2000',
                timeout=300)
            self.message_post(
                body=('💾 <b>Disk full simulation</b> — %s.<br/>'
                      'File: <code>/var/log/odoo/grow.log</code> (2000MB).'
                      ' Zabbix should alert "No free space".'
                      % self.name),
                message_type='notification')
            return {'success': True, 'minion': self.name, 'result': result}
        except Exception as e:
            _logger.warning('Fault grow log failed for %s: %s', self.name, e)
            return {'success': False, 'error': str(e)}

    def action_fault_wazuh_bruteforce(self):
        """Append failed SSH logins to auth.log. Wazuh agent will detect.

        The Wazuh agent on the minion reads auth.log, detects the brute
        force pattern, forwards to Wazuh Manager, which propagates to
        Zabbix.
        """
        self.ensure_one()
        try:
            # Append a burst of failed SSH login attempts to auth.log
            cmd = (
                'for i in $(seq 1 30); do '
                'echo "$(date +\'%b %e %H:%M:%S\') testhost sshd[$(shuf -i 1000-9999 -n1)]: '
                'Failed password for invalid user admin from 203.0.113.42 port $(shuf -i 40000-60000 -n1) ssh2" '
                '>> /var/log/auth.log; done'
            )
            result = self._call_salt_api(
                'local', self.name, 'cmd.run', cmd, timeout=60)
            self.message_post(
                body=('🚨 <b>Wazuh brute force simulation</b> — %s.<br/>'
                      '30 failed SSH logins written to '
                      '<code>/var/log/auth.log</code> from IP 203.0.113.42.'
                      ' The Wazuh agent should detect and alert.'
                      % self.name),
                message_type='notification')
            return {'success': True, 'minion': self.name, 'result': result}
        except Exception as e:
            _logger.warning('Fault wazuh bruteforce failed for %s: %s',
                            self.name, e)
            return {'success': False, 'error': str(e)}

    def action_fault_cleanup(self):
        """Remove injected faults: grow.log + restart odoo."""
        self.ensure_one()
        results = {}
        try:
            results['grow_log_removed'] = self._call_salt_api(
                'local', self.name, 'cmd.run',
                'rm -f /var/log/odoo/grow.log && echo removed', timeout=30)
        except Exception as e:
            results['grow_log_removed'] = {'error': str(e)}
        try:
            results['odoo_started'] = self._call_salt_api(
                'local', self.name, 'cmd.run',
                'systemctl start odoo && echo started', timeout=60)
        except Exception as e:
            results['odoo_started'] = {'error': str(e)}
        self.message_post(
            body=('🧹 <b>Fault injections cleaned up</b> on %s.<br/>'
                  'grow.log removed, odoo started.' % self.name),
            message_type='notification')
        return {'success': True, 'minion': self.name, 'results': results}
