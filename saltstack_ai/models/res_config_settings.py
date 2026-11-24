# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""
Triage-inställningar (steg 3).

Den regelbaserade triagen stänger kända bruslarm INNAN de når LLM:en. Den
ligger i saltstack_ai (inte i basmodulen) eftersom den är en AI-angelägenhet:
basmodulen ska vara infrastruktur-neutral och kunna släppas separat.
"""

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    alert_coworker_id = fields.Many2one(
        'ai.coworker',
        string='AI Coworker',
        config_parameter='saltstack.alert.coworker_id',
        help='AI coworker that performs diagnosis on drift alerts. '
             'Default is the one bundled with the module (Infrastructure Operator).',
    )

    def get_values(self):
        res = super().get_values()
        coworker_id = self.env['ir.config_parameter'].get_param(
            'saltstack.alert.coworker_id', False)
        if coworker_id and 'ai.coworker' in self.env:
            res['alert_coworker_id'] = int(coworker_id)
        return res

    def set_values(self):
        super().set_values()
        if self.alert_coworker_id:
            self.env['ir.config_parameter'].set_param(
                'saltstack.alert.coworker_id', self.alert_coworker_id.id)

    alert_triage_enabled = fields.Boolean(
        'Regelbaserad triage före AI',
        config_parameter='saltstack.alert.triage_enabled',
        default=True,
        help='Stäng kända bruslarm utan LLM-anrop: samma värd + samma trigger, '
             'minst N tidigare bedömda larm, samtliga falska positiva.')
    alert_triage_min_history = fields.Integer(
        'Triage: minsta historik',
        config_parameter='saltstack.alert.triage_min_history',
        default=3,
        help='Antal tidigare BEDÖMDA larm (satt utfall, av människa eller AI) '
             'med samma värd + trigger som krävs innan triagen stänger ett '
             'larm som falskt positivt. Finns ett enda "åtgärdat" bland dem '
             'släpps larmet alltid vidare till AI.')
