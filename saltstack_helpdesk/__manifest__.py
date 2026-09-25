# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'website': 'https://vertel.se/apps/odoo-saltstack/saltstack_helpdesk',
    'name': 'SaltStack Helpdesk Bridge',
    'version': '18.0.1.0.2',
    'license': 'AGPL-3',
    'category': 'Infrastructure',
    'summary': 'Create helpdesk tickets from infrastructure incidents.',
    'description': '''
SaltStack Helpdesk Bridge
=========================

    Create helpdesk tickets from infrastructure incidents.

    Features:

        - Demo Data: Ships pre-configured demo data for the industry.
    ''',
    'depends': ['saltstack_ai', 'helpdesk_mgmt'],
    'data': [
        'security/ir.model.access.csv',
        'data/helpdesk_tools.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
