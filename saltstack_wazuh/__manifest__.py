# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Wazuh',
    'version': '18.0.1.8.0',
    'category': 'Infrastructure',
    'summary': 'Wazuh source for Drift Alerts — selection_add + settings',
    'author': 'Vertel Sverige AB',
    'license': 'AGPL-3',
    'website': 'https://vertel.se',
    'depends': ['saltstack', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'views/salt_alert_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
