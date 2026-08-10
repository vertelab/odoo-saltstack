# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

import secrets
import logging

_logger = logging.getLogger(__name__)


def _ensure_webhook_config(env):
    """Ensure the Driftlarm webhook URL + API key exist after install.

    - Generates a secure random API key (Bearer token) if none is set.
    - Fills the webhook URL from web.base.url if none is set.
    Runs only at first install (not on upgrade) — respects user changes.
    """
    params = env['ir.config_parameter'].sudo()

    # 1. API-nyckel (Bearer token) — generate if missing
    if not params.get_param('saltstack.alert.webhook_token'):
        token = secrets.token_urlsafe(32)
        params.set_param('saltstack.alert.webhook_token', token)
        _logger.info('saltstack: genererade ny Driftlarm API-nyckel (%d tecken)',
                     len(token))

    # 2. Webhook URL — fill from web.base.url if missing
    if not params.get_param('saltstack.alert.webhook_url'):
        base = params.get_param('web.base.url', 'http://localhost:8069')
        url = '%s/saltstack/alert' % base.rstrip('/')
        params.set_param('saltstack.alert.webhook_url', url)
        _logger.info('saltstack: webhook-URL ifylld: %s', url)
