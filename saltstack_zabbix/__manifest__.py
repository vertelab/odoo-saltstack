# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Zabbix',
    'version': '18.0.1.0.0',
    'license': 'AGPL-3',
    'category': 'Infrastructure',
    'summary': 'Zabbix-anslutning för SaltStack — settings + test',
    'depends': ['saltstack_ai'],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
