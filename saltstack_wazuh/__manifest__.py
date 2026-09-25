# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Wazuh',
    'version': '18.0.1.8.0',
    'category': 'Infrastructure',
    'summary': 'Wazuh source for Drift Alerts — selection_add + settings.',
    'description': '''
SaltStack Wazuh
===============

    Wazuh source for Drift Alerts — selection_add + settings.

    Features:

        - UI Integration: Extends 2 view(s) in the Odoo interface.
        - Extends Odoo: Builds on saltstack.alert, wazuh.api.
    ''',
    'author': 'Vertel Sverige AB',
    'license': 'AGPL-3',
    'website': 'https://vertel.se/apps/odoo-saltstack/saltstack_wazuh',
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
