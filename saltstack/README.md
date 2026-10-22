# saltstack — SaltStack Infrastructure Base Module

Basmodul för att hantera SaltStack-infrastruktur från Odoo: minion-register,
pillar-ankare och felinjektion för att testa övervakningskedjan.

> **Repo-kontext**: Denna modul ligger i `/usr/share/odoo-saltstack/saltstack/`.
> Repot innehåller flera moduler; se `/usr/share/odoo-saltstack/README.md`.

## Modeller

### `salt.minion` — Salt Minion Registry

Det relationella navet — maskinregistret som andra moduler (helpdesk,
maintenance, mgmtsystem, keykeep) refererar när de kopplar driftdata till
infrastruktur.

**Nyckelfält:**
- `name` — Minion-ID (från Salt)
- `ip`, `os`, `os_version` — Systeminfo (från grains)
- `dc` — Datacenter (ska / sto)
- `roles` — Kommaseparerade roller (caddy, odoo, postgres, ...)
- `customer` — Kund (för containrar)
- `odoo_version` — Detekterad Odoo-version
- `is_container` / `host_machine` — LXD-containerstatus
- `is_online`, `last_seen`, `last_sync` — Status
- `grains_json` — Full grains-data från senaste synk

**Actions:**
- **Sync with Salt Master** — Hämta minions + grains från Salt API
- **Ping** — test.ping via Salt API
- **Felinjektion** (server actions, admin only):
  - 🛑 **Stoppa Odoo** — `systemctl stop odoo` (Zabbix larmar)
  - 💾 **Simulera disk full** — växande `/var/log/odoo/grow.log` (2000MB)
  - 🚨 **Simulera Wazuh-säkerhetshot** — failed SSH-logins till `auth.log`
  - 🧹 **Rensa fel** — ta bort grow.log + starta odoo

### `salt.pillar` — Salt Pillar Anchors

Nyckel/värde-metadata som pekar på Salt pillar-data. Värdena är live-data
lästa från Salt API — Odoo lagrar ankaret så andra moduler (t.ex. keykeep)
kan referera pillar-hemligheter.

**Nyckelfält:**
- `key` — Pillar-nyckel (t.ex. `postgres.version`)
- `value` — Pillar-värde (auto-detekterar typ)
- `data_type` — string, integer, boolean, float, dict, list, json, secret
- `pillar_file` — Käll-pillar-fil
- `minion_target` — Minion-mål

**Actions:**
- **Sync with Salt Master** — Upptäck pillar-nycklar från Salt API
- **Duplicate** — Kopiera med inkrementerad nyckel

### `salt.minion.storage` — lagring per artefakt

En rad per artefakt och minion. Typen är **artefakten**, inte var datan
råkar ligga — det är vad som gör Storage-fliken begriplig.

| Typ | Label | Lager | Vad den mäter |
|---|---|---|---|
| `lxd_container` | LXD-container | primär | Containerns rotvolym (använt + allokerat) |
| `disk` | Disk | primär | Diskutrymme utöver rotvolymen |
| `postgres` | Postgres | primär | Databasens storlek |
| `filestore_s3` | Filestore (S3) | primär | Odoos filestore i Garage-bucketen |
| `disk_borg` | Disk-backup (borg) | backup | Borg-arkiv av disk/container |
| `postgres_borg` | Postgres-backup | backup | Borg-arkiv av databasen |
| `filestore_restic` | Filestore-backup (restic) | backup | Restic-repo av filestore |

**Nyckelfält:**
- `bytes_used` — faktiskt använt utrymme (källan till sanningen)
- `bytes_allocated` — allokerat utrymme (kvot/diskstorlek)
- `size_gb` / `allocated_gb` — beräknade från bytes, för listkolumner
- `layer` — `primary` eller `backup`, beräknat från typen
- `is_measured` — `False` = estimat, aldrig presenterat som mätning
- `legacy_reference` — historisk rad som aldrig ingår i en summa

**Tvålagerssummering på minionen:**
- `storage_primary_gb` = container + disk + postgres + filestore
- `storage_backup_gb` = borg + restic
- `storage_total_gb` = primär + backup

Backup är en **kopia** av samma bytes. En Borg-backup av en container
innehåller containerns data — att summera dem skulle dubbelräkna kunden.
Därför adderas backup aldrig till primärsumman.

**Dirvish är inte en typ.** Dirvish är avvecklat (Borg + Restic ersatte det).
En typ som finns men inte får användas är en fälla, så den togs bort ur
selection. Befintliga Dirvish-rader migreras av `_auto_init` till
`disk_borg` + `legacy_reference=True`: storlek och mättid bevaras, men raden
ingår aldrig i en summa. Migreringen är idempotent och körs vid varje
uppgradering.

**Gamla typer mappas** av `LEGACY_TYPE_MAP` så att en stale anropare inte kan
återinföra en avvecklad typ:

```
lxd_host   → lxd_container
s3         → filestore_s3
s3_backup  → filestore_restic
dirvish    → disk_borg (legacy_reference)
```

## Menyer

```
SaltStack
├── Pillars              (synk med Salt Master)
├── Minions              (synk med Salt Master)
└── Konfiguration        (sista menyvalet)
    ├── Inställningar    (första menyvalet — SaltStack-settings)
    ├── Synka minions
    ├── Synka pillars
    └── Schemalagda jobb (cron_tick-registret, filtrerat på modulen)
```

## Schemalagda jobb — `cron_tick`, inte `ir.cron`

Modulen skapar **ingen aktiv `ir.cron`**. Schemat deklareras som `cron.task`
i `data/cron_tasks.xml` (och `saltstack_ai/data/diagnosis_cron.xml`) och körs
av motorn i `cron_tick`. Tre triggers, en körväg: tick-cronen, webhooken
`POST /cron/trigger/<technical_name>` och "Kör nu"-knappen.

| Jobb | `technical_name` | Frekvens | Motiv |
|---|---|---|---|
| Minionsynk + pillarsynk | `minion_sync` | intervall 24 h | Se D1 nedan |
| Backfill `open_alert_count` | `alert_count_backfill` | `min` (5 min) | Var 15:e minut räcker; 5 är billigt |
| Auto-resolve driftslarm | `alert_auto_resolve` | `min` (5 min) | Tidsokänsligt (7/14-dygn + live Zabbix) |
| AI-diagnos (i `saltstack_ai`) | `salt_alert_diagnosis` | `min` + webhook | Webhooken är primär väg, cron är säkerhetslina |

### Två jobb ligger medvetet kvar i `ir.cron`

| Jobb | Varför |
|---|---|
| `cron_sync_snapshots` (restic) | Ordningsgarantin mot restic-statusrapporten 06:15 kan inte uttryckas. `nextcall` 06:30 är den enda garantin för färskt innehåll — kadens `daily`/`week` ger 06:00, alltså femton minuter **före** rapporten, och läsaren kontrollerar inte filens ålder. Se `data/sync_snapshot_cron.xml`. |
| `cron_sync_dirvish_snapshots` | Ska **avvecklas**, inte konverteras. Dirvish är ersatt av Borg + Restic. Avvecklingen ägs av ändringen `minio-snapshot-tab`, som också äger ordningsfrågan (MinIO-synken måste finnas innan Dirvish släcks). |

⚠ **Städa inte bort dem.** Specen `scheduled-jobs` kräver att skälet står
bredvid jobbet; testerna i `tests/test_cron_tick_migration.py` räknar upp dem
som dokumenterade undantag.

### D1 — varför `minion_sync` använder intervall, inte kadens

`cadence='daily'` är `last_run.date() < now().date()` **och**
`now().minute >= offset`, där `offset = sha256(technical_name) % 60`. För
`minion_sync` blir offset **22**, och `cron_daily` tickar en gång i timmen →
jobbet landar ~**01:22**, inne i Borg-fönstret 01:00–03:00.

`interval 24h` bevarar dagens beteende exakt (den gamla `ir.cron` var också
`interval 24h`) utan spridning. Enda skillnaden: intervallet mäts från
körningens **slut** i stället för dess start — försumbart för ett jobb som tar
~3 minuter. Vid en missad körning glider schemat, men det syns nu i
`is_overdue` i stället för att vara tyst.

`cadence='week'` förkastades: en veckovis synk fryser `last_seen`-semafonen i
sju dygn och registret skulle visa hela flottan som offline.

### Intervallet ändras i registret — inte i Inställningar

`saltstack.sync_interval_number` och `_update_sync_cron()` är **borttagna**
(2026-10-22). En inställningssparning skrev tidigare `interval_number` rakt på
en `ir.cron`: produktionsschemat blev ändringsbart från ett formulär, utan
spår och utan validering. Intervallet sätts nu i **Konfiguration →
Schemalagda jobb**, där ändringen loggas och där körhistorik, felantal, lås
och överskriden deadline syns.

Inställningar har en knapp som öppnar samma register.

### Vad konverteringen INTE löser

`psycopg2.errors.SerializationFailure` på `salt_minion.open_alert_count` är
ett **samtidighetsproblem**, inte ett schemaproblem. Fyra skrivare finns
(driftlarm-webhook, auto-resolve, backfill, synkens `_apply_grains`) under
repeatable read. `cron_tick`s lås är per task och hindrar bara en andra
*synk*. Ingen rad i den koden ändras av den här ändringen.

### `max_failures` — ny beteendeförändring

`cron.task` avaktiverar sig själv efter 10 konsekutiva fel. En `ir.cron` gör
aldrig det. Det är avsiktligt (ett jobb som ständigt kraschar ska sluta
försöka), men det betyder att ett konverterat jobb kan stängas av — synligt i
chatter, `last_error` och `active=False`, aldrig tyst.


## Inställningar

Konfigurera i **SaltStack → Konfiguration → Inställningar** — fliken
**SaltStack &amp; Zabbix**:

| Inställning | Syfte |
|-------------|-------|
| Salt API URL | t.ex. `http://192.168.11.22:8377` |
| Salt API Token | Auth-token för Salt API |
| Salt Auth Method | `token` eller `keykeep` (keykeep-brygga) |
| Zabbix API URL | t.ex. `https://zabbix.vertel.se` |
| Zabbix API Token | Auth-token för Zabbix API |
| Zabbix Auth Method | `token` eller `keykeep` |

⚠ **Synkintervallet finns inte längre här** (borttaget 2026-10-22). Det sätts
i **Konfiguration → Schemalagda jobb**. Inställningar har en knapp som öppnar
registret. Se avsnittet "Schemalagda jobb" ovan.

## Installation

```bash
# Repot ligger i addons_path (ingen symlink):
# /etc/odoo/odoo.conf → addons_path = /usr/share/odoo-saltstack, ...

sudo checkmodule -d <db> -m saltstack
```

## Användning

1. **Inställningar** — konfigurera Salt API URL + token
2. **Minions → Sync with Salt Master** — populera registret
3. **Pillars → Sync with Salt Master** — upptäck pillar-nycklar
4. Testa övervakningskedjan med felinjektion:
   - Stoppa Odoo → Zabbix larmar → AI-agenten undersöker
   - Växande logg → disk full → AI-agenten rensar
   - Wazuh brute-force → SIEM-kedja → AI-agenten svarar

## Datumgruppering — varför ordningen sitter i modellen

`saltstack.alert` och `saltstack.runlog` har båda en `_read_group`-override
som sätter `order = '<term> desc nulls last'` för `timestamp`-granulariteter
när ingen explicit `order` angetts.

**Ta inte bort den som "överflödig" — den behövs.** Odoo faller tillbaka på
**stigande** ordning på grupptermen när `order` är tom
(`_read_group_orderby`), så dagsgrupperna hamnar äldst först medan posterna
inuti varje grupp är yngst först. Sökvyn kan inte uttrycka riktningen:
`context="{'group_by': ...}"` bär ingen ordning, och webbklienten skickar
bara `__count` från Asc/Desc-knappen. Det finns ingen XML-nyckel för det.

Att lägga ordningen i modellen (i stället för i vyn) ger tre saker:
den gäller list-, kanban-, `web_read_group`- och exportvägar; den kan inte
glida tillbaka vid en framtida vyredigering; och den kräver ingen sparad
favorit per användare.

`nulls last` är avsiktligt: PostgreSQL sorterar `NULL` **först** under
`DESC`, så utan det hamnar gruppen för poster utan `timestamp` ovanför den
senaste dagen.

En explicit `order` från användaren vinner alltid — villkoret är
`order is None`. Regressionstester: `tests/test_read_group_order.py`.

## Version

18.0.1.0.0 — Odoo 18

## Licens

AGPL-3 — Copyright (C) 2026 Vertel Sverige AB
