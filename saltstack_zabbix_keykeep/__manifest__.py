# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'website': 'https://vertel.se/apps/odoo-saltstack/saltstack_zabbix_keykeep',
    'name': 'SaltStack Zabbix Keykeep',
    'version': '18.0.1.0.1',
    'category': 'Infrastructure',
    'summary': 'Keykeep Managed for the Zabbix connection.',
    'description': '''
SaltStack Zabbix Keykeep
========================

    Keykeep Managed for the Zabbix connection.

    Features:

        - Extends Odoo: Builds on existing Odoo models.
    ''',
    'license': 'AGPL-3',
    'depends': ['saltstack_zabbix', 'keykeep'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
