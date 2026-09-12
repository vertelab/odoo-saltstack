# odoo-saltstack — SaltStack for Odoo

Odoo-moduler för att hantera SaltStack-infrastruktur. Repot innehåller flera
oberoende Odoo-moduler; varje modul ligger i en egen undermapp.

## Moduler

| Modul | Katalog | Beskrivning | Beroenden |
|-------|---------|-------------|-----------|
| **saltstack** | `saltstack/` | Basmodul: minion-register, pillar-ankare, felinjektion, inställningar | `base`, `mail` |
| **saltstack_ai** | `saltstack_ai/` | AI-brygga: SaltAPI-klient, tools, skills | `saltstack`, `ai_agent_core`, `mail` |
| **saltstack_zabbix** | `saltstack_zabbix/` | Zabbix-anslutning: settings + test-knapp | `saltstack_ai` |
| **saltstack_wazuh** | `saltstack_wazuh/` | Wazuh/SIEM-anslutning: settings + test-knapp | `saltstack_ai` |
| **saltstack_zabbix_keykeep** | `saltstack_zabbix_keykeep/` | Keykeep Managed för Zabbix | `saltstack_zabbix`, `keykeep` |
| **saltstack_wazuh_keykeep** | `saltstack_wazuh_keykeep/` | Keykeep Managed för Wazuh | `saltstack_wazuh`, `keykeep` |
| **saltstack_keykeep** | `saltstack_keykeep/` | Synka pillar-hemligheter till Keykeep | `saltstack`, `keykeep` |
| **saltstack_helpdesk** | `saltstack_helpdesk/` | Skapa helpdesk-ärenden från incidenter | `saltstack_ai`, `helpdesk_mgmt` |
| **saltstack_maintenance** | `saltstack_maintenance/` | Notifiera underhållssystem | `saltstack_ai`, `base_maintenance` |
| **saltstack_managementsystem** | `saltstack_managementsystem/` | Dokumentera avvikelser som nonconformity | `saltstack_ai`, `mgmtsystem_nonconformity` |

## Installation

Lägg repot i Odoos `addons_path` — ingen symlink behövs:

```ini
# /etc/odoo/odoo.conf
addons_path = /usr/share/odoo-saltstack, ...
```

Uppdatera sedan modulerna:

```bash
sudo checkmodule -d <db> -m saltstack            # basmodul
sudo checkmodule -d <db> -m saltstack_ai         # AI-brygga
```

## Basmodulen `saltstack`

Se `saltstack/README.md` för full dokumentation.

**Menyer:**
```
SaltStack
├── Pillars              (synk med Salt Master)
├── Minions              (synk med Salt Master)
└── Konfiguration        (sista menyvalet)
    └── Inställningar    (första menyvalet — SaltStack-settings)
```

**Felinjektion** (server actions på minion, admin only):
- 🛑 Stoppa Odoo
- 💾 Simulera disk full (växande loggfil)
- 🚨 Simulera Wazuh-säkerhetshot (brute force i auth.log)
- 🧹 Rensa felinjektioner

**End-to-end-simulering** (server actions på minion, admin only):
- 🧪 Simulera full larmkedja (stoppa odoo) — injicerar felet, bygger
  webhook-payload, anropar `/saltstack/alert` internt, startar AI-diagnos
- 🧪 Simulera full larmkedja (disk full)
- 🧪 Simulera full larmkedja (Wazuh brute-force)

## AI-diagnos & auto-fix (saltstack + saltstack_ai)

När ett larm kommer in via `/saltstack/alert` startar Infrastructure Operator
coworkern en diagnos. Denna ändring (2026-08-12) tillförde:

- **Chatter/writeback** — diagnosstart, resultat och åtgärd postas på
  `saltstack.alert`-posten (via mail.thread), sammanfattning på den berörda
  `salt.minion`-posten (med klickbar länk till alerten), och åtgärdsplan i
  Driftlarm-kanalen vid severity ≥ 12.
- **Auto-fix-taxonomi** i diagnos-prompten: säkra fel åtgärdas automatiskt
  (odoo/postfix/dovecot nere → starta tjänsten, grow.log + disk > 85 % → ta
  bort filen, Caddy 502 → kolla upstream först, sedan reload, PostgreSQL nere
  på replica → starta, ALDRIG primary). Övriga (OOM, CPU-spikar) dokumenteras
  bara → helpdesk-ticket/nonconformity.
- **Odoo ORM-verktyg** från ai_agent_core (`describe_model`, `odoo_search`,
  `odoo_write`, `odoo_call_method`, `odoo_create`, `odoo_unlink`,
  `okf_search`) är tilldelade Infrastructure Operator — AI:n kan svara på
  frågor som "Hur många minioner kör Odoo 18?" och skriva tillbaka på poster.
- **Zabbix Troubleshooting-skill** (5-stegs metodik: problem → trigger →
  item-historik → korrelation → Salt-mappning) bunden till coworkern.
- **Helpdesk/nonconformity-verktyg** (`create_helpdesk_ticket` i
  saltstack_helpdesk, `document_nonconformity` i saltstack_managementsystem)
  binds till coworkern när respektive modul installeras.
- **Källspecifik avstängning av AI-diagnos** (2026-09-12): bas-modulen
  anropar `_auto_diagnose_enabled_for_source()` i `process_webhook` — en hook
  som bryggmoduler kan overrida för sin egen källa. `saltstack_zabbix`
  använder den: inställningen **"Auto diagnosis on Zabbix alerts"**
  (`zabbix.alert.auto_diagnose`, default på) styr om Zabbix-larm ska
  AI-diagnostiseras. När den är av registreras, dedupas, korreleras och
  notifieras larmet som vanligt — bara diagnossteget hoppas över. Andra
  källor (Wazuh, Shuffle) påverkas inte. Kräver att den globala
  "Auto diagnosis on alert" också är på.

### Boolean-inställningar: explicit 'True'/'False' (2026-09-12)

**Buggen:** `ir.config_parameter.set_param(key, False)` **raderar** raden,
varefter `get_param()` faller tillbaka på fältets default. För en
boolean-inställning med `default=True` innebar det att kryssrutan **inte gick
att bocka av** — den studsade tillbaka till på. Gällde både
`zabbix.alert.auto_diagnose`, `saltstack.alert.auto_diagnose` och
`saltstack.alert.webhook_enabled`.

**Fixen:** `set_values()` skriver ett **explicit** `'True'`/`'False'` som
sträng, och läsningen (`_auto_diagnose_enabled`, `_auto_diagnose_enabled_for_source`)
använder `_get_param()` (returnerar `None` när raden saknas) så att
"aldrig konfigurerad" = fältets default (på).

**Tester:** `saltstack_zabbix/tests/test_auto_diagnose.py`
(`test_setting_can_be_turned_off_and_sticks`, `test_missing_param_reads_as_on`).

**Tester:** `saltstack/tests/test_salt_alert.py` (chatter, prompt, simulering)
+ `saltstack_ai/tests/test_tool_access.py` (ORM-tools, skills)
+ `saltstack_zabbix/tests/test_auto_diagnose.py` (källspecifik diagnos-gate).

```bash
sudo checkmodule -d ledningssystem -m saltstack,saltstack_ai -t
```

## Konventioner

- **Rena Odoo-repon** — varje modul i egen undermapp, addons_path direkt mot repot
- **Inga symlinks** i Odoo-repon
- **Inga pip-install i Odoo-repon** — använd `--break-system-packages` på Ubuntu 24.04+ vid behov, aldrig i repo:t
- Alla hemligheter i pillar/keykeep, aldrig i kod

## OpenSpec

Proposals och specar ligger i `openspec/`:

```bash
openspec list --json
```

## Licens

AGPL-3 — Copyright (C) 2026 Vertel Sverige AB

## Minion-registret (salt.minion) — v18.0.1.26.0

- **Logga**: visas till vänster om namnet i formuläret (Odoo 18 `.oe_avatar`
  floatar höger — överrids med `style="float:none"`).
- **IDENTITY**: `private_ip`, `public_ip`, `external_domain` (t.ex.
  svenskfast.azzar.org), `has_gateway`, `os`, `dc`, `roles`.
- **Odoo-fliken** (visas för Odoo-minioner): Odoo-version, Odoo-databaser
  (komma-separerad, `(demo)` efter db-namn vid demodata), antal användare,
  senaste inloggning, antal AI-medarbetare + M systemtokens (1M bas per
  medarbetare + extra budgeterat via `monthly_cap_mtokens`).
- **Kund** (partner_id): sätts från `customer`-grain, fallback till
  minion-namn-baserad `res.partner`-lookup (`_search_partner_by_name`).
- **Kugg menyn**: server action "Uppdatera grunduppgifter"
  (`action_update_basic_info`) — grains-sync när fält saknas/är gamla +
  Odoo-statistik (bash via Salt API, läser `db_*` ur odoo.conf, kör som
  root med PGPASSWORD).
- `active` visas inte som fält — inaktiva minioner markeras med ribbon
  "Inaktiv".

## saltstack_keykeep (v18.0.1.0.4)

- `keykeep.credential.minion_id` — kopplar nycklar till minionen (sätts vid
  pillar-sync och masternyckel-hämtning).
- **Smartknapp "Keykeep-nycklar"** på minion-formuläret: räknare + öppnar
  minionens keykeep-credentials.
- **"Hämta Keykeep masternyckel"**: läser `keykeep_encryption_key` ur
  minionens odoo.conf och valvar den som keykeep.credential (krypterad,
  auditerad) — en av nycklarna är masternyckeln.
- **"Deploy keykeep key"**: kör `state.apply keykeep` på minionen (Salt
  state `/srv/salt/keykeep/init.sls`) → genererar Fernet-nyckeln idempotent
  (bara om den saknas), skriver till odoo.conf, startar om Odoo.

## Storage-fliken (v18.0.1.27.0)

`salt.minion.storage` — fyra rader per minion + summa:

| Rad | Källa | Metod |
|-----|-------|-------|
| **LXD-host** | container på LXD-hosten | `du -sb <pool>/containers/<minion>` på hosten (dir-pool, t.ex. `/var/lib/lxd_ext4` på fors). Host detekteras + cachas (`lxd_host`, `saltstorage.lxd_hosts`). |
| **Dirvish** | backup-trädet `/srv/backup/<host>` | Dirvish backar HELA hosts → per-minion-andel = minion-LXD × kvot(dirvish/LXD). Kvoten beräknas med bakgrunds-du (nohup) + cachas (`saltstorage.dirvish_ratio.<host>`). |
| **S3 (Garage)** | kundens src-bucket | `restic-status.json` på restic-minionen (`customers[].size`) |
| **S3-backup** | restic-backup-bucket | `restic-status.json` (`customers[].backup_size`) |

- "Mät storage"-knapp + gear server action (`action_measure_storage`).
- Rader är redigerbara manuellt i fliken.
- **res.partner**: smartknapp "Minioner" → listar kundens minioner.
- Minion-listan: IP-kopiera-ikon (`saltstack_copy_value`), domän-öppna-ikon
  (`act_url` ny flik), dc, lxd-host, storage-total, odoo-användare, 1M systemtokens.
- `lxc.list`-Salt-modulen saknas och bulk-target (L@) är opålitlig via
  netapi → sekventiell `cmd.run`-probing med korta timeouts + caching.
