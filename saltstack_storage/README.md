# SaltStack Storage Measurement

Measures storage per minion/container across the artefacts that actually
exist in the fleet, and summarises it per customer in two layers so the same
bytes are never counted twice.

## Artefacts

| Artefact | What is measured | `bytes_used` | `bytes_secondary` |
|---|---|---|---|
| `lxd_container` | LXD container root + extra volumes | `du -sbx /` inside the container | — |
| `postgres_db` | Odoo database on the **actual** `db_host` | `pg_database_size()` | — |
| `filestore_s3` | Odoo attachments + Garage bucket | recorded `ir_attachment.file_size` | actual bucket size (deviation) |
| `borg_db` | Borg archive of the database | compressed archive size | uncompressed source |
| `borg_container` | Borg archive of the container | compressed archive size | uncompressed source |
| `restic_filestore` | Restic repository per bucket | repository total | — |

**Dirvish is not an artefact.** It is retired (Borg + Restic replaced it).
The `artifact` field is an allowlist, so a Dirvish value cannot enter a sum
by accident. Imported historical values go in `legacy_reference`, which is
never summed.

## One collection, three payloads

`storage.info` returns everything the minion can tell us, because the parts
come from the same `odoo.conf` and the same database listing. Collecting
them separately meant reading the same files twice — and gave two chances
to drift apart.

| Key | Destination | What |
|---|---|---|
| `lxd_container`, `postgres_db`, `filestore_s3`, `borg_*`, `restic_filestore` | `saltstack.storage.measurement` | Storage volumes with history |
| `lxd_resources` | (response only, for now) | CPU/RAM settings + live usage |
| `odoo_stats` | `salt.minion.odoo_*` | Version, users, coworkers, tokens, demo, last login |

### `lxd_resources` — settings vs usage

Two different things that must not be confused:

- **Settings** come from `expanded_config`, NOT `config`. A container
  inherits its limits from its profile, so `lxc config show <c>` returns an
  empty config for almost every container in this fleet.
- **Usage** comes from `/1.0/instances/<c>/state`.

`limits.cpu` is usually absent, which means "all host CPUs". The response
reports the host's core count **and** `cpu_configured=False`, so a reader
never mistakes an inherited limit for a configured one.

```json
{"container": "ledningssystem", "cpu_cores": 48, "cpu_configured": false,
 "memory_limit": 10737418240, "memory_used": 7853219840,
 "memory_peak": 0, "cpu_time_ns": 7509365929000, "host_cores": 48}
```

### `odoo_stats` — only the instance's own databases

A shared PostgreSQL server holds 80+ databases for different customers.
`_target_databases()` reads `db_name` from `odoo.conf` and measures only
those — measuring all of them from every Odoo minion would report the same
numbers everywhere and take minutes.

### Two silent failures found while building this

1. **`_run()` did not pass `env` to `subprocess.run`.** `_psql()` built
   `PGPASSWORD` and dropped it, so every query returned `''` with no error.
2. **`_odoo_databases()` queried the `postgres` maintenance database.** An
   Odoo role (`ledningssystem`) owns its database but has no CONNECT on
   `postgres`, so the listing came back empty. It now connects to `db_name`
   first and falls back to `postgres`.

Both are the same class of bug: an empty result that looks like "no data"
instead of an error.

## Method: measured vs estimated

Every row carries `method`:

- `measured` — straight from a measuring tool.
- `estimated` — derived. Shown as such in every view; never presented as a
  measurement.

Borg archive sizes are `estimated` when they come from the repository-total
share (see below), because deduplication makes archive sizes non-additive.

## Two-layer summary — why backup is never added to primary data

A Borg archive of a container **contains the container's bytes**. Summing
them together double-counts the customer. The summary therefore has two
independent layers:

- **primary** = `lxd_container` + `postgres_db` + `filestore_s3`
- **backup**  = `borg_db` + `borg_container` + `restic_filestore`

Total = primary + backup, always labelled. Missing artefacts are reported as
missing — never as 0 in a total that pretends to be complete.

The S3 bucket size is a **deviation** against the recorded attachment size,
not a summand: the bucket may hold other data, and the recorded size is the
customer's data.

## Borg performance — the expensive part

`borg list --json` returns archive **names only** (no sizes). Per-archive
sizes require `borg info <repo>::<archive>`, which:

- takes **75–120 s per archive** (measured on tull1/fors), and
- takes an **exclusive repository lock** that collides with the nightly
  backup.

Running that for 1129 archives took **2 h 20 min** and held the lock the
whole time.

### The two-tier strategy

1. **Repository total** — `borg info <repo>` *without* an archive name.
   ~0.2 s, gives `cache.stats.unique_csize` (the deduplicated size the repo
   actually occupies). This is the only number that may be summed.
2. **Per-archive size** — cached in `/var/lib/storage-info/borg-sizes.json`
   and refreshed for at most `archive_stats` archives per run (default **1**).
   Everything else reports the cached value, or the repository total divided
   by the archive count, marked `estimated`.

Result: `storage.info` for a full host went from **2 h 20 min → 17 s**.

Set `saltstack_storage.borg_archive_stats = 0` to skip per-archive
measurement entirely (repository total only).

## Scheduling — `cron_tick`, not `ir.cron`

The module creates **no `ir.cron`**. It registers one `cron.task`:

| Field | Value |
|---|---|
| `technical_name` | `storage_collect` |
| `cadence` | `week` |
| `model_name` | `saltstack.storage.measurement` |
| `method_name` | `_cron_collect_all` |

The engine in `cron_tick` runs it via `cron_week` (deterministically spread
over a day). Three triggers, one run path:

- the weekly tick,
- `POST /cron/trigger/storage_collect` (after a Salt deploy — same Bearer
  token as `saltstack.alert`),
- the "Run now" button on the task.

**Why weekly:** Borg/Restic read repository metadata and are expensive. The
cheap artefacts (LXD, PostgreSQL, filestore) are refreshed at every minion
sync anyway.

## The decoy-PostgreSQL trap

A machine can run a local PostgreSQL with stale data while Odoo talks to a
remote server. Every `psql` call in `storage.py` passes `-h "$DB_HOST"` from
`/etc/odoo/odoo.conf`, and the measured host is stored in `raw.host` and
shown on the Odoo tab. Measuring without `-h` silently measures the wrong
database.

## Verified against manual commands (2026-10-19)

| Measurement | Module | Manual | Match |
|---|---|---|---|
| `lxd_container` used, fors/ledningssystem | 24 994 689 110 B | `du -sbx /` → 24 994 693 930 B | ✓ (4 kB diff = in-flight writes) |
| `lxd_container` allocated | 1 140 171 661 312 B | `df -B1 --output=used /` → 1 140 172 054 528 B | ✓ (392 kB diff) |
| Borg repo total, tull1/lxd/fors | 314.15 GB | `borg info` → `unique_csize` 314.15 GB | ✓ exact |
| `storage.info` total runtime | 17.5 s | (was 2 h 20 min) | ✓ |

### Two bugs found and fixed by this comparison

1. **`du -sb /` returned 140 TB** instead of 25 GB — without `-x`, du walks
   `/proc`, `/sys` and every mount. Fixed to `du -sbx`.
2. **`lxc info --format json` does not exist** in this LXD version, and
   `lxc config device list` is empty for containers (the root disk is
   inherited from the profile). Allocated size now comes from `df -B1`.

Also: `borg` needs `BORG_PASSCOMMAND` from `/etc/borg/borg.conf`, otherwise
every call dies with "BORG_PASSCOMMAND is not set".

## Settings

| Setting | `ir.config_parameter` | Default |
|---|---|---|
| Collect at minion sync | `saltstack_storage.enabled` | `True` |
| Borg host | `saltstack_storage.borg_host` | `tull1` |
| Borg archives per run | `saltstack_storage.borg_archive_stats` | `1` |
| Retention (days) | `saltstack_storage.retention_days` | `90` |

## Installation

```bash
checkmodule -d <db> -m saltstack_storage
```

Requires `saltstack` and `cron_tick`. The Salt execution module
`storage.py` must be synced to the minions:

```bash
salt '*' saltutil.sync_modules
```

## Tests

```bash
checkmodule -d <db> -m saltstack_storage -t -l test > /tmp/storage.log 2>&1
```

20 tests cover the model, the two-layer summary, freshness, Dirvish
exclusion, the Odoo tab, the `cron.task` registration and the webhook path.
