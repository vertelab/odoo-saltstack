# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

import json
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class SaltRunlog(models.Model):
    """Driftslogg — published run reports (backup, highstate, sync...).

    A machine (e.g. the "restic" or "SaltStack" minion) publishes a
    structured run report to /saltstack/log after each scheduled run.
    Unlike saltstack.alert (Driftslarm — problems needing attention) this
    model stores routine status logs so operators can see "what ran and
    what happened" in one place.
    """

    _name = 'saltstack.runlog'
    _description = 'Driftslogg'
    _order = 'timestamp desc, id desc'

    # ── Identity ────────────────────────────────────────────────────────
    name = fields.Char(
        string='Rubrik',
        compute='_compute_name',
        store=True,
    )
    source = fields.Selection([
        ('restic', 'Restic'),
        ('odoosa', 'Odoo SA'),
        ('dirvish', 'Dirvish'),
            ('github-backup', 'GitHub backup'),
        ('salt', 'Salt'),
        ('zabbix', 'Zabbix'),
        ('wazuh', 'Wazuh'),
        ('manual', 'Manual action'),
        ('other', 'Other'),
    ], string='Source', default='other',
        help='Source system that published the run. "Manuell åtgärd" is '
             'used for notes written by hand about a change made on a '
             'minion.')
    run_type = fields.Selection([
        ('backup', 'Backup'),
        ('highstate', 'Highstate'),
        ('sync', 'Sync'),
        ('translation', 'Odoo SA'),
        ('security', 'Security'),
        ('test', 'Test'),
        ('change', 'Change'),
        ('other', 'Other'),
    ], string='Run type', default='other')
    host = fields.Char(string='Host')
    minion_id = fields.Many2one(
        'salt.minion',
        string='Minion',
        ondelete='set null',
        index=True,
        help='Minion this run/change belongs to. Set automatically from '
             '"host" when a report is published; can also be set manually '
             'on hand-written notes.',
    )

    # ── Result ──────────────────────────────────────────────────────────
    status = fields.Selection([
        ('ok', 'OK'),
        ('warning', 'Varning'),
        ('error', 'Error'),
    ], string='Status', default='ok')
    summary = fields.Char(string='Sammanfattning')
    raw_log = fields.Text(string='Logg')
    json_payload = fields.Text(string='JSON')
    timestamp = fields.Datetime(string='Timestamp')

    # ── Computed ────────────────────────────────────────────────────────

    @api.depends('host', 'run_type', 'status', 'summary', 'timestamp')
    def _compute_name(self):
        for rec in self:
            run_type = dict(rec._fields['run_type'].selection).get(
                rec.run_type, rec.run_type or '')
            status = dict(rec._fields['status'].selection).get(
                rec.status, rec.status or '?')
            ts = rec.timestamp or rec.create_date
            when = fields.Datetime.to_string(ts)[:16] if ts else ''
            # Visa lokal tid (Europe/Stockholm) i stället för UTC —
            # posterna lagras som naive UTC, så konvertera explicit.
            # (Driftfix 2026-09-10: rubriken visade 04:16 (UTC) när
            # backupen kördes 06:16 lokal tid.)
            try:
                from datetime import datetime
                import pytz
                local = pytz.timezone('Europe/Stockholm')
                when = datetime.strptime(when, '%Y-%m-%d %H:%M') \
                    .replace(tzinfo=pytz.utc).astimezone(local).strftime('%Y-%m-%d %H:%M')
            except Exception:
                pass
            # Lägg till omfattning (kundantal) från summary om möjligt —
            # t.ex. "24 kunder" ur "24 kunder — 24 ✅ / 0 ⚠️ ...".
            scope = ''
            if rec.summary:
                import re
                m = re.match(r'^(\d+)\s+kunder', rec.summary)
                if m:
                    scope = f" {m.group(1)} kunder"
            rec.name = '%s — %s (%s)%s %s' % (
                rec.host or '?', run_type or 'Run', status, scope, when)

    # ── Webhook-processing ──────────────────────────────────────────────

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

    @api.model
    def _find_minion(self, host):
        """Best-effort lookup of the salt.minion a run belongs to.

        Returns an empty recordset when host is empty or does not match
        exactly one minion. Never raises — a failed lookup must not break
        the webhook.
        """
        if not host:
            return self.env['salt.minion']
        try:
            minion = self.env['salt.minion'].sudo().search(
                [('name', '=', host)], limit=1)
            return minion
        except Exception as e:
            _logger.warning('Driftslogg: minion-uppslag misslyckades för '
                            'host=%s: %s', host, e)
            return self.env['salt.minion']

    @api.model
    def _backfill_minion_id(self):
        """Fill minion_id on existing records from their host (idempotent).

        Safe to re-run: only records with an empty minion_id are touched.
        """
        records = self.sudo().search([('minion_id', '=', False),
                                     ('host', '!=', False)])
        filled = 0
        for rec in records:
            minion = self._find_minion(rec.host)
            if minion:
                rec.minion_id = minion.id
                filled += 1
        _logger.info('Driftslogg: backfill satte minion_id på %s/%s poster',
                     filled, len(records))
        return filled

    @api.model
    def process_webhook(self, payload):
        """Process an incoming run-log payload. Returns result dict."""
        try:
            source = str(payload.get('source', '')).strip()
            run_type = str(payload.get('run_type', '')).strip()
            host = str(payload.get('host', '')).strip()
            status = str(payload.get('status', 'ok')).strip()

            if source not in dict(self._fields['source'].selection):
                source = 'other'
            if run_type not in dict(self._fields['run_type'].selection):
                run_type = 'other'
            if status not in dict(self._fields['status'].selection):
                status = 'ok'

            raw_log = str(payload.get('raw_log', '')) or ''
            # Machine-readable metadata (without the raw log — it is already
            # stored in raw_log and would otherwise be duplicated)
            meta = {k: payload.get(k) for k in (
                'source', 'run_type', 'host', 'status', 'summary',
                'timestamp')}

            record = self.create({
                'source': source,
                'run_type': run_type,
                'host': host,
                'minion_id': self._find_minion(host).id or False,
                'status': status,
                'summary': str(payload.get('summary', ''))[:500],
                'raw_log': raw_log[:200000],
                'json_payload': json.dumps(
                    meta, ensure_ascii=False)[:200000],
                'timestamp': self._parse_timestamp(payload.get('timestamp')),
            })

            _logger.info('Driftslogg: %s/%s från %s status=%s',
                         source, run_type, host or '?', status)
            return {'status': 'ok', 'runlog_id': record.id}
        except Exception as e:
            _logger.exception('Driftslogg-webhook misslyckades: %s', e)
            return {'status': 'error', 'error': str(e)}
