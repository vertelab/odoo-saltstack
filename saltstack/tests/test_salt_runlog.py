# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""saltstack — tests for the Driftslogg (saltstack.runlog) webhook model.

Run with: checkmodule -d <db> -m saltstack -t
Covers: process_webhook payload handling, status/run_type fallback, and
timestamp parsing.
"""

import os

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestRunlogWebhook(TransactionCase):
    """saltstack.runlog.process_webhook()"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Runlog = cls.env['saltstack.runlog']

    def _publish(self, **kw):
        payload = {
            'source': 'restic',
            'run_type': 'backup',
            'host': 'restic',
            'status': 'ok',
            'summary': '16 kunder — 4 OK / 12 WARNING / 0 ERROR',
            'raw_log': 'Kund              Källa     Status\n'
                       'immich            10.4 G    OK',
            'timestamp': '2026-08-12T06:15:00Z',
        }
        payload.update(kw)
        return self.Runlog.process_webhook(payload)

    def test_create_ok(self):
        """A normal payload creates a runlog with all fields."""
        res = self._publish()
        self.assertEqual(res['status'], 'ok')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertEqual(rec.source, 'restic')
        self.assertEqual(rec.run_type, 'backup')
        self.assertEqual(rec.host, 'restic')
        self.assertEqual(rec.status, 'ok')
        self.assertIn('16 kunder', rec.summary)
        self.assertIn('immich', rec.raw_log)
        self.assertIn('source', rec.json_payload)

    def test_invalid_status_falls_back(self):
        """Unknown status values fall back to 'ok'."""
        res = self._publish(status='bogus')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertEqual(rec.status, 'ok')

    def test_invalid_run_type_falls_back(self):
        """Unknown run_type values fall back to 'other'."""
        res = self._publish(run_type='nightly-magic')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertEqual(rec.run_type, 'other')

    def test_invalid_source_falls_back(self):
        """Unknown source values fall back to 'other'."""
        res = self._publish(source='ufo')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertEqual(rec.source, 'other')

    def test_backup_sources_are_known(self):
        """Every backup publisher has its own source — not 'other'.

        KVM-backupen (kvm-publish.sh) och LXD-backupen publicerar till samma
        webhook som dirvish/restic. Utan en egen selection-post tystas de till
        'other' och går inte att filtrera på i Driftsloggen — det var precis
        vad som hände KVM-posten (2026-10-06).
        """
        for src in ('lxd-backup', 'kvm', 'dirvish', 'restic'):
            with self.subTest(source=src):
                res = self._publish(source=src)
                rec = self.Runlog.browse(res['runlog_id'])
                self.assertEqual(rec.source, src)

    def test_publisher_sources_are_known(self):
        """Every source/run_type a /srv/salt publisher sends is in the selection.

        The hand-written list in test_backup_sources_are_known only covers the
        publishers someone remembered. image_build_publish.sh sent
        source=image-build and run_type=image-build for weeks and both were
        silently rewritten to 'other' (PITFALL 36) — the Driftslogg showed the
        post but it could not be filtered, and nothing warned.

        This test reads the publishers from the Salt tree instead of listing
        them, so a NEW publisher fails the suite until its source is added.
        Skipped when /srv/salt is not mounted (CI without the Salt tree).
        """
        import re
        from pathlib import Path

        salt_root = Path(os.environ.get('SALT_ROOT', '/srv/salt'))
        if not salt_root.is_dir():
            self.skipTest('no Salt tree at %s' % salt_root)

        pat = re.compile(
            r"""['"](?:source|run_type)['"]\s*:\s*['"]([a-z0-9-]+)['"]""")

        found = {}
        for path in salt_root.rglob('*'):
            if path.suffix not in ('.sh', '.py', '.sls') or not path.is_file():
                continue
            try:
                text = path.read_text(errors='ignore')
            except OSError:
                continue
            if '/saltstack/log' not in text:
                continue
            for value in pat.findall(text):
                found.setdefault(value, set()).add(
                    str(path.relative_to(salt_root)))

        self.assertTrue(found, 'no publishers found under %s' % salt_root)

        src_sel = {k for k, _ in self.Runlog._fields['source'].selection}
        rt_sel = {k for k, _ in self.Runlog._fields['run_type'].selection}

        for value, files in sorted(found.items()):
            if value in src_sel or value in rt_sel:
                continue
            self.fail(
                'source/run_type %r is sent by %s but is missing from the '
                "saltstack.runlog selection — process_webhook() rewrites it "
                "to 'other' without warning (PITFALL 36)."
                % (value, ', '.join(sorted(files))))

    def test_timestamp_parsed(self):
        """ISO timestamp is parsed into a naive Odoo datetime."""
        res = self._publish(timestamp='2026-08-12T06:15:00Z')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertEqual(rec.timestamp.strftime('%Y-%m-%d %H:%M'),
                         '2026-08-12 06:15')

    def test_missing_host_not_fatal(self):
        """A missing host is accepted (log entry without host is fine)."""
        res = self._publish(host='')
        self.assertEqual(res['status'], 'ok')

    def test_order_newest_first(self):
        """_order = timestamp desc — newest log comes first."""
        older = self._publish(timestamp='2026-08-10T06:15:00Z', summary='äldre')
        newer = self._publish(timestamp='2026-08-12T06:15:00Z', summary='nyare')
        # Scope the search to THIS test's records: the database also holds
        # real runlog rows (Dirvish/Restic) whose timestamps would otherwise
        # win the ordering assertion.
        ids = [older['runlog_id'], newer['runlog_id']]
        first = self.Runlog.search([('id', 'in', ids)], limit=1)
        self.assertEqual(first.id, newer['runlog_id'])
        self.assertEqual(first.summary, 'nyare')
        last = self.Runlog.search([('id', 'in', ids)], order='timestamp asc', limit=1)
        self.assertEqual(last.id, older['runlog_id'])

    # ── Minion-koppling ────────────────────────────────────────────────

    def test_minion_linked_from_host(self):
        """A published run is linked to the minion matching its host."""
        minion = self.env['salt.minion'].create({'name': 'asterisk'})
        res = self._publish(host='asterisk')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertEqual(rec.minion_id, minion)

    def test_minion_unknown_host_leaves_link_empty(self):
        """An unknown host does not break the webhook — minion_id stays empty."""
        res = self._publish(host='no-such-minion-xyz')
        self.assertEqual(res['status'], 'ok')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertFalse(rec.minion_id)
        self.assertEqual(rec.host, 'no-such-minion-xyz')

    def test_backfill_minion_id(self):
        """Backfill sets minion_id on existing records from their host."""
        minion = self.env['salt.minion'].create({'name': 'asterisk-backfill'})
        rec = self.Runlog.create({
            'host': 'asterisk-backfill',
            'source': 'other',
            'run_type': 'other',
            'status': 'ok',
        })
        self.assertFalse(rec.minion_id)
        self.Runlog._backfill_minion_id()
        self.assertEqual(rec.minion_id, minion)

    def test_backfill_is_idempotent(self):
        """A second backfill run changes nothing."""
        minion = self.env['salt.minion'].create({'name': 'asterisk-idem'})
        rec = self.Runlog.create({
            'host': 'asterisk-idem',
            'source': 'other',
            'run_type': 'other',
            'status': 'ok',
        })
        self.Runlog._backfill_minion_id()
        self.assertEqual(rec.minion_id, minion)
        # Second run: no records left with an empty minion_id for this host
        self.Runlog._backfill_minion_id()
        self.assertEqual(rec.minion_id, minion)

    # ── Manuella poster ────────────────────────────────────────────────

    def test_manual_source_and_change_type_accepted(self):
        """The manual/change selection values are valid."""
        res = self._publish(source='manual', run_type='change',
                            summary='pbx.logrotate — maxsize 500M')
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertEqual(rec.source, 'manual')
        self.assertEqual(rec.run_type, 'change')

    def test_minion_runlog_count_and_actions(self):
        """A minion counts its runlogs and can open/add them."""
        minion = self.env['salt.minion'].create({'name': 'asterisk-ui'})
        self.assertEqual(minion.runlog_count, 0)
        self.Runlog.create({
            'host': 'asterisk-ui',
            'minion_id': minion.id,
            'source': 'manual',
            'run_type': 'change',
            'status': 'ok',
            'summary': 'test',
        })
        self.assertEqual(minion.runlog_count, 1)
        action = minion.action_view_runlogs()
        self.assertEqual(action['res_model'], 'saltstack.runlog')
        self.assertIn(('minion_id', '=', minion.id), action['domain'])
        add = minion.action_add_runlog()
        self.assertEqual(add['context']['default_source'], 'manual')
        self.assertEqual(add['context']['default_run_type'], 'change')
        self.assertEqual(add['context']['default_minion_id'], minion.id)

    # ── Rubriken (_compute_name) ────────────────────────────────────────

    def test_name_strips_source_prefix_from_summary(self):
        """Källnamnet ska inte stå två gånger i rubriken.

        Driftfix 2026-11-13 (T/12598): Wazuhs summary är
        "Wazuh 2026-11-13: 668034 larm ..." — källnamn följt av DATUM, inte
        kolon. Det gamla regexet krävde kolon direkt efter källnamnet, så
        prefixet strippades aldrig och rubriken blev
        "Wazuh — Wazuh 2026-11-13: ... (Error)".
        """
        res = self._publish(
            source='wazuh',
            run_type='security',
            host='*',
            status='error',
            summary='Wazuh 2026-11-13: 668034 larm (3066 high, 6129 critical) '
                    '| 58 hosts | 20 FIM-ändringar i /etc',
        )
        rec = self.Runlog.browse(res['runlog_id'])
        self.assertEqual(rec.name.count('Wazuh'), 1, rec.name)
        self.assertIn('668034 larm', rec.name)
        self.assertNotIn('Wazuh 2026-11-13:', rec.name)

    def test_name_never_truncates_midword(self):
        """Rubriken ska inte sluta mitt i en siffra eller på en avdelare.

        Det gamla ``s[:60]`` klippte "... | 58 hosts" till "... | 5", vilket
        såg ut som en statuskod.
        """
        res = self._publish(
            source='wazuh',
            run_type='security',
            host='*',
            status='error',
            summary='Wazuh 2026-11-13: 674951 larm (2167 high, 6022 critical) '
                    '| 58 hosts | 20 FIM-ändringar i /etc | 559147 bruslarm exkluderade',
        )
        rec = self.Runlog.browse(res['runlog_id'])
        detail = rec.name.split('—', 1)[1].rsplit('(', 1)[0].strip()
        # Ingen hängande avdelare eller ensam siffra i slutet.
        self.assertNotRegex(detail, r'[\s|,;:/–-]$')
        self.assertFalse(detail.endswith('| 5'), detail)
        # Ska sluta på en ellips om den klipptes.
        if str(rec.summary) and len(rec.summary) > 60:
            self.assertTrue(detail.endswith('…') or len(detail) < 62, detail)
