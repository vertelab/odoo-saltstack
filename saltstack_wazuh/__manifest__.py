# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Wazuh',
    'version': '18.0.1.7.1',
    'category': 'Infrastructure',
    'summary': 'Wazuh source for Drift Alerts — selection_add + settings',
    'author': 'Vertel Sverige AB',
    'license': 'AGPL-3',
    'website': 'https://vertel.se',
    'depends': ['saltstack', 'saltstack_ai', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
