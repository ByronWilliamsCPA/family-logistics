---
title: "Runbook"
schema_type: common
status: published
owner: core-maintainer
purpose: "Generic operating procedures for Family Logistics: start, stop, health, modes, key rotation, restore and escalation."
tags:
  - operations
  - runbook
---

This runbook is generic. Hosts, paths, recipients, monitors and backup
locations for a real deployment live in the private deployment repository;
never add them here.

## Start and stop

The container runs one process with one SQLite writer.

- **Start:** start the deployment's container. The image runs
  `family-logistics serve --upgrade-db`, which migrates the schema to head
  before serving. Outside a container, run `family-logistics migrate` and then
  `family-logistics serve`. The process refuses to start (exit 2) when
  `FL_HA_TOKEN` is missing or shorter than 32 characters, when `FL_DATA_KEY`,
  `FL_AUDIT_KEY` or `FL_APPRISE_URL` is missing in production mode, when
  `FL_SCHEDULER_ENABLED=true` without CalDAV credentials or with
  `FL_CHANGE_SCAN_MINUTES` of 15 or less, when the schema is behind
  head (without `--upgrade-db`), when the schema is newer than the image (see
  [Upgrading the image](#upgrading-the-image)), or when a setting fails
  validation; read the startup error and fix it. A refused start lists every
  problem it found and never creates or migrates the database.
- **Stop:** stop the container. Wait for it to exit before touching the
  database file, so the WAL is checkpointed.
- Never run two serving instances against the same database file. The
  migration lock (see [Upgrading the image](#upgrading-the-image)) only makes an
  accidental concurrent start safe: the second start waits for the first
  upgrade to finish instead of migrating twice. It does not make two writers a
  supported setup.

## Upgrading the image

Take a backup (see [Backup and restore](#backup-and-restore)) before starting
an image that ships a new schema revision; `serve --upgrade-db` migrates the
database as soon as the container starts.

- A migration runs in one transaction: if it fails, nothing is changed and the
  service refuses to start with `migration failed and was rolled back`.
- Migrations take the database's write lock, so a second container started on
  the same volume waits for the first to finish instead of migrating twice. If
  the lock is held for more than two minutes, it refuses to start with
  `database is locked by another process`.
- An image older than the database's schema (a rolled-back image, or a backup
  taken from a newer release) refuses to start with `database schema revision
  ... is newer than this release`. Start the newer image, or restore a backup
  taken for this release; never downgrade with the older image.

## Health

```bash
family-logistics healthcheck      # exit 0 when healthy
curl -s http://127.0.0.1:8080/healthz
```

| Response | Meaning | Action |
|----------|---------|--------|
| `200 {"status": "ok", "database": true, "version": "..."}` | Healthy | None |
| `503 {"status": "degraded", "database": false, "version": "..."}` | Database unreachable | Check the data volume is mounted and writable, free disk space, then restart |
| No response | Process down | Check container logs, then start |

A healthy process does not mean the last assessment succeeded. The
family-wide status card reports `incomplete` or `failed` runs separately, and
either state is never shown as clear.

## Notification modes

| `FL_NOTIFICATION_MODE` | Behaviour |
|------------------------|-----------|
| `shadow` (default) | Real collection and validation; every push, ops alerts, status and the weekly report included, goes to the operator only (`fl-shadow` tag) with a `[shadow]` title and a first line naming the tag production would have used; nothing reaches the other parent. Planning, de-duplication and quiet hours run exactly as in production, so shadow is a faithful preview ([ADR 0008](ADRs/0008-notification-service.md), D1) |
| `production` | Urgent pushes go to `fl-urgent`, other findings to `fl-warning`, daily and on-demand status to `fl-status`, the weekly run summary to `fl-report` and operator alerts to `fl-ops`. Quiet hours (`FL_QUIET_HOURS_START` to `FL_QUIET_HOURS_END`) hold everything except one urgent push per episode; held findings are sent after quiet hours end, but a daily status or weekly report due inside quiet hours is dropped, not deferred, so keep `FL_DAILY_CRON` and `FL_WEEKLY_CRON` outside the window ([ADR 0008](ADRs/0008-notification-service.md), D6) |

Switching to `production` requires the joint approval of both parents after a
shadow period, recorded in the private deployment repository. To switch back,
set `shadow` and restart; this is always safe.

## Scheduler

`FL_SCHEDULER_ENABLED=true` starts the scheduler inside `serve` (one process,
one writer; it is off by default). On each tick it collects the calendars,
runs one assessment with the latest school snapshot and dispatches the pushes
the run owes ([ADR 0008](ADRs/0008-notification-service.md)). Jobs, in
`FL_TIMEZONE`:

| Job | When | What |
|-----|------|------|
| `scan` | every `FL_CHANGE_SCAN_MINUTES`, first after 60 seconds | incremental run; findings and ops pushes |
| `daily` | `FL_DAILY_CRON` | run that also makes the status push |
| `weekly` | `FL_WEEKLY_CRON` | run that also makes the weekly report |
| `purge` | 03:30 daily | retention purges, one transaction each, notification state included |
| `urgent-retry` | 5 minutes after a dispatch reported `urgent_failed` | one extra pipeline; it never chains |

One pipeline runs at a time: a scan that finds another pipeline running is
skipped (the running one is as fresh), and the other jobs wait up to 30
minutes. A run that raises sends the "could not verify" push, and so does a
daily, weekly or urgent-retry tick that waited the full 30 minutes for the lock
and a daily or weekly fire that was missed beyond the 15-minute grace time
([ADR 0008](ADRs/0008-notification-service.md), F6): the family is told the
status or report could not be verified rather than hearing nothing. A failed or
incomplete run is dispatched like any other; nothing is reported as clear on a
failure. A crash in one tick is logged by exception class only and counted; the
scheduler keeps running.

## On-demand runs

`POST /api/v1/assessments` (the Home Assistant button) starts one assessment of
what is stored, with the latest school snapshot, and answers 202 with a
`run_id`; `GET /api/v1/assessments/{run_id}` reports it and
`GET /api/v1/status` is the family-wide card. Reference:
[API reference](api-reference.md#status-and-assessment-routes).

- **Off by default.** `POST /api/v1/assessments` is refused with a 503 (`code`
  `on_demand_disabled`) until `FL_ON_DEMAND_ENABLED=true` and `serve` restarts.
  Enabling it, like enabling the scheduler, makes the first production
  assessment run, so the open correctness blockers must be closed first:
  [#19](https://github.com/ByronWilliamsCPA/family-logistics/issues/19)
  (retained confirmed legs),
  [#20](https://github.com/ByronWilliamsCPA/family-logistics/issues/20)
  (trip snapshot content),
  [#21](https://github.com/ByronWilliamsCPA/family-logistics/issues/21)
  (an incomplete run must not resolve findings),
  [#22](https://github.com/ByronWilliamsCPA/family-logistics/issues/22)
  (run serialization) and
  [#23](https://github.com/ByronWilliamsCPA/family-logistics/issues/23)
  (corrupt snapshot healing); see the
  [roadmap](project/roadmap.md#m3-blockers).
  The status and run-lookup routes are always available.

- **One `serve` process only.** The pipeline lock the scheduler and the
  on-demand runs share is process-local. A second `serve`, or `collect --once`
  while a run executes, is not excluded; the database-level write lock is
  issue #22 and is still open. The lock is held whether or not
  `FL_SCHEDULER_ENABLED` is set, so a button press during a scheduled run waits
  or joins it, never overlaps it.
- **A double press is one run.** A second press while a run is queued, or while
  a scheduled run is executing, returns the same `run_id` with
  `coalesced: true`.
- **It does not collect.** The run judges the calendars as last collected; if
  they are stale it reports DATA-011 (incomplete), not clear, and the status
  card adds `calendar_stale`. With the scheduler enabled, wait for the next
  scheduled scan, then press again. With the scheduler disabled, run
  `collect --once` first (nothing else collects), then press again.
- **How a bad outcome reads.** A queued run that waits 30 minutes for the lock,
  a run that raises, and a run whose process died all read as `finished` with
  status `failed`. The status card shows `failed` with reason `run_interrupted`
  for a started run that never finished while nothing is executing. None of
  them is ever clear. A run still queued when the service stops leaves no row,
  so its `run_id` answers 404 after a restart; press again.
- **Stale or missing data on the card.** The card carries three stale flags
  (`analysis_stale`, `calendar_stale`, `school_snapshot_stale`) and a list of
  `reasons`; any reason turns an otherwise clear card into `incomplete`.
  `analysis_stale` and the reason of the same name mean the latest finished
  run is older than `FL_SOURCE_MAX_STALENESS_MINUTES`; with no run at all the
  reason is `no_run` (and `analysis_stale` is still true). `calendar_stale`
  means the oldest successful calendar collection is older than that same limit,
  or no calendar was ever collected. `school_snapshot_stale` is true after 26
  hours without a snapshot, and the reason is `school_snapshot_stale`; with no
  snapshot at all the reason is `school_snapshot_missing` (the flag is true
  either way).
- **Failed press, unchanged card.** A run that failed without leaving a database
  row (it never got the lock, the route provider could not be built, or it was
  abandoned at shutdown) shows `failed` on `GET /api/v1/assessments/{run_id}`,
  but `GET /api/v1/status` keeps the previous verdict, possibly `clear`, until
  that analysis is older than `FL_SOURCE_MAX_STALENESS_MINUTES` (default 120).
  The card is the last stored analysis, so this is by design (design rule 4 is
  not broken). Home Assistant should poll the run id after a press and treat
  `failed` there as authoritative for that press.
- **`run_in_flight`.** True while an on-demand run is queued or executing, and
  also while any pipeline holds the lock: a scheduled collect, assess and
  notify, or the nightly purge. It means "the lock is busy", not only "a button
  run exists".
- **Failed runs are remembered 32 deep.** A run that failed before it wrote a
  row (for example one that never got the lock) reads `finished` / `failed`
  from an in-memory list of the last 32; past that, or after a restart, its
  `run_id` answers 404.
- **Shutdown.** Stopping `serve` first stops accepting and starting on-demand
  runs, then stops the scheduler (which waits for its running pipeline), then
  waits for an on-demand run that is executing. A queued run is abandoned and
  never starts during shutdown.
- **Known risk: scan starvation.** Presses that follow one another can keep the
  pipeline lock busy so the scheduler's scans are skipped (a scan that finds
  the lock held does not run). There is no cooldown yet; if the card shows
  `analysis_stale` or `calendar_stale` while someone is pressing the button,
  stop pressing and let a scan run. A cooldown is future work.
- **Route provider.** The provider is built when the run starts, so
  `FL_ROUTE_PROVIDER=google_routes` (not built yet) makes each on-demand run
  fail rather than stopping the app at start.

Two operating rules follow from the single in-process lock:

- Do not run `family-logistics collect --once` against a live `serve` that has
  the scheduler enabled. The lock lives inside the server process, so the CLI
  collection is not serialized with the scheduler's pipelines. Stop the
  scheduler (`FL_SCHEDULER_ENABLED=false`, restart) first, or wait for the next
  tick.
- Stopping the container waits for the running pipeline, but Docker's default
  stop grace period is 10 seconds and then kills the process. Set
  `stop_grace_period` (compose) or `--stop-timeout` to longer than a pipeline
  takes (see `duration_ms` in the tick log), or accept at-least-once: a hard
  kill mid-send is covered by the 15-minute claim lifetime, so the next tick
  sends again.

Write `FL_DAILY_CRON` and `FL_WEEKLY_CRON` with day names (`SUN`, `mon-fri`),
never numbers: the scheduler library counts day 0 as Monday where standard cron
counts it as Sunday, so `0 15 * * 0` would fire on the wrong day. A numeric
day-of-week is refused at startup (exit 2, no traceback). The route provider
must be `ha_relay`; `static` and `google_routes` are refused the same way.

### Reading a tick's health

Each tick logs `scheduler.tick_finished` with `job`, `outcome`, `run_id`,
`run_status`, `collection`, `duration_ms`, the job's running counters (`ticks`,
`failures`, `skipped`, `consecutive_skips`; in memory, reset by a restart) and
the dispatch counts (`sent`, `failed`, `urgent_failed`, `held`, `dropped`,
`stale`, `unconfigured`). That log line is where an operator reads the health
counters; there is no separate endpoint.

| `outcome` | Meaning | Action |
|-----------|---------|--------|
| `ok` | run stored and dispatched (whatever its status) | none; read `run_status` and `collection` |
| `unavailable` | the "could not verify" push went out: the run raised (`scheduler.run_failed`, exception class), a daily, weekly or urgent-retry tick waited the full 30 minutes for the lock (`scheduler.lock_wait_expired`), or a daily or weekly fire was missed (`scheduler.job_skipped` with `reason=missed`) | find what held the pipeline or raised; the push already told the family. A stuck pipeline needs a restart |
| `dispatch_failed` | the dispatch raised (a notification state database or tag error) | read `scheduler.dispatch_failed`; the pushes stay owed |
| `crashed` | unexpected error in the tick | read `scheduler.job_crashed` |
| `skipped_busy` (scan, purge) | another pipeline held the lock | one is normal; `scheduler.scans_blocked` at WARNING means about an hour of consecutive scans found it busy (a stuck pipeline): check the running pipeline and restart if it is hung |
| `stopped` | shutdown began | none |
| `purge_failed` | a purge raised | read `scheduler.purge_failed` (table name); the other purges ran |

`collection` is `complete`, `incomplete` or `crashed`; the run still judges
freshness, so a source that stays stale makes runs `incomplete` and raises the
once-per-episode ops push. `scheduler.job_skipped` (missed time or overlapping
instance) repeating every interval means the pipeline outlasts the interval.
`scheduler.cron_in_quiet_hours` at start names a cron whose push would be
dropped; move it outside quiet hours.

### Shadow soak

Run in shadow mode (the default) for the soak the roadmap requires before
production: the scheduler runs for real and every push goes to the operator
only with a `[shadow]` title. The soak does not start counting until
issues #19 to #23 are closed; days before that do not count toward the 7. Check each day that ticks are `ok`, that the
daily status and weekly report arrive, that no `dispatch_failed` or `crashed`
outcome repeats, and that no push would have disturbed quiet hours. Switch to
`production` only with both parents' approval ([Notification modes](#notification-modes)).

## Rotating secrets

### `FL_HA_TOKEN`

1. Generate a new token: `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.
2. Set it in the deployment's secret store and in Home Assistant's secrets.
3. Restart the container, then confirm Home Assistant's status sensor updates.

Rotate after any suspected exposure and at least once a year.

### `FL_DATA_KEY`

`FL_DATA_KEY` encrypts protected addresses and coordinates at rest. A wrong or
missing key fails closed: protected addresses and coordinates cannot be read and
nothing is stored in plain text.

Do not rotate `FL_DATA_KEY` yet. The re-key command is not delivered, and it is
tracked as an open carry-over in [the roadmap](project/roadmap.md). The registry
stores Fernet-encrypted addresses and coordinates under this one key. Setting a
new key without re-encrypting both leaves the stored values unreadable, and the
app fails closed on them; restoring the old key is then the only recovery.
Rotation stays blocked until a procedure re-encrypts and verifies every
encrypted registry field.

Once that command exists, the procedure is: generate a key with
`python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`,
stop the container and take a backup (see below), re-encrypt with the old and
new keys, then set the new key and start the container. Keep the previous key
until a restore test with the new key succeeds.

### `FL_AUDIT_KEY`

`FL_AUDIT_KEY` keys the audit-log hashes (HMAC-SHA256), so the table alone
cannot be used to confirm a guessed name or address. It must differ from
`FL_DATA_KEY`. Without it no audit row is written and production `serve` will
not start. Stored hashes cannot be recomputed under a new key, so changing the
key breaks comparison with older rows unless the version is raised.

1. Generate a key: `python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'`.
2. Set it in the deployment's secret store and raise `FL_AUDIT_KEY_VERSION` by
   one, so new rows are recorded under the new version (every row stores the
   version that produced its hashes).
3. Restart the container.

Keep each retired key until the rows recorded under its version are purged
(365 days), or older hashes can no longer be checked.

!!! warning "Databases created before `hash_key_version`"
    Migration `0001` gained the `hash_key_version` column (and its CHECK
    constraint) in place, with the same revision id. A local or development
    database created before that change reports the schema as current but lacks
    the column. Delete it and run `family-logistics migrate` again; no
    production database predates the change.

### CalDAV app-specific password

Revoke the old password in the Apple ID account, create a new one, update
`FL_CALDAV_APP_PASSWORD` and restart. Use only the dedicated, View Only Apple
ID.

## Backup and restore

Backups are verified copies of the SQLite file taken by a separate job in the
deployment. To restore:

1. Stop the container (the only writer).
2. Copy the chosen backup over the database file; remove any stale `-wal` and
   `-shm` files next to it.
3. Check the restored file (read-only; it is not modified):

   ```bash
   family-logistics verify-db /path/to/family_logistics.db
   ```

   It reports `Integrity: ok` or `FAILED`, the schema state (`at head`,
   `behind head`, or `unknown revision, newer than this release`) and the
   schema revision next to the head revision of the image you will start. It
   exits 0 only when the file is intact and at head, and 1 otherwise,
   including when the schema is merely behind head; a revision behind head is
   upgraded by `serve --upgrade-db` at start. A newer revision needs the newer
   image.
4. Start the container and confirm `/healthz` returns `200`.
5. Run an on-demand assessment and confirm the status card is not stale.

Test a restore at least monthly.

### Rolling back a schema change

Downgrades are explicit and may destroy data, including audit history. Take a
backup first, stop the container, then run the downgrade with the image that
holds the current revision (an older image refuses a schema it does not know):

```bash
family-logistics migrate --downgrade --revision <revision>
```

The command asks for confirmation (`--yes` skips the prompt in scripts) and
refuses a target that is not below the current revision. `migrate` without
`--downgrade` only moves forward. Start the matching older image afterwards; a
newer image would upgrade the schema again.

## Ops alerting

The Apprise client posts to the saved Apprise configuration,
`POST /notify/{FL_APPRISE_CONFIG_KEY}`, and selects the services within it with
the `fl-*` tag in the JSON body; who receives each tag is configured in Apprise.
It sends only to the six configured `FL_APPRISE_TAG_*` tags and refuses any
other tag before making a request; `ops_alert()` sends operator alerts to the
`fl-ops` tag.

Failed and incomplete runs alert through `NotificationService` (one `fl-ops`
push per failed episode and one per incomplete episode, not one per run; held
during quiet hours), and `dispatch_unavailable` raises the same alert when a
run itself raises, once the [scheduler](#scheduler) is enabled. Collection
failures, the routing budget threshold and an unreachable database do not
raise ops alerts yet (the tick logs are the signal), so keep the external
monitor of `/healthz`.

## Escalation

1. A run reports `incomplete` or `failed` close to an imminent ride: treat the
   schedule as unverified and confirm rides directly with the drivers.
2. Collection or database failures that persist after a restart: switch to
   `shadow`, take a backup, and investigate from the logs.
3. Suspected credential or data exposure: rotate the affected secret
   immediately and follow [SECURITY.md](https://github.com/ByronWilliamsCPA/family-logistics/blob/main/SECURITY.md).
