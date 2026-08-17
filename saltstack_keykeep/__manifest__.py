# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Keykeep Bridge',
    'version': '18.0.1.0.2',
    'category': 'Infrastructure',
    'summary': 'Sync Salt pillar secrets to Keykeep credentials',
    'description': """
SaltStack Keykeep Bridge
========================

Bridge between Salt pillar secrets and Keykeep credentials.

- Sync pillar values (data_type='secret') to keykeep.credential records
- Support for Bifrost provider key syncing
- API auth via keykeep (SaltAPI and ZabbixAPI read tokens from keykeep)
    """,
    'author': 'Vertel Sverige AB',
    'license': 'AGPL-3',
    'website': 'https://vertel.se',
    'depends': ['saltstack', 'keykeep'],
    'data': [
        'security/ir.model.access.csv',
        'views/salt_pillar_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
