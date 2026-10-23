# Utvecklingsmiljö — jakob18 vs ledningssystem

**Regel: `ledningssystem` är produktion. `jakob18` (den här maskinen) är fri.**

Det här dokumentet finns för att skillnaden kostade tid 2026-10-23: en trasig vy
i `saltstack` blockerade all utveckling, och lösningen var att sluta behandla
utvecklingsdatorn som produktion.

## De två miljöerna

| | `jakob18` (utvecklingsdatorn) | `ledningssystem` (produktion) |
|---|---|---|
| Roll | Fri spelplan | Skarp drift |
| Odoo-tjänst | Får startas om fritt | `Restart=on-failure` — **maska aldrig** |
| Databaser | `jakob18`, `cm_test_*`, `pi_*`, `test_*` | `ledningssystem` |
| addons_path | ~132 `/usr/share/odoo-*` + core | Samma, men bara det som behövs |
| Får gå sönder | Ja — det är poängen | Aldrig |
| Deploy | `git pull` + `checkmodule` | `git pull` + `checkmodule --multi-user` |

## Vad "fritt fram" betyder på jakob18

- Testdatabaser får skapas, droppas och fyllas med skräp.
- `systemctl restart odoo` är tillåtet.
- En trasig modul får fälla registret — det är ett testresultat, inte en incident.
- Man får peka `addons_path` mot en isolerad katalog och hoppa över allt skräp.

## Vad som INTE är fritt även på jakob18

- `/usr/share/odoo-fortnox`, `/usr/share/odoo-payroll` m.fl. är **andra repon
  och andra kunders moduler**. Ändra dem inte som en bieffekt av eget arbete —
  det kräver ett eget beslut.
- Delade resurser: `postgres16.vertel.se`, Salt-mastern, Zabbix, Wazuh.
  En testkörning får inte skriva dit.
- Git-pushar till `origin/18.0` — de når produktion vid nästa deploy.

## Fällan: en trasig modul i `addons_path` fäller HELA registret

`addons_path` på jakob18 innehåller ~132 kataloger. Odoo validerar **alla**
manifest vid registry-load, inte bara de installerade modulerna. Ett enda
ogiltigt manifestversion (`'version': '14.0.0.0.1'`) räcker för att
`ValueError: Invalid version` ska fälla hela uppstarten.

Konsekvens: en `systemctl restart odoo` på jakob18 kan fallera av ett skäl som
inte har med ditt arbete att göra. 2026-10-23 fanns **86 ogiltiga manifest**
i `addons_path` (mest `odoo-fortnox`, `odoo-payroll`, `odoo-imagemagick`,
`jitsi_meet`, `odoo-recruitment`).

### Lösningen: isolerad addons_path

Bygg en minimal katalog med bara det du testar, och peka Odoo dit:

```bash
mkdir -p ~/pi-test-addons
for m in base web mail html_editor hr base_automation \
         cron_tick ai_agent_core web_pwa_customize web_pwa_push \
         saltstack saltstack_ai; do
  ln -sfn /usr/share/odoo-*/$m ~/pi-test-addons/$m 2>/dev/null
done
chmod o+rx ~                     # Odoo kör som annan användare
```

Då slipper du `odoo-fortnox` helt. Det är den snabbaste vägen till en grön
baseline — men det **döljer** risken i produktion, så den måste hanteras separat.

## Testdatabas — komplett recept

```bash
# 1. Roll (engångs)
sudo -u postgres psql -c "CREATE ROLE pi_test LOGIN SUPERUSER PASSWORD 'pi_test_pw';"

# 2. Databas + pgvector (krävs av ai_agent_core — måste skapas INNAN Odoo)
createdb -h localhost -U pi_test pi_cron_tick_test
psql -h localhost -U pi_test -d pi_cron_tick_test -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 3. Installera + kör tester
odoo -d pi_cron_tick_test \
  --addons-path=$HOME/pi-test-addons \
  --db_host=localhost --db_port=5432 \
  --db_user=pi_test --db_password=pi_test_pw \
  --data-dir=$HOME/pi-test-data \
  -i saltstack,saltstack_ai --stop-after-init --without-demo=all \
  --test-enable --test-tags=/saltstack \
  --log-level=test --http-port=8199
```

### Fällor i receptet

| Fälla | Symptom | Fix |
|---|---|---|
| `--data-dir` saknas | `PermissionError: '/home/krajak/.local/share'` | sätt `--data-dir` |
| `~krajak` inte `o+rx` | Odoo kan inte läsa data-dir | `chmod o+rx ~` |
| Port 8069 upptagen | `Address already in use` | `--http-port=8199` |
| pgvector saknas | `type "vector" does not exist` | `CREATE EXTENSION` **före** Odoo startar |
| `CREATE EXTENSION` i fel DB | samma fel | kör i testdatabasen, inte `postgres` |
| `ag_catalog` saknas | varningar från `ai_agent_core` | ofarligt, graph-stöd ej installerat |
| `.pyc`-cache | nya tester hittas inte | `find … -name __pycache__ -exec rm -rf {} +` |
| Testklass fel | tester körs inte | `--test-tags=/<modul>:<Klass>` |

## Baselines — vad som är grönt

Kör alltid en baseline på **orört HEAD** innan du rapporterar "mina tester är
gröna". `git stash` ditt arbete, kör, jämför siffrorna.

Per 2026-10-23, `saltstack` + `saltstack_ai` på jakob18:

- **52 tester, 7 fel** på orört HEAD — alla i `test_salt_alert`
  (`TestAlertWebhookGround`, `TestAlertDedup`). De är pre-existerande och
  oberoende av cron-arbetet.
- Efter T/12316-fixen: **58 tester, 7 fel** — samma 7, plus 6 nya gröna.

Tumregel: **jämför mot baseline, inte mot noll.** "0 failed" är inte målet om
baseline aldrig var 0.

## Produktion — det som gäller på ledningssystem

- `checkmodule --multi-user` — parallell Odoo på port 4444–4499, **ingen nedtid**.
  Utan flaggan startas tjänsten om och besökare får 502.
- **Versionen MÅSTE bumpas** i `__manifest__.py`, annars hoppar `--update` över
  modulen trots att koden ändrats.
- Decoy-DB: använd alltid `-h postgres16.vertel.se -U ledningssystem -d ledningssystem`.
  Den lokala postgresen har en föråldrad DB med samma namn.
- Maska aldrig `odoo`-tjänsten (`Restart=on-failure` → permanent driftstopp).

## Lärdom

Att behandla utvecklingsdatorn som produktion kostade en hel arbetssession:
först letade vi fel i `odoo-fortnox` (som inte var problemet), sedan byggde vi
en isolerad miljö för att komma runt det, och väl där visade sig den verkliga
buggen vara vår egen — en `store=False` utan `search=` som fällde
vyvalideringen. **Den isolerade miljön var rätt svar; att tveka om den var
misstaget.**
