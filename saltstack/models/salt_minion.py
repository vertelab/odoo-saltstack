# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

import json
import logging
import ssl
import urllib.request

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class SaltMinion(models.Model):
    _name = 'salt.minion'
    _description = 'Salt Minion Registry'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'is_demo, dc, name'
    _rec_names_search = ['name', 'customer', 'host_machine']

    # ── Identity ────────────────────────────────────────────────────────

    name = fields.Char(
        string='Minion ID',
        required=True,
        index=True,
    )
    ip = fields.Char(
        string='IP Address',
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
        string='Customer',
        help='Customer name if this is a customer container',
    )
    odoo_version = fields.Char(
        string='Odoo Version',
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
        help='Minion-status: online (svarar), offline (ej svarar), faulty (fel).',
    )
    image = fields.Image(
        string='Logo',
        max_width=256,
        max_height=256,
        help='Logo for the minion. Filled automatically based on role at '
             'sync, but can be updated manually in the form.',
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

    @api.depends('last_seen')
    def _compute_state(self):
        """Minion status: online if seen within the last hour, else offline."""
        from datetime import datetime, timedelta
        threshold = datetime.now() - timedelta(hours=1)
        for rec in self:
            rec.state = 'online' if (
                rec.last_seen and rec.last_seen > threshold) else 'offline'

    def _set_default_image(self):
        """Set a default logo per role if image is empty."""
        role_logo = {
            'odoo': self._logo_from_module('base', 'static/description/icon.png'),
            'postgres': self._logo_from_module('base', 'static/description/icon.png'),
            'bifrost': self._logo_from_module('base', 'static/description/icon.png'),
            'caddy': self._logo_from_module('base', 'static/description/icon.png'),
            'gateway': self._logo_from_module('base', 'static/description/icon.png'),
            'lxd-host': self._logo_from_module('base', 'static/description/icon.png'),
        }
        for rec in self:
            if rec.image:
                continue  # keep manually set image
            roles = [r.strip().lower() for r in (rec.roles or '').split(',') if r.strip()]
            matched = next((role_logo[r] for r in roles if r in role_logo), None)
            rec.image = matched or role_logo.get('gateway')

    def _logo_from_module(self, module, path):
        """Return base64 of a logo file from a module, or None."""
        import base64 as b64
        import os as _os
        try:
            mod = self.env['ir.module.module'].search(
                [('name', '=', module)], limit=1)
            if mod:
                full = _os.path.join(mod.get_manifest_glob('') or '/', path)
                # try addons path
                for ap in self.env['ir.config_parameter'].get_param(
                        'addons_path', '').split(','):
                    cand = _os.path.join(ap.strip(), module, path)
                    if _os.path.isfile(cand):
                        with open(cand, 'rb') as f:
                            return b64.b64encode(f.read())
        except Exception:
            pass
        return None

    # ── Salt API Helpers ─────────────────────────────────────────────────

    def _get_salt_api_config(self):
        """Return (api_url, api_token) from ir.config_parameter."""
        params = self.env['ir.config_parameter']
        url = params.get_param('saltstack.api_url', '')
        token = params.get_param('saltstack.api_token', '')
        return url, token

    def _salt_login(self, api_key=None, timeout=15):
        """Exchange sharedsecret API key for a session token via /login.

        The API key (saltstack.api_token with auth_method='sharedsecret',
        or the value in keykeep.credential purpose='saltstack_api') is NOT a
        session token — it must be exchanged via POST /login.
        """
        api_url = self._get_salt_api_config()[0]
        if api_key is None:
            api_key = self._get_salt_api_config()[1]
        payload = {
            'username': 'saltapi',
            'password': api_key,
            'eauth': 'sharedsecret',
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f'{api_url.rstrip("/")}/login',
            data=data,
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            result = json.loads(resp.read().decode())
        try:
            return result['return'][0]['token']
        except (KeyError, IndexError, TypeError):
            raise ValueError('Salt login failed: %s' % result)

    def _call_salt_api(self, client, tgt, fun, *args, timeout=120, **kwargs):
        """Call the Salt REST API. Returns dict or raises."""
        api_url, api_token = self._get_salt_api_config()
        if not api_url:
            raise ValueError('Salt API URL not configured')
        auth_method = self.env['ir.config_parameter'].get_param(
            'saltstack.auth_method', 'token')
        if auth_method == 'keykeep' and 'keykeep.credential' in self.env:
            cred = self.env['keykeep.credential'].search([
                ('purpose', '=', 'saltstack_api'),
            ], limit=1)
            if cred and cred._get_decrypted_value():
                api_token = self._salt_login(cred._get_decrypted_value())
        elif auth_method == 'sharedsecret':
            api_token = self._salt_login()

        payload = {'client': client, 'fun': fun, 'timeout': timeout}
        if client in ('local', 'local_async', 'local_batch'):
            payload['tgt'] = tgt
        if args:
            payload['arg'] = list(args)
        if kwargs:
            payload['kwarg'] = kwargs

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f'{api_url.rstrip("/")}/',
            data=data,
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'X-Auth-Token': api_token,
            },
        )

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with urllib.request.urlopen(req, timeout=timeout + 30, context=ctx) as resp:
            return json.loads(resp.read().decode())

    # ── Actions ──────────────────────────────────────────────────────────

    def action_ping(self):
        """Ping this minion via Salt API and update status."""
        self.ensure_one()
        try:
            self._call_salt_api('local', self.name, 'test.ping')
            self.last_seen = fields.Datetime.now()
            self.state = 'online'
            return {'success': True, 'minion': self.name, 'status': 'up'}
        except Exception as e:
            _logger.warning('Ping failed for %s: %s', self.name, e)
            return {'success': False, 'minion': self.name, 'error': str(e)}

    def _apply_grains(self, grains):
        """Write grains data onto this record (no API call)."""
        self.ensure_one()
        if not grains or not isinstance(grains, dict):
            _logger.warning('No grains for %s (got %r)', self.name, grains)
            return False

        # Extract ip (fqdn_ip4/ipv4 can be empty lists — IndexError-safe)
        fqdn_ip4 = grains.get('fqdn_ip4') or []
        ipv4 = grains.get('ipv4') or []
        ip = (fqdn_ip4[0] if fqdn_ip4 else None) or (ipv4[0] if ipv4 else None)
        if not ip and grains.get('ip4_interfaces'):
            for iface, addrs in grains['ip4_interfaces'].items():
                if iface != 'lo' and addrs:
                    ip = addrs[0]
                    break

        roles = grains.get('roles', '')
        if isinstance(roles, list):
            roles = ','.join(roles)

        self.write({
            'ip': ip,
            'os': str(grains.get('os', '')).title(),
            'os_version': str(grains.get('osrelease', '')),
            'dc': grains.get('dc'),
            'roles': roles,
            'customer': grains.get('customer'),
            'odoo_version': grains.get('odoo_version') or grains.get('odoo_full_version'),
            'is_container': bool(grains.get('lxc')),
            'host_machine': grains.get('host'),
            'grains_json': json.dumps(grains, indent=2, default=str),
            'last_sync': fields.Datetime.now(),
        })
        self._set_default_image()
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

    def action_sync_all_minions(self):
        """List all minions via Salt API and create/update records.

        Optimized (2026-08-09): fetches grains for ALL minions in ONE call
        (tgt='*') instead of one call per minion. Previously the sync could
        take many minutes — each down minion waited out a 120 s timeout
        sequentially. Now the whole sync takes max ~45 s regardless of the
        number of down minions.
        """
        try:
            result = self._call_salt_api('runner', None, 'manage.status')
        except Exception as e:
            _logger.warning('Minion list failed: %s', e)
            return {'error': str(e)}

        all_minions = []
        returned = result.get('return', [{}])[0]
        for state in ('up', 'down', 'unaccepted'):
            all_minions.extend(returned.get(state, []))

        # Bulk-grains: ett anrop mot alla minioner → {minion_id: grains}
        bulk_grains = {}
        try:
            bulk_result = self._call_salt_api('local', '*', 'grains.items', timeout=45)
            bulk_grains = bulk_result.get('return', [{}])[0] or {}
        except Exception as e:
            _logger.warning('Bulk grains fetch failed: %s', e)

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
                updated += 1
            else:
                rec = self.create({'name': minion_id})
                rec._apply_grains(grains)
                created += 1

        # Deactivate minions no longer present on the Salt master
        known = self.search([('active', '=', True)])
        for rec in known:
            if rec.name not in all_minions:
                rec.active = False
                deactivated += 1

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
