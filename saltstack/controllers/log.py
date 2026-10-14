# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

import hmac
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SaltLogController(http.Controller):

    @http.route('/saltstack/log', type='json', auth='none',
                methods=['POST'], csrf=False)
    def salt_log_webhook(self):
        """Receive published run logs (backup, highstate, sync...) via webhook.

        Used by the SaltStack/restic minions to publish their scheduled run
        reports to the Driftslogg. Shares the API key + enabled flag with the
        Driftlarm webhook (saltstack.alert.webhook_token / webhook_enabled).

        Always returns 200 (even on errors) so senders do not retry
        indefinitely. Auth via Bearer token in Authorization header.
        """
        params = request.env['ir.config_parameter'].sudo()

        # 1. Enabled?
        if params.get_param('saltstack.alert.webhook_enabled',
                            'True') not in ('True', 'true', '1'):
            return {'status': 'disabled'}

        # 2. Bearer token auth (same key as /saltstack/alert)
        expected = params.get_param('saltstack.alert.webhook_token', '')
        auth_header = request.httprequest.headers.get('Authorization', '')
        provided = auth_header[7:] if auth_header.startswith('Bearer ') else ''
        if not expected or not hmac.compare_digest(provided, expected):
            _logger.warning(
                'Driftslogg-webhook: ogiltig/ saknad Bearer token (len=%s)',
                len(provided))
            return {'status': 'error', 'error': 'Unauthorized'}

        # 3. Process payload
        try:
            payload = request.get_json_data() or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
        except Exception as e:
            _logger.warning('Driftslogg-webhook: ogiltig JSON: %s', e)
            return {'status': 'error', 'error': 'Invalid JSON'}

        _logger.info('Driftslogg webhook: run %s/%s från %s',
                     payload.get('source'), payload.get('run_type'),
                     payload.get('host'))

        result = request.env['saltstack.runlog'].sudo().process_webhook(payload)
        return result

    @http.route('/saltstack/log/summary', type='json', auth='none',
                methods=['POST'], csrf=False)
    def salt_log_summary(self):
        """Return the latest run log per source, for the daily overview.

        T/12111 (2026-10-14): the sources run on five different machines
        (restic minion, LXD hosts, tull1, the master, odoosa-translate), so
        no single host can read the others' status files. This endpoint
        lets the daily overview read what already has been published.

        Same auth pattern as the webhook: auth='none' + Bearer token +
        sudo(). No new user, no new concept.

        Returns {'status': 'ok', 'sources': [ {source, label, status,
        summary, timestamp, host}, ... ]} or {'status': 'error', ...}.
        """
        params = request.env['ir.config_parameter'].sudo()

        # 1. Enabled?
        if params.get_param('saltstack.alert.webhook_enabled',
                            'True') not in ('True', 'true', '1'):
            return {'status': 'disabled'}

        # 2. Bearer token auth (same key as /saltstack/log)
        expected = params.get_param('saltstack.alert.webhook_token', '')
        auth_header = request.httprequest.headers.get('Authorization', '')
        provided = auth_header[7:] if auth_header.startswith('Bearer ') else ''
        if not expected or not hmac.compare_digest(provided, expected):
            _logger.warning(
                'Driftslogg-summary: ogiltig/saknad Bearer token (len=%s)',
                len(provided))
            return {'status': 'error', 'error': 'Unauthorized'}

        # 3. Latest record per source. Read the newest N and pick the
        #    first hit per source — the log is small and this avoids a
        #    GROUP BY that would hide the timestamp.
        RunLog = request.env['saltstack.runlog'].sudo()
        rows = RunLog.search_read(
            [], ['source', 'host', 'status', 'summary', 'timestamp'],
            limit=200, order='id desc')

        seen = {}
        for r in rows:
            src = r.get('source') or 'other'
            if src not in seen and src != 'daily':
                seen[src] = r

        # Display labels + order. Order is deliberate: the sources that
        # fail most often come first, so the eye lands there.
        labels = dict(RunLog._fields['source'].selection)
        order = ['restic', 'lxd-backup', 'kvm', 'dirvish', 'github-backup',
                 'wazuh', 'zabbix', 'odoosa', 'salt', 'manual', 'other']

        sources = []
        for src in order:
            r = seen.get(src)
            if not r:
                sources.append({
                    'source': src, 'label': labels.get(src, src),
                    'status': None, 'summary': '', 'timestamp': None,
                    'host': None})
                continue
            sources.append({
                'source': src,
                'label': labels.get(src, src),
                'status': r.get('status'),
                'summary': r.get('summary') or '',
                'timestamp': r.get('timestamp'),
                'host': r.get('host'),
            })

        return {'status': 'ok', 'sources': sources}
