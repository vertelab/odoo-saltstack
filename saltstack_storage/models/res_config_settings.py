# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    storage_collect_enabled = fields.Boolean(
        string='Mät lagring vid minionsynk',
        config_parameter='saltstack_storage.enabled',
        help='When enabled, the minion sync collects storage measurements '
             'after status and registration are done. A failing measurement '
             'never affects the sync itself.',
    )
    storage_borg_host = fields.Char(
        string='Borg-värd',
        config_parameter='saltstack_storage.borg_host',
        help='Minion that holds the Borg repositories (default: tull1).',
    )
    storage_borg_archive_stats = fields.Integer(
        string='Borg-arkiv per körning',
        config_parameter='saltstack_storage.borg_archive_stats',
        default=1,
        help='How many Borg archives get a real per-archive measurement per '
             'run. Each one takes 75-120 s and an exclusive repository lock, '
             'so keep this low. 0 = repository total only.',
    )
    storage_retention_days = fields.Integer(
        string='Behåll mätningar (dagar)',
        config_parameter='saltstack_storage.retention_days',
        default=90,
        help='How long measurement history is kept. 0 = keep everything.',
    )
