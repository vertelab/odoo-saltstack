# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Keykeep Bridge',
    'version': '18.0.1.0.4',
    'category': 'Infrastructure',
    'summary': 'Sync Salt pillar secrets to Keykeep credentials.',
    'description': '''
SaltStack Keykeep Bridge
========================

    Sync Salt pillar secrets to Keykeep credentials.

    Features:

        - UI Integration: Extends 2 view(s) in the Odoo interface.
        - Extends Odoo: Builds on keykeep.credential, salt.minion, salt.pillar.
    ''',
    'author': 'Vertel Sverige AB',
    'license': 'AGPL-3',
    'website': 'https://vertel.se/apps/odoo-saltstack/saltstack_keykeep',
    'depends': ['saltstack', 'keykeep'],
    'data': [
        'security/ir.model.access.csv',
        'views/salt_pillar_views.xml',
        'views/salt_minion_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
