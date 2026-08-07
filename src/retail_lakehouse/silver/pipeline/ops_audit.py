"""Ops — the three audit tables that make silver's correctness checkable rather than asserted.

`ops.row_conservation`  — QLT-003, enforced by a fail-the-update expectation with no tolerance.
`ops.join_coverage`     — QLT-004 and MOD-003, per join, per run.
`ops.dq_metrics`        — QLT-006, quality as a time series rather than a point check.

Why these are pipeline datasets rather than a post-run script
-------------------------------------------------------------
A check that runs after the pipeline is a check that can be skipped, forgotten, or run against a
different state than the one that was written. Expressing row conservation as an expectation on a
pipeline dataset makes the failure mode *the update fails*, which is the only signal anyone
reliably acts on. A script that prints "conservation violated" into a log is a script nobody reads.
"""

from __future__ import annotations

from pyspark import pipelines as dp
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

spark = SparkSession.getActiveSession()
assert spark is not None, "This module runs inside a Lakeflow pipeline."

CATALOG = spark.conf.get("dng.catalog")
SEED = spark.conf.get("dng.seed")
PIPELINE_ID = spark.conf.get("pipelines.id", "local")

_START = "__START_AT"
_END = "__END_AT"


# ---------------------------------------------------------------------------------------------
# QLT-003 — row conservation
# ---------------------------------------------------------------------------------------------
@dp.materialized_view(
    name="ops.row_conservation",
    comment=(
        "QLT-003. Asserts bronze_rows = duplicates_collapsed + passed_rows + quarantined_rows for "
        "the basket-line fact, with no tolerance: the expectation fails the update. The identity "
        "is deliberately stronger than input = passed + quarantined: folding deduplication "
        "into the input term would hide the number that proves the duplicate-replay scenario worked."
    ),
    table_properties={"quality": "ops"},
)
@dp.expect_or_fail(
    "row_conservation",
    "bronze_rows = duplicates_collapsed + passed_rows + quarantined_rows",
)
@dp.expect_or_fail(
    # A second, independent statement of the same invariant from the other direction. If the DQ
    # split ever leaks — a row landing in both halves, or in neither — the sum above can still
    # balance by coincidence while this one cannot.
    "split_is_a_partition",
    "distinct_event_ids = passed_rows + quarantined_rows",
)
def row_conservation() -> DataFrame:
    bronze = spark.read.table(f"{CATALOG}.bronze.basket_line_events_raw").agg(
        F.count(F.lit(1)).alias("bronze_rows"),
        F.countDistinct("event_id").alias("distinct_event_ids"),
    )
    passed = spark.read.table(f"{CATALOG}.silver.fact_basket_line").agg(
        F.count(F.lit(1)).alias("passed_rows")
    )
    quarantined = spark.read.table(f"{CATALOG}.silver.fact_basket_line_quarantine").agg(
        F.count(F.lit(1)).alias("quarantined_rows")
    )

    return (
        bronze.crossJoin(passed)
        .crossJoin(quarantined)
        .select(
            F.lit("silver.fact_basket_line").alias("dataset"),
            "bronze_rows",
            "distinct_event_ids",
            (F.col("bronze_rows") - F.col("distinct_event_ids")).alias("duplicates_collapsed"),
            "passed_rows",
            "quarantined_rows",
            F.round(
                100.0 * F.col("passed_rows") / F.greatest(F.col("distinct_event_ids"), F.lit(1)), 4
            ).alias("pass_rate_pct"),
            F.lit(PIPELINE_ID).alias("pipeline_id"),
            F.current_timestamp().alias("computed_at"),
        )
    )


# ---------------------------------------------------------------------------------------------
# QLT-004 + MOD-003 — join coverage
# ---------------------------------------------------------------------------------------------
def _coverage(
    name: str,
    facts: DataFrame,
    matched: DataFrame,
    *,
    key: str,
    referential: bool,
    note: str,
) -> DataFrame:
    """One coverage row. `matched` is the left-joined result; unmatched rows carry a NULL probe."""
    fact_totals = facts.agg(
        F.count(F.lit(1)).alias("fact_rows"),
        F.countDistinct(key).alias("fact_distinct_keys"),
    )
    match_totals = matched.agg(
        F.count(F.lit(1)).alias("joined_rows"),
        F.sum(F.when(F.col("_probe").isNotNull(), 1).otherwise(0)).alias("matched_rows"),
        F.countDistinct(F.when(F.col("_probe").isNotNull(), F.col(key))).alias(
            "matched_distinct_keys"
        ),
    )
    return fact_totals.crossJoin(match_totals).select(
        F.lit(name).alias("join_name"),
        "fact_rows",
        "joined_rows",
        "matched_rows",
        (F.col("fact_rows") - F.col("matched_rows")).alias("unmatched_rows"),
        "fact_distinct_keys",
        "matched_distinct_keys",
        (F.col("fact_distinct_keys") - F.col("matched_distinct_keys")).alias(
            "unmatched_distinct_keys"
        ),
        F.round(100.0 * F.col("matched_rows") / F.greatest(F.col("fact_rows"), F.lit(1)), 4).alias(
            "row_match_rate_pct"
        ),
        F.round(
            100.0
            * F.col("matched_distinct_keys")
            / F.greatest(F.col("fact_distinct_keys"), F.lit(1)),
            4,
        ).alias("key_match_rate_pct"),
        F.lit(referential).alias("is_referential_integrity_join"),
        F.lit(note).alias("note"),
        F.lit(PIPELINE_ID).alias("pipeline_id"),
        F.current_timestamp().alias("computed_at"),
    )


def _point_in_time(facts: DataFrame, dim: DataFrame, *, key: str) -> DataFrame:
    """Left join to a Type 2 dimension, valid at the fact's own event time.

    The `__START_AT <= transaction_ts < __END_AT` predicate is the entire difference between this
    and the join that inflates every measure in the warehouse. Half-open on the upper bound, so a
    fact landing exactly at a version boundary matches one version rather than two.
    """
    f = facts.alias("f")
    d = dim.select(key, _START, _END).withColumn("_probe", F.lit(1)).alias("d")
    condition = (
        (F.col(f"f.{key}") == F.col(f"d.{key}"))
        & (F.col("f.transaction_ts") >= F.col(f"d.{_START}"))
        & (F.col(f"d.{_END}").isNull() | (F.col("f.transaction_ts") < F.col(f"d.{_END}")))
    )
    return f.join(d, condition, "left").select("f.*", "_probe")


@dp.materialized_view(
    name="ops.join_coverage",
    comment=(
        "QLT-004 and MOD-003, per join, per run. Records how much of the fact table each "
        "dimension join actually matches, so a silent inner join never gets to decide. Finding "
        "F2 is why: causal_data covers 115 of 582 stores but 98.6% of lines, so an inner join "
        "loses 1.4% of revenue — invisible — and 80% of the store count — catastrophic for any "
        "store-count metric. Snapshot of the current run; the run-over-run series lives in "
        "ops.dq_metrics and in the pipeline event log."
    ),
    table_properties={"quality": "ops"},
)
@dp.expect_all_or_fail(
    {
        # MOD-003. If a point-in-time join fans out, the join produces more rows than the fact
        # table has. This is the assertion that stops the classic Type 2 bug reaching gold, and it
        # costs one comparison per run.
        "point_in_time_join_does_not_fan_out": "joined_rows <= fact_rows",
        # QLT-004. Zero orphans on the joins that claim referential integrity. The promotion
        # coverage row is explicitly not one of those — its whole purpose is to measure a gap.
        "no_orphan_facts": "NOT is_referential_integrity_join OR unmatched_rows = 0",
    }
)
def join_coverage() -> DataFrame:
    facts = spark.read.table(f"{CATALOG}.silver.fact_basket_line")
    products = spark.read.table(f"{CATALOG}.silver.dim_product_scd2")
    households = spark.read.table(f"{CATALOG}.silver.dim_household_scd2")
    stores = spark.read.table(f"{CATALOG}.silver.dim_store")

    product_join = _coverage(
        "fact_basket_line -> dim_product_scd2 (point-in-time)",
        facts,
        _point_in_time(facts, products, key="product_id"),
        key="product_id",
        referential=True,
        note="Validity-window join on transaction_ts. Orphans here would mean a product sold before its dimension row existed.",
    )
    household_join = _coverage(
        "fact_basket_line -> dim_household_scd2 (point-in-time)",
        facts,
        _point_in_time(facts, households, key="household_key"),
        key="household_key",
        referential=True,
        note="Covers all transacting households, not only the 801 with demographics (F4).",
    )
    store_join = _coverage(
        "fact_basket_line -> dim_store",
        facts,
        facts.alias("f")
        .join(
            stores.select("store_id").withColumn("_probe", F.lit(1)).alias("d"),
            F.col("f.store_id") == F.col("d.store_id"),
            "left",
        )
        .select("f.*", "_probe"),
        key="store_id",
        referential=True,
        note="Type 1 join. The dimension is derived from the full seed, so it is a superset of the sampled stores.",
    )

    # F2, measured rather than described. Deliberately NOT a referential-integrity join: the
    # unmatched rows are the finding, not a defect.
    promo = spark.read.parquet(f"{SEED}/causal_data.parquet")
    promo_keys = (
        promo.select(F.col("STORE_ID").cast("bigint").alias("store_id"))
        .distinct()
        .withColumn("_probe", F.lit(1))
    )
    promo_join = _coverage(
        "fact_basket_line -> causal_data store coverage",
        facts,
        facts.alias("f")
        .join(promo_keys.alias("d"), F.col("f.store_id") == F.col("d.store_id"), "left")
        .select("f.*", "_probe"),
        key="store_id",
        referential=False,
        note="F2. Expect a high row match rate and a low key match rate — that asymmetry is the whole point: an inner join here loses ~1.4% of lines and ~80% of stores.",
    )

    return product_join.unionByName(household_join).unionByName(store_join).unionByName(promo_join)


# ---------------------------------------------------------------------------------------------
# QLT-006 — quality as a time series
# ---------------------------------------------------------------------------------------------
_EXPECTATION_SCHEMA = (
    "array<struct<name:string, dataset:string, passed_records:bigint, failed_records:bigint>>"
)


@dp.materialized_view(
    name="ops.dq_metrics",
    comment=(
        "QLT-006. One row per dataset per pipeline update: rows written, expectation pass rate, "
        "and per-rule failure counts. Derived from the pipeline event log, which is the append-only "
        "record the platform keeps and therefore the durable time series — this view is a governed "
        "projection of it, not a second copy. A materialized view cannot append to itself, so "
        "history has to live somewhere that already accumulates. TRAILING BY ONE UPDATE: an update "
        "cannot observe its own completion events, so update N appears here during update N+1."
    ),
    table_properties={"quality": "ops"},
)
def dq_metrics() -> DataFrame:
    events = spark.read.table(f"{CATALOG}.ops.pipeline_events").filter(
        F.col("event_type") == "flow_progress"
    )
    parsed = events.select(
        F.col("origin.update_id").alias("pipeline_update_id"),
        F.col("origin.pipeline_id").alias("pipeline_id"),
        F.col("origin.flow_name").alias("dataset"),
        # The grain of a flow_progress event is (flow, batch), and the SAME batch is reported
        # several times as the flow advances — the metrics are cumulative for that batch, not
        # increments. Summing them over-counts by however many progress events the platform
        # happened to emit: the first version of this view reported 990,065 rows written to
        # fact_basket_line, which is 198,013 x 5. The batch id is what makes the deduplication
        # possible, so it has to be carried all the way through the aggregation.
        F.col("origin.batch_id").alias("batch_id"),
        F.col("timestamp"),
        F.get_json_object("details", "$.flow_progress.metrics.num_output_rows")
        .cast("bigint")
        .alias("output_rows"),
        # AUTO CDC flows do not report `num_output_rows`. They report `num_upserted_rows` and
        # `num_deleted_rows`, because an upsert has no single "output" count — a row can be
        # inserted, updated, or collapse into an existing one. Reading only num_output_rows would
        # have made every CDC-fed table look like it wrote nothing, which is exactly the kind of
        # metric that gets trusted for months before anyone notices it is always NULL.
        F.get_json_object("details", "$.flow_progress.metrics.num_upserted_rows")
        .cast("bigint")
        .alias("upserted_rows"),
        F.get_json_object("details", "$.flow_progress.metrics.num_deleted_rows")
        .cast("bigint")
        .alias("deleted_rows"),
        F.get_json_object("details", "$.flow_progress.data_quality.dropped_records")
        .cast("bigint")
        .alias("dropped_records"),
        F.from_json(
            F.get_json_object("details", "$.flow_progress.data_quality.expectations"),
            _EXPECTATION_SCHEMA,
        ).alias("expectations"),
    ).filter(
        # The event log carries an internal placeholder flow, `pipelines.flowTimeMetrics.
        # missingFlowName`, which is not a dataset. Restricting to names in this catalog keeps
        # the grain honest: one row per *dataset* per update, not one per event source.
        F.col("dataset").startswith(f"{CATALOG}.")
    )

    grain = ["pipeline_update_id", "pipeline_id", "dataset"]

    # Collapse each batch to its high-water mark, then add batches together. `max` within a batch
    # because the reports are cumulative; `sum` across batches because a streaming flow genuinely
    # processes several.
    flow_totals = (
        parsed.groupBy(*grain, "batch_id")
        .agg(
            F.max("timestamp").alias("last_event_at"),
            F.max("output_rows").alias("output_rows"),
            F.max("upserted_rows").alias("upserted_rows"),
            F.max("deleted_rows").alias("deleted_rows"),
            F.max("dropped_records").alias("dropped_records"),
        )
        .groupBy(*grain)
        .agg(
            F.max("last_event_at").alias("last_event_at"),
            F.sum("output_rows").alias("output_rows"),
            F.sum("upserted_rows").alias("upserted_rows"),
            F.sum("deleted_rows").alias("deleted_rows"),
            F.coalesce(F.sum("dropped_records"), F.lit(0)).alias("dropped_records"),
        )
    )

    # Same treatment per expectation. `explode_outer`, not `explode`, so a flow with no
    # expectations still produces a row and the join below stays total — otherwise the datasets
    # with nothing to check would silently vanish from the metrics table, which is the opposite of
    # what a quality time series is for.
    per_rule = (
        parsed.select(*grain, "batch_id", F.explode_outer("expectations").alias("expectation"))
        .groupBy(*grain, "batch_id", F.col("expectation.name").alias("rule_name"))
        .agg(
            F.max("expectation.passed_records").alias("passed"),
            F.max("expectation.failed_records").alias("failed"),
        )
        .groupBy(*grain, "rule_name")
        .agg(F.sum("passed").alias("passed"), F.sum("failed").alias("failed"))
    )

    expectation_totals = per_rule.groupBy(*grain).agg(
        F.count("rule_name").alias("expectations_evaluated"),
        F.coalesce(F.sum("passed"), F.lit(0)).alias("expectation_passes"),
        F.coalesce(F.sum("failed"), F.lit(0)).alias("expectation_failures"),
        # Per-rule counts kept without changing the grain. The alternative — one row per rule per
        # dataset per run — reads better in a notebook and makes "pass rate for this table" a
        # windowed query instead of a column.
        F.map_from_entries(
            F.collect_list(
                F.when(
                    F.col("rule_name").isNotNull(),
                    F.struct(F.col("rule_name").alias("key"), F.col("failed").alias("value")),
                )
            )
        ).alias("failures_by_rule"),
    )

    return flow_totals.join(expectation_totals, on=grain, how="inner").select(
        "pipeline_update_id",
        "pipeline_id",
        "dataset",
        "last_event_at",
        "output_rows",
        "upserted_rows",
        "deleted_rows",
        "dropped_records",
        "expectations_evaluated",
        "expectation_passes",
        "expectation_failures",
        F.round(
            100.0
            * F.col("expectation_passes")
            / F.greatest(F.col("expectation_passes") + F.col("expectation_failures"), F.lit(1)),
            4,
        ).alias("expectation_pass_rate_pct"),
        "failures_by_rule",
    )
