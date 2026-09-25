# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack AI Bridge',
    'version': '18.0.1.16.0',
    'category': 'Infrastructure',
    'summary': 'AI-powered SaltStack, Zabbix och Wazuh integration.',
    'description': '''
SaltStack AI Bridge
===================

    Generic AI-powered SaltStack, Zabbix och Wazuh integration. Provides:

    Contains zero infrastructure-specific knowledge — safe to open-source.

    Features:

        - Automation: Scheduled jobs: Saltstack AI: Process pending diagnoses.
        - UI Integration: Extends 2 view(s) in the Odoo interface.
        - Extends Odoo: Builds on saltstack.alert.
    ''',
    'author': 'Vertel Sverige AB',
    'license': 'AGPL-3',
    'website': 'https://vertel.se/apps/odoo-saltstack/saltstack_ai',
    'depends': ['saltstack', 'ai_agent_core', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'views/salt_alert_views.xml',
        'views/res_config_settings_views.xml',
        'data/generic_tools.xml',
        'data/driftlarm_tool.xml',
        'data/access_groups.xml',
        'data/capabilities.xml',
        'data/generic_skills.xml',
        'data/infrastructure_operator.xml',
        'data/agent_tools.xml',
        'data/domain_tools.xml',
        'data/diagnosis_cron.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
