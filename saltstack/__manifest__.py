# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Infrastructure',
    'version': '18.0.1.24.0',
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
    """,
    'author': 'Vertel Sverige AB',
    'license': 'AGPL-3',
    'website': 'https://vertel.se',
    'depends': ['base', 'mail'],
    'data': [
        'security/saltstack_groups.xml',
        'data/model_registry.xml',
        'security/ir_model_access.xml',
        'data/config_data.xml',
        'data/fault_actions.xml',
        'data/sync_cron.xml',
        'data/sync_actions.xml',
        'views/saltstack_menu_views.xml',
        'views/res_config_settings_views.xml',
        'views/salt_pillar_views.xml',
        'views/salt_minion_views.xml',
        'views/salt_alert_views.xml',
            'views/salt_runlog_views.xml',
    ],
    'post_init_hook': '_ensure_webhook_config',
    'installable': True,
    'application': True,
    'auto_install': False,
}
