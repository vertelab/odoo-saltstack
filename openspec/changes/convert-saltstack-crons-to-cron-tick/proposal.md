# Konvertera saltstack-cronjobb till cron_tick

**Uppgift:** T/12316 (`project.task` 51044)
**Status:** pågår — steg 0 klart, blockerare löst

## Varför

`saltstack` och `saltstack_ai` schemalägger sex jobb via `ir.cron`. `ir.cron`
ger ingen synlig drift: ingen vet om ett jobb gick, hur länge det tog, eller om
det tyst slutade köra. `cron_tick` ger körningshistorik, övervakning av
överhoppade körningar, manuell trigger och ett webbhook-gränssnitt.

## Omfattning — fyra jobb flyttar, två stannar

| Jobb | Modul | Beslut |
|---|---|---|
| `cron_sync_minions_pillars` | saltstack | → `cron.task` `minion_sync`, **24 h intervall** |
| `cron_backfill_alert_counts` | saltstack | → `cron.task`, kadens `min` |
| `cron_auto_resolve_alerts` | saltstack | → `cron.task`, kadens `min` |
| `cron_salt_alert_diagnosis` | saltstack_ai | → `cron.task`, kadens `min`, limit 5 → 20 |
| `cron_sync_snapshots` | saltstack | **stannar** i `ir.cron` |
| `cron_sync_dirvish_snapshots` | saltstack | **stannar** i `ir.cron` |

**Varför de två stannar:** de har ordnings- och tidsberoenden som en kadens inte
kan uttrycka — restic-rapporten går 06:15 och `daily`-ticksen 06:00. En
`cron.task` med kadens skulle tappa den relativa ordningen.

**Varför `minion_sync` är 24 h intervall och inte kadens `daily`:** kadens
`daily` beräknar sin offset ur `sha256('cron_tick:daily')` ≈ 01:22, vilket
krockar med Borg-fönstret 01:00–03:00. Ett 24 h-intervall undviker det.

## Krav

### R1 — cron_tick som beroende
`saltstack/__manifest__.py` ska lista `cron_tick` i `depends` före `'mail'`.
Ingen ny extern dependency — `cron_tick` ligger i `odoo-base`.

### R2 — cron.task-poster i data-XML
Ny fil `saltstack/data/cron_tasks.xml` med `<data noupdate="1">`:

| `technical_name` | `interval_number` | `interval_type` | `timeout_seconds` | seq |
|---|---|---|---|---|
| `minion_sync` | 24 | `hours` | 600 | 10 |
| `alert_count_backfill` | — | kadens `min` | — | 10 |
| `alert_auto_resolve` | — | kadens `min` | — | 20 |

`technical_name` är **unikt och får aldrig ändras i produktion** — det är
identiteten i `/cron/trigger/<technical_name>`.

`cadence` och `interval_type` är ömsesidigt uteslutande. Aldrig båda.

### R3 — pensionera ir.cron på plats
De gamla `ir.cron`-posterna ska **inte** raderas. Sätt `active=False` och
prefixa `name` med `[ERSATT]`. `model_id`, `code` och `interval_*` lämnas kvar
så att historiken och spårbarheten består.

**Notera:** `[ERSATT]`-mönstret fanns inte tidigare i kodbasen — det införs här.
Det finns inget automatiskt som tolkar prefixet; det är enbart en läsbar markör.

### R4 — webhook för diagnos
`saltstack_ai` ska trigga `POST /cron/trigger/salt_alert_diagnosis` från
`_schedule_diagnosis()`, men **bara** när den returnerar `True`:

- timeout 2 s
- `try/except` som bara loggar — får aldrig fälla anroparen
- svarar alltid HTTP 200

Skälet: diagnosens latens går från 1 min till 5 min (kadens `min`). Webhooken
kompenserar. **Steg 2 och steg 4 måste levereras i samma commit** — annars
finns ett fönster där diagnosen varken schemaläggs eller triggas.

### R5 — `_state_threshold_hours()` läser cron.task
`salt_minion.py:530` ska härleda tröskeln ur `minion_sync`-postens
`_interval_seconds() / 3600 * 1.5`, med fallback 24 h. Skyddad med
`if 'cron.task' in self.env` så modulen fungerar utan `cron_tick`.

Detta är en **regressionsvakt mot T/12164**: ändras intervallet ska tröskeln
följa med automatiskt i stället för att glida isär.

### R6 — ta bort `saltstack.cron.line`
Inställningsblocket för cron-rader ska bort helt:

| Fil | Rader |
|---|---|
| `models/res_config_settings.py` | 40-50, 98-99, 107-118, 139-158, 276-360 |
| `views/res_config_settings_views.xml` | 44-52, 99-126 |
| `security/ir_model_access.xml` | 91-99 |
| `i18n/sv.po` | 466, 1137 |

### R7 — menyer
- Ny meny **"Schemalagda jobb"** (seq 15) → `cron_tick.action_cron_task` med
  `search_default_group_module`.
- "Synka minions"-menyn → server action som wrapper `action_run_now()`.

### R8 — `odoo_cron_status` läser båda
`saltstack_ai/data/domain_tools.xml:60-83` ska rapportera både `ir.cron` och
`cron.task`. Annars ljuger AI-verktyget efter konverteringen.

## Acceptanskriterier

- [ ] `saltstack` installerar och uppgraderar på en ren databas (exit 0)
- [ ] Fyra `cron.task`-poster finns med rätt `technical_name`
- [ ] Fyra `ir.cron`-poster är `active=False` med `[ERSATT]`-prefix
- [ ] De två snapshot-jobben är orörda
- [ ] `POST /cron/trigger/salt_alert_diagnosis` triggar en körning
- [ ] `_state_threshold_hours()` följer `minion_sync`-intervallet
- [ ] `odoo_cron_status` listar båda tabellerna
- [ ] Nya tester gröna; antalet fel matchar baseline (7 pre-existerande)

## Utanför omfattning

- `SerializationFailure` på `salt_minion.open_alert_count` (eget spår)
- `saltstack.runlog` som körningshistorik för `cron.task`
- De två snapshot-cronjobben

## Blockerare — löst 2026-10-23

`saltstack` gick **inte** att installera eller uppgradera på någon databas:

```
ParseError: saltstack/views/salt_minion_views.xml:498
Unsearchable field "is_critical_role" in path "is_critical_role"
in domain of <filter name="critical_roles">
```

Orsak: commit `0214645` (T/12164) la till `is_critical_role` och `is_odoo_stale`
som `store=False` **utan** `search=`, men använde dem i sökvyns filter och
`group_by`. Odoo 18 avvisar det i `_validate_tag_filter` →
`_check_field_paths`, som kastar när `not field._description_searchable`
(`fields.py`: `_description_searchable = bool(self.store or self.search)`).

Åtgärd (commits `234bcba` + `21d2970`):

1. `search=`-metoder som översätter compute-uttrycken till exakta domäner
2. `group_by` på `is_critical_role` borttagen — ett icke-lagrat fält kan inte
   konverteras till SQL för gruppering
3. `_join_domains`-hjälpare som bygger **korrekta** prefix-domäner

### De tre domänfällorna (värda att minnas)

1. **`['|'] * (n-1) + leaves` är bara giltigt för n == 2.** Odoo-domäner är
   prefix-notation (polsk): en operator gäller de **nästa** n termerna. Med 17
   termer blir operatorantalet och nästningen fel → `Domain ... is
   syntactically not correct`.
2. **`['!'] + [A, B, C]` negerar bara `A`.** Konjunktionen måste bindas med
   `'&'` först, annars returnerar `= False` en tom mängd.
3. **`not ilike` kan aldrig uttrycka "rollen saknas" på en CSV-Char.**
   `'postgres,odoo' NOT ILIKE 'postgres'` är **sant** i SQL — delsträngen finns,
   men strängen är längre. Motsatsen måste byggas genom att negera hela den
   positiva domänen, inte varje rollnamn för sig.

Dessutom: `'not =ilike'` finns inte som operator (`ValueError: Invalid leaf`).

## Commit-ordning

1. Steg 0 — rätta det röda testet (`9dcf9ae`, klart)
2. Blockeraren — sökbara fält (`234bcba`, `21d2970`, klart)
3. `cron_tick` i `depends` + `cron_tasks.xml`
4. **Steg 2 + steg 4 i samma commit** (pensionering + webhook)
5. Steg 5 — `_state_threshold_hours()`
6. Steg 6 — ta bort `cron.line`
7. Steg 7 — menyer
8. Steg 8 — `odoo_cron_status`
9. Steg 9 — tester
