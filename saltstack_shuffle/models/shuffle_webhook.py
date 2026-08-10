# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

from odoo import fields, models


class ShuffleWebhook(models.Model):
    _name = 'shuffle.webhook'
    _description = 'Shuffle Webhook'
    _order = 'name'

    name = fields.Char()
    url = fields.Char()
    source = fields.Selection([
        ('wazuh', 'Wazuh'),
        ('zabbix', 'Zabbix'),
    ], string='Source')
    destination = fields.Selection([
        ('shuffle', 'Shuffle'),
        ('odoo', 'Odoo'),
    ], string='Destination')
    active = fields.Boolean(default=True)
