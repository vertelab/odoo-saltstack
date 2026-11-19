## Purpose

Insamling av lagringsmätningar per minion: vilka artefakter som mäts, hur en
enskild minions fel isoleras från resten av svepet, och vilka fält insamlingen
äger rätt att skriva. Capabilityn beskriver det observerbara kontraktet för
`storage_collect` — att ett svep antingen fullbordas med mätdata eller
rapporterar ett fel som går att se i körningshistoriken.

## ADDED Requirements

### Requirement: Svepet ska isolera fel per minion

Ett insamlingssvep ska köras mot varje aktiv minion i tur och ordning, och ett
fel för en minion får inte påverka mätningen av nästa. Svepet ska fullbordas
och rapportera hur många minions som mättes respektive failade.

Felet ska fångas på ett sådant sätt att transaktionen förblir användbar —
isoleringen får inte själv kasta ett nytt fel.

#### Scenario: En minion svarar inte

- **WHEN** `storage.info` för en minion returnerar ett fel elleringen data
- **THEN** ska den minionen räknas som failed
- **AND** ska svepet fortsätta med nästa minion
- **AND** ska transaktionen förbli användbar så att `last_run` kan skrivas

#### Scenario: Alla minions svarar

- **WHEN** varje `storage.info`-anrop returnerar ett payload
- **THEN** ska svepet rapportera `measured` = antalet minions
- **AND** ska `failed` vara tom
- **AND** ska `last_run` sättas

#### Scenario: Ett skrivfel uppstår för en minion

- **WHEN** en skrivning för en enskild minion nekas av databasen
- **THEN** ska felet begränsas till den minionen
- **AND** ska svepet inte avbrytas
- **AND** ska efterföljande minions mätas normalt

### Requirement: Insamlingen ska inte konkurrera om minionsraden

Lagringsmätningen ska inte skriva de fält som tillhör minionsynkroniseringen.
Ett svep ska kunna köras parallellt med en synkronisering utan att någon av dem
blockeras av den andras radlås.

#### Scenario: Synkronisering håller radlåset

- **WHEN** en minionsynkronisering pågår och håller radlåset på `salt.minion`
- **THEN** ska insamlingssvepet kunna slutföras utan att blockeras
- **AND** ska mätningarna sparas
- **AND** ska inga `lock timeout`-fel uppstå i svepet

#### Scenario: Insamlingen anropas från synkroniseringen

- **WHEN** insamlingen anropas inifrån synkroniseringen, som redan äger raden
- **THEN** får insamlingen skriva de sammanställda nyckeltalen
- **AND** ska det ske utan ytterligare låstagning

### Requirement: Svepet ska ha en tidsbudget per minion

Varje minions mätning ska ha en övre gräns för hur länge den får vänta på lås.
En minion som inte kan mätas inom budgeten ska räknas som failed, inte stoppa
svepet.

#### Scenario: En minion överskrider tidsbudgeten

- **WHEN** en mätning för en minion tar längre tid än den tillåtna budgeten
- **THEN** ska den minionen räknas som failed
- **AND** ska svepet fortsätta

### Requirement: Körningshistoriken ska visa utfallet

Ett svep som failar ska synas som ett misslyckande i körningshistoriken, inte
som en uppgift som aldrig körts.

#### Scenario: Svepet failar

- **WHEN** svepet avslutas med minst en failed minion men utan att kasta
- **THEN** ska `last_run` sättas
- **AND** ska `last_duration_ms` sättas
- **AND** ska `failure_count` nollställas, eftersom svepet genomfördes

#### Scenario: Svepet kastar ett fel

- **WHEN** svepet kastar ett fel som inte kunde isoleras
- **THEN** ska `last_run` ändå sättas om transaktionen är användbar
- **AND** ska felet framgå av körningsloggen
