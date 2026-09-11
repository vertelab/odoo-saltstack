# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""Registrering av backup-servrar (var restore-kommandon körs).

En "backup-server" är en maskin som har restic-repon och kan återskapa data
(t.ex. restic-minionen). Registret gör att varje snapshot-rad kan peka ut
**vilken dator** operatören ska logga in på — och att nya servrar kan läggas
till utan kodändring: skapa en post, sätt `customers` om servern bara
hanterar vissa kunder, och kör `storage.backup` på den nya maskinen.
"""

import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class SaltBackupServer(models.Model):
    _name = 'salt.backup.server'
    _description = 'Backup-server (restore-mål)'
    _order = 'sequence, name'

    name = fields.Char(
        string='Namn',
        required=True,
        help='Backend-namn, t.ex. restic. Används som nyckel i synken.',
    )
    minion_id = fields.Many2one(
        'salt.minion',
        string='Minion',
        ondelete='set null',
        help='Salt-minionen som är backup-server. Ger IP och status.',
    )
    role = fields.Selection(
        selection=[
            ('restic', 'Restic (Garage S3)'),
            ('dirvish', 'Dirvish'),
            ('other', 'Övrig'),
        ],
        string='Roll',
        default='restic',
        required=True,
    )
    login_host = fields.Char(
        string='Inloggningsadress',
        help='Adress att SSH:a till för att köra restore-kommandon. '
             'Lämnas tom → minionens privata IP används.',
    )
    login_user = fields.Char(
        string='Inloggningskonto',
        default='ubuntu',
        help='Kontot att logga in som, t.ex. ubuntu. Verifierat: '
             'ubuntu-kontot har NOPASSWD-sudo på backup-servrarna, så '
             'kommandot kan köras utan lösenordsprompt.',
    )
    use_sudo = fields.Boolean(
        string='Använd sudo',
        default=True,
        help='Kör hjälpskriptet med sudo (krävs för att läsa '
             '/etc/rclone/*.conf och restic-lösenordet).',
    )
    helper_path = fields.Char(
        string='Hjälpskript',
        default='/usr/local/bin/restic-restore.sh',
        help='Skript på maskinen som tar <kund> <snapshot-id> och hanterar '
             'credentials (deployas av state storage.backup).',
    )
    credentials_dir = fields.Char(
        string='Credential-katalog',
        default='/etc/rclone',
        help='Katalog med <kund>.conf (AK/SK) på backup-servern.',
    )
    restore_base = fields.Char(
        string='Restore-baskatalog',
        default='/restore',
        help='Var återskapad data hamnar på backup-servern.',
    )
    customers = fields.Text(
        string='Kunder (valfritt)',
        help='Ett kundnamn per rad om servern bara hanterar vissa kunder. '
             'Tomt = servern är standard och hanterar alla kunder som inte '
             'matchar en annan server.',
    )
    sequence = fields.Integer(
        string='Ordning',
        default=10,
        help='Lägre = prioriteras när flera servrar matchar.',
    )
    active = fields.Boolean(string='Aktiv', default=True)
    notes = fields.Text(string='Anteckning')

    customer_count = fields.Integer(
        string='Antal kunder',
        compute='_compute_customer_count',
        help='0 = hanterar alla kunder (standard-server).',
    )
    restore_preview = fields.Char(
        string='Kommandopreview',
        compute='_compute_restore_preview',
        help='Exempel på hur ett restore-kommando för denna server ser ut.',
    )

    @api.depends('customers')
    def _compute_customer_count(self):
        for rec in self:
            rec.customer_count = len(rec.customer_list())

    @api.depends('name', 'login_host', 'login_user', 'use_sudo',
                 'minion_id', 'helper_path', 'restore_base')
    def _compute_restore_preview(self):
        for rec in self:
            rec.restore_preview = (
                'Så här ser ett restore-kommando ut för den här servern:\n%s\n'
                '→ data hamnar under %s/<kund> på %s.'
                % (rec.restore_example(),
                   rec.restore_base or '/restore',
                   rec.name or '<server>'))

    # ── Hjälpmetoder ─────────────────────────────────────────────────────

    def effective_login_host(self):
        """Adress att SSH:a till (login_host, annars minionens privata IP)."""
        self.ensure_one()
        return self.login_host or self.minion_id.private_ip or ''

    def effective_login_user(self):
        """Kontot att logga in som (default 'ubuntu')."""
        self.ensure_one()
        return (self.login_user or 'ubuntu').strip()

    def ssh_target(self):
        """'ubuntu@192.168.11.112' — det man SSH:ar till."""
        self.ensure_one()
        host = self.effective_login_host()
        if not host:
            return ''
        return '%s@%s' % (self.effective_login_user(), host)

    def command_prefix(self):
        """'sudo ' om servern kräver det, annars tom sträng."""
        self.ensure_one()
        return 'sudo ' if self.use_sudo else ''

    def customer_list(self):
        """Kundnamnen servern hanterar (lowercase); tom lista = alla."""
        self.ensure_one()
        return [
            c.strip().lower()
            for c in (self.customers or '').splitlines()
            if c.strip()
        ]

    def restore_display(self):
        """'restic (192.168.11.112)' — för visning i vyer och hjälptexter."""
        self.ensure_one()
        host = self.effective_login_host()
        return '%s (%s)' % (self.name, host) if host else self.name

    def restore_example(self, customer='<kund>', snapshot='latest'):
        """Fullt exempel-kommando (SSH) för denna server."""
        self.ensure_one()
        helper = self.helper_path or '/usr/local/bin/restic-restore.sh'
        cmd = '%s%s %s %s' % (
            self.command_prefix(), helper, customer, snapshot)
        target = self.ssh_target()
        return 'ssh %s "%s"' % (target, cmd) if target else cmd

    # ── Uppslag ──────────────────────────────────────────────────────────

    @api.model
    def _find_for_customer(self, customer):
        """Hitta backup-servern som hanterar en kund.

        Prioritet:
        1. En server vars `customers`-lista innehåller kunden.
        2. En server utan kundlista (= standard, hanterar alla).

        Returnerar en tom recordset om registret är tomt — synken faller då
        tillbaka på config-parametern `saltstack.backup.default_restic_minion`
        (default 'restic') så att funktionen fungerar även innan registret
        fyllts.
        """
        customer = (customer or '').strip().lower()
        servers = self.search([('active', '=', True)])
        for srv in servers:
            names = srv.customer_list()
            if names and customer in names:
                return srv
        for srv in servers:
            if not srv.customer_list():
                return srv
        return self.browse()

    @api.model
    def _fallback_server_info(self):
        """(namn, login_host, helper, restore_base) ur config-parametern.

        Används när registret är tomt, så att snapshot-rader ändå får ett
        körbart kommando med rätt maskin.
        """
        params = self.env['ir.config_parameter'].sudo()
        name = params.get_param(
            'saltstack.backup.default_restic_minion', 'restic')
        login = params.get_param('saltstack.backup.default_restic_host', '')
        helper = params.get_param(
            'saltstack.backup.default_restic_helper',
            '/usr/local/bin/restic-restore.sh')
        base = params.get_param('saltstack.backup.restore_base', '/restore')
        if not login and name:
            minion = self.env['salt.minion'].search(
                [('name', '=', name)], limit=1)
            login = minion.private_ip or ''
        return name, login, helper, base
