# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

import hmac
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SaltAlertController(http.Controller):

    @http.route('/saltstack/alert', type='json', auth='none',
                methods=['POST'], csrf=False)
    def salt_alert_webhook(self):
        """Receive drift alerts (Wazuh, Zabbix) via webhook.

        Shared endpoint for all sources. The payload must include
        'source' (e.g. 'wazuh' or 'zabbix') — the value is validated
        against the selection on saltstack.alert.

        Always returns 200 (even on errors) so senders do not retry
        indefinitely. Auth via Bearer token in Authorization header.
        """
        params = request.env['ir.config_parameter'].sudo()

        # 1. Enabled?
        if params.get_param('saltstack.alert.webhook_enabled',
                            'True') not in ('True', 'true', '1'):
            return {'status': 'disabled'}

        # 2. Bearer token auth
        expected = params.get_param('saltstack.alert.webhook_token', '')
        auth_header = request.httprequest.headers.get('Authorization', '')
        provided = auth_header[7:] if auth_header.startswith('Bearer ') else ''
        if not expected or not hmac.compare_digest(provided, expected):
            _logger.warning(
                'Driftlarm-webhook: ogiltig/ saknad Bearer token (len=%s)',
                len(provided))
            return {'status': 'error', 'error': 'Unauthorized'}

        # 3. Process payload
        try:
            payload = request.get_json_data() or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
        except Exception as e:
            _logger.warning('Driftlarm-webhook: ogiltig JSON: %s', e)
            return {'status': 'error', 'error': 'Invalid JSON'}

        _logger.info('Driftlarm webhook: alert for %s (source %s)',
                     payload.get('host'), payload.get('source'))

        result = request.env['saltstack.alert'].sudo().process_webhook(payload)
        return result
