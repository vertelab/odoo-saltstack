# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Zabbix',
    'version': '18.0.1.1.0',
    'license': 'AGPL-3',
    'category': 'Infrastructure',
    'summary': 'Zabbix connection for SaltStack — API client + settings + correlation',
    'depends': ['saltstack'],
    'data': [
        'security/ir.model.access.csv',
        'views/salt_alert_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
