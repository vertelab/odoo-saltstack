# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Helpdesk Bridge',
    'version': '18.0.1.0.0',
    'license': 'AGPL-3',
    'category': 'Infrastructure',
    'summary': 'Create helpdesk tickets from infrastructure incidents',
    'depends': ['saltstack_ai', 'helpdesk_mgmt'],
    'data': [
        'security/ir.model.access.csv',
        'data/helpdesk_tools.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
