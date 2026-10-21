"""In-process scheduler (APScheduler 3.x, single writer).

TODO(M3): weekly (FL_WEEKLY_CRON), daily (FL_DAILY_CRON) and incremental
(FL_CHANGE_SCAN_MINUTES) jobs in FL_TIMEZONE; coalesce overlapping on-demand
requests into one run.
"""
