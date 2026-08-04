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
