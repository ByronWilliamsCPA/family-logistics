---
title: "Configuration"
schema_type: common
status: published
owner: core-maintainer
purpose: "Every FL_ setting Family Logistics reads, with defaults."
tags:
  - guide
  - configuration
---

Family Logistics reads its configuration from environment variables with the
`FL_` prefix (Pydantic Settings, `family_logistics.core.config.Settings`). A
`.env` file in the working directory is also read, after the process
environment, which always wins; it is meant for local development, and
production values come from the deployment's secret store. A blank value
(`FL_X=`) counts as unset, so a template line or a compose default leaves the
setting at its default (or, for a required secret, fails closed with a clear
message). Start from
[`.env.example`](https://github.com/ByronWilliamsCPA/family-logistics/blob/main/.env.example).
Secrets are supplied only at runtime and are never printed: `family-logistics config`
shows non-secret values only.

## Runtime

| Variable | Default | Purpose |
|----------|---------|---------|
| `FL_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `FL_JSON_LOGS` | `false` | Emit JSON logs; the container image sets `true` |
| `FL_INCLUDE_TIMESTAMP` | `true` | Include timestamps in logs |
| `FL_BIND_HOST` | `127.0.0.1` | Listen address; the container image sets `0.0.0.0` |
| `FL_PORT` | `8080` | Listen port |
| `FL_TIMEZONE` | `America/Los_Angeles` | IANA zone for human-facing times; storage is UTC |
| `FL_DATABASE_PATH` | `/data/family_logistics.db` | SQLite database (WAL mode, single writer) |

## Calendars (read-only)

| Variable | Default | Purpose |
|----------|---------|---------|
| `FL_CALDAV_URL` | `https://caldav.icloud.com` | CalDAV base URL; must use `https://` |
| `FL_CALDAV_USERNAME` | unset | Dedicated Apple ID holding View Only calendar shares |
| `FL_CALDAV_APP_PASSWORD` | unset | **Secret.** App-specific password for that Apple ID |
| `FL_CALDAV_CALENDARS` | empty | Comma-separated calendar hrefs to collect |

## Home Assistant and the registry UI

| Variable | Default | Purpose |
|----------|---------|---------|
| `FL_HA_TOKEN` | unset | **Secret.** Bearer token Home Assistant presents; at least 32 characters; `serve` refuses to start without it |
| `FL_PROXY_TRUSTED_CIDRS` | empty | Reverse-proxy addresses whose identity headers are believed; empty trusts none |
| `FL_UI_ALLOWED_USERS` | empty | Single sign-on usernames allowed into the registry UI; empty denies everyone |
| `FL_DATA_KEY` | unset | **Secret.** Fernet key encrypting protected addresses at rest; required before any address is stored, and `serve` refuses to start without it when `FL_NOTIFICATION_MODE=production` |

## Notifications

| Variable | Default | Purpose |
|----------|---------|---------|
| `FL_APPRISE_URL` | unset | Apprise API base URL (`http` or `https`, no embedded credentials); `serve` refuses to start without it when `FL_NOTIFICATION_MODE=production` |
| `FL_APPRISE_CONFIG_KEY` | `family-logistics` | Key of the saved Apprise configuration holding the app's services; 1 to 64 letters, digits, `_` or `-` |
| `FL_APPRISE_TAG_REPORT` | `fl-report` | Weekly and daily report e-mail |
| `FL_APPRISE_TAG_STATUS` | `fl-status` | Completed-assessment push |
| `FL_APPRISE_TAG_WARNING` | `fl-warning` | New validation error push |
| `FL_APPRISE_TAG_URGENT` | `fl-urgent` | Critical issue on an imminent ride (bypasses quiet hours) |
| `FL_APPRISE_TAG_OPS` | `fl-ops` | Service-health alerts to the operator |
| `FL_APPRISE_TAG_SHADOW` | `fl-shadow` | All output while in shadow mode |
| `FL_NOTIFICATION_MODE` | `shadow` | `shadow` or `production`; production needs both parents' approval |
| `FL_QUIET_HOURS_START` | `22:00` | Start of quiet hours (local time) |
| `FL_QUIET_HOURS_END` | `06:00` | End of quiet hours (local time) |
| `FL_IMMINENT_WINDOW_MINUTES` | `180` | Rides starting within this window may bypass quiet hours |

The client sends through one saved Apprise configuration: `POST
/notify/{FL_APPRISE_CONFIG_KEY}` with a JSON body of `title`, `body` and the
`fl-*` tag. The key selects the configuration; the tag selects the services
inside it. The key is not a tag.

Recipients for each tag are configured in Apprise, never in this application.
The Apprise client refuses any tag other than these six before making a
request, so shared severity tags can never be used.

## Routing

| Variable | Default | Purpose |
|----------|---------|---------|
| `FL_ROUTE_PROVIDER` | `ha_relay` | `ha_relay`, `google_routes` or `static` |
| `FL_GOOGLE_ROUTES_API_KEY` | unset | **Secret.** Optional paid routing key |
| `FL_ROUTES_MONTHLY_BUDGET_USD` | `10` | Hard monthly cap enforced by the application |
| `FL_ROUTES_BUDGET_WARN_USD` | `8` | Spend that triggers an operator warning |

## Scheduling

| Variable | Default | Purpose |
|----------|---------|---------|
| `FL_WEEKLY_CRON` | `0 15 * * SUN` | Weekly review (local time) |
| `FL_DAILY_CRON` | `0 19 * * *` | Daily review (local time) |
| `FL_CHANGE_SCAN_MINUTES` | `60` | Incremental change scan interval (minimum 5) |

## Narrative LLM (M5)

| Variable | Default | Purpose |
|----------|---------|---------|
| `FL_LLM_ENABLED` | `false` | Attempt the narrative call |
| `FL_LLM_BASE_URL` | unset | OpenAI-compatible base URL |
| `FL_LLM_API_KEY` | unset | **Secret.** Bearer key for the inference route |
| `FL_LLM_MODEL` | unset | Model identifier |

## Accessing settings in code

```python
from family_logistics.core.config import get_settings

settings = get_settings()
print(settings.notification_mode)
```
