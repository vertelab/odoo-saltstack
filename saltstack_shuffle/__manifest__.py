# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Shuffle SOAR',
    'version': '18.0.1.1.2',
    'category': 'Infrastructure',
    'summary': 'Shuffle SOAR-hantering — workflows, appar, webhooks.',
    'description': '''
SaltStack Shuffle SOAR
======================

    Shuffle SOAR-hantering — workflows, appar, webhooks.

    Features:

        - Web integration: Exposes HTTP endpoints for external systems.
        - UI Integration: Extends 5 view(s) in the Odoo interface.
        - Extends Odoo: Builds on shuffle.app, shuffle.webhook, shuffle.workflow.
    ''',
    'author': 'Vertel Sverige AB',
    'license': 'AGPL-3',
    'website': 'https://vertel.se/apps/odoo-saltstack/saltstack_shuffle',
    'depends': ['saltstack', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'views/shuffle_workflow_views.xml',
        'views/shuffle_app_views.xml',
        'views/shuffle_webhook_views.xml',
        'views/shuffle_menu.xml',
        'views/res_config_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
