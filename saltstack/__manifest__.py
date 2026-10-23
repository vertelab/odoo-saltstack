# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Infrastructure',
    'version': '18.0.1.68.0',
    'category': 'Infrastructure',
    'summary': 'Manage SaltStack minions, pillar anchors and infrastructure.',
    'description': '''
SaltStack Infrastructure
========================

    Manage SaltStack infrastructure from Odoo.

Features:

    - Minion registry with sync from the Salt Master.
    - Pillar management.
    - Alert handling with webhook ingestion.
    ''',
    'author': 'Vertel Sverige AB',
    'license': 'AGPL-3',
    'website': 'https://vertel.se/apps/odoo-saltstack/saltstack',
    # `cron_tick` is declared explicitly (2026-10-22): the module registers
    # `cron.task` records, so it must name the engine it writes into. It used
    # to arrive transitively via `ai_agent_core`, which is an invisible
    # coupling — `saltstack_storage` already declares it the same way.
    'depends': ['base', 'mail', 'cron_tick'],
    'data': [
        'security/saltstack_groups.xml',
        'data/model_registry.xml',
        'security/ir_model_access.xml',
        'security/ir_model_access_snapshot.xml',
        'data/config_data.xml',
        'data/fault_actions.xml',
        'data/cron_tasks.xml',
        'data/sync_cron.xml',
        'data/alert_count_cron.xml',
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
            'saltstack/static/src/scss/salt_minion_kanban.scss',
        ],
    },
    'post_init_hook': '_ensure_webhook_config',
    'installable': True,
    'application': True,
    'auto_install': False,
}
