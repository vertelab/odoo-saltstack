# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

from odoo import fields, models


class ShuffleApp(models.Model):
    _name = 'shuffle.app'
    _description = 'Shuffle App'
    _order = 'name'

    name = fields.Char(required=True)
    app_id = fields.Char(string='Shuffle App ID')
    api_key = fields.Char(
        string='API-nyckel',
        help='Krypteras vid lagring. För appar som abuseipdb, virustotal etc.',
    )
    config_json = fields.Text(string='Konfig (JSON)')
    active = fields.Boolean(default=True)
