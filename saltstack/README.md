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

## Menyer

```
SaltStack
├── Pillars              (synk med Salt Master)
├── Minions              (synk med Salt Master)
└── Konfiguration        (sista menyvalet)
    └── Inställningar    (första menyvalet — SaltStack-settings)
```

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
