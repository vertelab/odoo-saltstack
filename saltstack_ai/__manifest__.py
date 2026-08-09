# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack AI Bridge',
    'version': '18.0.1.8.0',
    'category': 'Infrastructure',
    'summary': 'AI-powered SaltStack, Zabbix och Wazuh integration',
    'description': """
SaltStack AI Bridge
===================

Generic AI-powered SaltStack, Zabbix och Wazuh integration. Provides:

- SaltAPI and ZabbixAPI client classes for REST/JSON-RPC communication
- Configurable settings for API URLs, tokens, and authentication method
- 26 generic ai.tool records (18 SaltStack + 8 Zabbix)
- 6 educational ai.skill records covering SaltStack and Zabbix concepts
- Infrastructure Operator ai.coworker (SaltStack + Zabbix + Wazuh)
- Extensible framework for infrastructure-specific bridge modules

Contains zero infrastructure-specific knowledge — safe to open-source.
    """,
    'author': 'Vertel Sverige AB',
    'license': 'AGPL-3',
    'website': 'https://vertel.se',
    'depends': ['saltstack', 'ai_agent_core', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'data/generic_tools.xml',
        'data/access_groups.xml',
        'data/capabilities.xml',
        'data/generic_skills.xml',
        'data/driftlarm_tool.xml',
        'data/infrastructure_operator.xml',
        'data/agent_tools.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
