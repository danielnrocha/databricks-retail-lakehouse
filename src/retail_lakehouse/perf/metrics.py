"""Per-statement metrics: collection, aggregation and the derived skew proxy.

Two sources, used for different reasons:

* `client.query_history.list(include_metrics=True)` — available within seconds of the statement
  finishing. This is what the runner polls, so a lab session does not stall.
* `system.query.history` — the durable table. It carries `shuffle_read_bytes`, `pruned_files`
  and `query_tags`, which the REST metrics payload does not, but it lags by minutes. Backfilled
  once at the end of a session.

Neither exposes per-task durations, which is why this module derives a *proxy* for skew rather
than claiming to measure it directly. See `RunMetrics.parallelism_efficiency`.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field, replace

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import QueryFilter, TimeRange

from retail_lakehouse.perf import warehouse

MS_PER_S = 1000
BYTES_PER_MIB = 1024 * 1024


@dataclass(frozen=True)
class RunMetrics:
    """One execution of one variant.

    Field names follow `system.query.history` so a number in a report can be traced back to a
    column, not to a transformation this module invented.
    """

    variant: str
    run_index: int
    statement_id: str
    execution_duration_ms: int
    compilation_duration_ms: int
    total_task_duration_ms: int
    spilled_local_bytes: int
    read_bytes: int
    read_cache_bytes: int
    read_rows: int
    produced_rows: int
    read_files: int
    pruned_files: int
    from_result_cache: bool
    # Backfilled from system.query.history; None until then.
    shuffle_read_bytes: int | None = None
    read_io_cache_percent: int | None = None

    @property
    def parallelism_efficiency(self) -> float:
        """total_task_duration_ms / execution_duration_ms — the skew proxy.

        Interpretation: how many task-seconds the cluster retired per wall-clock second. A stage
        whose work is evenly spread keeps every slot busy, so this tracks the slot count. A stage
        with one hot key collapses to a single long task at the tail, so wall-clock keeps
        accumulating while task time does not — the ratio falls.

        This is a *proxy*, not the platform's own skew definition. Databricks defines a skewed
        stage as max task duration > 1.5x p75 task duration, and no serverless surface exposes
        per-task durations, so that definition is unmeasurable here.
        """
        if self.execution_duration_ms <= 0:
            return 0.0
        return self.total_task_duration_ms / self.execution_duration_ms

    @property
    def io_cache_hit_ratio(self) -> float:
        """read_cache_bytes / read_bytes. The warm/cold control.

        `df.cache()` and `CACHE TABLE` raise on serverless, so warm-vs-cold cannot be forced.
        It can only be observed and reported, which is what this is for.
        """
        if self.read_bytes <= 0:
            return 0.0
        return self.read_cache_bytes / self.read_bytes


@dataclass
class VariantSummary:
    """Median-of-N for one variant, with the discarded warm-up kept for inspection."""

    variant: str
    runs: list[RunMetrics] = field(default_factory=list)
    discarded: list[RunMetrics] = field(default_factory=list)

    def _median(self, attr: str) -> float:
        values = [getattr(r, attr) for r in self.runs]
        return statistics.median(values) if values else 0.0

    @property
    def execution_duration_ms(self) -> float:
        return self._median("execution_duration_ms")

    @property
    def total_task_duration_ms(self) -> float:
        return self._median("total_task_duration_ms")

    @property
    def parallelism_efficiency(self) -> float:
        values = [r.parallelism_efficiency for r in self.runs]
        return statistics.median(values) if values else 0.0

    @property
    def spilled_local_bytes(self) -> float:
        return self._median("spilled_local_bytes")

    @property
    def read_bytes(self) -> float:
        return self._median("read_bytes")

    @property
    def read_rows(self) -> float:
        return self._median("read_rows")

    @property
    def produced_rows(self) -> float:
        return self._median("produced_rows")

    @property
    def pruned_files(self) -> float:
        return self._median("pruned_files")

    @property
    def shuffle_read_bytes(self) -> float:
        values = [r.shuffle_read_bytes for r in self.runs if r.shuffle_read_bytes is not None]
        return statistics.median(values) if values else 0.0

    @property
    def io_cache_hit_ratio(self) -> float:
        values = [r.io_cache_hit_ratio for r in self.runs]
        return statistics.median(values) if values else 0.0

    @property
    def spread_pct(self) -> float:
        """(max - min) / median of wall-clock, as a percentage.

        Reported alongside every median. A 3-run median with 60% spread is a different kind of
        number than one with 4% spread, and collapsing both to "the median" hides that.
        """
        values = [r.execution_duration_ms for r in self.runs]
        if len(values) < 2 or self.execution_duration_ms <= 0:
            return 0.0
        return 100.0 * (max(values) - min(values)) / self.execution_duration_ms


def _as_int(value: int | None) -> int:
    return int(value) if value is not None else 0


def fetch_rest_metrics(
    client: WorkspaceClient,
    statement_id: str,
    variant: str,
    run_index: int,
    *,
    window_start_ms: int,
) -> RunMetrics | None:
    """Pull one statement's metrics from the query history REST API.

    Filtered by start time rather than paged from the top so a busy workspace (the other agent
    shares this warehouse) cannot push the target statement off the first page.
    """
    response = client.query_history.list(
        filter_by=QueryFilter(query_start_time_range=TimeRange(start_time_ms=window_start_ms)),
        include_metrics=True,
        max_results=100,
    )
    for query in response.res or []:
        if query.query_id != statement_id:
            continue
        m = query.metrics
        if m is None:
            return None
        return RunMetrics(
            variant=variant,
            run_index=run_index,
            statement_id=statement_id,
            execution_duration_ms=_as_int(m.execution_time_ms),
            compilation_duration_ms=_as_int(m.compilation_time_ms),
            total_task_duration_ms=_as_int(m.task_total_time_ms),
            spilled_local_bytes=_as_int(m.spill_to_disk_bytes),
            read_bytes=_as_int(m.read_bytes),
            read_cache_bytes=_as_int(m.read_cache_bytes),
            read_rows=_as_int(m.rows_read_count),
            produced_rows=_as_int(m.rows_produced_count),
            read_files=_as_int(m.read_files_count),
            pruned_files=_as_int(m.pruned_files_count),
            from_result_cache=bool(m.result_from_cache),
        )
    return None


SYSTEM_HISTORY_SQL = """
SELECT statement_id,
       shuffle_read_bytes,
       read_io_cache_percent,
       spilled_local_bytes,
       total_task_duration_ms,
       execution_duration_ms
FROM system.query.history
WHERE statement_id IN ({ids})
"""


def backfill_from_system_table(
    client: WorkspaceClient,
    warehouse_id: str,
    runs: list[RunMetrics],
) -> tuple[list[RunMetrics], int]:
    """Attach `shuffle_read_bytes` and `read_io_cache_percent` from `system.query.history`.

    Returns the updated runs and the number that were found. A partial backfill is normal:
    the system table lags the REST API by minutes, and a lab session is shorter than that lag.
    Missing values stay None so the report can say "not yet materialised" instead of "0".
    """
    if not runs:
        return runs, 0
    ids = ", ".join(f"'{r.statement_id}'" for r in runs if r.statement_id)
    if not ids:
        return runs, 0
    result = warehouse.execute(
        client, warehouse_id, SYSTEM_HISTORY_SQL.format(ids=ids), bust_cache=False, label="backfill"
    )
    by_id = {row[0]: row for row in result.rows if row and row[0]}
    updated: list[RunMetrics] = []
    found = 0
    for run in runs:
        row = by_id.get(run.statement_id)
        if row is None:
            updated.append(run)
            continue
        found += 1
        updated.append(
            replace(
                run,
                shuffle_read_bytes=int(row[1]) if row[1] is not None else None,
                read_io_cache_percent=int(row[2]) if row[2] is not None else None,
            )
        )
    return updated, found
