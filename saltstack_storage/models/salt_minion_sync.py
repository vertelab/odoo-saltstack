# Copyright (C) 2026 Vertel Sverige AB (<https://vertel.se>).
"""Sync hook: collect storage measurements after the minion sync.

The hook is deliberately a separate file so the base sync stays untouched
and readable. It is called from `action_sync_all_minions()` in a try/except:
a failing measurement must never affect status, registration or
deactivation.
"""

import logging

from odoo import models

_logger = logging.getLogger(__name__)


class SaltMinionSyncStorage(models.Model):
    _inherit = 'salt.minion'

    def action_sync_all_minions(self):
        """Base sync, then the storage hook — never the other way around.

        The base result is computed first and returned unchanged. Storage
        collection runs afterwards inside `_sync_storage_measurements()`,
        which isolates every failure. A broken measurement therefore cannot
        change `created`/`updated`/`deactivated`, and cannot roll back the
        sync transaction.
        """
        result = super().action_sync_all_minions()
        if not isinstance(result, dict) or result.get('error'):
            return result
        try:
            with self.env.cr.savepoint():
                synced = self.search([('active', '=', True)])
                self._sync_storage_measurements(synced)
        except Exception as exc:  # noqa: BLE001 — the sync already succeeded
            self.env.invalidate_all()
            _logger.warning(
                'Storage collection after sync failed (sync result kept): %s',
                exc)
        return result

    def _sync_storage_measurements(self, minions):
        """Collect measurements for the given minions, best-effort.

        Controlled by the `saltstack_storage.enabled` setting. Errors are
        logged per minion and never propagated — the caller's sync result is
        the answer to "did the sync work", and storage is not part of it.
        """
        enabled = self.env['ir.config_parameter'].sudo().get_param(
            'saltstack_storage.enabled', 'True')
        if str(enabled).lower() in ('false', '0', ''):
            return {'skipped': 'disabled'}

        measured, failed = 0, []
        for minion in minions:
            try:
                with self.env.cr.savepoint():
                    minion._collect_storage_measurements()
                measured += 1
            except Exception as exc:  # noqa: BLE001 — isolate per minion
                self.env.invalidate_all()
                _logger.warning(
                    'Storage measurement failed during sync for %s: %s',
                    minion.name, exc)
                failed.append(minion.name)
        return {'measured': measured, 'failed': failed}
