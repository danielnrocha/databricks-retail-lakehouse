"""Integration tests for the skew lab.

These are regression guards on *claims*, not on code paths. Each one re-derives a number the
write-up depends on, so if the platform or the data changes underneath it, the document that
cites the number fails a test rather than quietly becoming false.

Requires an authenticated workspace (`DATABRICKS_CONFIG_PROFILE=dng`). Every query here reads
at most 36.8M rows and returns a handful of rows; the whole module is a few seconds of
warehouse time, which matters because Free Edition quota is shared across the account.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from retail_lakehouse.perf import platform_probe, skew_lab, tables, warehouse

pytestmark = pytest.mark.integration

RESULTS_DIR = Path(__file__).resolve().parents[2] / "data" / "perf"

# Volumes the entire lab is stated against. If these move, every measurement in perf-lab.md
# has a different denominator and the document is wrong until it is regenerated.
EXPECTED_TRANSACTION_ROWS = 2_595_732
EXPECTED_CAUSAL_ROWS = 36_786_524


@pytest.fixture(scope="module")
def session() -> tuple[object, str]:
    client = warehouse.workspace()
    warehouse_id, size = warehouse.resolve_warehouse(client)
    assert size == "2X-Small", f"lab is stated against a 2X-Small warehouse, got {size}"
    return client, warehouse_id


def test_lab_tables_have_the_stated_volumes(session: tuple[object, str]) -> None:
    client, warehouse_id = session
    facts = {
        t: tables.describe(client, warehouse_id, t) for t in (tables.TRANSACTIONS, tables.CAUSAL)
    }
    assert facts[tables.TRANSACTIONS].rows == EXPECTED_TRANSACTION_ROWS
    assert facts[tables.CAUSAL].rows == EXPECTED_CAUSAL_ROWS


def test_lab_tables_are_unclustered(session: tuple[object, str]) -> None:
    """The baseline has to stay a baseline.

    A background optimisation that adds clustering would improve the numbers and invalidate
    every before/after comparison in the write-up at the same time.
    """
    client, warehouse_id = session
    for table in (tables.TRANSACTIONS, tables.CAUSAL):
        assert tables.describe(client, warehouse_id, table).clustering_columns == "none"


def test_aqe_skew_confs_are_not_settable(session: tuple[object, str]) -> None:
    """The lab's central constraint: the "before" condition cannot be a config toggle.

    If Databricks ever makes these settable on a serverless SQL warehouse, this test fails and
    the lab's whole framing — code-level interventions only — needs revisiting.
    """
    client, warehouse_id = session
    for conf in (
        "spark.sql.adaptive.enabled",
        "spark.sql.adaptive.skewJoin.enabled",
        "spark.sql.adaptive.skewJoin.skewedPartitionThresholdInBytes",
        "spark.sql.shuffle.partitions",
    ):
        ok, message = warehouse.try_execute(
            client, warehouse_id, platform_probe.CONF_PROBES[conf], label="test"
        )
        assert not ok, f"{conf} became settable"
        assert "CONFIG_NOT_AVAILABLE" in message


def test_session_does_not_persist_between_statements(session: tuple[object, str]) -> None:
    """Rules out every session-scoped lever, including `use_cached_result = false`."""
    client, warehouse_id = session
    set_to, read_back = platform_probe.session_persists(client, warehouse_id)
    assert set_to == "false"
    assert read_back == "true"


def test_single_column_keys_are_severely_skewed(session: tuple[object, str]) -> None:
    """STORE_ID and PRODUCT_ID both blow past AQE's 5x factor condition on their own."""
    client, warehouse_id = session
    for key, minimum_ratio in (("STORE_ID", 1_000), ("PRODUCT_ID", 5_000)):
        profile = skew_lab.profile_key(client, warehouse_id, tables.TRANSACTIONS, key)
        assert profile.max_over_median > minimum_ratio


def test_composite_join_key_dissolves_the_skew(session: tuple[object, str]) -> None:
    """The lab's main negative result, asserted.

    `(PRODUCT_ID, STORE_ID, WEEK_NO)` distributes almost perfectly across shuffle partitions,
    so salting it is unwarranted. This test is what stops someone reintroducing a salt
    "because the dataset is skewed" — it is, but not on this key.
    """
    client, warehouse_id = session
    profile = skew_lab.profile_partitions(
        client,
        warehouse_id,
        tables.TRANSACTIONS,
        "PRODUCT_ID, STORE_ID, WEEK_NO",
        num_partitions=1024,
        bytes_per_row=0.0,
    )
    assert profile.max_over_median < 1.5
    assert not profile.trips_factor_condition


def test_hottest_store_partition_is_far_below_the_aqe_byte_threshold(
    session: tuple[object, str],
) -> None:
    """The headline hypothesis, asserted.

    STORE_ID is skewed 2,519x, which clears AQE's factor condition by three orders of
    magnitude. It does not come close to the 256 MB condition, and AQE requires both.

    The byte figure uses the estimated UnsafeRow width, so the assertion is deliberately loose:
    it demands an order of magnitude of headroom, which no plausible width error can cross.
    """
    client, warehouse_id = session
    profile = skew_lab.profile_key(client, warehouse_id, tables.TRANSACTIONS, "STORE_ID")
    hottest = skew_lab.hottest_key_bytes(profile, skew_lab.SHUFFLE_WIDTH_BYTES["transactions"])
    assert hottest < skew_lab.AQE_SKEW_THRESHOLD_BYTES / 10, (
        f"hottest STORE_ID partition is {hottest / 1024 / 1024:.1f} MiB; the lab's conclusion "
        "assumes it is at least an order of magnitude under the 256 MB threshold"
    )


def test_shuffle_bytes_are_not_reported_by_this_platform(session: tuple[object, str]) -> None:
    """Documents the measurement gap that forced the width estimate.

    `system.query.history.shuffle_read_bytes` is non-null and always zero here, while
    `spilled_local_bytes` on the same rows is not. If that ever changes, the estimate can be
    replaced by a measurement — and this failing test is how anyone finds out.
    """
    client, warehouse_id = session
    result = warehouse.execute(
        client,
        warehouse_id,
        "SELECT count(shuffle_read_bytes), max(shuffle_read_bytes), max(spilled_local_bytes) "
        "FROM system.query.history "
        "WHERE start_time > current_timestamp() - INTERVAL 24 HOURS",
        bust_cache=False,
        label="test",
    )
    non_null, max_shuffle, max_spill = (int(v or 0) for v in result.rows[0])
    assert non_null > 0, "no recent statements to judge"
    assert max_shuffle == 0, "shuffle_read_bytes now populates; replace the width estimate"
    assert max_spill > 0, "spilled_local_bytes should populate; if not, the column set changed"


def test_recorded_runs_are_not_result_cache_hits() -> None:
    """A cached run measures the cache, not the query. Guards the harness, not the platform."""
    for name in ("skew_runs.json", "smj_runs.json", "agg_runs.json"):
        path = RESULTS_DIR / name
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for entry in payload:
            for run in entry["runs"]:
                assert not run["from_result_cache"], f"{entry['variant']} run {run['run_index']}"
                assert run["read_bytes"] > 0, f"{entry['variant']} read nothing"
