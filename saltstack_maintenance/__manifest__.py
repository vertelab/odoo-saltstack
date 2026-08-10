# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Maintenance Bridge',
    'version': '18.0.1.0.1',
    'license': 'AGPL-3',
    'category': 'Infrastructure',
    'summary': 'Notify maintenance system about infrastructure machines',
    'depends': ['saltstack_ai', 'base_maintenance'],
    'data': [
        'security/ir.model.access.csv',
        'data/maintenance_tools.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
