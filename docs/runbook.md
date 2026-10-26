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
  `FL_AUDIT_KEY` or `FL_APPRISE_URL` is missing in production mode, when the schema is behind
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
| `production` | Urgent pushes go to `fl-urgent`, other findings to `fl-warning`, daily and on-demand status to `fl-status`, the weekly run summary to `fl-report` and operator alerts to `fl-ops`. Quiet hours (`FL_QUIET_HOURS_START` to `FL_QUIET_HOURS_END`) hold everything except one urgent push per episode |

Switching to `production` requires the joint approval of both parents after a
shadow period, recorded in the private deployment repository. To switch back,
set `shadow` and restart; this is always safe.

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
run itself raises. The service is built but nothing calls it until the
scheduler lands in M3, and collection failures, the routing budget threshold
and an unreachable database still arrive with the scheduler. Until then, rely
on the external monitor of `/healthz`.

## Escalation

1. A run reports `incomplete` or `failed` close to an imminent ride: treat the
   schedule as unverified and confirm rides directly with the drivers.
2. Collection or database failures that persist after a restart: switch to
   `shadow`, take a backup, and investigate from the logs.
3. Suspected credential or data exposure: rotate the affected secret
   immediately and follow [SECURITY.md](https://github.com/ByronWilliamsCPA/family-logistics/blob/main/SECURITY.md).
