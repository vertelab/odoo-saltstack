# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Storage Measurement',
    'version': '18.0.1.1.4',
    'category': 'Infrastructure',
    'summary': 'Measure and summarise storage per minion and customer.',
    'description': '''
SaltStack Storage Measurement
=============================

    Measures storage per minion/container across the artefacts that exist in
    the fleet, and summarises it per customer in two layers (primary data and
    backup) so the same bytes are never counted twice.

    Artefacts:

        - LXD container (allocated + used, per volume)
        - Filestore (S3 bucket + recorded attachment size)
        - PostgreSQL database (measured on the actual db_host)
        - Borg backup of the database
        - Borg backup of the LXD container
        - Restic backup of the filestore

    Dirvish is retired and is deliberately NOT an artefact: it cannot be
    measured and is excluded from every sum. Historical values can be kept in
    `legacy_reference` for reference only.

    Scheduling uses the `cron_tick` engine (`cron.task`, cadence `week`) —
    no `ir.cron` is created by this module.
    ''',
    'author': 'Vertel Sverige AB',
    'license': 'AGPL-3',
    'website': 'https://vertel.se/apps/odoo-saltstack/saltstack_storage',
    'depends': ['saltstack', 'cron_tick'],
    'data': [
        'security/ir.model.access.csv',
        'data/cron_storage_collect.xml',
        'views/storage_measurement_views.xml',
        'views/salt_minion_views.xml',
        'views/storage_menu_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
