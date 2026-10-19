# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

import hmac
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SaltMinionController(http.Controller):

    @http.route('/saltstack/minion', type='json', auth='none',
                methods=['POST'], csrf=False)
    def salt_minion_register(self):
        """Register one newly created minion immediately (onboarding hook).

        The ``salt.minion`` registry used to be filled ONLY by the 24-hour
        sync cron, so a container created by ``container_create.sh`` did not
        appear in the kanban until the next scheduled sync (up to a day).
        ``container_create_core.sh`` now POSTs here right after the Salt key
        is accepted.

        The record is created from the minion's own grains (fetched through
        the Salt API), so no grains need to be sent in the payload — the
        caller only has to name the minion.

        Shares the API key + enabled flag with the Driftlarm/Driftslogg
        webhooks (saltstack.alert.webhook_token / webhook_enabled).

        Always returns 200 (even on errors) so the caller — a provisioning
        script — never aborts a container creation because of this.
        Auth via Bearer token in the Authorization header.
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
                'Minion-registrering: ogiltig/ saknad Bearer token (len=%s)',
                len(provided))
            return {'status': 'error', 'error': 'Unauthorized'}

        # 3. Payload
        try:
            payload = request.get_json_data() or {}
        except Exception as e:
            _logger.warning('Minion-registrering: ogiltig JSON: %s', e)
            return {'status': 'error', 'error': 'Invalid JSON'}

        name = (payload.get('minion') or payload.get('name') or '').strip()
        if not name:
            return {'status': 'error', 'error': 'Missing minion id'}

        # 4. Register
        try:
            admin = request.env['res.users'].browse(1)
            result = (request.env['salt.minion'].sudo().with_user(admin)
                      .register_minion(name))
        except Exception as e:
            _logger.exception('Minion-registrering misslyckades för %s', name)
            return {'status': 'error', 'error': str(e)}

        _logger.info('Minion-registrering: %s -> %s', name, result)
        return result
