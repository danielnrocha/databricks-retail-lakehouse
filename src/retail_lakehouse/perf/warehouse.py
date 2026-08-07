"""Warehouse connection and single-statement execution.

Why the Statement Execution API and not Databricks Connect: Connect binds to serverless
*notebook* compute, whose per-query metrics are not exposed to the client. The SQL warehouse
path gives us `query_history` records with task time and spill bytes, which is the entire point
of this lab.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import (
    Disposition,
    Format,
    QueryTag,
    StatementResponse,
    StatementState,
)

# The Statement Execution API caps synchronous waits at 50 s; longer statements are polled.
DEFAULT_WAIT = "50s"
POLL_INTERVAL_S = 5.0

# Hard ceiling on any single lab statement. Free Edition quota is shared across the whole
# account, so a statement still running at three minutes is a scoping mistake, not a result
# worth waiting for — it gets cancelled and the experiment gets smaller.
DEADLINE_S = 180.0

# Free Edition's serverless starter warehouse. Resolved by name so the lab keeps working if the
# workspace is rebuilt, but pinned to 2X-Small so "what was held constant" has an answer.
WAREHOUSE_NAME = "Serverless Starter Warehouse"


class StatementFailedError(RuntimeError):
    """A lab statement returned FAILED. Carries the platform error verbatim.

    The error text matters as much as the success path here: several findings in this lab are
    error messages (e.g. which Spark confs the platform refuses), so it is never swallowed.
    """


class StatementTimeoutError(RuntimeError):
    """A statement exceeded the lab's deadline and was cancelled."""


@dataclass(frozen=True)
class StatementResult:
    """The outcome of one statement, before metrics are attached."""

    statement_id: str
    state: str
    rows: list[list[str | None]]
    error: str | None
    columns: tuple[str, ...] = ()

    def as_dicts(self) -> list[dict[str, str | None]]:
        """Rows keyed by column name, taken from the response manifest.

        Needed for `DESCRIBE DETAIL`, whose output cannot be wrapped in a subquery and whose
        column order Databricks has changed between runtime versions.
        """
        return [dict(zip(self.columns, row, strict=False)) for row in self.rows]


def workspace() -> WorkspaceClient:
    """Return a client bound to the `dng` profile unless the caller overrode it."""
    os.environ.setdefault("DATABRICKS_CONFIG_PROFILE", "dng")
    return WorkspaceClient()


def resolve_warehouse(client: WorkspaceClient, name: str = WAREHOUSE_NAME) -> tuple[str, str]:
    """Return `(warehouse_id, cluster_size)` for the named warehouse.

    Cluster size is returned rather than assumed because it is the single most important
    "held constant" in every table this lab produces.
    """
    for wh in client.warehouses.list():
        if wh.name == name:
            return wh.id or "", wh.cluster_size or "unknown"
    raise LookupError(f"no SQL warehouse named {name!r}")


def _bust_cache(sql: str, label: str) -> str:
    """Wrap `sql` so the result cache cannot serve it.

    Two things had to be ruled out first, and both are measurements in their own right:

    * `SET use_cached_result = false` succeeds but does not persist — the Statement Execution
      API opens a fresh session per request, so the next statement runs with the default again.
    * A unique leading comment does *not* miss the cache. Databricks normalises comments out of
      the cache key: two runs differing only in a `/* nonce=... */` prefix both came back with
      `result_from_cache = true`, `read_bytes = 0`, `task_total_time_ms = 0`.

    A varying literal in the outermost projection does miss, verified on the same query
    (`result_from_cache = false`, `read_bytes = 439,891` on both runs). It costs one constant
    column on a result set that every variant here keeps under a thousand rows.
    """
    nonce = uuid.uuid4().int % 1_000_000_007
    return (
        f"/* perf-lab {label} */\n"
        f"SELECT {nonce} AS _perf_nonce, perf_lab_q.*\nFROM (\n{sql}\n) AS perf_lab_q"
    )


def _await(
    client: WorkspaceClient, response: StatementResponse, *, deadline_s: float
) -> StatementResponse:
    """Poll a still-running statement to completion, cancelling it at the deadline.

    The Statement Execution API caps `wait_timeout` at 50 s, so anything longer has to be
    polled. The cancel is not optional politeness: Free Edition quota is shared across the whole
    account, and one runaway join would take every other workload down with it for the day.
    """
    statement_id = response.statement_id or ""
    deadline = time.monotonic() + deadline_s
    while response.status and response.status.state in (
        StatementState.PENDING,
        StatementState.RUNNING,
    ):
        if time.monotonic() > deadline:
            client.statement_execution.cancel_execution(statement_id)
            raise StatementTimeoutError(f"cancelled after {deadline_s:.0f}s: {statement_id}")
        time.sleep(POLL_INTERVAL_S)
        response = client.statement_execution.get_statement(statement_id)
    return response


def execute(
    client: WorkspaceClient,
    warehouse_id: str,
    sql: str,
    *,
    tags: dict[str, str] | None = None,
    bust_cache: bool = True,
    label: str = "adhoc",
    wait: str = DEFAULT_WAIT,
    deadline_s: float = DEADLINE_S,
) -> StatementResult:
    """Execute one statement and return its id, state and rows.

    Raises `StatementFailedError` on FAILED so a broken variant cannot silently contribute a
    missing row to a measurement table, and `StatementTimeoutError` past `deadline_s`.
    """
    statement = _bust_cache(sql, label) if bust_cache else sql
    query_tags = [QueryTag(key=k, value=v) for k, v in (tags or {}).items()] or None
    response = client.statement_execution.execute_statement(
        statement=statement,
        warehouse_id=warehouse_id,
        wait_timeout=wait,
        disposition=Disposition.INLINE,
        format=Format.JSON_ARRAY,
        query_tags=query_tags,
    )
    response = _await(client, response, deadline_s=deadline_s)
    status = response.status
    state = status.state.value if status and status.state else "UNKNOWN"
    error = status.error.message if status and status.error else None
    rows = list(response.result.data_array or []) if response.result else []
    schema = response.manifest.schema if response.manifest else None
    columns = tuple(c.name or "" for c in (schema.columns or [])) if schema else ()

    if status and status.state is StatementState.FAILED:
        raise StatementFailedError(error or "statement failed with no message")
    return StatementResult(
        statement_id=response.statement_id or "",
        state=state,
        rows=[list(row) for row in rows],
        error=error,
        columns=columns,
    )


def try_execute(
    client: WorkspaceClient, warehouse_id: str, sql: str, *, label: str = "probe"
) -> tuple[bool, str]:
    """Execute a statement expected to fail; return `(succeeded, message)`.

    Used for the platform-capability probes, where the error string *is* the evidence.
    """
    try:
        result = execute(client, warehouse_id, sql, bust_cache=False, label=label)
    except StatementFailedError as exc:
        return False, str(exc)
    except Exception as exc:  # platform errors surface in several SDK exception types
        return False, f"{type(exc).__name__}: {exc}"
    return True, result.state


def scalar(client: WorkspaceClient, warehouse_id: str, sql: str) -> str | None:
    """Return the first cell of the first row, or None."""
    result = execute(client, warehouse_id, sql, bust_cache=False, label="scalar")
    if not result.rows or not result.rows[0]:
        return None
    return result.rows[0][0]
