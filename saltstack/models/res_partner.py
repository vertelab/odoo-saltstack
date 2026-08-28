# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""Extend res.partner with a smart button listing customer minions."""

import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class ResPartnerSaltMinion(models.Model):
    _inherit = 'res.partner'

    minion_count = fields.Integer(
        string='Minioner',
        compute='_compute_minion_count',
    )

    @api.depends('name')
    def _compute_minion_count(self):
        for rec in self:
            rec.minion_count = rec._minion_count()

    def action_view_partner_minions(self):
        """Smart button: list salt.minion records for this partner."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Minioner — %s' % self.name,
            'res_model': 'salt.minion',
            'view_mode': 'list,kanban,form',
            'domain': [('partner_id', '=', self.id)],
            'context': {'search_default_partner_id': self.id},
        }
