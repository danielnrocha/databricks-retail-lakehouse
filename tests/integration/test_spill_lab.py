"""Integration tests for the spill lab.

The claims under test are: (a) the native seed does not spill on a 2X-Small, (b) widening the
sort key does, and (c) the mitigations that worked still work. (b) costs about 30 s of warehouse
time and is marked `slow` accordingly — it is the only test here that re-executes an expensive
statement, and it is worth it because "we induced genuine disk spill" is the load-bearing claim
of the whole section.

Requires an authenticated workspace (`DATABRICKS_CONFIG_PROFILE=dng`).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from databricks.sdk.service.sql import QueryFilter, TimeRange

from retail_lakehouse.perf import spill_lab, warehouse

pytestmark = pytest.mark.integration

RESULTS_DIR = Path(__file__).resolve().parents[2] / "data" / "perf"
CAUSAL_ROWS = 36_786_524


@pytest.fixture(scope="module")
def session() -> tuple[object, str]:
    client = warehouse.workspace()
    warehouse_id, size = warehouse.resolve_warehouse(client)
    assert size == "2X-Small", f"spill thresholds are stated against a 2X-Small, got {size}"
    return client, warehouse_id


def _spill_bytes(client: object, warehouse_id: str, sql: str, label: str) -> tuple[int, int]:
    """Run `sql` once and return `(spilled_bytes, rows_read)` from the query history API.

    The metrics record appears before it is complete: a first poll can return a `QueryMetrics`
    with `rows_read_count = 0` on a query that read 36.8M rows, which would turn an input-volume
    assertion into a flaky failure. Every query here reads from `causal`, so a zero row count is
    proof the record is still filling in rather than a real measurement — poll until it is not.
    """
    window_start_ms = int(time.time() * 1000) - 60_000
    result = warehouse.execute(client, warehouse_id, sql, label=label)
    for _ in range(8):
        response = client.query_history.list(
            filter_by=QueryFilter(query_start_time_range=TimeRange(start_time_ms=window_start_ms)),
            include_metrics=True,
            max_results=100,
        )
        for query in response.res or []:
            if query.query_id != result.statement_id or not query.metrics:
                continue
            rows_read = int(query.metrics.rows_read_count or 0)
            if rows_read > 0:
                return int(query.metrics.spill_to_disk_bytes or 0), rows_read
        time.sleep(3)
    pytest.fail(f"no complete metrics record for {label}")


@pytest.mark.slow
def test_wide_sort_key_spills_and_narrow_one_does_not(session: tuple[object, str]) -> None:
    """The induction claim, re-derived.

    Same table, same row count, same warehouse; only the sort key width differs. If both sides
    of this stop holding, the spill section's before/after has no "before".
    """
    client, warehouse_id = session
    narrow_spill, narrow_rows = _spill_bytes(
        client, warehouse_id, spill_lab.global_rank(0), "test/W000"
    )
    wide_spill, wide_rows = _spill_bytes(
        client, warehouse_id, spill_lab.global_rank(512), "test/W512"
    )
    assert narrow_rows == wide_rows == CAUSAL_ROWS
    assert narrow_spill == 0
    assert wide_spill > 0


def test_native_seed_does_not_spill(session: tuple[object, str]) -> None:
    """The null result, asserted. 36.8M rows of five narrow columns fit."""
    client, warehouse_id = session
    spilled, rows = _spill_bytes(client, warehouse_id, spill_lab.HIGH_CARD_AGG, "test/highcard-agg")
    assert rows == CAUSAL_ROWS
    assert spilled == 0


def test_preaggregation_removes_the_spill(session: tuple[object, str]) -> None:
    """The mitigation that worked. Same sort key width, fewer rows reaching the sort."""
    client, warehouse_id = session
    spilled, rows = _spill_bytes(client, warehouse_id, spill_lab.PREAGGREGATED, "test/M3")
    assert rows == CAUSAL_ROWS, "the scan is unchanged; only the sort input shrinks"
    assert spilled == 0


def test_recorded_spill_evidence_is_internally_consistent() -> None:
    """Guards the recorded numbers the write-up cites, with no warehouse cost.

    Every variant claimed to spill must have a non-zero `spilled_local_bytes` in *every*
    measured run, not just the median. A variant that spills intermittently is a different
    phenomenon and must not be reported as a clean before/after.
    """
    path = RESULTS_DIR / "spill_runs.json"
    if not path.exists():
        pytest.skip("no recorded spill runs; run `perf.cli spill`")
    payload = {entry["variant"]: entry["runs"] for entry in json.loads(path.read_text("utf-8"))}

    spilling = {"W256-global-rank", "W512-global-rank", "M1-partitioned", "M4-repartition"}
    clean = {
        "N1-window-wide",
        "N2-global-rank",
        "N3-highcard-agg",
        "N4-collect-list",
        "W000-global-rank",
        "W128-global-rank",
        "M2-filtered",
        "M3-preagg",
    }

    for variant in spilling & payload.keys():
        assert payload[variant], f"{variant} has no measured runs"
        for run in payload[variant]:
            assert run["spilled_local_bytes"] > 0, f"{variant} run {run['run_index']} did not spill"

    for variant in clean & payload.keys():
        for run in payload[variant]:
            assert run["spilled_local_bytes"] == 0, f"{variant} run {run['run_index']} spilled"
