"""Persistence for assessment runs, finding state and the trip snapshot.

``finding`` holds the current state of each issue by its stable ID. A run that
completes updates it: raised findings become new, ongoing or regressed, and
findings that stopped being raised become resolved. A run that fails writes
nothing but its own row, so a crash never resolves a finding.

#CRITICAL: Data integrity: a run row is written as ``failed`` before the engine
starts and is only upgraded when the whole outcome is stored in one
transaction, so a process that dies mid-run can never leave a clear result.
#VERIFY: tests/unit/test_assessments_repo.py pins the placeholder status and
that a failed result leaves findings and the trip snapshot untouched.

#CRITICAL: Concurrency: ``begin_run``, ``load_history`` and ``complete_run``
are separate transactions with no database lock across them (issue #22, still
open). One process serializes runs through the shared ``PipelineCoordinator``
lock (services/pipeline.py); a second process on the same database can still
overlap two runs. See the marker on ``AssessmentService.run``.
#VERIFY: tests/unit/test_on_demand.py (TestSharedLock) pins the in-process lock;
nothing pins the cross-process case.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from sqlalchemy import Integer, delete, func, insert, literal_column, select, update

from family_logistics.db.schema import analysis_run, finding, trip_snapshot
from family_logistics.db.sources import MAX_ERROR_LENGTH
from family_logistics.domain.lifecycle import Lifecycle
from family_logistics.domain.models import Finding, RunStatus, Severity
from family_logistics.domain.trip_codec import trip_from_json, trip_to_json

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from sqlalchemy.engine import Connection, Engine

    from family_logistics.domain.engine import AssessmentResult
    from family_logistics.domain.models import Trip

_RESOLVED = "resolved"
"""``finding.lifecycle`` of a finding that stopped being raised.

``domain.lifecycle.Lifecycle`` has no member for it (it describes open
findings only), so the stored value is named once here.
"""


@dataclass(frozen=True, slots=True)
class StoredHistory:
    """What the previous runs left behind.

    Attributes:
        open_findings: Findings still open after the last completed run.
        resolved_issue_ids: Issue IDs resolved earlier (a return is a regression).
        trips: Rides accepted by the last completed run.
    """

    open_findings: tuple[Finding, ...] = ()
    resolved_issue_ids: frozenset[str] = field(default_factory=frozenset[str])
    trips: tuple[Trip, ...] = ()


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One stored run.

    Attributes:
        run_id: Run identifier.
        trigger: What started it (weekly, daily, incremental, on_demand).
        window_start: Window start (UTC).
        window_end: Window end (UTC).
        started_at: Start time (UTC).
        finished_at: Finish time (UTC), None while running or after a crash.
        status: Run status.
        counts: Open findings per severity (read-only).
        rules_version: Version of the rules that judged the run.
        error: Why the run failed, if it did.
    """

    run_id: str
    trigger: str
    window_start: datetime
    window_end: datetime
    started_at: datetime
    finished_at: datetime | None
    status: RunStatus
    counts: MappingProxyType[str, int]
    rules_version: str
    error: str | None


def _mapping(row: object) -> dict[str, object]:
    """Widen a SQLAlchemy row mapping to ``dict[str, object]`` for typing."""
    return dict(cast("Mapping[str, object]", row))


def _finding_from_row(row: Mapping[str, object]) -> Finding:
    keys = cast("Sequence[str]", row["entity_keys"])
    suggestion = row["suggestion"]
    return Finding(
        rule_id=str(row["rule_id"]),
        severity=Severity(str(row["severity"])),
        message=str(row["message"]),
        entity_keys=tuple(keys),
        suggestion=str(suggestion) if suggestion is not None else None,
    )


def _record_from_row(row: Mapping[str, object]) -> RunRecord:
    counts = cast("Mapping[str, int]", row["counts"])
    error = row["error"]
    return RunRecord(
        run_id=str(row["run_id"]),
        trigger=str(row["trigger"]),
        window_start=cast("datetime", row["window_start"]),
        window_end=cast("datetime", row["window_end"]),
        started_at=cast("datetime", row["started_at"]),
        finished_at=cast("datetime | None", row["finished_at"]),
        status=RunStatus(str(row["status"])),
        counts=MappingProxyType(dict(counts)),
        rules_version=str(row["rules_version"]),
        error=str(error) if error is not None else None,
    )


class AssessmentRepository:
    """Runs, finding state and the previous run's rides.

    Args:
        engine: Database engine at the schema head.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def begin_run(  # noqa: PLR0913 - keyword-only run fields
        self,
        trigger: str,
        window_start: datetime,
        window_end: datetime,
        started_at: datetime,
        *,
        rules_version: str,
        run_id: str | None = None,
    ) -> str:
        """Record a run as started, with the safe placeholder status ``failed``.

        Args:
            trigger: What started the run.
            window_start: Window start (UTC).
            window_end: Window end (UTC).
            started_at: Start time (UTC).
            rules_version: Rule semantics version in force.
            run_id: An ID the caller already handed out (the on-demand API
                returns it before the run starts); a new UUID when None. An ID
                already stored raises the database's integrity error.

        Returns:
            The run ID.
        """
        if run_id is None:
            run_id = str(uuid.uuid4())
        with self._engine.begin() as connection:
            connection.execute(
                insert(analysis_run).values(
                    run_id=run_id,
                    trigger=trigger,
                    window_start=window_start,
                    window_end=window_end,
                    started_at=started_at,
                    finished_at=None,
                    status=RunStatus.FAILED.value,
                    counts={},
                    rules_version=rules_version,
                    error="run did not finish",
                )
            )
        return run_id

    def load_history(self) -> StoredHistory:
        """Read the finding state and the previous run's rides.

        A corrupt or unknown-version trip snapshot row makes
        ``trip_from_json`` raise ``ValidationError`` out of this method; rows
        are never skipped, so the caller records a failed run.

        Returns:
            Open findings, resolved issue IDs and the stored trips.
        """
        with self._engine.connect() as connection:
            rows = [
                _mapping(r)
                for r in connection.execute(select(finding)).mappings().all()
            ]
            snapshots = [
                cast("dict[str, object]", row[0])
                for row in connection.execute(select(trip_snapshot.c.payload))
            ]
        open_findings = tuple(
            _finding_from_row(r) for r in rows if r["lifecycle"] != _RESOLVED
        )
        resolved = frozenset(
            str(r["issue_id"]) for r in rows if r["lifecycle"] == _RESOLVED
        )
        trips = tuple(trip_from_json(payload) for payload in snapshots)
        return StoredHistory(open_findings, resolved, trips)

    def complete_run(
        self,
        run_id: str,
        result: AssessmentResult,
        finished_at: datetime,
        trips: Sequence[Trip],
    ) -> None:
        """Store a run's outcome atomically.

        A failed result updates only the run row. Any other result also
        updates the finding state and replaces the trip snapshot.

        Args:
            run_id: Run returned by ``begin_run``.
            result: The engine result.
            finished_at: Finish time (UTC).
            trips: Rides accepted this run (ignored for a failed result). There
                is no default: a caller that forgets them would wipe the
                snapshot of a completed run.
        """
        error = result.error[:MAX_ERROR_LENGTH] if result.error else None
        with self._engine.begin() as connection:
            connection.execute(
                update(analysis_run)
                .where(analysis_run.c.run_id == run_id)
                .values(
                    finished_at=finished_at,
                    status=result.run_status.value,
                    counts=result.counts(),
                    rules_version=result.rules_version,
                    error=error,
                )
            )
            if result.run_status is RunStatus.FAILED:
                return
            self._store_findings(connection, run_id, result, finished_at)
            connection.execute(delete(trip_snapshot))
            for trip in trips:
                connection.execute(
                    insert(trip_snapshot).values(
                        trip_key=trip.key.as_str(),
                        payload=trip_to_json(trip),
                        run_id=run_id,
                        updated_at=finished_at,
                    )
                )

    @staticmethod
    def _store_findings(
        connection: Connection,
        run_id: str,
        result: AssessmentResult,
        finished_at: datetime,
    ) -> None:
        existing = {
            str(row["issue_id"]): row
            for row in (
                _mapping(r) for r in connection.execute(select(finding)).mappings()
            )
        }
        for tracked in result.findings:
            item = tracked.finding
            values = {
                "rule_id": item.rule_id,
                "severity": item.severity.value,
                "message": item.message,
                "entity_keys": list(item.entity_keys),
                "suggestion": item.suggestion,
                "lifecycle": tracked.lifecycle.value,
                "last_seen": finished_at,
                "resolved_at": None,
                "run_id": run_id,
            }
            if item.issue_id in existing:
                connection.execute(
                    update(finding)
                    .where(finding.c.issue_id == item.issue_id)
                    .values(**values)
                )
            else:
                connection.execute(
                    insert(finding).values(
                        issue_id=item.issue_id, first_seen=finished_at, **values
                    )
                )
        for gone in result.resolved:
            row = existing.get(gone.issue_id)
            if row is None or row["lifecycle"] == _RESOLVED:
                continue
            connection.execute(
                update(finding)
                .where(finding.c.issue_id == gone.issue_id)
                .values(lifecycle=_RESOLVED, resolved_at=finished_at, run_id=run_id)
            )

    def latest_run(self) -> RunRecord | None:
        """Return the most recently started run.

        Returns:
            The run, or None when none has been recorded.
        """
        runs = self.recent_runs(1)
        return runs[0] if runs else None

    def latest_finished_run(self) -> RunRecord | None:
        """Return the most recently started run that has a finish time.

        A run that is still running, or died before it was stored, has none.

        Returns:
            The run, or None when no run ever finished.
        """
        runs = self.recent_runs(1, finished_only=True)
        return runs[0] if runs else None

    def open_lifecycle_counts(self) -> dict[str, int]:
        """Count the open findings per lifecycle state.

        Returns:
            ``new``, ``ongoing`` and ``regressed`` to their counts, zeros
            included. Resolved findings are not counted.
        """
        with self._engine.connect() as connection:
            rows = cast(
                "Sequence[tuple[str, int]]",
                connection.execute(
                    select(finding.c.lifecycle, func.count())
                    .where(finding.c.lifecycle != _RESOLVED)
                    .group_by(finding.c.lifecycle)
                ).all(),
            )
        tally = dict(rows)
        return {state.value: tally.get(state.value, 0) for state in Lifecycle}

    def get_run(self, run_id: str) -> RunRecord | None:
        """Return one run.

        Args:
            run_id: Run identifier.

        Returns:
            The run, or None when unknown.
        """
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(analysis_run).where(analysis_run.c.run_id == run_id)
                )
                .mappings()
                .first()
            )
        return _record_from_row(_mapping(row)) if row is not None else None

    def recent_runs(
        self, limit: int = 20, *, finished_only: bool = False
    ) -> tuple[RunRecord, ...]:
        """Return the latest runs, newest first.

        Args:
            limit: Maximum number of runs.
            finished_only: Leave out runs with no finish time.

        Returns:
            The runs.
        """
        query = select(analysis_run)
        if finished_only:
            query = query.where(analysis_run.c.finished_at.is_not(None))
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    query.order_by(
                        analysis_run.c.started_at.desc(),
                        # rowid grows with insertion (analysis_run is a rowid
                        # table), so equal started_at still orders newest first.
                        literal_column("analysis_run.rowid", Integer).desc(),
                        analysis_run.c.run_id,
                    ).limit(limit)
                )
                .mappings()
                .all()
            )
        return tuple(_record_from_row(_mapping(r)) for r in rows)

    def findings_of(self, run_id: str) -> tuple[Finding, ...]:
        """Return the findings last written by a run that are still open.

        Args:
            run_id: Run identifier.

        Returns:
            Open findings whose latest update came from this run.
        """
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(finding).where(
                        finding.c.run_id == run_id, finding.c.lifecycle != _RESOLVED
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_finding_from_row(_mapping(r)) for r in rows)
