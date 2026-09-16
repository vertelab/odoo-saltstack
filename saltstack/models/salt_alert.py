# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

import logging
import re

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

_PAREN_SUFFIX_RE = re.compile(r'\s*\([^)]*\)\s*$')
_WS_RE = re.compile(r'\s+')


class SaltAlert(models.Model):
    _name = 'saltstack.alert'
    _description = 'Driftslarm'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    # ── Identity ────────────────────────────────────────────────────────
    name = fields.Char(
        string='Alert',
        compute='_compute_name',
        store=True,
    )
    # host = den berörda minionen (Many2one → salt.minion). Klickbar till
    # minion-posten. Minions matchas i process_webhook via host-namn.
    host = fields.Many2one(
        'salt.minion', string='Host', ondelete='cascade',
        required=False,
        help='Berörd minion (salt.minion). Klickbar.')
    # Datacenter — hämtas från minionen (ska/sto osv)
    dc = fields.Selection(
        selection=[], store=True,
        related='host.dc', string='Datacenter',
        help='Datacenter för hosten (från salt.minion).')
    # ── Host access (T/11504) ────────────────────────────────────────────
    # Adresserna ärvs från minionen — de lagras INTE på larmet. Adressen är
    # en egenskap hos maskinen, inte hos larmet: en lagrad kopia blir fel
    # så snart minionen byter adress (se svenskfast 2026-08-25).
    # Syftet är att operatören ska kunna NÅ maskinen direkt från larmet —
    # kopiera IP:t till en terminal (ssh) eller öppna den externa domänen i
    # webbläsaren — utan att först öppna minion-posten.
    private_ip = fields.Char(
        related='host.private_ip', string='Private IP',
        help='Hostens privata adress (från salt.minion). Använd kopiera-'
             'knappen för att klistra in den i en terminal.')
    public_ip = fields.Char(
        related='host.public_ip', string='Public IP',
        help='Hostens publika adress (från salt.minion). Tom för containrar '
             'som bara nås via sin gateway.')
    external_domain = fields.Char(
        related='host.external_domain', string='Extern domän',
        help='Hostens publika domän (från salt.minion) — öppnas i webbläsaren '
             'med knappen bredvid.')
    host_machine = fields.Char(
        related='host.host_machine', string='Fysisk värd',
        help='Maskinen/containervärden som hosten kör på (från salt.minion).')
    source = fields.Selection(
        selection=[],
        string='Source',
        help='Source system (Wazuh, Zabbix...). Extended by bridge modules via selection_add.',
    )
    category = fields.Selection([
        ('kernel', 'Kernel'),
        ('process', 'Process'),
        ('database', 'Database'),
        ('proxy', 'Proxy'),
        ('odoo', 'Odoo'),
        ('system', 'System'),
        ('other', 'Other'),
    ], string='Category', default='other')
    severity = fields.Integer(string='Severity')

    # ── Alert details ────────────────────────────────────────────────────
    trigger_name = fields.Char(string='Trigger')
    normalized_trigger = fields.Char(
        string='Normalized Trigger',
        compute='_compute_normalized_trigger',
        store=True,
        index=True,
        help='Dedup key: lowercase trigger with trailing parenthetical '
             'groups stripped (e.g. "http endpoint not responding (http 0)" '
             'and "... (http 200)" collapse to the same key).',
    )
    occurrences = fields.Integer(
        string='Occurrences',
        default=1,
        help='How many times this same problem has been reported (dedup).',
    )
    last_occurrence = fields.Datetime(
        string='Last Occurrence',
        help='Timestamp of the latest deduplicated repeat.',
    )
    description = fields.Text(string='Description')
    raw_log = fields.Text(string='Raw log')
    timestamp = fields.Datetime(string='Timestamp')

    resolved = fields.Boolean(string='Resolved', default=False)
    active = fields.Boolean(string='Active', default=True)

    # ── Lifecycle state (2026-08-31) ────────────────────────────────────
    state = fields.Selection([
        ('reported', 'Reported'),
        ('deployed', 'Deployed'),
        ('resolved', 'Resolved'),
        ('error', 'Error'),
        ('aborted', 'Aborted'),
    ], string='Status', default='reported',
        help='Livscykel: reported (inrapporterad) → deployed (åtgärd pågår) → '
             'resolved (löst). Error = fel vid bearbetning, aborted = avbruten.')

    # ── Computed ─────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_trigger(name):
        """Normalize a trigger name into a stable dedup key.

        Lowercases, strips trailing parenthetical groups (repeatedly) and
        collapses whitespace. Kept conservative so only volatile suffixes
        like "(HTTP 0)"/"(HTTP 200)" disappear — different reports stay
        distinct.
        """
        if not name:
            return ''
        value = str(name).strip().lower()
        prev = None
        while prev != value:
            prev = value
            value = _PAREN_SUFFIX_RE.sub('', value).strip()
        return _WS_RE.sub(' ', value)

    @api.depends('trigger_name')
    def _compute_normalized_trigger(self):
        for rec in self:
            rec.normalized_trigger = self._normalize_trigger(rec.trigger_name)

    @api.model
    def _parse_timestamp(self, value):
        """Parse ISO timestamp into Odoo naive Datetime."""
        from datetime import datetime
        if not value:
            return fields.Datetime.now()
        try:
            ts = str(value).replace('Z', '+00:00')
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt.strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            try:
                return fields.Datetime.to_datetime(value)
            except Exception:
                return fields.Datetime.now()

    @api.depends('host', 'source', 'category', 'trigger_name')
    @staticmethod
    def _parse_severity(value):
        """Accept both numeric and named severities (Zabbix sends text).

        Zabbix: Not classified=0, Information=1, Warning=2, Average=3,
        High=4, Disaster=5. Unknown text falls back to 0.
        """
        if value is None or value == '':
            return 0
        if isinstance(value, int):
            return value
        text = str(value).strip().lower()
        names = {
            'not classified': 0, 'information': 1, 'info': 1,
            'warning': 2, 'warn': 2, 'average': 3, 'avg': 3,
            'high': 4, 'disaster': 5, 'catastrophe': 5,
        }
        if text in names:
            return names[text]
        try:
            return int(text)
        except (ValueError, TypeError):
            return 0

    @api.depends('host', 'trigger_name', 'source', 'category')
    def _compute_name(self):
        for rec in self:
            source = dict(rec._fields['source'].selection).get(rec.source, rec.source or '')
            host_label = rec.host.name if rec.host else (rec.host or '')
            # Löpnummer-prefix (L00001, L00002, ...) — deterministiskt från
            # record-id så det aldrig dupliceras.
            num = ('L%05d' % rec.id) if rec.id else ''
            rec.name = '%s %s — %s (%s)' % (
                num, host_label or '?', rec.trigger_name or 'Alert',
                source or rec.category or '?')

    # ── Minion state coupling ────────────────────────────────────────────

    def _recompute_minions(self, minions):
        """Refresh open_alert_count/state for minions."""
        minions = self.env['salt.minion'].browse(
            [m.id for m in (minions or []) if m])
        if minions:
            minions._update_open_alert_count()

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        hosts = {r.host for r in records if r.host}
        self._recompute_minions(hosts)
        return records

    def write(self, vals):
        resolved_touched = 'resolved' in vals
        hosts = {r.host for r in self if r.host}
        res = super().write(vals)
        if resolved_touched:
            self._recompute_minions(hosts)
        return res

    # ── Webhook-processing ───────────────────────────────────────────────

    @api.model
    def process_webhook(self, payload):
        """Process an incoming alert payload. Returns result dict.

        Deduplicates: when an unresolved alert for the same host + normalized
        trigger already exists, no new record is created — the existing one is
        updated (occurrences/last_occurrence/raw_log) and notification +
        correlation + diagnosis are skipped. A different normalized trigger
        (another report) creates a new record.

        Bridge modules (saltstack_zabbix, saltstack_ai) extend this flow by
        defining _correlate_zabbix / _start_diagnosis on the same model. The
        base guards with hasattr so it runs without any bridge installed.
        """
        try:
            host_name = str(payload.get('host', '')).strip()
            source = str(payload.get('source', '')).strip()
            category = str(payload.get('category', '')).strip()
            if not host_name:
                return {'status': 'error', 'error': 'Missing host'}
            if category not in dict(self._fields['category'].selection):
                category = 'other'

            trigger = str(payload.get('trigger_name', '') or '')
            norm = self._normalize_trigger(trigger)

            # Matcha/find-or-create salt.minion för host-namnet. host (Many2one)
            # pekar på minion-posten.
            minion = self.env['salt.minion'].search(
                [('name', '=', host_name)], limit=1)
            if not minion:
                minion = self.env['salt.minion'].create({'name': host_name})
            host_id = minion.id

            # ── Dedup: same host + normalized trigger, previous not resolved
            existing = self.search([
                ('host', '=', host_id),
                ('resolved', '=', False),
                ('normalized_trigger', '=', norm),
            ], limit=1) if norm else self.env['saltstack.alert']

            if existing:
                existing.write({
                    'occurrences': existing.occurrences + 1,
                    'last_occurrence': fields.Datetime.now(),
                    'raw_log': payload.get('raw_log', ''),
                })
                _logger.info(
                    'Dedup alert for %s (%s): alert #%s now occurrences=%s',
                    host_name, norm, existing.id, existing.occurrences + 1)
                return {
                    'status': 'ok',
                    'alert_id': existing.id,
                    'deduplicated': True,
                    'diagnosis_started': False,
                    'correlated_zabbix_alert': False,
                }

            now = fields.Datetime.now()
            alert = self.create({
                'host': host_id,
                'source': source or False,
                'category': category,
                'severity': self._parse_severity(payload.get('severity')),
                'trigger_name': trigger,
                'description': payload.get('description', ''),
                'raw_log': payload.get('raw_log', ''),
                'timestamp': self._parse_timestamp(payload.get('timestamp')),
                'occurrences': 1,
                'last_occurrence': now,
            })

            # Correlate with Zabbix (only when saltstack_zabbix installed)
            if hasattr(alert, '_correlate_zabbix'):
                alert._correlate_zabbix()

            # Diagnos-status: fältet defaultar till 'pending', vilket får
            # pending-cronen att plocka upp alerten. Är auto-diagnosen
            # avstängd (globalt eller för källan) markerar vi den som
            # 'unavailable' direkt — annars ligger den och skräpar i kön.
            if hasattr(alert, '_auto_diagnose_enabled') and not (
                    alert._auto_diagnose_enabled()
                    and alert._auto_diagnose_enabled_for_source()):
                alert.diagnosis_state = 'unavailable'

            # Notification for critical alerts (ground)
            if alert.severity >= 12:
                alert._notify_channel()

            # AI-diagnos (only when saltstack_ai installed). Asynkront via
            # _schedule_diagnosis (queue_job) när tillgängligt — webhook-svaret
            # returnerar direkt i stället för att blockera i coworker.run()
            # (som kan ta minuter). Synkron _start_diagnosis som fallback.
            #
            # Källspecifik avstängning: bridge-moduler (t.ex. saltstack_zabbix)
            # kan stänga av diagnosen för sin egen källa via
            # _auto_diagnose_enabled_for_source(). Basen känner inte till någon
            # källa — default är att källan tillåter diagnos.
            if (hasattr(alert, '_auto_diagnose_enabled')
                    and alert._auto_diagnose_enabled()
                    and alert._auto_diagnose_enabled_for_source()):
                if hasattr(alert, '_schedule_diagnosis'):
                    alert._schedule_diagnosis()
                else:
                    alert._start_diagnosis()

            return {
                'status': 'ok',
                'correlated_zabbix_alert': getattr(
                    alert, 'correlated_zabbix_alert', False),
                'diagnosis_started': getattr(alert, 'diagnosis_state', '') in (
                    'pending', 'running', 'done', 'unavailable'),
                'coworker_session_id': getattr(
                    alert, 'coworker_session_id', '') and (
                    alert.coworker_session_id.id
                    if hasattr(alert.coworker_session_id, 'id')
                    else alert.coworker_session_id) or '',
                'alert_id': alert.id,
                'deduplicated': False,
            }
        except Exception as e:
            _logger.exception('Webhook-processing misslyckades: %s', e)
            return {'status': 'error', 'error': str(e)}

    def _auto_diagnose_enabled_for_source(self):
        """Source-specific gate for AI diagnosis (default: always allowed).

        Bridge modules override this to disable diagnosis for their own
        source — e.g. saltstack_zabbix returns False when the
        'zabbix.alert.auto_diagnose' setting is off, so Zabbix alerts are
        still recorded/notified but never diagnosed. The base module has no
        knowledge of any source and therefore always returns True.
        """
        self.ensure_one()
        return True

    # ── Notification ─────────────────────────────────────────────────────

    def _get_or_create_channel(self):
        """Find or create the 'Driftlarm' discuss channel."""
        Channel = self.env['discuss.channel']
        channel = Channel.search([('name', '=', 'Driftlarm')], limit=1)
        if not channel:
            channel = Channel.with_user(
                self.env.ref('base.user_root')).create({
                    'name': 'Driftlarm',
                    'channel_type': 'channel',
                    'description': 'Driftlarm alerts from Wazuh/Zabbix (via webhook)',
                })
        return channel

    def _notify_channel(self):
        """Post critical alert to Driftlarm channel."""
        self.ensure_one()
        try:
            channel = self._get_or_create_channel()
            source_label = dict(self._fields['source'].selection).get(
                self.source, self.source or 'unknown')
            channel.with_user(
                self.env.ref('base.user_root')).message_post(
                body=(
                    f'🚨 <b>Driftlarm</b> (severity {self.severity})<br/>'
                    f'<b>Source:</b> {source_label}<br/>'
                    f'<b>Host:</b> {self.host.name if self.host else self.host}<br/>'
                    f'<b>Category:</b> {self.category}<br/>'
                    f'<b>Trigger:</b> {self.trigger_name}<br/>'
                    f'{self.description or ""}'
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_comment',
            )
        except Exception as e:
            _logger.warning('Could not notify Driftlarm channel: %s', e)

    # ── Host access (T/11504) ────────────────────────────────────────────

    def action_copy_alert_ip(self):
        """Copy the host IP to the clipboard (T/11504).

        Same behaviour as salt.minion.action_copy_private_ip: falls back to
        the public address so hosts that only have a public address
        (GleSYS/Hetzner) still copy something useful. The point is to get the
        address into a terminal without leaving the alert.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'saltstack_copy_value',
            'params': {'value': self.private_ip or self.public_ip or ''},
        }

    def action_open_host_domain(self):
        """Open the host's external domain in a new tab (T/11504).

        Mirrors salt.minion.action_open_external_domain so the operator can
        reach the machine in the browser straight from the alert. Warns
        instead of opening an empty tab when the domain is missing.
        """
        self.ensure_one()
        if not self.external_domain:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Extern domän saknas'),
                    'message': _('No external domain set for %s.') % (
                        self.host.name if self.host else self.host),
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

    # ── Actions ──────────────────────────────────────────────────────────

    def action_mark_resolved(self):
        self.write({'resolved': True, 'state': 'resolved'})
        return True

    def action_mark_deployed(self):
        """Mark as deployed (åtgärd pågår)."""
        self.write({'state': 'deployed', 'resolved': False})
        return True

    def action_mark_error(self):
        """Flag as error (bearbetning misslyckades)."""
        self.write({'state': 'error', 'resolved': False})
        return True

    def action_mark_aborted(self):
        """Mark as aborted (avbruten)."""
        self.write({'state': 'aborted', 'resolved': False})
        return True

    def action_cron_auto_resolve(self):
        """Auto-resolve stale/inactive alerts (cron, every 15 min).

        Rules:
          1. Zabbix alerts whose (host, trigger) is no longer active in
             Zabbix (recovery was never sent to the webhook) -> resolve.
          2. Informational alerts (severity <= 2) older than 7 days
             with no recent activity -> resolve.
          3. Any alert with last_occurrence older than 14 days and
             occurrences == 1 (dedup missed it) -> resolve.
        Idempotent. Returns a stats dict.
        """
        from datetime import timedelta
        now = fields.Datetime.now()
        resolved = 0
        stats = {"zabbix_recovered": 0, "stale_info": 0, "stale_dupes": 0}

        # 1. Zabbix recovery via problem.get + trigger.get (host-aware).
        #
        # IMPORTANT: match on (host, trigger) — NOT trigger alone. A single
        # active problem on one host must not keep alerts alive on every other
        # host that shares the same trigger name (e.g. "Load average is too
        # high" on tahr kept 55 stale load alerts alive fleet-wide).
        try:
            config = self.env['zabbix.api']
            result = config.zabbix_call('problem.get', {
                'output': ['eventid', 'objectid', 'name'],
                'recent': True,
                'severities': [3, 4, 5],
                'limit': 1000,
            })
            import json as _json
            problems = _json.loads(result) if isinstance(result, str) else result
            problems = problems or []

            # Resolve triggerid -> host name(s) via trigger.get
            trigger_ids = sorted({p.get('objectid') for p in problems if p.get('objectid')})
            trigger_hosts = {}
            if trigger_ids:
                tresult = config.zabbix_call('trigger.get', {
                    'triggerids': trigger_ids,
                    'output': ['triggerid'],
                    'selectHosts': ['host'],
                })
                tdata = _json.loads(tresult) if isinstance(tresult, str) else tresult
                for t in (tdata or []):
                    hosts = [h.get('host', '') for h in (t.get('hosts') or [])]
                    trigger_hosts[str(t.get('triggerid'))] = [h.strip().lower() for h in hosts if h]

            # Build the set of ACTIVE (host, trigger) pairs
            active_pairs = set()
            for p in problems:
                name = (p.get('name') or '').strip().lower()
                for h in trigger_hosts.get(str(p.get('objectid')), []):
                    active_pairs.add((h, name))

            domain = [("resolved", "=", False), ("source", "=", "zabbix")]
            for alert in self.search(domain):
                norm = (alert.normalized_trigger or alert.trigger_name or "").strip().lower()
                host_name = (alert.host.name or "").strip().lower() if alert.host else ""
                # Safety: only auto-resolve if no recent activity (webhook would
                # have updated last_occurrence for an ACTIVE problem). This
                # prevents resolving a live problem whose name differs slightly.
                recent = alert.last_occurrence and alert.last_occurrence >= now - timedelta(minutes=30)
                if not norm or recent:
                    continue
                # Still active if the SAME host has an active problem whose
                # name contains our normalized trigger (substring match on the
                # trigger part, host must match exactly).
                still_active = any(
                    h == host_name and norm in name
                    for h, name in active_pairs
                )
                if not still_active:
                    alert.write({"resolved": True, "state": "resolved"})
                    resolved += 1
                    stats["zabbix_recovered"] += 1
        except Exception as e:
            _logger.warning("Auto-resolve Zabbix check failed: %s", e)

        # 2. Stale informational alerts (sev <= 2, > 7 days)
        cutoff = now - timedelta(days=7)
        domain = [
            ("resolved", "=", False),
            ("severity", "<=", 2),
            ("create_date", "<", cutoff),
        ]
        for alert in self.search(domain):
            if not alert.last_occurrence or alert.last_occurrence < cutoff:
                alert.write({"resolved": True, "state": "resolved"})
                resolved += 1
                stats["stale_info"] += 1

        # 3. Stale single-occurrence dupes (> 14 days, occurrences=1)
        cutoff14 = now - timedelta(days=14)
        domain = [
            ("resolved", "=", False),
            ("occurrences", "=", 1),
            ("create_date", "<", cutoff14),
        ]
        for alert in self.search(domain):
            alert.write({"resolved": True, "state": "resolved"})
            resolved += 1
            stats["stale_dupes"] += 1

        if resolved:
            minions = self.mapped("host")
            if minions:
                minions._update_open_alert_count()
            _logger.info("Auto-resolve: %s alerts resolved (%s)", resolved, stats)
        return {"resolved": resolved, **stats}
