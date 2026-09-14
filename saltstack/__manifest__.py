# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Infrastructure',
    'version': '18.0.1.45.0',
    'category': 'Infrastructure',
    'summary': 'Manage SaltStack minions, pillar anchors and infrastructure',
    'description': """
SaltStack Infrastructure Management
====================================

Manage SaltStack infrastructure from Odoo.

Features:

- Minion registry with sync from Salt Master
- Pillar key/value anchors
- Fault injection server actions for testing the monitoring chain
- Driftlarm webhook (/saltstack/alert) med auto-genererad API-nyckel
- SaltStack API configuration in Settings
- Restic-snapshots per minion ("Återskapa data"): senaste 25 snapshots med
  kopiera-restore-kommando per rad, synkade från restic-status.json.
  Backup-server-registret (salt.backup.server) gör att varje rad visar
  VILKEN maskin kommandot körs på — och att fler backup-servrar kan läggas
  till utan kodändring.
- Dirvish-snapshots i samma flik: en rad per branch MED data (tree/) i
  varje vault, synkade från dirvish-status.json + dirvish-restore.sh --list.
  Branches utan tree/ (misslyckade backuper, tomma datumkataloger) tas
  aldrig med.
""",
    'author': 'Vertel Sverige AB',
    'license': 'AGPL-3',
    'website': 'https://vertel.se',
    'depends': ['base', 'mail'],
    'data': [
        'security/saltstack_groups.xml',
        'data/model_registry.xml',
        'security/ir_model_access.xml',
        'security/ir_model_access_snapshot.xml',
        'data/config_data.xml',
        'data/fault_actions.xml',
        'data/sync_cron.xml',
        'data/sync_snapshot_cron.xml',
        'data/sync_dirvish_snapshot_cron.xml',
        'data/auto_resolve_cron.xml',
        'data/backup_server_data.xml',
        'data/sync_actions.xml',
        'data/minion_update_actions.xml',
        'views/saltstack_menu_views.xml',
        'views/res_config_settings_views.xml',
        'views/res_partner_views.xml',
        'views/salt_pillar_views.xml',
        'views/salt_minion_views.xml',
        'views/salt_backup_server_views.xml',
        'views/salt_alert_views.xml',
        'views/salt_runlog_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'saltstack/static/src/js/saltstack.js',
        ],
    },
    'post_init_hook': '_ensure_webhook_config',
    'installable': True,
    'application': True,
    'auto_install': False,
}
