# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Management System Bridge',
    'version': '18.0.1.0.1',
    'license': 'AGPL-3',
    'category': 'Infrastructure',
    'summary': 'Document infrastructure anomalies as nonconformities',
    'depends': ['saltstack_ai', 'mgmtsystem_nonconformity'],
    'data': [
        'security/ir.model.access.csv',
        'data/anomaly_tools.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
