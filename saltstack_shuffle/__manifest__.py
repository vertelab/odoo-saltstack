# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

{
    'name': 'SaltStack Shuffle SOAR',
    'version': '18.0.1.1.1',
    'category': 'Infrastructure',
    'summary': 'Shuffle SOAR-hantering — workflows, appar, webhooks',
    'author': 'Vertel Sverige AB',
    'license': 'AGPL-3',
    'website': 'https://vertel.se',
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
