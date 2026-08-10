# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

import hmac
import json
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
    wazuh_rule_id = fields.Char(string='Wazuh Rule ID')
    zabbix_event_id = fields.Char(string='Zabbix Event ID')

    # ── Correlation & diagnosis ──────────────────────────────────────────
    correlated_zabbix_alert = fields.Boolean(
        string='Correlated with Zabbix',
        default=False,
    )
    zabbix_alert_id = fields.Char(string='Zabbix Alert ID')
    coworker_session_id = fields.Char(string='Coworker session')
    diagnosis_result = fields.Text(string='Diagnosis result')
    diagnosis_state = fields.Selection([
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('unavailable', 'AI unavailable'),
        ('error', 'Error'),
    ], string='Diagnosis status', default='pending')

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
        """Process an incoming alert payload. Returns result dict."""
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
                'wazuh_rule_id': str(payload.get('wazuh_rule_id', '')),
                'zabbix_event_id': str(payload.get('zabbix_event_id', '')),
                'timestamp': self._parse_timestamp(payload.get('timestamp')),
            })

            # Correlate with Zabbix
            alert._correlate_zabbix()

            # Notification for critical alerts
            if alert.severity >= 12:
                alert._notify_channel()

            # AI-diagnos
            if self._auto_diagnose_enabled():
                alert._start_diagnosis()

            return {
                'status': 'ok',
                'correlated_zabbix_alert': alert.correlated_zabbix_alert,
                'diagnosis_started': alert.diagnosis_state in (
                    'running', 'done', 'unavailable'),
                'coworker_session_id': alert.coworker_session_id or '',
                'alert_id': alert.id,
            }
        except Exception as e:
            _logger.exception('Webhook-processing misslyckades: %s', e)
            return {'status': 'error', 'error': str(e)}

    # ── Zabbix-korrelation ───────────────────────────────────────────────

    def _correlate_zabbix(self):
        """Look for an active Zabbix problem on the same host."""
        self.ensure_one()
        try:
            config = self.env['saltstack.ai.config']
            window = int(self.env['ir.config_parameter'].get_param(
                'saltstack.alert.correlation_window', 120))
            result = config.zabbix_call('problem.get', {
                'output': ['eventid', 'name', 'hosts'],
                'recent': True,
                'search': {'hosts': [self.host]},
                'limit': 10,
            })
            problems = json.loads(result) if isinstance(result, str) else result
            if isinstance(problems, list) and problems:
                self.correlated_zabbix_alert = True
                self.zabbix_alert_id = str(problems[0].get('eventid', ''))
            else:
                self.correlated_zabbix_alert = False
        except Exception as e:
            _logger.warning('Zabbix correlation failed for %s: %s',
                            self.host, e)
            self.correlated_zabbix_alert = False

    # ── AI diagnosis ─────────────────────────────────────────────────────

    @api.model
    def _auto_diagnose_enabled(self):
        return self.env['ir.config_parameter'].get_param(
            'saltstack.alert.auto_diagnose', 'True') in ('True', 'true', '1')

    def _start_diagnosis(self):
        """Start AI diagnosis via the selected AI coworker."""
        self.ensure_one()
        self.diagnosis_state = 'running'
        try:
            if 'ai.coworker' not in self.env:
                self.diagnosis_result = 'AI coworker unavailable (saltstack_ai not installed)'
                self.diagnosis_state = 'unavailable'
                return None

            Coworker = self.env['ai.coworker']
            coworker = self._get_diagnosis_coworker()
            if not coworker:
                self.diagnosis_result = 'AI coworker unavailable (no coworker exists)'
                self.diagnosis_state = 'unavailable'
                return None

            prompt = self._build_diagnosis_prompt()
            result = coworker.run(prompt)
            self.diagnosis_result = str(result)[:5000]
            self.diagnosis_state = 'done'

            if self.severity >= 12 and result:
                self._post_action_plan(str(result))

            return result
        except Exception as e:
            _logger.exception('AI diagnosis failed: %s', e)
            self.diagnosis_result = 'AI coworker unavailable: %s' % str(e)
            self.diagnosis_state = 'unavailable'
            return None

    def _get_diagnosis_coworker(self):
        """Return the coworker selected in settings, else Infrastructure Operator."""
        Coworker = self.env['ai.coworker']
        coworker_id = self.env['ir.config_parameter'].get_param(
            'saltstack.alert.coworker_id', False)
        if coworker_id:
            coworker = Coworker.browse(int(coworker_id))
            if coworker.exists():
                return coworker
        return Coworker.search(
            [('name', '=', 'Infrastructure Operator')], limit=1)

    def _build_diagnosis_prompt(self):
        """Build the diagnosis prompt from alert context + category mapping.

        Includes the Driftlarm record so the coworker can write its assessment
        and change the status directly on the record.
        """
        instructions = {
            'kernel': 'Run: salt <host> cmd.run \'dmesg | tail -50\'. Look for OOM, kernel panic.',
            'process': 'Run: salt <host> cmd.run \'systemctl status odoo\'. If down, suggest restart.',
            'database': 'Run: salt <host> cmd.run \'pg_isready\'. Check replication lag.',
            'proxy': 'Run: salt <host> cmd.run \'systemctl status caddy\'. Check upstream.',
            'odoo': 'Run: salt <host> cmd.run \'tail -100 /var/log/odoo/odoo-server.log\'. Interpret traceback.',
            'system': 'Run: uptime, free -m, df -h, dmesg, journalctl.',
            'other': 'Diagnose generally: system status, services, logs.',
        }
        cat_instruction = instructions.get(self.category, instructions['other'])
        source_label = dict(self._fields['source'].selection).get(
            self.source, self.source or 'unknown')

        return (
            f"Driftlarm alert on host '{self.host}'.\n"
            f"Source: {source_label}\n"
            f"Category: {self.category}\n"
            f"Trigger: {self.trigger_name}\n"
            f"Description: {self.description}\n"
            f"Severity: {self.severity}\n\n"
            f"## Diagnosis instruction ({self.category})\n{cat_instruction}\n\n"
            f"## Raw log (excerpt)\n{self.raw_log[:2000]}\n\n"
            f"Analyze the root cause, verify against the system, and give a "
            f"concrete action plan with commands.\n\n"
            f"## Driftlarm record\n"
            f"You are working on the Driftlarm record with ID {self.id} "
            f"(model saltstack.alert). You CAN write your assessment and "
            f"change the status directly on the record via the tool "
            f"driftlarm_update_assessment. Use it to:\n"
            f"- Save your assessment (diagnosis_result)\n"
            f"- Set the status (pending/running/done/error)\n"
            f"- Mark as resolved when the action is complete\n"
            f"- Leave an action plan in the description\n"
            f"\nAlso report the result in your answer."
        )

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

    def _post_action_plan(self, action_plan):
        """Post the AI action plan to Driftlarm channel."""
        self.ensure_one()
        try:
            channel = self._get_or_create_channel()
            channel.with_user(
                self.env.ref('base.user_root')).message_post(
                body=(
                    f'🧠 <b>Action plan from AI diagnosis</b> '
                    f'(alert {self.id}, {self.host})<br/>'
                    f'<pre>{action_plan[:4000]}</pre>'
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_comment',
            )
        except Exception as e:
            _logger.warning('Could not post action plan: %s', e)

    # ── Actions ──────────────────────────────────────────────────────────

    def action_diagnose(self):
        """Manually (re)run diagnosis."""
        for rec in self:
            rec._start_diagnosis()
        return True

    def action_mark_resolved(self):
        self.write({'resolved': True})
