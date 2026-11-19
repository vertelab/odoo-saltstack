## 1. Kodändringar

- [x] 1.1 Ge `_collect_storage_measurements()` parametern `write_odoo_stats=True` och låt `_apply_odoo_stats(payload.get('odoo_stats'))` bara anropas när den är sann; verifiera att `grep -n "write_odoo_stats" saltstack_storage/models/salt_minion.py` visar både signaturen och det villkorade anropet
- [x] 1.2 Anropa `_collect_storage_measurements(write_odoo_stats=False)` från `_cron_collect_all()`; verifiera att `grep -n "write_odoo_stats=False" saltstack_storage/models/storage_measurement.py` ger träff
- [x] 1.3 Byt `self.env.invalidate_all()` mot `self.env['saltstack.storage.measurement'].invalidate_model()` i `_cron_collect_all()`s `except`-gren; verifiera att `grep -n "invalidate_all" saltstack_storage/models/storage_measurement.py` inte längre ger träff i `_cron_collect_all`
- [x] 1.4 Sätt `SET LOCAL lock_timeout = '2s'` innanför varje minions savepoint i `_cron_collect_all()` (per D4) och verifiera att `grep -n "lock_timeout" saltstack_storage/models/storage_measurement.py` ger träff
- [x] 1.5 Kommentera de tre ändringarna med daterade fynd (samma stil som `salt_minion.py:245`) så nästa läsare ser varför; verifiera med `grep -c "2026-11-13" saltstack_storage/models/*.py`

## 2. Tester

- [x] 2.1 Utöka `saltstack_storage/tests/test_storage_measurement.py` med ett test som anropar `_cron_collect_all()` med en minion vars `_collect_storage_measurements` kastar, och verifiera att returvärdet har `failed` med den minionen samt att `measured` räknar resten
- [x] 2.2 Lägg ett test som verifierar att `_cron_collect_all()` inte skriver `odoo_*`-fälten på `salt.minion`; verifiera genom att jämföra `odoo_stats_synced` före och efter
- [x] 2.3 Kör sviten och verifiera att antalet fel matchar baseline (7 pre-existerande, se `convert-saltstack-crons-to-cron-tick/proposal.md`); spara utskriften till fil eftersom `checkmodule -t` bara skriver resultat med `-l test`

## 3. Versionsbump och uppgradering

- [x] 3.1 Bumpa `saltstack_storage/__manifest__.py` `18.0.1.1.5` → `18.0.1.1.6`; verifiera med `grep version saltstack_storage/__manifest__.py`
- [x] 3.2 Kör `checkmodule saltstack_storage` i **multi-user** (klockan är inom 07–18) och verifiera exit 0 utan `Missing model` eller `ParseError` i utskriften

## 4. Verifiering i drift

- [x] 4.1 Trigga `POST /cron/trigger/storage_collect` med Bearer-token och verifiera HTTP 200 samt en ny rad i `cron.task.log` med `status='ok'`
- [x] 4.2 Verifiera att `cron_task.last_run` för id 3 inte längre är NULL: `SELECT last_run, last_duration_ms, failure_count FROM cron_task WHERE id=3`
- [x] 4.3 Verifiera att nya mätrader tillkommit: `SELECT max(measured_at), count(*) FROM saltstack_storage_measurement` jämfört med 2026-11-13 före körning (208 rader, max 2026-10-29 13:12)
- [x] 4.4 Verifiera att loggen saknar `Storage collection failed for … lock timeout` efter körningen: `grep -c "Storage collection failed" /var/log/odoo/odoo-server.log` ska vara 0 för dagens tidsstämplar
- [ ] 4.5 Låt nästa schemalagda svep gå och verifiera att `last_run` flyttas fram och att inga `lock timeout`-rader uppstår (verifieras vid nästa `daily`-tick)

## 5. Dokumentation

- [x] 5.1 Uppdatera `saltstack_storage/README.md` med att svepet inte skriver `odoo_*`-fälten (D1) och hänvisa till denna change
- [x] 5.2 Committa i `vertelab/odoo-saltstack` på branch `18.0` med engelsk commit-text som namnger rotorsaken (`lock_timeout` på `salt_minion` + `invalidate_all`) och verifiera med `git log --oneline -1`
