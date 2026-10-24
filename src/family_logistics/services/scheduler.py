"""In-process scheduler (APScheduler 3.x, single writer).

Not implemented yet. Roadmap milestone M3 (``docs/project/roadmap.md``, "weekly
and daily scheduling" and "ops alerts") covers: weekly (FL_WEEKLY_CRON), daily
(FL_DAILY_CRON) and incremental (FL_CHANGE_SCAN_MINUTES) jobs in FL_TIMEZONE;
coalescing overlapping on-demand requests into one run; and calling
``adapters.apprise.client.ops_alert`` for failed or incomplete runs, source
collection failures, a degraded database and the routing budget warning. Until
then nothing schedules a run and no code path raises ops alerts.
"""
