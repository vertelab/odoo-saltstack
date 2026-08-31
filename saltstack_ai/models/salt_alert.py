# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""
AI diagnosis extension for saltstack.alert.

Bridge module (saltstack_ai) adds AI-coupled fields and methods on top of
the ground model. The base module never depends on these — process_webhook
guards with hasattr, so the AI flow only runs when this bridge is installed.
"""

import logging

from odoo import api, fields, models

# queue_job (OCA) för asynkron diagnos-körning: webhook/knapp dispatcher
# jobbet i bakgrunden i stället för att blockera i `coworker.run()` (som kan
# ta minuter vid nere minioner).
try:
    from odoo.addons.queue_job.job import job as queue_job_job
    _queue_job = True
except ImportError:
    queue_job_job = None
    _queue_job = False

_logger = logging.getLogger(__name__)


class SaltAlert(models.Model):
    _inherit = 'saltstack.alert'

    # ── Correlation & diagnosis ──────────────────────────────────────────
    coworker_session_id = fields.Char(string='Coworker session')
    diagnosis_result = fields.Text(string='Diagnosis result')
    diagnosis_state = fields.Selection([
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('unavailable', 'AI unavailable'),
        ('error', 'Error'),
    ], string='Diagnosis status', default='pending')

    # ── AI diagnosis ─────────────────────────────────────────────────────

    @api.model
    def _auto_diagnose_enabled(self):
        return self.env['ir.config_parameter'].get_param(
            'saltstack.alert.auto_diagnose', 'True') in ('True', 'true', '1')

    def _schedule_diagnosis(self):
        """Planera AI-diagnos asynkront.

        Anropas synkront från process_webhook / action_diagnose-knappen.
        Sätter diagnosen till 'pending' så webhook/HTTP-svar returnerar
        OMEDELBART (coworker.run() kan ta minuter, särskilt vid nere
        minioner). En Odoo-cron (_run_pending_diagnoses) plockar upp
        pending-alerts och exekverar _start_diagnosis i bakgrunden.
        """
        self.ensure_one()
        self.diagnosis_state = 'pending'
        self.diagnosis_result = ''
        return True

    def _run_pending_diagnoses(self, limit=5):
        """Cron: exekvera köade (pending) diagnoser i bakgrunden.

        Kör _start_diagnosis på upp till `limit` pending-alerts. Den hänger
        med en egen cursor (queue_job-from-cron-mönster) så en lång
        coworker.run() inte påverkar andra requests. Idempotent per alert.
        """
        from odoo import api as _api, registry as _registry
        from odoo.service.db import check_db_management_enabled  # noqa
        # Använd den nuvarande registryn med en färsk cursor per alert för
        # att undvika teardown-att-problem under långa LLM-körningar.
        cr = self._cr
        dbname = cr.dbname
        uid = self.env.uid
        ctx = dict(self.env.context)
        recs = self.sudo().search([
            ('diagnosis_state', '=', 'pending')], order='write_date asc',
            limit=limit)
        for rec in recs:
            # Ny registry/cursor per körning (long-running)
            try:
                new_cr = _registry(dbname).cursor()
                rec_env = _api.Environment(
                    new_cr, self.env.uid, dict(
                        ctx, _ai_force_coworker_groups=True))
                rec = rec_env['saltstack.alert'].browse(rec.id)
                rec._start_diagnosis()
                new_cr.commit()
            except Exception:
                _logger.exception('pending diagnosis failed for alert %s',
                                  rec.id)
                try:
                    new_cr.rollback()
                except Exception:
                    pass
            finally:
                try:
                    new_cr.close()
                except Exception:
                    pass
        return True

    def _start_diagnosis(self):
        """Start AI diagnosis via the selected AI coworker (kört av cron)."""
        self.ensure_one()
        self.diagnosis_state = 'running'
        self._post_diagnosis_start()
        try:
            if 'ai.coworker' not in self.env:
                self.diagnosis_result = 'AI coworker unavailable (saltstack_ai not installed)'
                self.diagnosis_state = 'unavailable'
                self._post_diagnosis_result(
                    'AI coworker unavailable (saltstack_ai not installed)', '')
                return None

            Coworker = self.env['ai.coworker']
            coworker = self._get_diagnosis_coworker()
            if not coworker:
                self.diagnosis_result = 'AI coworker unavailable (no coworker exists)'
                self.diagnosis_state = 'unavailable'
                self._post_diagnosis_result(
                    'AI coworker unavailable (no coworker exists)', '')
                return None

            prompt = self._build_diagnosis_prompt()
            # Kör diagnosen med coworkerns EGNA access-grupper
            # (Infrastructure Operator) så salt/zabbix-verktygen är
            # tillgängliga oavsett vem som triggar (webhook=kör som admin,
            # knappklick=kör som inloggad användare). Utan detta nekas
            # verktygen för användare utan "Infrastructure Operator"-gruppen
            # och diagnosen blir blind (2026-08-31).
            coworker = coworker.with_context(
                _ai_force_coworker_groups=True)
            # Supervisor-kontextoptimering (2026-08-31): begränsa var
            # diagnosis-coworkern ser till en UPPGIFTSRELEVANT verktygsuppsättning.
            # Alla 50+ verktyg i payload:en får LLM:en att svälja tool_calls i
            # content-text och misslyckas; ~8-12 relevanta → native tool_calls.
            whitelist = self._diagnosis_tool_whitelist()
            if whitelist:
                coworker = coworker.with_context(
                    _ai_tool_whitelist=whitelist)
            result = coworker.run(prompt)
            result_str = str(result)[:5000] if result else ''
            self.diagnosis_result = result_str
            self.diagnosis_state = 'done'

            # Chatter writeback
            self._post_diagnosis_result(result_str, '')
            self._post_minion_chatter(result_str)

            if self.severity >= 12 and result:
                self._post_action_plan(str(result))

            return result
        except Exception as e:
            _logger.exception('AI diagnosis failed: %s', e)
            self.diagnosis_result = 'AI coworker unavailable: %s' % str(e)
            self.diagnosis_state = 'unavailable'
            self._post_diagnosis_result(
                'AI diagnosis failed: %s' % str(e), '')
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
            'process': 'Run: salt <host> cmd.run \'systemctl status odoo\'. If down, AUTO-RESTART: systemctl start odoo. Verify after 5s.',
            'database': 'Run: salt <host> cmd.run \'pg_isready\'. Check replication lag. AUTO-RESTART only on replica, NEVER on primary.',
            'proxy': 'Run: salt <host> cmd.run \'systemctl status caddy\'. Check upstream with curl localhost:8069. If upstream OK, AUTO-RELOAD: systemctl reload caddy.',
            'odoo': 'Run: salt <host> cmd.run \'tail -100 /var/log/odoo/odoo-server.log\'. Interpret traceback. If Odoo is down, AUTO-RESTART.',
            'system': 'Run: uptime, free -m, df -h, dmesg, journalctl. If grow.log found and disk > 85%, AUTO-REMOVE grow.log (it is a test file).',
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
            f"## Driftlarm record\n"
            f"You are working on the Driftlarm record with ID {self.id} "
            f"(model saltstack.alert). You CAN write your assessment and "
            f"change the status directly on the record via the tool "
            f"driftlarm_update_assessment. Use it to:\n"
            f"- Save your assessment (diagnosis_result)\n"
            f"- Set the status (pending/running/done/error)\n"
            f"- Mark as resolved when the action is complete\n"
            f"- Leave an action plan in the description\n\n"
            f"## Odoo ORM tools\n"
            f"- Describe the schema first: describe_model(model='salt.minion') / "
            f"describe_model(model='saltstack.alert') to understand fields\n"
            f"- Find the minion record: odoo_search(model='salt.minion', "
            f"domain=[['name', '=', '{self.host}']])\n"
            f"- Check alert history: odoo_search(model='saltstack.alert', "
            f"domain=[['host', '=', '{self.host}'], ['trigger_name', '=', "
            f"'{self.trigger_name}']], order='create_date desc', limit=5)\n"
            f"- If the minion record has action methods, use odoo_call_method\n\n"
            f"## Auto-fix rules\n"
            f"- SAFE TO AUTO-FIX: odoo/postfix/dovecot down → restart service. "
            f"grow.log disk full → remove file. Caddy 502 if upstream OK → reload.\n"
            f"- DOCUMENT ONLY (no auto-fix): OOM kill → restart process but "
            f"create helpdesk ticket. CPU spikes → document as nonconformity. "
            f"Primary database down → NEVER auto-restart.\n\n"
            f"## Chatter rules\n"
            f"- Post ALL findings and actions on the alert record (id={self.id})\n"
            f"- Post a summary on the minion record after diagnosis\n"
            f"- If auto-fix fails: create helpdesk ticket\n\n"
            f"Analyze the root cause, verify against the system, apply auto-fix "
            f"if safe, and document everything."
        )

    # ── Chatter / Writeback ────────────────────────────────────────────

    def _diagnosis_tool_whitelist(self):
        """Rekommendera en begränsad, uppgiftsrelevant verktygsuppsättning.

        Supervisor-kontextoptimering: istället för att exponera alla 50+
        verktyg för diagnosis-coworkern (som då SVÄLJER tool_calls i text),
        välj per kategori en kompakt uppsättning (~8-14) med bas-driftverktyg
        + kategorispecifika. Returnerar lista av a:tool-namn (eller [] för att
        behålla alla som fallback).
        """
        BASE = [
            'describe_model', 'odoo_search', 'odoo_call_method',
            'salt_test_ping', 'salt_cmd_run',
            'driftlarm_update_assessment', 'create_helpdesk_ticket',
        ]
        by_cat = {
            'kernel': [
                'salt_cmd_run', 'salt_journal_errors', 'salt_memory_usage',
                'salt_process_list', 'document_nonconformity'],
            'process': [
                'salt_service_status', 'salt_service_restart',
                'salt_process_list', 'tail_odoo_log', 'grep_odoo_errors'],
            'database': [
                'pg_isready', 'pg_stat_activity', 'pg_replication_lag',
                'odoo_cron_status', 'salt_service_status'],
            'proxy': [
                'caddy_status', 'caddy_recent_errors',
                'caddy_upstream_health', 'salt_service_status'],
            'odoo': [
                'tail_odoo_log', 'grep_odoo_errors', 'odoo_cron_status',
                'salt_service_status'],
            'system': [
                'salt_disk_usage', 'salt_memory_usage', 'salt_system_load',
                'salt_journal_errors', 'salt_grains_items'],
            'other': [
                'zabbix_get_problems', 'zabbix_get_host', 'zabbix_get_triggers',
                'salt_pillar_items', 'salt_grains_items', 'salt_service_status',
                'salt_system_load', 'salt_journal_errors'],
        }
        extras = by_cat.get(self.category, by_cat['other'])
        # Deduplicera, bevara ordning
        out = []
        seen = set()
        for n in BASE + extras:
            if n not in seen:
                seen.add(n)
                out.append(n)
        _logger.info('diagnosis whitelist (%s): %d verktyg',
                     self.category, len(out))
        return out

    def _post_diagnosis_start(self):
        """Post diagnosis start as chatter on the alert record."""
        self.ensure_one()
        self.message_post(
            body=(
                f'🔍 <b>AI-diagnos påbörjad</b><br/>'
                f'Kategori: {self.category}<br/>'
                f'Undersöker {self.host} — {self.trigger_name}'
            ),
            message_type='notification',
        )

    def _post_diagnosis_result(self, result, action_taken):
        """Post diagnosis result + action taken as chatter on the alert record."""
        self.ensure_one()
        msg = f'<b>Diagnos klar</b>'
        if action_taken:
            msg += f'<br/>🔧 <b>Åtgärd:</b> {action_taken}'
        if result:
            excerpt = result[:3000] if len(result) > 3000 else result
            msg += f'<br/><pre>{excerpt}</pre>'
        self.message_post(body=msg, message_type='notification')

    def _post_minion_chatter(self, result):
        """Post a summary of the alert + diagnosis on the related minion record.

        Finds the salt.minion via the host field.
        """
        self.ensure_one()
        if not self.host:
            return
        Minion = self.env['salt.minion']
        minion = Minion.search([('name', '=', self.host)], limit=1)
        if not minion:
            _logger.info('No minion record for host %s — skipping chatter', self.host)
            return
        source_label = dict(self._fields['source'].selection).get(
            self.source, self.source or 'unknown')
        summary = result[:500] if result and len(result) > 500 else (result or '')
        minion.message_post(
            body=(
                f'🚨 <b>Driftlarm</b> ({source_label}) — '
                f'<a href="/web#id={self.id}&model=saltstack.alert">alert #{self.id}</a><br/>'
                f'<b>Trigger:</b> {self.trigger_name}<br/>'
                f'<b>Severity:</b> {self.severity}<br/>'
                f'<b>Diagnos:</b> {self.diagnosis_state}<br/>'
                f'<b>Sammanfattning:</b> {summary}'
            ),
            message_type='notification',
        )

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
        """Manually (re)run diagnosis (asynkront via queue_job)."""
        for rec in self:
            rec._schedule_diagnosis()
        return True
