# Design — `storage_collect` lock contention

Se `proposal.md` — Why. Detta dokument beskriver hur, inte varför.

## Context

Två `cron.task`-jobb rör samma rad i `salt_minion`:

| Jobb | Kadens/intervall | Skriver | Körtid |
|---|---|---|---|
| `minion_sync` (id 171) | 24 h | `salt_minion` (status, grains, `odoo_*`) | 121–157 s |
| `storage_collect` (id 3) | kadens `week`, offset 885 min → 14:45 UTC | `saltstack_storage_measurement` **och** `salt_minion.odoo_*` | — |

`lock_timeout = 10 s` (postgresql.conf). `minion_sync` håller radlåset i upp
till 157 s. Varje skrivning från `storage_collect` till samma rad får därför
`canceling statement due to lock timeout`.

Dessutom triggar `minion_sync` själv `_sync_storage_measurements()` — samma
insamling, men inifrån en transaktion som redan äger raden.

Anropet är redan delegerat till Salt-modulen `storage.info` (en transport, en
mätlogik). Problemet är uteslutande persistensen.

## Goals / Non-Goals

**Goals:**

- Ett insamlingssvep ska fullbordas även när `minion_sync` håller radlåset.
- En minion som failar får inte fälla svepet.
- Utfallet ska synas i `cron.task`-historiken.

**Non-Goals:**

- Korta `minion_sync`. Den får fortsätta ta 2 minuter.
- Backfill av mätluckan 2026-10-29 → nu.
- Ändra `storage.info`-modulen i `/srv/salt/_modules/storage.py`.
- `_cron_borg_archive_cache`.

## Decisions

### D1 — Skilj på "mäta" och "sammanställa"

`_collect_storage_measurements()` gör i dag två saker: sparar mätningar och
skriver `odoo_stats` tillbaka till minionsraden. Den senare är en
sammanställning som hör till `minion_sync`, inte till mätinsamlingen.

**Beslut:** `_apply_odoo_stats()` anropas bara när insamlingen körs inifrån
synkroniseringen. Svepet från `storage_collect` mäter och sparar mätningar —
inget annat.

*Alternativ som övervägdes:*
- **Höj `lock_timeout` för svepet.** Löser symptomet men låter svepet vänta
  157 s per minion × 157 minions. Oacceptabelt.
- **Skriv `odoo_stats` i egen transaktion (ny cursor).** Odoo tillåter det
  men det bryter transaktionsgränsen och gör felhanteringen svår. Dessutom
  kvarstår konkurrensen om raden — bara utanför svepets transaktion.
- **Låt `minion_sync` äga fälten helt.** Det är vad D1 gör, uttryckt som regel.

### D2 — Explicit flagga, inte gissning på `env.context`

En `context`-nyckel (`storage_skip_odoo_stats`) är osynlig i anropet och kan
ärvas oavsiktligt. En explicit parameter på `_collect_storage_measurements()`
gör avsikten läsbar på anropsplatsen och omöjlig att sätta av misstag.

**Beslut:** `_collect_storage_measurements(write_odoo_stats=True)`. Svepet
anropar med `False`; `_sync_storage_measurements()` behåller default.

Bakåtkompatibelt: `salt_minion_sync.py` behöver inte ändras.

### D3 — `invalidate_model()`, inte `invalidate_all()`

`invalidate_all()` fluschar hela transaktionen. När savepointen redan rullats
tillbaka kastar den en andra `SerializationFailure` som **ersätter** det
ursprungliga felet och escapar isoleringen.

`invalidate_model()` på `saltstack.storage.measurement` släpper samma stale
cache utan att röra transaktionen. Samma fix som 2026-10-21 i
`salt_minion_sync.py:70` och `salt_minion.py:245`.

**Beslut:** `_cron_collect_all()` följer samma mönster.

### D4 — Tidsbudget per minion via `SET LOCAL lock_timeout`

`SET LOCAL` är transaktionslokalt och läcker inte i anslutningspoolen (samma
skäl som `cypher()` använder det). Svepet sätter en kortare budget än
serverns 10 s — men **inte** kortare än att en normal skrivning hinner igenom.

**Beslut:** `SET LOCAL lock_timeout = '2s'` runt varje minions savepoint.
En minion som inte får låset på 2 s räknas som failed; svepet går vidare.
Budgeten är per minion, inte per svep.

*Varför 2 s:* en normal `UPDATE salt_minion` tar millisekunder. 2 s är
generöst för det legitima fallet och kort nog att 157 minions inte ska kunna
ackumulera minuter av väntan.

### D5 — Svepet räknas som genomfört även med failed minions

`_cron_collect_all()` returnerar en dict med `measured` och `failed`. Att
kasta när någon minion failar vore fel: svepet *gjorde sitt jobb* — det
isolerade felet och mätte resten. Att kasta skulle dessutom kräva att
`last_run` skrivs i en except-gren, vilket är precis den väg som var trasig.

**Beslut:** svepet returnerar alltid. `failed` syns i returvärdet och i
loggen (`Storage collection: N minions measured, M failed`). `last_run` sätts
av `_run_locked_task` som vanligt.

## Risks / Trade-offs

| Risk | Mitigation |
|---|---|
| `odoo_stats` slutar uppdateras om `minion_sync` inte körs | `minion_sync` är 24 h-intervall och har `failure_count`-vakt. `salt_minion.odoo_stats_synced` är synligt fält — en stale rad syns i vyn. |
| 2 s `lock_timeout` kan vara för kort på en lastad DB | Felet är isolerat per minion; svepet fortsätter. Höj till 5 s om loggen visar systematiska timeouts utan att `minion_sync` kör. |
| `invalidate_model()` släpper inte cache för `salt.minion` | Behövs inte — svepet skriver inte `salt.minion` efter D1. |
| Svepet tar fortfarande lång tid (157 × ~10 s) | `timeout_seconds = 1800` räcker. `storage.info` mättes till 4–33 s per värd; medianen är låg eftersom de flesta svarar `not available` direkt. |
| Mätluckan 2026-10-29 → nu består | Accepterat (non-goal). Historiken är inte kontinuerlig; det syns i `measured_at`. |

## Migration Plan

1. Bumpa `saltstack_storage` `18.0.1.1.5` → `18.0.1.1.6`.
2. `checkmodule saltstack_storage` i **multi-user** (inom 07–18).
3. Verifiera att modulen installerar rent (exit 0, inga `Missing model`).
4. Kör `storage_collect` manuellt via `POST /cron/trigger/storage_collect`
   och kontrollera `cron.task.log` + antalet nya rader i
   `saltstack_storage_measurement`.
5. Låt nästa schemalagda svep (14:45 UTC) gå och verifiera att `last_run`
   sätts och att inga `lock timeout`-rader dyker upp i loggen.

**Rollback:** återställ `__manifest__.py` till `18.0.1.1.5`, kör
`checkmodule` igen. Ingen datamigrering — ändringen rör bara kodvägar, inga
fält eller scheman.

## Open Questions

Inga. `minion_sync`-längden (121–157 s) är ett separat spår men påverkar inte
detta beslut: D1 gör konkurrensen om raden omöjlig oavsett hur lång synken är.
