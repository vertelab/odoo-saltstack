# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).

import json
import logging

import yaml

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class SaltPillar(models.Model):
    _name = 'salt.pillar'
    _description = 'Salt Pillar'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'pillar_file, key'
    _rec_names_search = ['key', 'pillar_file']

    # ── Fields ──────────────────────────────────────────────────────────────

    key = fields.Char(
        string='Key',
        required=True,
        help='Pillar key (e.g. "postgres.version", "odoo.admin_password")',
    )
    value = fields.Text(
        string='Value',
        help='Pillar value (stored as text; use data_type to interpret)',
    )
    data_type = fields.Selection(
        selection=[
            ('string', 'String'),
            ('integer', 'Integer'),
            ('boolean', 'Boolean'),
            ('float', 'Float'),
            ('dict', 'Dictionary (YAML)'),
            ('list', 'List (YAML)'),
            ('json', 'JSON'),
            ('secret', 'Secret'),
        ],
        string='Data Type',
        required=True,
        default='string',
    )
    pillar_file = fields.Char(
        string='Pillar File',
        help='Source pillar file (e.g. "odoo.sls", "postgres/init.sls")',
    )
    minion_target = fields.Char(
        string='Minion Target',
        help='Salt target pattern for this pillar data',
    )
    description = fields.Text(
        string='Description',
    )
    active = fields.Boolean(
        string='Active',
        default=True,
    )
    sequence = fields.Integer(
        string='Sequence',
        default=10,
    )

    # ── Constraints ──────────────────────────────────────────────────────────

    @api.constrains('data_type', 'value')
    def _check_value_type(self):
        for rec in self:
            if not rec.value:
                continue
            if rec.data_type == 'json':
                try:
                    json.loads(rec.value)
                except json.JSONDecodeError:
                    raise ValidationError(
                        _('Invalid JSON in value for key "%s"', rec.key)
                    )
            elif rec.data_type in ('dict', 'list'):
                try:
                    yaml.safe_load(rec.value)
                except yaml.YAMLError:
                    raise ValidationError(
                        _('Invalid YAML in value for key "%s"', rec.key)
                    )
            elif rec.data_type == 'integer':
                try:
                    int(rec.value.strip())
                except ValueError:
                    raise ValidationError(
                        _('Value for key "%s" is not a valid integer', rec.key)
                    )
            elif rec.data_type == 'float':
                try:
                    float(rec.value.strip())
                except ValueError:
                    raise ValidationError(
                        _('Value for key "%s" is not a valid float', rec.key)
                    )

    # ── Computed ─────────────────────────────────────────────────────────────

    def _get_typed_value(self):
        """Return the value coerced to its data_type."""
        self.ensure_one()
        if not self.value:
            return None
        if self.data_type == 'string':
            return self.value
        elif self.data_type == 'integer':
            return int(self.value.strip())
        elif self.data_type == 'float':
            return float(self.value.strip())
        elif self.data_type == 'boolean':
            return self.value.strip().lower() in ('true', '1', 'yes', 'on')
        elif self.data_type == 'json':
            return json.loads(self.value)
        elif self.data_type in ('dict', 'list'):
            return yaml.safe_load(self.value)
        elif self.data_type == 'secret':
            return self.value
        return self.value

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_export_yaml(self):
        """Export selected pillars as YAML."""
        result = {}
        for rec in self:
            result[rec.key] = rec._get_typed_value()
        yaml_str = yaml.dump(result, default_flow_style=False, allow_unicode=True)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Pillar YAML Export'),
            'res_model': 'salt.pillar.export.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_yaml_content': yaml_str,
                'default_pillar_ids': [(6, 0, self.ids)],
            },
        }

    def action_import_yaml(self):
        """Open wizard to import YAML pillar data."""
        return {
            'type': 'ir.actions.act_window',
            'name': _('Import Pillar YAML'),
            'res_model': 'salt.pillar.import.wizard',
            'view_mode': 'form',
            'target': 'new',
        }

    def action_duplicate(self):
        """Duplicate selected pillars with incremented keys."""
        for rec in self:
            base_key = rec.key
            counter = 1
            new_key = f'{base_key}_copy{counter}'
            while self.search_count([('key', '=', new_key)]):
                counter += 1
                new_key = f'{base_key}_copy{counter}'
            rec.copy(default={'key': new_key})

    # ── CRUD ─────────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('value') and not vals.get('data_type'):
                # Auto-detect type
                val = vals['value'].strip()
                if val.startswith('{') or val.startswith('['):
                    try:
                        json.loads(val)
                        vals['data_type'] = 'json'
                    except json.JSONDecodeError:
                        try:
                            yaml.safe_load(val)
                            vals['data_type'] = 'dict'
                        except yaml.YAMLError:
                            pass
                elif val.lower() in ('true', 'false', 'yes', 'no'):
                    vals['data_type'] = 'boolean'
                else:
                    try:
                        int(val)
                        vals['data_type'] = 'integer'
                    except ValueError:
                        try:
                            float(val)
                            vals['data_type'] = 'float'
                        except ValueError:
                            vals['data_type'] = 'string'
        return super().create(vals_list)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def get_pillar_dict(self, minion_target=None):
        """Return a dictionary of all pillar data.

        Args:
            minion_target: Optional filter for minion target.

        Returns:
            dict: {key: typed_value, ...}
        """
        domain = [('active', '=', True)]
        if minion_target:
            domain.append(('minion_target', '=', minion_target))
        records = self.search(domain)
        result = {}
        for rec in records:
            result[rec.key] = rec._get_typed_value()
        return result

    # ── Salt Master Sync ────────────────────────────────────────────────────

    def _salt_login(self, timeout=15):
        """Exchange sharedsecret API key for a session token via /login."""
        import json as _json
        import ssl as _ssl
        import urllib.request as _urllib
        params = self.env['ir.config_parameter']
        api_url = params.get_param('saltstack.api_url', '')
        api_key = params.get_param('saltstack.api_token', '')
        payload = {
            'username': 'saltapi',
            'password': api_key,
            'eauth': 'sharedsecret',
        }
        data = _json.dumps(payload).encode()
        req = _urllib.Request(
            f'{api_url.rstrip("/")}/login',
            data=data,
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        )
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        with _urllib.urlopen(req, timeout=timeout, context=ctx) as resp:
            result = _json.loads(resp.read().decode())
        try:
            return result['return'][0]['token']
        except (KeyError, IndexError, TypeError):
            raise ValueError('Salt login misslyckades: %s' % result)

    def _call_salt_api(self, client, tgt, fun, *args, timeout=120, **kwargs):
        """Call the Salt REST API. Returns dict or raises."""
        import json as _json
        import ssl as _ssl
        import urllib.request as _urllib

        params = self.env['ir.config_parameter']
        api_url = params.get_param('saltstack.api_url', '')
        api_token = params.get_param('saltstack.api_token', '')
        if not api_url:
            raise ValueError('Salt API URL not configured')
        if params.get_param('saltstack.auth_method', 'token') == 'sharedsecret':
            api_token = self._salt_login()

        payload = {'client': client, 'fun': fun, 'timeout': timeout}
        if client in ('local', 'local_async', 'local_batch'):
            payload['tgt'] = tgt
        if args:
            payload['arg'] = list(args)
        if kwargs:
            payload['kwarg'] = kwargs
        data = _json.dumps(payload).encode()
        req = _urllib.Request(
            f'{api_url.rstrip("/")}/',
            data=data,
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'X-Auth-Token': api_token,
            },
        )
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        with _urllib.urlopen(req, timeout=timeout + 30, context=ctx) as resp:
            return _json.loads(resp.read().decode())

    _SECRET_HINTS = (
        'password', 'passwd', 'token', 'secret', 'credential',
        'api_key', 'apikey', 'private_key', 'p12', 'pfx', 'pem',
    )

    @staticmethod
    def _is_secret_key(key):
        k = key.lower()
        return any(h in k for h in SaltPillar._SECRET_HINTS)

    def action_sync_from_salt(self):
        """Fetch pillar keys from ALL minions and create/update records.

        Hämtar pillar från ALLA minioner i ett bulk-anrop (tgt='*') och
        registrerar unionen av alla nycklar — även nästlade (t.ex.
        "servers.gw0.ipv4", "odoo.databases.dina"). Fångar därmed även
        kundspecifik pillar, inte bara masterns globala.

        Föredrar SaltStack-masterns värde (globala pillaren) när samma nyckel
        finns på flera minioner. Maskerar hemlighetsliknande värden
        (data_type='secret', value='*****') — interna användare ska inte kunna
        läsa lösenord/API-nycklar.
        """
        try:
            result = self._call_salt_api(
                'local', '*', 'pillar.items', timeout=90)
        except Exception as e:
            return {'error': str(e)}

        returned = result.get('return', [{}])[0]
        if not isinstance(returned, dict) or not returned:
            return {'error': 'Ingen pillar-data från minionerna'}

        def _dtype(value):
            if isinstance(value, bool):
                return 'boolean'
            if isinstance(value, int):
                return 'integer'
            if isinstance(value, float):
                return 'float'
            return 'string'

        def _flatten(prefix, data):
            """Platta ut pillar-dict till punktnycklar (alla nivåer)."""
            for key, value in sorted(data.items()):
                full = f'{prefix}.{key}' if prefix else key
                if isinstance(value, dict):
                    yield full, value, 'dict'
                    yield from _flatten(full, value)
                elif isinstance(value, list):
                    yield full, value, 'list'
                else:
                    yield full, value, _dtype(value)

        # Union av alla nycklar; föredra SaltStack-mastern (global pillar)
        flattened = {}  # key -> (value, dtype, minion_name)
        for minion_name, pillar_data in sorted(returned.items()):
            if not isinstance(pillar_data, dict):
                continue
            for key, value, dtype in _flatten('', pillar_data):
                if key == '_errors':
                    continue
                if key not in flattened or minion_name == 'SaltStack':
                    flattened[key] = (value, dtype, minion_name)

        created = 0
        updated = 0
        deactivated = 0
        seen_keys = set(flattened.keys())
        for key, (value, dtype, minion_name) in sorted(flattened.items()):
            existing = self.search([('key', '=', key)], limit=1)
            if self._is_secret_key(key):
                dtype = 'secret'
                val_str = '*****'
            elif dtype in ('dict', 'list'):
                val_str = yaml.safe_dump(
                    value, default_flow_style=False, allow_unicode=True).strip()
                if len(val_str) > 10000:
                    # Markör istället för trunkerad YAML — trunkerad YAML är
                    # ogiltig och kraschar _check_value_type
                    val_str = '# för stor att lagra (%d tecken)' % len(val_str)
            else:
                val_str = str(value)
            vals = {
                'key': key,
                'value': val_str,
                'data_type': dtype,
                'minion_target': minion_name,
                'pillar_file': 'salt-master',
            }
            if existing:
                if not existing.active:
                    existing.active = True
                existing.write(vals)
                updated += 1
            else:
                self.create(vals)
                created += 1

        # Deactivate pillars no longer present on the Salt master
        known = self.search([('active', '=', True)])
        for rec in known:
            if rec.key not in seen_keys:
                rec.active = False
                deactivated += 1

        return {
            'created': created,
            'updated': updated,
            'deactivated': deactivated,
            'source_minions': len(returned),
        }
