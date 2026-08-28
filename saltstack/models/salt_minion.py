# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

import json
import logging

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
        selection=[('ska', 'SKA'), ('sto', 'STO')],
        string='Datacenter',
    )
    roles = fields.Char(
        string='Roles',
        help='Comma-separated roles (e.g. "caddy,odoo,postgres")',
    )

    # ── Customer ─────────────────────────────────────────────────────────

    customer = fields.Char(
        string='Kund',
        help='Customer name if this is a customer container (grain mirror).',
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
    image = fields.Image(
        string='Logo',
        max_width=256,
        max_height=256,
        help='Logo for the minion. Filled automatically based on role at '
             'sync, but can be updated manually in the form.',
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

        Called by the alert model on create/resolve and at sync end (backfill).
        Writing the stored field triggers the stored state recomputation.
        """
        for rec in self:
            count = self.env['saltstack.alert'].search_count([
                ('host', '=', rec.name),
                ('resolved', '=', False),
            ])
            if rec.open_alert_count != count:
                rec.open_alert_count = count
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

    def _set_default_image(self):
        """Set a technology logo per primary role if image is empty."""
        for rec in self:
            if rec.image:
                continue  # keep manually set image
            roles = [r.strip().lower() for r in (rec.roles or '').split(',') if r.strip()]
            asset = next(
                (logo for role, logo in self._ROLE_LOGO_PRIORITY if role in roles),
                self._LOGO_FALLBACK,
            )
            rec.image = self._logo_from_asset(asset)

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

        private_ip: first address in 192.168.11.0/24 (management network),
        falling back to the first non-loopback non-bridge address.
        public_ip: first non-RFC1918 address.
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

        private = None
        public = None
        for _iface, ip in candidates:
            if not isinstance(ip, str) or '.' not in ip:
                continue
            if ip.startswith('192.168.11.'):
                if private is None:
                    private = ip
            elif not _is_rfc1918(ip):
                if public is None:
                    public = ip
        return (private or ''), (public or '')

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

        is_container = bool(grains.get('lxc'))
        if is_container:
            public_ip = ''

        dc = grains.get('dc') or self._infer_dc(grains.get('host'), private_ip)
        if not dc:
            dc = False

        role_list = [r.strip().lower() for r in roles.split(',') if r.strip()]
        has_gateway = 'gateway' in role_list or bool(grains.get('gw'))
        external_domain = grains.get('external_domain') or ''

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

    # ── Odoo statistics ────────────────────────────────────────────────

    _ODOO_STATS_SCRIPT = r'''
set -uo pipefail
CONF=/etc/odoo/odoo.conf
DB_HOST=$(awk -F' = ' '/^db_host/{print $2; exit}' "$CONF")
DB_PORT=$(awk -F' = ' '/^db_port/{print $2; exit}' "$CONF")
DB_USER=$(awk -F' = ' '/^db_user/{print $2; exit}' "$CONF")
DB_PASS=$(awk -F' = ' '/^db_password/{print $2; exit}' "$CONF")
export PGPASSWORD="$DB_PASS"
PSQL="psql -h ${DB_HOST:-localhost} -p ${DB_PORT:-5432} -U ${DB_USER:-odoo} -tA"
while read -r db; do
  [ -z "$db" ] && continue
  is_odoo=$($PSQL -d "$db" -tAc "SELECT 1 FROM information_schema.tables WHERE table_name='res_users'" 2>/dev/null | head -1)
  [ "$is_odoo" != "1" ] && continue
  users=$($PSQL -d "$db" -tAc "SELECT count(*) FROM res_users WHERE active" 2>/dev/null | head -1)
  last_login=$($PSQL -d "$db" -tAc "SELECT max(create_date) FROM res_users_log" 2>/dev/null | head -1)
  if [ -z "$last_login" ]; then
    last_login=$($PSQL -d "$db" -tAc "SELECT max(log_date) FROM res_log WHERE log_action='login'" 2>/dev/null | head -1)
  fi
  coworkers=$($PSQL -d "$db" -tAc "SELECT count(*) FROM ai_coworker WHERE active" 2>/dev/null | head -1)
  tokens=$($PSQL -d "$db" -tAc "SELECT COALESCE(SUM(GREATEST(monthly_cap_mtokens, 1)), 0) FROM ai_coworker WHERE active" 2>/dev/null | head -1)
  demo=$($PSQL -d "$db" -tAc "SELECT count(*) FROM res_company WHERE name ILIKE '%(san francisco)%' OR name ILIKE '%(chicago)%' OR name ILIKE '%(demo)%'" 2>/dev/null | head -1)
  echo "DB|$db|users=${users:-0}|last=${last_login:-}|coworkers=${coworkers:-0}|tokens=${tokens:-0}|demo=${demo:-0}"
done < <($PSQL -l 2>/dev/null | cut -d'|' -f1 | grep -vE '^(template0|template1|postgres|_errors)$')
'''

    def _collect_odoo_stats(self):
        """Collect Odoo statistics from this minion via Salt API.

        Runs a bash script on the minion that reads db_* from odoo.conf and
        reports, per Odoo database: active users, last login, ai.coworker
        count, monthly systemtoken budget (M) and demo-data presence.
        Best-effort: any failure leaves the fields untouched.
        """
        self.ensure_one()
        roles = [r.strip().lower() for r in (self.roles or '').split(',') if r.strip()]
        if 'odoo' not in roles and not self.odoo_version:
            return False
        try:
            result = self._call_salt_api(
                'local', self.name, 'cmd.run', self._ODOO_STATS_SCRIPT,
                timeout=180, shell='/bin/bash',
            )
        except Exception as e:
            _logger.warning('Odoo stats collection failed for %s: %s', self.name, e)
            return False
        raw = result.get('return', [{}])[0].get(self.name, '')
        if not isinstance(raw, str):
            raw = ''

        dbs = []
        users_total = 0
        coworkers_total = 0
        tokens_total = 0
        last_login = None
        for line in raw.splitlines():
            if not line.startswith('DB|'):
                continue
            parts = dict(
                p.split('=', 1) for p in line.split('|')[2:] if '=' in p
            )
            db = line.split('|')[1]
            users_total += int(parts.get('users') or 0)
            coworkers_total += int(parts.get('coworkers') or 0)
            tokens_total += int(parts.get('tokens') or 0)
            if parts.get('demo') and parts['demo'] not in ('0', ''):
                dbs.append('%s (demo)' % db)
            else:
                dbs.append(db)
            last_val = parts.get('last') or ''
            if last_val and (not last_login or last_val > last_login):
                last_login = last_val

        vals = {
            'odoo_databases': ', '.join(dbs),
            'odoo_user_count': users_total,
            'odoo_coworker_count': coworkers_total,
            'odoo_coworker_tokens_m': tokens_total,
            'odoo_stats_synced': fields.Datetime.now(),
        }
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
            bulk_result = self._call_salt_api('local', '*', 'grains.items', timeout=45)
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

        # Backfill open alert counts (first run after upgrade + alert drift).
        for rec in known:
            rec._update_open_alert_count()

        return {
            'created': created,
            'updated': updated,
            'deactivated': deactivated,
            'total': len(all_minions),
        }

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
