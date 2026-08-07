"""Type 2 dimension semantics — MOD-001, MOD-002, MOD-003, and idempotent dedupe (ING-005).

These run on a local Spark session with no workspace connection (ENV-006). That is not a
convenience: the failure they exist to catch — a point-in-time join that silently fans out — is a
property of the join predicate, and a test that needs a pipeline to check a join predicate is a
test that gets skipped and then deleted.

MOD-003 is the one that matters. `test_point_in_time_join_is_one_to_one` asserts the correct join
returns exactly one dimension row per fact row, and `test_naive_join_fans_out` proves the
comparison is not vacuous by showing the ordinary-looking join inflating the same measure. A
1:1 assertion against a dimension where every key has one version passes trivially; the fixture
here deliberately gives one key three versions.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

import pytest
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from retail_lakehouse.silver.lib.dedupe import latest_per_key
from retail_lakehouse.silver.lib.scd2 import (
    END_COL,
    START_COL,
    build_scd2_from_change_feed,
    current_row_violations,
    naive_key_join,
    overlapping_windows,
    point_in_time_join,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def spark() -> SparkSession:
    # PySpark refuses to run when driver and worker interpreters differ by a minor version, and
    # the worker default is whatever `python3` resolves to on PATH. Pinning it to the interpreter
    # running the test is the difference between a green suite and an opaque stack trace on any
    # machine with more than one Python.
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    session = (
        SparkSession.builder.master("local[2]")
        .appName("test-scd2")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "2")
        .getOrCreate()
    )
    yield session
    session.stop()


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


@pytest.fixture(scope="module")
def change_feed(spark: SparkSession):
    """A product change feed with three keys of deliberately different shapes.

    * 100 — three versions, the second of which is a genuine attribute change and the third a
      *no-op* re-delivery of the same attributes. A correct SCD2 build produces two versions, not
      three, and getting this wrong is the most common way a dimension quietly triples in size.
    * 200 — two versions.
    * 300 — one version, never changed. Present because a fixture where every key has history
      hides the off-by-one in "close the previous window".
    """
    rows = [
        (100, "GROCERY", "FRZN ICE", _ts("2023-01-01 00:00:00")),
        (100, "GROCERY", "FRZN NOVELTY", _ts("2024-06-01 00:00:00")),
        (100, "GROCERY", "FRZN NOVELTY", _ts("2025-01-01 00:00:00")),
        (200, "PRODUCE", "APPLES", _ts("2023-01-01 00:00:00")),
        (200, "PRODUCE", "ORGANIC APPLES", _ts("2025-06-01 00:00:00")),
        (300, "DELI", "CHEESE", _ts("2023-01-01 00:00:00")),
    ]
    return spark.createDataFrame(
        rows, "product_id BIGINT, department STRING, commodity_desc STRING, _cdc_ts TIMESTAMP"
    )


@pytest.fixture(scope="module")
def dim(change_feed):
    return build_scd2_from_change_feed(
        change_feed,
        keys=["product_id"],
        sequence_col="_cdc_ts",
        tracked_columns=["department", "commodity_desc"],
    ).cache()


@pytest.fixture(scope="module")
def facts(spark: SparkSession):
    """Six fact rows straddling every version boundary in the dimension."""
    rows = [
        ("e1", 100, _ts("2023-06-01 12:00:00"), 10.0),  # before the change
        ("e2", 100, _ts("2024-06-01 00:00:00"), 20.0),  # exactly at the boundary
        ("e3", 100, _ts("2025-09-01 12:00:00"), 30.0),  # after both changes
        ("e4", 200, _ts("2024-01-01 12:00:00"), 40.0),
        ("e5", 200, _ts("2025-12-01 12:00:00"), 50.0),
        ("e6", 300, _ts("2024-03-01 12:00:00"), 60.0),
    ]
    return spark.createDataFrame(
        rows, "event_id STRING, product_id BIGINT, transaction_ts TIMESTAMP, sales_amt DOUBLE"
    )


# ---------------------------------------------------------------------------------------------
# MOD-001
# ---------------------------------------------------------------------------------------------
def test_attribute_change_creates_version(dim):
    """A changed attribute opens a new version; an unchanged re-delivery does not."""
    versions = {
        row["product_id"]: row["n"]
        for row in dim.groupBy("product_id").count().withColumnRenamed("count", "n").collect()
    }
    assert versions == {100: 2, 200: 2, 300: 1}, (
        "Key 100 received three change records but only two distinct attribute states. A build "
        "that reports three versions is versioning CDC events rather than business changes."
    )


def test_prior_version_window_is_closed(dim):
    closed = dim.filter(F.col(END_COL).isNotNull()).collect()
    assert len(closed) == 2
    for row in closed:
        assert row[START_COL] < row[END_COL]
    superseded = {row["product_id"]: row[END_COL] for row in closed}
    assert superseded[100] == _ts("2024-06-01 00:00:00")
    assert superseded[200] == _ts("2025-06-01 00:00:00")


def test_exactly_one_current_row_per_key(dim):
    assert current_row_violations(dim, key="product_id").count() == 0
    assert dim.filter(F.col("is_current")).count() == 3


# ---------------------------------------------------------------------------------------------
# MOD-002
# ---------------------------------------------------------------------------------------------
def test_no_overlapping_validity_windows(dim):
    assert overlapping_windows(dim, key="product_id").count() == 0


def test_overlap_detector_actually_detects(spark: SparkSession):
    """A detector that has never fired is an untested detector (F7).

    Two versions of key 100 that both claim 2024, one of them open-ended. This is what a
    late-arriving change record inserted out of sequence produces, and it is invisible to any
    check that only compares adjacent rows.
    """
    corrupt = spark.createDataFrame(
        [
            (100, _ts("2023-01-01 00:00:00"), _ts("2025-01-01 00:00:00")),
            (100, _ts("2024-01-01 00:00:00"), None),
        ],
        f"product_id BIGINT, {START_COL} TIMESTAMP, {END_COL} TIMESTAMP",
    )
    assert overlapping_windows(corrupt, key="product_id").count() == 1


# ---------------------------------------------------------------------------------------------
# MOD-003 — the one the warehouse depends on
# ---------------------------------------------------------------------------------------------
def test_point_in_time_join_is_one_to_one(facts, dim):
    joined = point_in_time_join(facts, dim, key="product_id", fact_time="transaction_ts")

    assert joined.count() == facts.count(), "Point-in-time join changed the fact row count."
    per_fact = joined.groupBy("f.event_id").count().collect()
    assert {row["count"] for row in per_fact} == {1}, "A fact row matched more than one version."


def test_point_in_time_join_selects_the_version_in_force(facts, dim):
    joined = point_in_time_join(facts, dim, key="product_id", fact_time="transaction_ts").select(
        F.col("f.event_id"), F.col("d.commodity_desc")
    )
    resolved = {row["event_id"]: row["commodity_desc"] for row in joined.collect()}
    assert resolved == {
        "e1": "FRZN ICE",
        # The boundary case. With a closed upper bound this row matches two versions, and the
        # fan-out is invisible because it affects one row rather than all of them.
        "e2": "FRZN NOVELTY",
        "e3": "FRZN NOVELTY",
        "e4": "APPLES",
        "e5": "ORGANIC APPLES",
        "e6": "CHEESE",
    }


def test_naive_join_fans_out(facts, dim):
    """The bug, measured — so the 1:1 assertion above is known not to be vacuous."""
    naive = naive_key_join(facts, dim, key="product_id")

    # Five facts sit on keys with two versions each and one sits on a key with one, so six rows
    # become eleven. Note that the inflation factor is not uniform — it varies by key — which is
    # why the resulting totals stay plausible instead of looking like an obvious doubling.
    assert naive.count() == 11

    correct_total = facts.agg(F.sum("sales_amt")).collect()[0][0]
    inflated_total = naive.agg(F.sum("f.sales_amt")).collect()[0][0]
    # 360 against 210. Revenue overstated by 71%, and no single row in the output looks wrong.
    assert (correct_total, inflated_total) == (210.0, 360.0)


def test_point_in_time_join_leaves_no_orphans(facts, dim):
    """QLT-004 in miniature: every fact resolves, because version one predates every fact."""
    left = point_in_time_join(facts, dim, key="product_id", fact_time="transaction_ts", how="left")
    orphans = left.filter(F.col(f"d.{START_COL}").isNull())
    assert orphans.count() == 0


def test_fact_before_first_version_is_an_orphan_not_a_silent_match(spark: SparkSession, dim):
    """The inverse case, asserted because 'no orphans' must mean something.

    A fact predating the dimension's first version has no valid row. A left join reports it; an
    inner join deletes it and the revenue with it. QLT-004 exists so that choice is never made by
    accident.
    """
    early = spark.createDataFrame(
        [("e0", 100, _ts("2020-01-01 12:00:00"), 99.0)],
        "event_id STRING, product_id BIGINT, transaction_ts TIMESTAMP, sales_amt DOUBLE",
    )
    assert point_in_time_join(early, dim, key="product_id", fact_time="transaction_ts").count() == 0
    left = point_in_time_join(early, dim, key="product_id", fact_time="transaction_ts", how="left")
    assert left.count() == 1
    assert left.select(F.col(f"d.{START_COL}")).collect()[0][0] is None


# ---------------------------------------------------------------------------------------------
# ING-005 — idempotent dedupe
# ---------------------------------------------------------------------------------------------
def test_dedupe_is_idempotent_under_replay(spark: SparkSession):
    """Replaying a window leaves the target unchanged, and picks the same winner every time."""
    schema = "event_id STRING, sales_amt DOUBLE, _ingest_ts TIMESTAMP, _source_file STRING"
    original = spark.createDataFrame(
        [
            ("a", 1.0, _ts("2026-01-01 00:00:00"), "events-000001.json"),
            ("b", 2.0, _ts("2026-01-01 00:00:00"), "events-000001.json"),
        ],
        schema,
    )
    replayed = original.unionByName(
        spark.createDataFrame(
            [
                ("a", 1.0, _ts("2026-01-01 00:00:00"), "events-000002.json"),
                ("b", 2.0, _ts("2026-01-01 00:00:00"), "events-000002.json"),
            ],
            schema,
        )
    )

    keys = ["event_id"]
    sequence = ["_ingest_ts", "_source_file"]
    once = latest_per_key(original, keys=keys, sequence_cols=sequence)
    twice = latest_per_key(replayed, keys=keys, sequence_cols=sequence)

    assert once.count() == 2
    assert twice.count() == 2, "A replayed window duplicated rows."
    assert twice.agg(F.sum("sales_amt")).collect()[0][0] == 3.0


def test_dedupe_winner_is_deterministic_when_timestamps_tie(spark: SparkSession):
    """The tie-breaker earns its place.

    Replayed duplicates share an ingest timestamp. Without a second sequence column the winner is
    whichever row the shuffle happened to deliver last — the row count stays correct and the
    checksum does not, which is precisely the failure MOD-004 is written to catch.
    """
    schema = "event_id STRING, _ingest_ts TIMESTAMP, _source_file STRING"
    rows = [
        ("a", _ts("2026-01-01 00:00:00"), "events-000001.json"),
        ("a", _ts("2026-01-01 00:00:00"), "events-000002.json"),
    ]
    frame = spark.createDataFrame(rows, schema)
    winners = {
        latest_per_key(
            frame.repartition(n), keys=["event_id"], sequence_cols=["_ingest_ts", "_source_file"]
        ).collect()[0]["_source_file"]
        for n in (1, 2, 3, 4)
    }
    assert winners == {"events-000002.json"}
