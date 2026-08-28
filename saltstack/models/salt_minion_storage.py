# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""Salt minion storage usage (LXD host, dirvish, S3/Garage, restic backup)."""

import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class SaltMinionStorage(models.Model):
    _name = 'salt.minion.storage'
    _description = 'Salt Minion Storage Usage'
    _order = 'storage_type, id'

    minion_id = fields.Many2one(
        'salt.minion',
        string='Minion',
        required=True,
        ondelete='cascade',
        index=True,
    )
    storage_type = fields.Selection(
        selection=[
            ('lxd_host', 'LXD-host'),
            ('dirvish', 'Dirvish'),
            ('s3', 'S3 (Garage)'),
            ('s3_backup', 'S3-backup (restic)'),
        ],
        string='Typ',
        required=True,
        help='Where the storage is used: on the LXD host filesystem, the '
             'Dirvish backup tree, Garage S3 source buckets or the '
             'S3 backup (restic) bucket.',
    )
    provider = fields.Char(
        string='Källa',
        help='Source of the measurement, e.g. the LXD host name (fors), '
             'dirvish, or the Garage bucket name.',
    )
    size_gb = fields.Float(
        string='Storlek (GB)',
        digits=(12, 2),
    )
    method = fields.Char(
        string='Metod',
        help='How the size was measured or estimated.',
    )
    measured_at = fields.Datetime(
        string='Mätt',
    )
    note = fields.Char(string='Anteckning')

    _sql_constraints = [
        (
            'minion_type_uniq',
            'unique (minion_id, storage_type)',
            'A storage row of this type already exists for the minion.',
        ),
    ]
