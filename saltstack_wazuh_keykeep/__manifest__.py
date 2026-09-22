# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'website': 'https://vertel.se/apps/odoo-saltstack/saltstack_wazuh_keykeep',
    'name': 'SaltStack Wazuh Keykeep',
    'version': '18.0.1.0.1',
    'category': 'Infrastructure',
    'summary': 'Keykeep Managed for the Wazuh connection',
    'license': 'AGPL-3',
    'depends': ['saltstack_wazuh', 'keykeep'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
