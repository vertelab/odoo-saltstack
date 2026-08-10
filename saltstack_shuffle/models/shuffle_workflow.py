# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

from odoo import fields, models


class ShuffleWorkflow(models.Model):
    _name = 'shuffle.workflow'
    _description = 'Shuffle Workflow'
    _order = 'name'

    name = fields.Char(required=True)
    workflow_id = fields.Char(string='Shuffle Workflow ID')
    playbook = fields.Selection([
        ('pb1_brute_force', 'pb1_brute_force'),
        ('pb2_rootkit', 'pb2_rootkit'),
        ('pb3_drift', 'pb3_drift'),
        ('pb4_cve', 'pb4_cve'),
        ('pb5_compliance', 'pb5_compliance'),
    ], string='Playbook')
    status = fields.Selection([
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('error', 'Error'),
    ], default='active')
    last_execution = fields.Datetime(string='Last execution')
    execution_count = fields.Integer(string='Execution count', default=0)
    json_definition = fields.Text(string='Workflow-JSON (read-only)')
    active = fields.Boolean(default=True)
