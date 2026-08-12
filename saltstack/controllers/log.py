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
