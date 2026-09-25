# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'website': 'https://vertel.se/apps/odoo-saltstack/saltstack_zabbix',
    'name': 'SaltStack Zabbix',
    'version': '18.0.1.3.0',
    'license': 'AGPL-3',
    'category': 'Infrastructure',
    'summary': 'Zabbix connection for SaltStack — API client + settings + correlation.',
    'description': '''
SaltStack Zabbix
================

    Zabbix connection for SaltStack — API client + settings + correlation.

    Features:

        - UI Integration: Extends 2 view(s) in the Odoo interface.
        - Extends Odoo: Builds on saltstack.alert, zabbix.api.
    ''',
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
