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
    host = fields.Char(required=True, string='Host')
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

    def _compute_name(self):
        for rec in self:
            source = dict(rec._fields['source'].selection).get(rec.source, rec.source or '')
            rec.name = '%s — %s (%s)' % (
                rec.host or '?', rec.trigger_name or 'Alert',
                source or rec.category or '?')

    # ── Minion state coupling ────────────────────────────────────────────

    def _recompute_minions(self, hosts):
        """Refresh open_alert_count/state for minions matching the hosts."""
        hosts = {h for h in (hosts or set()) if h}
        if not hosts:
            return
        minions = self.env['salt.minion'].search([('name', 'in', list(hosts))])
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
            host = str(payload.get('host', '')).strip()
            source = str(payload.get('source', '')).strip()
            category = str(payload.get('category', '')).strip()
            if not host:
                return {'status': 'error', 'error': 'Missing host'}
            if category not in dict(self._fields['category'].selection):
                category = 'other'

            trigger = str(payload.get('trigger_name', '') or '')
            norm = self._normalize_trigger(trigger)

            # ── Dedup: same host + normalized trigger, previous not resolved
            existing = self.search([
                ('host', '=', host),
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
                    host, norm, existing.id, existing.occurrences + 1)
                return {
                    'status': 'ok',
                    'alert_id': existing.id,
                    'deduplicated': True,
                    'diagnosis_started': False,
                    'correlated_zabbix_alert': False,
                }

            now = fields.Datetime.now()
            alert = self.create({
                'host': host,
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

            # Notification for critical alerts (ground)
            if alert.severity >= 12:
                alert._notify_channel()

            # AI-diagnos (only when saltstack_ai installed)
            if (hasattr(alert, '_auto_diagnose_enabled')
                    and alert._auto_diagnose_enabled()):
                alert._start_diagnosis()

            return {
                'status': 'ok',
                'correlated_zabbix_alert': getattr(
                    alert, 'correlated_zabbix_alert', False),
                'diagnosis_started': getattr(alert, 'diagnosis_state', '') in (
                    'running', 'done', 'unavailable'),
                'coworker_session_id': getattr(
                    alert, 'coworker_session_id', '') or '',
                'alert_id': alert.id,
                'deduplicated': False,
            }
        except Exception as e:
            _logger.exception('Webhook-processing misslyckades: %s', e)
            return {'status': 'error', 'error': str(e)}

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
                    f'<b>Host:</b> {self.host}<br/>'
                    f'<b>Category:</b> {self.category}<br/>'
                    f'<b>Trigger:</b> {self.trigger_name}<br/>'
                    f'{self.description or ""}'
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_comment',
            )
        except Exception as e:
            _logger.warning('Could not notify Driftlarm channel: %s', e)

    # ── Actions ──────────────────────────────────────────────────────────

    def action_mark_resolved(self):
        self.write({'resolved': True})
