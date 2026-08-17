# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""
Zabbix correlation extension for saltstack.alert.

Bridge module (saltstack_zabbix) adds zabbix fields and correlation on
top of the ground model. The base never depends on this — process_webhook
guards with hasattr, so correlation only runs when this bridge is installed.
"""

import json
import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class SaltAlert(models.Model):
    _inherit = 'saltstack.alert'

    source = fields.Selection(
        selection_add=[('zabbix', 'Zabbix')],
    )

    # ── Correlation ──────────────────────────────────────────────────────
    correlated_zabbix_alert = fields.Boolean(
        string='Correlated with Zabbix',
        default=False,
    )
    zabbix_alert_id = fields.Char(string='Zabbix Alert ID')
    zabbix_event_id = fields.Char(string='Zabbix Event ID')

    # ── Zabbix-korrelation ───────────────────────────────────────────────

    def _correlate_zabbix(self):
        """Look for an active Zabbix problem on the same host."""
        self.ensure_one()
        try:
            config = self.env['zabbix.api']
            window = int(self.env['ir.config_parameter'].get_param(
                'saltstack.alert.correlation_window', 120))
            result = config.zabbix_call('problem.get', {
                'output': ['eventid', 'name', 'hosts'],
                'recent': True,
                'search': {'hosts': [self.host]},
                'limit': 10,
            })
            problems = json.loads(result) if isinstance(result, str) else result
            if isinstance(problems, list) and problems:
                self.correlated_zabbix_alert = True
                self.zabbix_alert_id = str(problems[0].get('eventid', ''))
            else:
                self.correlated_zabbix_alert = False
        except Exception as e:
            _logger.warning('Zabbix correlation failed for %s: %s',
                            self.host, e)
            self.correlated_zabbix_alert = False
