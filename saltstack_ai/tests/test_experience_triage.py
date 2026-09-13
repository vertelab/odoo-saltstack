# -*- coding: utf-8 -*-
"""saltstack_ai — erfarenhetsloop och regelbaserad triage.

driftlarm-erfarenhet-triage (steg 2–3):
  * utfallet skrivs tillbaka som erfarenhet på matchande skills (idempotent)
  * erfarenheten hamnar på samma skills som aktiveringen skulle välja
  * den regelbaserade triagen stänger kända bruslarm utan LLM-anrop
  * inställningarna styr triagen, och en avstängd triage rör inget larm

Run with: checkmodule -d <db> -m saltstack_ai -t
"""

from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAlertExperience(TransactionCase):
    """Steg 2: utfallet bokförs som erfarenhet på matchande skills."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Alert = cls.env['saltstack.alert']
        cls.Minion = cls.env['salt.minion']
        cls.Skill = cls.env['ai.skill']

    def _minion(self, name):
        m = self.Minion.search([('name', '=', name)], limit=1)
        return m or self.Minion.create({'name': name})

    def _skill(self, name, keywords):
        return self.Skill.create({
            'name': name,
            'description': 'test-skill för erfarenhetsloop',
            'trigger_keywords': keywords,
        })

    def _alert(self, host='exp-host-01', trigger='ertestnoact usage high',
               category='system', outcome=False):
        vals = {
            'host': self._minion(host).id,
            'source': 'zabbix',
            'category': category,
            'severity': 10,
            'trigger_name': trigger,
            'description': 'Simulerat larm',
            'raw_log': 'disk full',
        }
        if outcome:
            vals['outcome'] = outcome
        return self.Alert.create(vals)

    # ── Steg 2: bokföring ─────────────────────────────────────────────

    def test_false_positive_writes_failure_case(self):
        """2.1/3.2: falskt positivt ⇒ failure_cases på matchande skill."""
        skill = self._skill('Ertest disk-skill', 'ertestdisk, ertestlagring')
        alert = self._alert(trigger='ertestdisk usage high')
        before = skill.recipe_text
        alert.outcome = 'false_positive'
        self.assertIn(skill, alert.matched_skill_ids)
        self.assertIn('Falskt positivt', skill.failure_cases or '')
        self.assertEqual(skill.recipe_text, before,
                         'recipe_text får aldrig ändras av erfarenheten')

    def test_acted_writes_success_case(self):
        """2.1: åtgärdat ⇒ success_cases."""
        skill = self._skill('Ertest acted-skill', 'ertestacted')
        alert = self._alert(host='exp-host-02', trigger='ertestacted usage high')
        alert.outcome = 'acted'
        self.assertIn(skill, alert.matched_skill_ids)
        self.assertIn('exp-host-02', skill.success_cases or '')
        self.assertIn('ertestacted usage high', skill.success_cases or '')

    def test_not_acted_writes_no_experience(self):
        """2.1: ej åtgärdat/eskalerat ⇒ ingen erfarenhet (inget att lära)."""
        skill = self._skill('Ertest noact-skill', 'ertestnoact')
        alert = self._alert(host='exp-host-03', trigger='ertestnoact usage high')
        alert.outcome = 'not_acted'
        self.assertFalse(skill.success_cases)
        self.assertFalse(skill.failure_cases)

    def test_no_outcome_writes_no_experience(self):
        """2.1: larm utan utfall bokför ingenting."""
        skill = self._skill('Ertest noout-skill', 'ertestnoout')
        alert = self._alert(host='exp-host-04', trigger='ertestnoout usage high')
        self.assertFalse(alert.outcome)
        self.assertFalse(skill.success_cases)
        self.assertFalse(skill.failure_cases)

    def test_writeback_is_idempotent_on_toggle(self):
        """2.2: att växla utfallet fram och tillbaka dubbelloggar inte.

        Notera: produktionsdatabasen kan ha andra skills vars nyckelord
        matchar larmet — därför mäts antalet rader hos MIN skill, inte
        summan över alla matchade skills.
        """
        skill = self._skill('Ertest idem-skill', 'idemtestunik')
        alert = self._alert(host='exp-host-05', trigger='idemtestunik trigger',
                            outcome='false_positive')
        self.assertIn(skill, alert.matched_skill_ids)
        lines_after_first = len(
            [l for l in (skill.failure_cases or '').split('\n') if l.strip()])
        self.assertEqual(lines_after_first, 1)
        # Växla fram och tillbaka till SAMA utfall → ingen ny erfarenhet.
        alert.outcome = 'acted'
        alert.outcome = 'false_positive'
        lines_now = len(
            [l for l in (skill.failure_cases or '').split('\n') if l.strip()])
        self.assertLessEqual(
            lines_now, lines_after_first,
            'samma erfarenhet ska dedupas (räknare), inte bli ny rad')

    def test_matching_uses_whole_word(self):
        """3.1: helordsmatchning — delsträng ska inte ge träff.

        Nyckelordet 'wordtest' får inte matcha inuti 'wordtestx'.
        """
        skill = self._skill('Ertest word-skill', 'wordtest')
        # 'wordtestx' innehåller 'wordtest' som DELSTRÄNG men inte som helord.
        alert = self._alert(host='exp-host-06',
                            trigger='wordtestx error hittad')
        alert.outcome = 'false_positive'
        self.assertNotIn(
            skill, alert.matched_skill_ids,
            'helordsregeln ska inte matcha delsträngen wordtest i wordtestx')
        # Men ett fristående helord ska ge träff.
        alert2 = self._alert(host='exp-host-06b',
                             trigger='wordtest hittad')
        alert2.outcome = 'false_positive'
        self.assertIn(skill, alert2.matched_skill_ids)

    def test_alert_without_matching_skill_does_not_fail(self):
        """3.3: larm utan matchande skill felmarkeras inte.

        Verifierar att anropet inte kastar och att vår egen skill (med ett
        unikt nyckelord) inte matchas — produktionsdatabasen kan ha andra
        skills vars nyckelord råkar matcha larmtexten.
        """
        skill = self._skill('Ertest nomatch-skill', 'zzq-unikt-nyckelord')
        alert = self._alert(host='exp-host-07',
                            trigger='Helt unik trigger qxzv')
        alert.outcome = 'false_positive'  # får inte kasta
        self.assertNotIn(skill, alert.matched_skill_ids)
        self.assertTrue(alert.outcome)
        self.assertFalse(skill.success_cases)
        self.assertFalse(skill.failure_cases)


@tagged('post_install', '-at_install')
class TestAlertTriage(TransactionCase):
    """Steg 3: regelbaserad triage stänger kända bruslarm utan LLM."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Alert = cls.env['saltstack.alert']
        cls.Minion = cls.env['salt.minion']
        cls.Config = cls.env['ir.config_parameter'].sudo()

    def setUp(self):
        super().setUp()
        # Ren triage-inställning per test.
        self.Config.set_param('saltstack.alert.triage_min_history', '3')
        self.Config.set_param('saltstack.alert.triage_enabled', 'True')
        self._trigger = False

    def _minion(self, name):
        m = self.Minion.search([('name', '=', name)], limit=1)
        return m or self.Minion.create({'name': name})

    def _alert(self, host='tri-host-01', trigger=False,
               outcome=False):
        if not trigger:
            # Unik trigger per test → produktionsdata kan inte interferera.
            import uuid
            trigger = 'Noisy trigger %s' % uuid.uuid4().hex[:8]
            self._trigger = trigger
        vals = {
            'host': self._minion(host).id,
            'source': 'zabbix',
            'category': 'process',
            'severity': 8,
            'trigger_name': trigger,
            'description': 'Simulerat bruslarm',
            'raw_log': 'noise',
        }
        if outcome:
            vals['outcome'] = outcome
        return self.Alert.create(vals)

    def _history(self, n=3, host='tri-host-01', trigger=None,
                 outcome='false_positive'):
        if trigger is None:
            import uuid
            trigger = 'Noisy trigger %s' % uuid.uuid4().hex[:8]
        for _ in range(n):
            a = self._alert(host=host, trigger=trigger)
            a.outcome = outcome
        self._trigger = trigger
        return n

    def _next_alert(self, host='tri-host-01'):
        """Nytt larm med SAMMA trigger som historiken."""
        return self._alert(host=host, trigger=self._trigger)

    # ── 5.1 känd brusregel stängs ─────────────────────────────────────

    def test_triage_closes_known_noise_without_llm(self):
        """5.1: N falska positiva + samtliga ⇒ nästa larm stängs utan LLM."""
        self._history(3)
        alert = self._next_alert()
        with patch.object(type(alert), '_schedule_diagnosis') as mocked:
            triaged = alert._triage()
            self.assertTrue(triaged, 'triagen ska ha stängt larmet')
            self.assertFalse(mocked.called,
                             'inget AI-anrop får startas av triagen')
        self.assertEqual(alert.outcome, 'false_positive')
        self.assertTrue(alert.resolved)
        self.assertTrue(alert.triage_reason)
        self.assertEqual(alert.diagnosis_state, 'done')

    def test_triage_posts_explanation_to_chatter(self):
        """5.4: triagestängningen postar förklaringen i chatter."""
        self._history(3)
        alert = self._next_alert()
        before = len(alert.message_ids)
        alert._triage()
        self.assertGreater(len(alert.message_ids), before)

    def test_triage_chatter_failure_does_not_break_close(self):
        """5.4: ett misslyckat chatter-post får inte fälla stängningen."""
        self._history(3)
        alert = self._next_alert()
        with patch.object(type(alert), 'message_post',
                          side_effect=Exception('chatter nere')):
            triaged = alert._triage()  # får inte kasta
        self.assertTrue(triaged)
        self.assertEqual(alert.outcome, 'false_positive')

    # ── 5.2 verkligt problem släpps vidare ────────────────────────────

    def test_one_acted_blocks_triage(self):
        """5.2: ett enda åtgärdat larm i historiken ⇒ triagen avstår."""
        self._history(3)
        acted = self._next_alert()
        acted.outcome = 'acted'
        alert = self._alert()
        self.assertFalse(alert._triage())
        self.assertFalse(alert.outcome)

    def test_not_acted_blocks_triage(self):
        """5.2: not_acted/escalated är inte entydigt brus ⇒ avstå."""
        self._history(2)
        other = self._next_alert()
        other.outcome = 'escalated'
        alert = self._alert()
        self.assertFalse(alert._triage())

    # ── 5.3 otillräcklig historik ─────────────────────────────────────

    def test_insufficient_history_does_not_triage(self):
        """5.3: färre än N bedömda larm ⇒ triagen avstår."""
        self._history(2)  # N = 3
        alert = self._alert()
        self.assertFalse(alert._triage())
        self.assertFalse(alert.outcome)

    def test_untouched_alerts_are_not_history(self):
        """2.3: endast larm MED satt utfall räknas som historik."""
        # Skapa 5 larm UTAN utfall — de får inte bli triage-historik.
        for _ in range(5):
            self._next_alert()
        alert = self._next_alert()
        self.assertFalse(alert._triage())
        self.assertEqual(len(alert._triage_history()), 0)

    def test_already_adjudicated_alert_is_not_triaged(self):
        """2.4: ett larm som redan har ett utfall triageras aldrig."""
        self._history(3)
        alert = self._next_alert()
        alert.outcome = 'acted'
        self.assertFalse(alert._triage())

    # ── 4.2/4.4 inställningar ─────────────────────────────────────────

    def test_triage_disabled_leaves_alert_untouched(self):
        """4.4: avstängd triage lämnar larmet orört."""
        self._history(3)
        self.Config.set_param('saltstack.alert.triage_enabled', 'False')
        alert = self._next_alert()
        self.assertFalse(alert._triage())
        self.assertFalse(alert.outcome)

    def test_missing_param_means_enabled(self):
        """4.2: saknad parameterrad ⇒ triagen är PÅ (default)."""
        self.Config.set_param('saltstack.alert.triage_enabled', False)
        self.env.cr.execute(
            "delete from ir_config_parameter where key=%s",
            ('saltstack.alert.triage_enabled',))
        self.assertTrue(self.Alert._triage_enabled())

    def test_false_string_is_read_as_off(self):
        """4.2: strängen 'False' ⇒ av (truthiness-fällan)."""
        self.Config.set_param('saltstack.alert.triage_enabled', 'False')
        self.assertFalse(self.Alert._triage_enabled())
        self.Config.set_param('saltstack.alert.triage_enabled', 'True')
        self.assertTrue(self.Alert._triage_enabled())

    def test_min_history_is_configurable(self):
        """4.1: triage_min_history styr hur många bedömda larm som krävs."""
        self.Config.set_param('saltstack.alert.triage_min_history', '1')
        self.assertEqual(self.Alert._triage_min_history(), 1)
        self.Config.set_param('saltstack.alert.triage_min_history', '5')
        self.assertEqual(self.Alert._triage_min_history(), 5)
        # Ogiltigt värde ⇒ fallback
        self.Config.set_param('saltstack.alert.triage_min_history', 'nonsense')
        self.assertEqual(self.Alert._triage_min_history(), 3)


@tagged('post_install', '-at_install')
class TestExperienceVisibility(TransactionCase):
    """Steg 6: erfarenhetsloggning får inte fela tyst."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Alert = cls.env['saltstack.alert']
        cls.Minion = cls.env['salt.minion']

    def _minion(self, name):
        m = self.Minion.search([('name', '=', name)], limit=1)
        return m or self.Minion.create({'name': name})

    def test_save_survives_experience_failure(self):
        """6.3: kastar erfarenhetsloggningen fel sparas utfallet ändå."""
        self.env['ai.skill'].create({
            'name': 'Vis-test skill', 'description': 'x',
            'trigger_keywords': 'visibilitytest',
        })
        alert = self.Alert.create({
            'host': self._minion('vis-host-01').id,
            'source': 'zabbix',
            'category': 'process',
            'severity': 8,
            'trigger_name': 'visibilitytest trigger',
            'description': 'x',
            'raw_log': 'x',
        })
        with patch.object(type(alert), '_record_experience',
                          side_effect=RuntimeError('inlärningen nere')):
            # write() ska svälja felet och ändå spara utfallet.
            alert.write({'outcome': 'false_positive'})
        self.assertEqual(alert.outcome, 'false_positive')

    def test_failed_logging_is_visible_in_counter(self):
        """6.2/6.3: misslyckad loggning syns utanför loggen (räknare)."""
        alert = self.Alert.create({
            'host': self._minion('vis-host-02').id,
            'source': 'zabbix',
            'category': 'process',
            'severity': 8,
            'trigger_name': 'visibilitytest 2',
            'description': 'x',
            'raw_log': 'x',
        })
        self.assertTrue(
            hasattr(alert, '_bump_experience_failure_count'),
            'det ska finnas ett synligt spår av misslyckad erfarenhetsloggning')
        key = 'saltstack.alert.experience_failures'
        self.env['ir.config_parameter'].sudo().set_param(key, '0')
        alert._bump_experience_failure_count()
        value = self.env['ir.config_parameter'].sudo().get_param(key)
        self.assertEqual(int(value or 0), 1)


@tagged('post_install', '-at_install')
class TestExperienceOnCreate(TransactionCase):
    """Steg 2: erfarenhet bokförs även när utfallet sätts vid skapandet."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Alert = cls.env['saltstack.alert']
        cls.Minion = cls.env['salt.minion']
        cls.Skill = cls.env['ai.skill']

    def _minion(self, name):
        m = self.Minion.search([('name', '=', name)], limit=1)
        return m or self.Minion.create({'name': name})

    def test_experience_recorded_when_outcome_set_on_create(self):
        """Ett larm som SKAPAS med ett utfall ska också logga erfarenhet.

        write()-hooken fångar bara utfall som sätts i efterhand — ett larm
        från import/API/AI-writeback som kommer med utfallet redan satt
        skulle annars tyst tappa sin erfarenhet.
        """
        skill = self.Skill.create({
            'name': 'Ertest create-skill',
            'description': 'x',
            'trigger_keywords': 'createtestunik',
        })
        alert = self.Alert.create({
            'host': self._minion('create-host-01').id,
            'source': 'zabbix',
            'category': 'system',
            'severity': 8,
            'trigger_name': 'createtestunik larm',
            'description': 'x',
            'raw_log': 'x',
            'outcome': 'false_positive',
        })
        self.assertIn(skill, alert.matched_skill_ids)
        self.assertIn('createtestunik', skill.failure_cases or '')
        self.assertTrue(alert.experience_recorded)
        self.assertFalse(skill.recipe_text)
