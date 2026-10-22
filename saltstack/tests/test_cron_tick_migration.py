# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""saltstack — schemaläggningen ligger i cron_tick, inte i ir.cron.

Run with: checkmodule -d <db> -m saltstack -t
Covers: att modulen inte skapar någon aktiv `ir.cron`, att de ersatta
posterna finns kvar men är inaktiva och märkta, att varje `cron.task` har
antingen kadens ELLER intervall (aldrig båda), och att den enda post som
medvetet ligger kvar i `ir.cron` är restic-synken.

Why: `cron_tick` äger schemat (samma regel som `saltstack_storage` redan
slår fast). Utan de här testerna kan en framtida datafil återinföra en
`ir.cron` utan att något går sönder — och då tappar jobbet tyst sin
körhistorik, sitt lås och sin överskriden-deadline-markering.
"""

from odoo.tests.common import TransactionCase, tagged

# Jobb som medvetet ligger kvar i `ir.cron`, med skälet dokumenterat i
# `data/sync_snapshot_cron.xml`: ordningsgarantin mot restic-statusrapporten
# 06:15 kan inte uttryckas i registret (design D3).
#
# Dirvish-synken ligger också kvar — men av ett ANNAT skäl: den ska AVVECKLAS
# (Dirvish är ersatt av Borg + Restic), och avvecklingen ägs av ändringen
# `minio-snapshot-tab`, som också äger ordningsfrågan (MinIO-synken måste
# finnas innan Dirvish släcks). Att släcka den här hade tagit bort
# återställningshistoriken innan ersättaren finns.
LEGACY_IR_CRON_XMLIDS = (
    'saltstack.cron_sync_snapshots',
    'saltstack.cron_sync_dirvish_snapshots',
)

# Ersatta poster: kvar med full definition, men inaktiva och märkta.
RETIRED_IR_CRON_XMLIDS = (
    'saltstack.cron_sync_minions_pillars',
    'saltstack.cron_backfill_alert_counts',
    'saltstack.cron_auto_resolve_alerts',
)

# Modulens cron.task-poster (technical_name).
EXPECTED_TASKS = (
    'minion_sync',
    'alert_count_backfill',
    'alert_auto_resolve',
)


@tagged('post_install', '-at_install')
class TestCronTickMigration(TransactionCase):
    """Schemat ägs av cron_tick — ir.cron ska vara tom på aktiva jobb."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.CronTask = cls.env['cron.task']
        cls.IrCron = cls.env['ir.cron']

    # ── 6.1 Ingen aktiv ir.cron ──────────────────────────────────────────

    def test_no_active_ir_cron_created(self):
        """Modulen får inte skapa någon AKTIV ir.cron.

        Samma regel som `saltstack_storage` redan har. Undantaget är
        restic-synken, som ligger kvar med flit — den räknas upp explicit så
        att ett nytt undantag inte kan smyga in.
        """
        active = self.IrCron.search([
            ('active', '=', True),
            ('ir_actions_server_id.model_id.model', 'in', [
                'salt.minion', 'saltstack.alert', 'salt.minion.snapshot',
            ]),
        ])
        allowed = self.env['ir.cron']
        for xmlid in LEGACY_IR_CRON_XMLIDS:
            allowed |= self.env.ref(xmlid, raise_if_not_found=False)
        unexpected = active - allowed
        self.assertFalse(
            unexpected,
            'Aktiva ir.cron-poster som inte är dokumenterade undantag: %s'
            % (unexpected.mapped('name'),))

    def test_restic_sync_stays_in_ir_cron(self):
        """Den enda kvarvarande ir.cron är restic-synken, med 06:30."""
        cron = self.env.ref('saltstack.cron_sync_snapshots')
        self.assertTrue(cron.active)
        self.assertEqual(cron.interval_number, 24)
        self.assertEqual(cron.interval_type, 'hours')
        # nextcall 06:30 är ordningsgarantin mot rapporten 06:15.
        self.assertEqual(
            cron.nextcall.strftime('%H:%M'), '06:30',
            'nextcall måste vara 06:30 — det är garantin mot att läsa en '
            'gammal restic-status.json')

    def test_retired_ir_crons_are_inactive_and_marked(self):
        """Ersatta poster finns kvar, är inaktiva och bär [ERSATT]."""
        for xmlid in RETIRED_IR_CRON_XMLIDS:
            cron = self.env.ref(xmlid)
            self.assertFalse(
                cron.active, '%s ska vara inaktiv (ersatt av cron.task)'
                % xmlid)
            self.assertTrue(
                cron.name.startswith('[ERSATT]'),
                '%s ska vara märkt [ERSATT], heter %r' % (xmlid, cron.name))
            # Full definition kvar — annars kraschar en uppgradering som
            # försöker läsa posten, och spårbarheten försvinner.
            self.assertTrue(cron.code, '%s tappade sin code' % xmlid)

    # ── 6.2 Kadens XOR intervall ─────────────────────────────────────────

    def test_every_task_has_cadence_or_interval(self):
        """Varje cron.task har kadens ELLER intervall — aldrig båda."""
        tasks = self.CronTask.search([
            ('technical_name', 'in', list(EXPECTED_TASKS)),
        ])
        self.assertEqual(
            len(tasks), len(EXPECTED_TASKS),
            'Saknade cron.task-poster: %s' % (
                set(EXPECTED_TASKS) - set(tasks.mapped('technical_name')),))
        for task in tasks:
            has_cadence = bool(task.cadence)
            has_interval = bool(task.interval_type)
            self.assertNotEqual(
                has_cadence, has_interval,
                '%s har cadence=%r och interval_type=%r — exakt en av dem '
                'ska vara satt' % (
                    task.technical_name, task.cadence, task.interval_type))
            self.assertTrue(
                task.model_name and task.method_name,
                '%s saknar model_name/method_name' % task.technical_name)

    def test_minion_sync_uses_interval_not_cadence(self):
        """minion_sync använder intervall 24 h — inte kadens (design D1).

        Kadens `daily` hade lagt jobbet ~01:22, inne i Borg-fönstret
        01:00–03:00.
        """
        task = self.CronTask.search(
            [('technical_name', '=', 'minion_sync')], limit=1)
        self.assertTrue(task)
        self.assertFalse(task.cadence, 'minion_sync ska INTE ha en kadens')
        self.assertEqual(task.interval_number, 24)
        self.assertEqual(task.interval_type, 'hours')

    def test_minute_tasks_are_ordered(self):
        """Backfill kör före auto-resolve i samma tick (sequence 10 < 20)."""
        backfill = self.CronTask.search(
            [('technical_name', '=', 'alert_count_backfill')], limit=1)
        resolve = self.CronTask.search(
            [('technical_name', '=', 'alert_auto_resolve')], limit=1)
        self.assertEqual(backfill.cadence, 'min')
        self.assertEqual(resolve.cadence, 'min')
        self.assertLess(
            backfill.sequence, resolve.sequence,
            'backfill ska ha lägre sequence än auto-resolve')

    # ── Inställningssidan ────────────────────────────────────────────────

    def test_settings_has_no_sync_interval_field(self):
        """Intervallet är inte längre ett inställningsfält."""
        Settings = self.env['res.config.settings']
        self.assertNotIn('sync_interval_number', Settings._fields)

    def test_cron_line_model_is_gone(self):
        """Den egna schematabellen är borttagen."""
        self.assertNotIn('saltstack.cron.line', self.env)
        self.assertNotIn('saltstack_cron_line_ids',
                         self.env['res.config.settings']._fields)
