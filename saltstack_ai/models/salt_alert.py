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
    # Klickbar länk till ai.coworker.session (chat/diagnos-session i Odoo).
    # Many2one: id:et renderas som en klickbar länk till sessionen.
    coworker_session_id = fields.Many2one(
        'ai.coworker.session', string='Coworker session', ondelete='set null',
        help='Länk till den AI-coworker-session (ai.coworker.session) som '
             'körde/använde denna diagnos. Klickbar till chatten.')
    diagnosis_result = fields.Text(string='Diagnosis result')
    diagnosis_state = fields.Selection([
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('unavailable', 'AI unavailable'),
        ('error', 'Error'),
    ], string='Diagnosis status', default='pending')

    # ── Utfall (steg 2: erfarenheten som skrivs tillbaka till skills) ─────
    # Detta är människans/AI:ns dom över vad larmet var — inte vad diagnosen
    # SA, utan vad som visade sig gälla. Utfallet matar success_cases /
    # failure_cases på de skills vars trigger_keywords matchade larmet, och
    # blir underlaget för den regelbaserade triagen (steg 3) och
    # förbättringsloopen (steg 4).
    outcome = fields.Selection([
        ('acted', 'Åtgärdat'),
        ('false_positive', 'Falskt positivt'),
        ('not_acted', 'Ej åtgärdat'),
        ('escalated', 'Eskalerat'),
    ], string='Utfall',
        help='Vad larmet visade sig vara. Åtgärdat + falskt positivt loggas som '
             'erfarenhet på matchande skills: åtgärdat som success_case, '
             'falskt positivt som failure_case ("så här ska vi inte larma").')
    experience_recorded = fields.Boolean(
        string='Erfarenhet loggad', default=False, readonly=True,
        help='Sätts när utfallet skrivits tillbaka till matchande skills — '
             'förhindrar dubbelloggning om fältet ändras fram och tillbaka.')
    triage_reason = fields.Char(
        string='Triage', readonly=True,
        help='Satt när larmet avgjordes av den regelbaserade triagen i stället '
             'för av en AI-diagnos — dvs. utan LLM-anrop. Tomt = AI tittade på det.')
    matched_skill_ids = fields.Many2many(
        'ai.skill', string='Matchade skills', readonly=True,
        help='De skills vars trigger_keywords matchade detta larm. Fylls i '
             'när erfarenheten loggas.')

    # ── AI diagnosis ─────────────────────────────────────────────────────

    def create(self, vals_list):
        """Skapa larm — och bokför erfarenhet om utfallet sätts direkt.

        write()-hooken fångar utfall som sätts i efterhand, men ett larm som
        skapas MED ett utfall (import, API, AI-writeback vid skapandet) skulle
        annars tyst tappa sin erfarenhet. Samma idempotens gäller —
        experience_recorded förhindrar dubbelloggning.
        """
        records = super().create(vals_list)
        for rec in records:
            if rec.outcome:
                try:
                    rec._record_experience()
                except Exception:
                    _logger.warning(
                        'Erfarenhetsloggning misslyckades för larm %s',
                        rec.id, exc_info=True)
                    rec._bump_experience_failure_count()
        return records

    def write(self, vals):
        """Logga erfarenhet när utfallet sätts eller ändras.

        Idempotent via experience_recorded: att växla utfallet fram och
        tillbaka loggar inte samma erfarenhet två gånger, men ett NYTT utfall
        (t.ex. false_positive → acted) loggas, eftersom det är ny kunskap.

        Ett misslyckande i loggningen får ALDRIG hindra att utfallet sparas —
        men det får heller inte försvinna spårlöst (6.2): att loopen slutat
        lära sig är annars omöjligt att upptäcka i efterhand, eftersom
        utfallet ändå ser sparat ut.
        """
        res = super().write(vals)
        if 'outcome' in vals and vals.get('outcome'):
            for alert in self:
                try:
                    alert._record_experience()
                except Exception:
                    _logger.warning(
                        'Erfarenhetsloggning misslyckades för larm %s',
                        alert.id, exc_info=True)
                    alert._bump_experience_failure_count()
        return res

    # ── Synlighet: erfarenhetsloggning får inte fela tyst (6.2) ────────
    #
    # Ett tyst misslyckande här är särskilt farligt: larmets utfall sparas
    # ändå, så allt ser normalt ut — men loopen lär sig ingenting. Därför
    # räknas varje misslyckande i en ir.config_parameter som Zabbix kan
    # larma på, i stället för att bara hamna i loggen.
    _EXPERIENCE_FAILURE_KEY = 'saltstack.alert.experience_failures'

    def _bump_experience_failure_count(self):
        """Öka räknaren för misslyckad erfarenhetsloggning (Zabbix-mätvärde)."""
        try:
            params = self.env['ir.config_parameter'].sudo()
            current = params.get_param(self._EXPERIENCE_FAILURE_KEY, '0')
            params.set_param(
                self._EXPERIENCE_FAILURE_KEY, str(int(current or 0) + 1))
        except Exception:
            _logger.error(
                'kunde inte öka erfarenhets-felräknaren', exc_info=True)

    @api.model
    def _experience_failure_count(self):
        """Läs räknaren (för Zabbix/monitoring)."""
        value = self.env['ir.config_parameter'].sudo().get_param(
            self._EXPERIENCE_FAILURE_KEY, '0')
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _alert_text_part(value):
        """Gör ett faltvarde till text — host är en many2one (recordset)."""
        if not value:
            return ''
        if hasattr(value, 'name'):
            return str(value.name or '')
        return str(value)

    def _matching_skills(self):
        """Skills vars trigger_keywords matchar larmet (helord).

        Samma matchningsregel som skill-aktiveringen i ai_agent_core, så att
        den erfarenhet vi loggar hamnar på precis de skills som faktiskt
        aktiverades för larmet.
        """
        self.ensure_one()
        import re
        Skill = self.env['ai.skill']
        text = ' '.join([
            self._alert_text_part(self.trigger_name),
            self._alert_text_part(self.description),
            self._alert_text_part(self.category),
            self._alert_text_part(self.host),
        ]).lower()
        if not text.strip():
            return Skill.browse()
        matched = Skill.browse()
        for skill in Skill.search([]):
            for kw in re.split(r'[,\n]', skill.trigger_keywords or ''):
                kw = kw.strip().lower()
                if not kw:
                    continue
                if re.search(r'(?<![a-z0-9])' + re.escape(kw)
                             + r'(?![a-z0-9])', text):
                    matched |= skill
                    break
        return matched

    def _record_experience(self):
        """Skriv utfallet tillbaka som erfarenhet på matchande skills.

        åtgärdat        → success_case  (regeln ledde rätt)
        falskt positivt → failure_case  (regeln ska inte larma så här)
        ej åtgärdat / eskalerat → ingen erfarenhet (inget att lära ännu)
        """
        self.ensure_one()
        if not self.outcome:
            return False
        verdicts = {'acted': 'success', 'false_positive': 'failure'}
        verdict = verdicts.get(self.outcome)
        skills = self._matching_skills()
        self.matched_skill_ids = [(6, 0, skills.ids)]
        if not verdict or not skills:
            self.experience_recorded = bool(skills) or self.experience_recorded
            return False
        note = '%s: %s' % (self._alert_text_part(self.host) or '?',
                           (self.trigger_name or '')[:160])
        if self.outcome == 'false_positive':
            note = 'Falskt positivt — ' + note
        written = 0
        for skill in skills:
            if skill.record_experience(
                    note, verdict=verdict,
                    source='saltstack.alert %s' % self.id):
                written += 1
        self.experience_recorded = True
        _logger.info('Larm %s (%s): %d erfarenhet(er) loggade på %d skills',
                     self.id, self.outcome, written, len(skills))
        return bool(written)

    # ── Steg 3: regelbaserad triage före LLM ─────────────────────────────

    @api.model
    def _triage_enabled(self):
        """Global växel för den regelbaserade triagen (default: på)."""
        value = self.env['ir.config_parameter'].sudo()._get_param(
            'saltstack.alert.triage_enabled')
        if value is None:
            return True
        return str(value).lower() in ('true', '1')

    @api.model
    def _triage_min_history(self):
        """Hur många tidigare bedömda larm som krävs innan triagen agerar."""
        value = self.env['ir.config_parameter'].sudo()._get_param(
            'saltstack.alert.triage_min_history')
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 3

    def _triage_history(self):
        """Tidigare larm med samma värd + trigger som har ett satt utfall.

        Bara larm MÄNNISKAN eller AI:n faktiskt bedömt räknas — annars skulle
        triagen kunna bekräfta sina egna gissningar.
        """
        self.ensure_one()
        return self.search([
            ('id', '!=', self.id),
            ('host', '=', self.host.id if self.host else False),
            ('trigger_name', '=', self.trigger_name),
            ('outcome', '!=', False),
        ])

    def _triage(self):
        """Regelbaserad triage FÖRE LLM.

        Returnerar True när larmet avgjordes utan ett enda LLM-anrop.

        Regeln: samma värd + samma trigger, minst `triage_min_history`
        tidigare BEDÖMDA larm, och samtliga bedömda som 'false_positive' →
        larmet är känt brus och stängs som falskt positivt.

        Medvetet konservativ: finns ett enda 'acted' bland historiken (det var
        alltså ett verkligt problem någon gång) släpps larmet vidare till AI.
        Likaså om någon bedömt det som 'not_acted' eller 'escalated' — då är
        det inte entydigt brus.
        """
        self.ensure_one()
        if not self._triage_enabled():
            return False
        if self.outcome:
            return False  # redan bedömd — triagen lägger sig inte i
        history = self._triage_history()
        minimum = self._triage_min_history()
        if len(history) < minimum:
            return False
        if set(history.mapped('outcome')) != {'false_positive'}:
            return False  # verkligt problem har förekommit → låt AI titta

        n = len(history)
        reason = ('Auto: %d tidigare larm med samma trigger på samma värd '
                  'var falska positiva.' % n)
        host_name = self._alert_text_part(self.host) or '?'
        self.write({
            'outcome': 'false_positive',
            'diagnosis_state': 'done',
            'resolved': True,
            'triage_reason': reason[:120],
            'diagnosis_result': (
                'Regelbaserad triage — inget LLM-anrop.\n\n'
                'Värd: %s\nTrigger: %s\n\n%s\n\n'
                'Larmet stängs som falskt positivt. Ändra utfallet om det var '
                'fel — det loggas då som erfarenhet och triagen slutar agera.'
                % (host_name, self.trigger_name or '', reason)),
        })
        try:
            self.message_post(
                body=self._triage_reason_html(reason),
                message_type='notification')
        except Exception:
            _logger.debug('triage chatter-post misslyckades', exc_info=True)
        _logger.info('Triage: larm %s stängt utan LLM (%s)', self.id, reason)
        return True

    @staticmethod
    def _triage_reason_html(reason):
        return '<p><b>Regelbaserad triage</b> — inget LLM-anrop.</p><p>%s</p>' % reason

    @api.model
    def _auto_diagnose_enabled(self):
        """Global switch for AI diagnosis on drift alerts.

        Matches the field default (on). set_values() always writes an
        explicit 'True'/'False' string, so a missing row only happens on a
        system that never opened Settings — treat that as on.

        NOTE: get_param(key) returns False (not None) when the row is
        missing, so the absence check must test for both.
        """
        value = self.env['ir.config_parameter'].sudo()._get_param(
            'saltstack.alert.auto_diagnose')
        if value is None:
            return True  # never configured → field default
        return str(value).lower() in ('true', '1')

    def _schedule_diagnosis(self):
        """Planera AI-diagnos asynkront.

        Anropas synkront från process_webhook / action_diagnose-knappen.
        Sätter diagnosen till 'pending' så webhook/HTTP-svar returnerar
        OMEDELBART (coworker.run() kan ta minuter, särskilt vid nere
        minioner). En Odoo-cron (_run_pending_diagnoses) plockar upp
        pending-alerts och exekverar _start_diagnosis i bakgrunden.

        Gaten kontrolleras först: är auto-diagnosen avstängd (globalt eller
        för källan) sätts ingen 'pending' alls, så cronen aldrig plockar upp
        alerten.

        Steg 3: innan 'pending' sätts körs den regelbaserade triagen. Är
        larmet känt brus hanteras det här och blir aldrig en 'pending' —
        alltså noll LLM-anrop.
        """
        self.ensure_one()
        if not (self._auto_diagnose_enabled()
                and self._auto_diagnose_enabled_for_source()):
            self.diagnosis_state = 'unavailable'
            self.diagnosis_result = ''
            return False
        if self._triage():
            return False
        self.diagnosis_state = 'pending'
        self.diagnosis_result = ''
        return True

    def _run_pending_diagnoses(self, limit=5):
        """Cron: exekvera köade (pending) diagnoser i bakgrunden.

        Kör _start_diagnosis på upp till `limit` pending-alerts. Den hänger
        med en egen cursor (queue_job-from-cron-mönster) så en lång
        coworker.run() inte påverkar andra requests. Idempotent per alert.

        Gaten (global + källspecifik) kontrolleras HÄR OCKSÅ: en alert kan ha
        satts till 'pending' innan inställningen stängdes av, eller av en
        webhook som körde mot en äldre konfiguration. Utan denna kontroll
        plockar cronen upp gated alerts och kör diagnosen ändå.
        """
        from odoo import api as _api, registry as _registry
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
            # Respektera gaten: global av eller källan avstängd → kör inte.
            if not (rec._auto_diagnose_enabled()
                    and rec._auto_diagnose_enabled_for_source()):
                _logger.info(
                    'Skipping pending diagnosis for alert %s (source %s) '
                    '— auto diagnosis is disabled', rec.id, rec.source)
                rec.diagnosis_state = 'unavailable'
                continue
            # Steg 3: triagen körs i den färska cursorn nedan (rec_env).
            # Ny registry/cursor per körning (long-running)
            try:
                new_cr = _registry(dbname).cursor()
                rec_env = _api.Environment(
                    new_cr, self.env.uid, dict(
                        ctx, _ai_force_coworker_groups=True))
                rec = rec_env['saltstack.alert'].browse(rec.id)
                # Steg 3: regelbaserad triage FÖRE LLM. Känd brusmönster
                # hanteras här — annars hade varje sådant larm kostat en
                # supervisor-körning med specialistteam.
                if rec._triage():
                    new_cr.commit()
                    _logger.info(
                        'Triage avgjorde larm %s utan LLM (%s)',
                        rec.id, rec.triage_reason)
                else:
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
            f"## Utfall (obligatoriskt — det är så systemet lär sig)\n"
            f"När du är klar: anropa driftlarm_update_assessment med `outcome` satt "
            f"till vad larmet VISADE sig vara (inte vad du först misstänkte):\n"
            f"- acted: ett verkligt problem hittades och åtgärdades\n"
            f"- false_positive: larmet var brus (transient spik, känt underhåll, "
            f"omstartsvåg). Sätt detta så snart du konstaterat att inget var fel.\n"
            f"- not_acted: verkligt problem men medvetet inte åtgärdat\n"
            f"- escalated: kräver människa\n"
            f"Utan outcome kan inte triagen lära sig vilka larm som är brus, och "
            f"samma larm kostar en AI-körning igen nästa gång.\n\n"
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
        from markupsafe import escape as html_escape
        msg = f'<b>Diagnos klar</b>'
        if action_taken:
            msg += f'<br/>🔧 <b>Åtgärd:</b> {action_taken}'
        if result:
            excerpt = result[:3000] if len(result) > 3000 else result
            # Escape resultatet så LLM:ens HTML/markdown visas som TEXT (inte
            # renderas rå) — förhindrar "html i klartext" och injektion.
            msg += f'<br/><pre>{html_escape(excerpt)}</pre>'
        self.message_post(body=msg, message_type='notification',
                          subtype_xmlid='mail.mt_note')

    def _post_minion_chatter(self, result):
        """Post a summary of the alert + diagnosis on the related minion record.

        Finds the salt.minion via the host field.
        """
        self.ensure_one()
        if not self.host:
            return
        # host är Many2one → salt.minion; använd recordet direkt
        minion = self.host
        if not minion:
            _logger.info('No minion record for host %s — skipping chatter',
                         self.host)
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
