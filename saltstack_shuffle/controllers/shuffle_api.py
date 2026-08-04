# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

import hmac
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class ShuffleAPI(http.Controller):

    def _auth_ok(self):
        """Check Bearer token against saltstack_shuffle.api_token."""
        params = request.env['ir.config_parameter'].sudo()
        expected = params.get_param('saltstack_shuffle.api_token', '')
        auth_header = request.httprequest.headers.get('Authorization', '')
        provided = auth_header[7:] if auth_header.startswith('Bearer ') else ''
        return bool(expected) and hmac.compare_digest(provided, expected)

    @http.route('/saltstack_shuffle/workflow/status', type='json',
                auth='none', methods=['GET', 'POST'], csrf=False)
    def workflow_status(self):
        """Return status for all Shuffle workflows. Used by Salt state."""
        if not self._auth_ok():
            return {'status': 'error', 'error': 'Unauthorized'}
        workflows = request.env['shuffle.workflow'].sudo().search([])
        return {
            'workflows': [{
                'name': w.name,
                'status': w.status,
                'last_execution': w.last_execution.isoformat()
                    if w.last_execution else None,
                'execution_count': w.execution_count,
            } for w in workflows],
        }

    @http.route('/saltstack_shuffle/webhook/register', type='json',
                auth='none', methods=['POST'], csrf=False)
    def webhook_register(self):
        """Register a webhook URL. Called by Salt state after Shuffle setup."""
        if not self._auth_ok():
            return {'status': 'error', 'error': 'Unauthorized'}
        payload = request.get_json_data() or {}
        name = payload.get('name', '')
        url = payload.get('url', '')
        source = payload.get('source', '')
        destination = payload.get('destination', '')
        if not url:
            return {'status': 'error', 'error': 'Missing url'}

        webhook = request.env['shuffle.webhook'].sudo().search([
            ('url', '=', url),
        ], limit=1)
        if not webhook:
            webhook = request.env['shuffle.webhook'].sudo().create({
                'name': name or url[:50],
                'url': url,
                'source': source if source in ('wazuh', 'zabbix') else False,
                'destination': destination
                    if destination in ('shuffle', 'odoo') else False,
            })
        else:
            webhook.write({
                'name': name or webhook.name,
                'source': source if source in ('wazuh', 'zabbix') else webhook.source,
                'destination': destination
                    if destination in ('shuffle', 'odoo') else webhook.destination,
            })
        return {'status': 'ok', 'id': webhook.id}
