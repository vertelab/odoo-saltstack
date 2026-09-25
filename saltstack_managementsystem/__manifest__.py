# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'website': 'https://vertel.se/apps/odoo-saltstack/saltstack_managementsystem',
    'name': 'SaltStack Management System Bridge',
    'version': '18.0.1.0.2',
    'license': 'AGPL-3',
    'category': 'Infrastructure',
    'summary': 'Document infrastructure anomalies as nonconformities.',
    'description': '''
SaltStack Management System Bridge
==================================

    Document infrastructure anomalies as nonconformities.

    Features:

        - Demo Data: Ships pre-configured demo data for the industry.
    ''',
    'depends': ['saltstack_ai', 'mgmtsystem_nonconformity'],
    'data': [
        'security/ir.model.access.csv',
        'data/anomaly_tools.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
