# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


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
    description = fields.Text(string='Description')
    raw_log = fields.Text(string='Raw log')
    timestamp = fields.Datetime(string='Timestamp')

    resolved = fields.Boolean(string='Resolved', default=False)
    active = fields.Boolean(string='Active', default=True)

    # ── Computed ─────────────────────────────────────────────────────────

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
    def _compute_name(self):
        for rec in self:
            source = dict(rec._fields['source'].selection).get(rec.source, rec.source or '')
            rec.name = '%s — %s (%s)' % (
                rec.host or '?', rec.trigger_name or 'Alert',
                source or rec.category or '?')

    # ── Webhook-processing ───────────────────────────────────────────────

    @api.model
    def process_webhook(self, payload):
        """Process an incoming alert payload. Returns result dict.

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

            alert = self.create({
                'host': host,
                'source': source or False,
                'category': category,
                'severity': int(payload.get('severity', 0) or 0),
                'trigger_name': payload.get('trigger_name', ''),
                'description': payload.get('description', ''),
                'raw_log': payload.get('raw_log', ''),
                'timestamp': self._parse_timestamp(payload.get('timestamp')),
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
