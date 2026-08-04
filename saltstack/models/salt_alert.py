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

    # ── Identitet ────────────────────────────────────────────────────────
    name = fields.Char(
        string='Larm',
        compute='_compute_name',
        store=True,
    )
    host = fields.Char(required=True, string='Host')
    source = fields.Selection(
        selection=[],
        string='Källa',
        help='Källsystem (Wazuh, Zabbix...). Utökas av bryggmoduler via selection_add.',
    )
    category = fields.Selection([
        ('kernel', 'Kernel'),
        ('process', 'Process'),
        ('database', 'Database'),
        ('proxy', 'Proxy'),
        ('odoo', 'Odoo'),
        ('system', 'System'),
        ('other', 'Övrigt'),
    ], string='Kategori', default='other')
    severity = fields.Integer(string='Allvarlighetsgrad')

    # ── Larmdetaljer ─────────────────────────────────────────────────────
    trigger_name = fields.Char(string='Trigger')
    description = fields.Text(string='Beskrivning')
    raw_log = fields.Text(string='Rå logg')
    timestamp = fields.Datetime(string='Larmtid')
    wazuh_rule_id = fields.Char(string='Wazuh Rule ID')
    zabbix_event_id = fields.Char(string='Zabbix Event ID')

    # ── Korrelation & diagnos ────────────────────────────────────────────
    correlated_zabbix_alert = fields.Boolean(
        string='Korrelerat med Zabbix',
        default=False,
    )
    zabbix_alert_id = fields.Char(string='Zabbix Alert ID')
    coworker_session_id = fields.Char(string='Coworker-session')
    diagnosis_result = fields.Text(string='Diagnosresultat')
    diagnosis_state = fields.Selection([
        ('pending', 'Väntar'),
        ('running', 'Pågår'),
        ('done', 'Klar'),
        ('unavailable', 'AI otillgänglig'),
        ('error', 'Fel'),
    ], string='Diagnosstatus', default='pending')

    resolved = fields.Boolean(string='Löst', default=False)
    active = fields.Boolean(string='Aktiv', default=True)

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
                rec.host or '?', rec.trigger_name or 'Larm',
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

            # Korrelation med Zabbix
            alert._correlate_zabbix()

            # Notifiering för kritiska larm
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
            _logger.warning('Zabbix-korrelation misslyckades för %s: %s',
                            self.host, e)
            self.correlated_zabbix_alert = False

    # ── AI-diagnos ───────────────────────────────────────────────────────

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
                self.diagnosis_result = 'AI coworker unavailable (saltstack_ai ej installerad)'
                self.diagnosis_state = 'unavailable'
                return None

            Coworker = self.env['ai.coworker']
            coworker = self._get_diagnosis_coworker()
            if not coworker:
                self.diagnosis_result = 'AI coworker unavailable (ingen coworker finns)'
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
            _logger.exception('AI-diagnos misslyckades: %s', e)
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

        Inkluderar Driftlarm-recordet så coworkern kan skriva bedömning
        och ändra status direkt på recordet.
        """
        instructions = {
            'kernel': 'Kör: salt <host> cmd.run \'dmesg | tail -50\'. Letar OOM, kernel panic.',
            'process': 'Kör: salt <host> cmd.run \'systemctl status odoo\'. Om nere, föreslå omstart.',
            'database': 'Kör: salt <host> cmd.run \'pg_isready\'. Kolla replication-lag.',
            'proxy': 'Kör: salt <host> cmd.run \'systemctl status caddy\'. Kolla upstream.',
            'odoo': 'Kör: salt <host> cmd.run \'tail -100 /var/log/odoo/odoo-server.log\'. Tolka traceback.',
            'system': 'Kör: uptime, free -m, df -h, dmesg, journalctl.',
            'other': 'Diagnostisera generellt: systemstatus, tjänster, loggar.',
        }
        cat_instruction = instructions.get(self.category, instructions['other'])
        source_label = dict(self._fields['source'].selection).get(
            self.source, self.source or 'okänt')

        return (
            f"Driftlarm på host '{self.host}'.\n"
            f"Källa: {source_label}\n"
            f"Kategori: {self.category}\n"
            f"Trigger: {self.trigger_name}\n"
            f"Beskrivning: {self.description}\n"
            f"Severity: {self.severity}\n\n"
            f"## Diagnosinstruktion ({self.category})\n{cat_instruction}\n\n"
            f"## Rå logg (utdrag)\n{self.raw_log[:2000]}\n\n"
            f"Analysera rotorsaken, verifiera mot systemet, och ge en "
            f"konkret åtgärdsplan med kommandon.\n\n"
            f"## Driftlarm-record\n"
            f"Du arbetar mot Driftlarm-recordet med ID {self.id} "
            f"(modell saltstack.alert). Du KAN skriva din bedömning och "
            f"ändra status direkt på recordet via verktyget "
            f"driftlarm_update_bedömning. Använd det för att:\n"
            f"- Spara din bedömning (diagnosis_result)\n"
            f"- Sätta status (pending/running/done/error)\n"
            f"- Markera löst (resolved) när åtgärden är klar\n"
            f"- Lämna en åtgärdsplan i beskrivningen\n"
            f"\nRapportera även resultatet i ditt svar."
        )

    # ── Notifiering ──────────────────────────────────────────────────────

    def _get_or_create_channel(self):
        """Find or create the 'Driftlarm' discuss channel."""
        Channel = self.env['discuss.channel']
        channel = Channel.search([('name', '=', 'Driftlarm')], limit=1)
        if not channel:
            channel = Channel.with_user(
                self.env.ref('base.user_root')).create({
                    'name': 'Driftlarm',
                    'channel_type': 'channel',
                    'description': 'Driftlarm från Wazuh/Zabbix (via webhook)',
                })
        return channel

    def _notify_channel(self):
        """Post critical alert to Driftlarm channel."""
        self.ensure_one()
        try:
            channel = self._get_or_create_channel()
            source_label = dict(self._fields['source'].selection).get(
                self.source, self.source or 'okänt')
            channel.with_user(
                self.env.ref('base.user_root')).message_post(
                body=(
                    f'🚨 <b>Driftlarm</b> (severity {self.severity})<br/>'
                    f'<b>Källa:</b> {source_label}<br/>'
                    f'<b>Host:</b> {self.host}<br/>'
                    f'<b>Kategori:</b> {self.category}<br/>'
                    f'<b>Trigger:</b> {self.trigger_name}<br/>'
                    f'{self.description or ""}'
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_comment',
            )
        except Exception as e:
            _logger.warning('Kunde inte notifiera Driftlarm-kanalen: %s', e)

    def _post_action_plan(self, action_plan):
        """Post the AI action plan to Driftlarm channel."""
        self.ensure_one()
        try:
            channel = self._get_or_create_channel()
            channel.with_user(
                self.env.ref('base.user_root')).message_post(
                body=(
                    f'🧠 <b>Åtgärdsplan från AI-diagnos</b> '
                    f'(larm {self.id}, {self.host})<br/>'
                    f'<pre>{action_plan[:4000]}</pre>'
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_comment',
            )
        except Exception as e:
            _logger.warning('Kunde inte posta åtgärdsplan: %s', e)

    # ── Actions ──────────────────────────────────────────────────────────

    def action_diagnose(self):
        """Manually (re)run diagnosis."""
        for rec in self:
            rec._start_diagnosis()
        return True

    def action_mark_resolved(self):
        self.write({'resolved': True})
