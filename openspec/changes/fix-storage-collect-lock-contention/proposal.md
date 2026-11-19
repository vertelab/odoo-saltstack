# Fix `storage_collect` lock contention

**Uppgift:** T/12316-spåret (`saltstack_storage`)
**Status:** förslag — rotorsak verifierad i produktion 2026-11-13

## Why

`cron.task` `storage_collect` (id 3, kadens `week`) har aldrig slutförts sedan
den skapades 2026-10-20. Lagringsmätningarna står stilla sedan **2026-10-29
13:12** (208 rader i `saltstack_storage_measurement`), och
`salt_minion.odoo_stats_synced` sedan samma dag 11:52 — två veckor utan mätdata.

Rotorsaken är inte `storage.info` (den svarar på 4–33 s per värd) utan två
samverkande fel i `saltstack_storage`:

1. **Radlås på `salt_minion`.** `_apply_odoo_stats()` skriver tillbaka till
   `salt_minion` för varje minion. `minion_sync` håller radlåset på samma rad i
   121–157 s, och `lock_timeout = 10 s`. Resultatet är
   `canceling statement due to lock timeout / while updating tuple (N,M) in
   relation "salt_minion"` — fyra gånger i loggen 2026-11-12 14:47–14:51.
2. **`env.invalidate_all()` i `except`-grenen.** Den fluschar hela
   transaktionen; när savepointen redan rullats tillbaka kastar den en andra
   `SerializationFailure` och felet **escapar** per-minion-isoleringen. Samma
   fälla fixades 2026-10-21 i `salt_minion_sync.py:70` och i
   `salt_minion.py:245` — `_cron_collect_all` har den kvar.

Följden är tyst: `_run_locked_task` kan inte skriva `_record_failure` när
transaktionen är död, så `last_run` förblir `NULL` och `failure_count` `0`.
Uppgiften ser "aldrig körd" ut i stället för "misslyckad".

## What Changes

- `_apply_odoo_stats()` skriver inte längre `odoo_stats`-fälten när anropet
  kommer från `storage_collect`. `minion_sync` äger de fälten; mätinsamlingen
  ska bara samla mätningar.
- `_cron_collect_all()` byter `env.invalidate_all()` mot `invalidate_model()`
  i `except`-grenen, så en minion som failar inte kan fälla hela körningen.
- Mätinsamlingen får en **egen** `savepoint` per skrivning och en **explicit**
  `lock_timeout`-budget, så ett radlås kostar en minion — inte en körning.
- `saltstack_storage` bumpas och uppgraderas.

## Non-goals

- `minion_sync`-körningens längd (121–157 s). Den adresseras separat; här
  undviker vi bara att kollidera med den.
- `ir.cron`-workerns `idle in transaction` som höll advisory-låset
  `957049415` 2026-11-12. Separat spår.
- `_cron_borg_archive_cache` (sequence 25). Den fungerar.
- Historiska mätrader. Ingen backfill — luckan 2026-10-29 → nu består.

## Capabilities

### New Capabilities

- `storage-measurement`: insamling av lagringsmätningar per minion — vilka
  artefakter som mäts, hur en enskild minions fel isoleras, och vilka fält
  insamlingen får skriva.

### Modified Capabilities

Inga. `openspec/specs/` är tomt i repot; detta är den första specen.

## Impact

| Fil | Ändring |
|---|---|
| `saltstack_storage/models/salt_minion.py` | `_apply_odoo_stats()` — villkorad skrivning |
| `saltstack_storage/models/storage_measurement.py` | `_cron_collect_all()` — isolering |
| `saltstack_storage/__manifest__.py` | versionsbump `18.0.1.1.5` → `18.0.1.1.6` |
| `saltstack_storage/i18n/sv.po` | om någon UI-sträng tillkommer |

Beroenden: `cron_tick` (befintligt), `saltstack` (`salt.minion`,
`saltstack.api`). Ingen ny extern dependency.

Drift: `checkmodule saltstack_storage` i **multi-user** (inom 07–18).
Verifiering sker mot `saltstack_storage_measurement` och
`salt_minion.odoo_stats_synced` — se tasks.md.
