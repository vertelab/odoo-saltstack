# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    shuffle_api_url = fields.Char(
        string='Shuffle API URL',
        config_parameter='saltstack_shuffle.api_url',
        default='https://siem.vertel.se',
        help='Shuffle API base URL',
    )
    shuffle_api_token = fields.Char(
        string='Shuffle API Token',
        config_parameter='saltstack_shuffle.api_token',
        help='Shuffle API-token',
    )
    shuffle_odoo_webhook_url = fields.Char(
        string='Odoo Webhook URL',
        config_parameter='saltstack_shuffle.odoo_webhook_url',
        help='Webhook-URL som Shuffle anropar för att skicka driftlarm till Odoo '
             '(t.ex. http://luke18:8069/saltstack/alert)',
    )
    shuffle_odoo_api_key = fields.Char(
        string='Odoo API-nyckel',
        config_parameter='saltstack_shuffle.odoo_api_key',
        help='API-nyckel (Bearer token) som Shuffle skickar tillsammans med '
             'webhook-anropen. Samma som Driftlarm-webhookens token.',
    )
    shuffle_sync_enabled = fields.Boolean(
        string='Auto-synca workflow-status',
        config_parameter='saltstack_shuffle.sync_enabled',
        default=True,
        help='Synca workflow-status automatiskt från Shuffle',
    )
