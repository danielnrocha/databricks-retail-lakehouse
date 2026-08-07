"""Gold — the aggregates that answer the North Star decisions directly.

Each table here names the decision it serves in its comment and its properties. A gold table that
cannot name a decision is a table somebody built because the data was there, and it will be
maintained forever by people who do not know what it is for.

`agg_household_rfm` (D1, D2)
    Recency, frequency, monetary value plus category breadth. Behavioural only, by necessity:
    only 801 of 2,500 households have demographics (finding F4), so a feature set that requires
    them would train on a self-selected third and would not transfer to the rest.

`agg_promo_performance` (D3)
    Sales split by promotion exposure, per product-week. The `unknown` bucket is carried through
    rather than filtered out, so the denominator of any lift calculation is visible.

`agg_store_daily` (D4)
    Daily store totals — the baseline an anomaly detector subtracts from.
"""

from __future__ import annotations

from pyspark import pipelines as dp
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

spark = SparkSession.getActiveSession()
assert spark is not None, "This module runs inside a Lakeflow pipeline."

CATALOG = spark.conf.get("dng.catalog")


def _fact() -> DataFrame:
    return spark.read.table(f"{CATALOG}.gold.fct_basket_line")


@dp.materialized_view(
    name="gold.agg_household_rfm",
    comment=(
        "Household behavioural profile: recency, frequency, monetary value and category breadth. "
        "Deliberately excludes demographics — only 801 of 2,500 households have them, so a model "
        "requiring them would train on a self-selected third. Demographics are available on the "
        "fact for measuring their incremental lift, never as a required input. Serves D1 (coupon "
        "targeting) and D2 (household lapse)."
    ),
    table_properties={"quality": "gold", "dng.decision": "D1,D2"},
    cluster_by_auto=True,
)
def agg_household_rfm() -> DataFrame:
    fact = _fact()
    # Recency is measured against the dataset's own maximum, not against today. The seed's DAY is
    # relative with no calendar anchor (ADR-0003), so "days since last purchase" is only meaningful
    # relative to the observation window's end. Anchoring to wall-clock time would make every
    # household look lapsed by however long ago the data was collected.
    #
    # The anchor is a single-row DataFrame cross-joined in, NOT `.collect()[0][0]`. The eager form
    # was here and produced `recency_days` null on all 2,337 rows: a Lakeflow function body is
    # evaluated when the graph is *planned*, and at that moment `fct_basket_line` is being built by
    # the same update and has no data. `collect()` therefore returned `None`, `F.lit(None)` typed
    # the anchor as null, and `datediff` propagated null through every row without erroring.
    #
    # It survived review, and it survived GOV-001 — the column carries a comment describing
    # behaviour it did not have. A documented column and a correct column are different claims.
    as_of = fact.agg(F.max("transaction_date").alias("_as_of"))

    return (
        fact.groupBy("household_key")
        .agg(
            F.max("transaction_date").alias("_last_seen"),
            F.countDistinct("basket_id").alias("frequency_baskets"),
            F.round(F.sum("sales_amt"), 2).alias("monetary_amt"),
            F.round(F.sum("sales_amt") / F.countDistinct("basket_id"), 2).alias("avg_basket_amt"),
            F.countDistinct("department").alias("distinct_departments"),
            F.countDistinct("commodity_desc").alias("distinct_commodities"),
            F.round(
                F.sum(F.when(F.col("coupon_disc_amt") != 0, F.col("sales_amt")).otherwise(0))
                / F.sum("sales_amt"),
                4,
            ).alias("coupon_share_of_spend"),
            F.round(
                F.sum(
                    F.when(
                        F.col("promo_exposure").isin("display", "mailer", "both"),
                        F.col("sales_amt"),
                    ).otherwise(0)
                )
                / F.sum("sales_amt"),
                4,
            ).alias("promo_share_of_spend"),
            F.max("household_has_demographics").alias("has_demographics"),
            F.min("transaction_date").alias("first_seen_date"),
            F.max("transaction_date").alias("last_seen_date"),
        )
        .crossJoin(as_of)
        .withColumn("recency_days", F.datediff(F.col("_as_of"), F.col("_last_seen")))
        .drop("_as_of", "_last_seen")
        .select(
            "household_key",
            "recency_days",
            "frequency_baskets",
            "monetary_amt",
            "avg_basket_amt",
            "distinct_departments",
            "distinct_commodities",
            "coupon_share_of_spend",
            "promo_share_of_spend",
            "has_demographics",
            "first_seen_date",
            "last_seen_date",
        )
    )


@dp.materialized_view(
    name="gold.agg_promo_performance",
    comment=(
        "Sales by promotion exposure per product-week. The 'unknown' bucket — weeks outside the "
        "9..101 window where exposure was never recorded — is carried through rather than "
        "filtered, so any lift calculation has a visible denominator. Serves D3 (mid-flight promo "
        "performance)."
    ),
    table_properties={"quality": "gold", "dng.decision": "D3"},
    cluster_by=["week_no", "product_id"],
)
def agg_promo_performance() -> DataFrame:
    return (
        _fact()
        .groupBy("week_no", "product_id", "department", "promo_exposure")
        .agg(
            F.round(F.sum("sales_amt"), 2).alias("sales_amt"),
            F.sum("quantity_units").alias("quantity_units"),
            F.countDistinct("basket_id").alias("baskets"),
            F.countDistinct("household_key").alias("households"),
            F.countDistinct("store_id").alias("stores"),
            F.round(F.sum("retail_disc_amt"), 2).alias("retail_disc_amt"),
        )
    )


@dp.materialized_view(
    name="gold.agg_store_daily",
    comment=(
        "Daily store totals — the baseline a store-anomaly detector subtracts from. Carries "
        "has_promo_exposure_data via store_volume_decile so a store's absence from the promotion "
        "feed is not mistaken for a drop in trade. Serves D4 (store anomaly)."
    ),
    table_properties={"quality": "gold", "dng.decision": "D4"},
    cluster_by=["transaction_date", "store_id"],
)
def agg_store_daily() -> DataFrame:
    return (
        _fact()
        .groupBy("transaction_date", "store_id", "store_volume_decile")
        .agg(
            F.round(F.sum("sales_amt"), 2).alias("sales_amt"),
            F.countDistinct("basket_id").alias("baskets"),
            F.countDistinct("household_key").alias("households"),
            F.count("*").alias("lines"),
            F.round(F.sum("sales_amt") / F.countDistinct("basket_id"), 2).alias("avg_basket_amt"),
        )
    )


@dp.materialized_view(
    name="gold.gold_reconciliation",
    comment=(
        "Proves gold did not invent or lose money. Compares gold's fact row count and revenue "
        "against silver's. A non-zero variance means a join changed the grain — which is the "
        "failure mode point-in-time joins exist to prevent, so it is asserted rather than assumed."
    ),
    table_properties={"quality": "ops"},
)
def gold_reconciliation() -> DataFrame:
    gold = _fact().agg(
        F.count("*").alias("gold_rows"), F.round(F.sum("sales_amt"), 2).alias("gold_revenue")
    )
    silver = spark.read.table(f"{CATALOG}.silver.fact_basket_line").agg(
        F.count("*").alias("silver_rows"), F.round(F.sum("sales_amt"), 2).alias("silver_revenue")
    )
    return (
        gold.crossJoin(silver)
        .withColumn("row_variance", F.col("gold_rows") - F.col("silver_rows"))
        .withColumn("revenue_variance", F.round(F.col("gold_revenue") - F.col("silver_revenue"), 2))
        .withColumn("reconciles", (F.col("row_variance") == 0) & (F.col("revenue_variance") == 0))
        .withColumn("measured_at", F.current_timestamp())
    )
