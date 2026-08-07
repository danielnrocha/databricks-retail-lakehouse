"""Silver — the store dimension, derived, because the seed has no store master.

There is no `store.csv`. `STORE_ID` appears in `transaction_data` and in `causal_data` and nowhere
else, so the dimension is *inferred from behaviour*: a store is a site that has transactions, and
its attributes are what those transactions say about it.

Two decisions worth defending
-----------------------------
**Type 1, not Type 2.** Every attribute here is a derived aggregate over the whole seed window.
Versioning a derived aggregate does not record business history, it records recomputation history
— you would get a new version every time the input grew, with no business event behind it. Type 2
is for attributes that *change*, and "how many lines has this store ever rung" does not change,
it accumulates.

**Derived from the seed transactions, not from the event stream.** The amplifier resamples 260 of
the 582 stores. A dimension built from the events would know about 260 sites and would make the
other 322 look closed rather than unsampled.

`has_promo_exposure_data` is finding F2, made queryable
-------------------------------------------------------
`causal_data` — promotion exposure, display and mailer — covers 115 of 582 stores. Those 115 carry
98.6% of transaction lines. So an inner join of transactions to promotion exposure loses 1.4% of
revenue, which nobody notices, while losing 80% of the store count, which destroys any
store-count metric sitting next to it.

The flag makes that a filter the analyst chooses rather than a join that decides for them.
`ops.join_coverage` records the match rate every run, so a change in coverage is a visible event
rather than a number that quietly moved.
"""

from __future__ import annotations

from pyspark import pipelines as dp
from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

spark = SparkSession.getActiveSession()
assert spark is not None, "This module runs inside a Lakeflow pipeline."

SEED = spark.conf.get("dng.seed")
DATASET = "silver.dim_store"


@dp.materialized_view(
    name=DATASET,
    comment=(
        "Store dimension, derived from seed transactions because the dunnhumby seed ships no "
        "store master. Type 1 by design: every attribute is a derived aggregate, and versioning "
        "one records recomputation rather than business history. has_promo_exposure_data marks "
        "the 115 of 582 stores that causal_data covers — those 115 carry 98.6% of lines, so an "
        "inner join to promotion exposure loses 1.4% of revenue and 80% of the stores (F2). "
        "Deliberately holds no monetary measures: measures belong in gold facts, not in a "
        "dimension."
    ),
    table_properties={"quality": "silver"},
)
@dp.expect_all_or_fail(
    {
        "store_id_present": "store_id IS NOT NULL",
        "store_activity_is_positive": "transaction_lines > 0",
    }
)
def dim_store() -> DataFrame:
    transactions = spark.read.parquet(f"{SEED}/transaction_data.parquet")
    promo = spark.read.parquet(f"{SEED}/causal_data.parquet")

    promo_stores = promo.groupBy("STORE_ID").agg(
        F.countDistinct("WEEK_NO").cast("int").alias("promo_weeks_observed")
    )

    activity = transactions.groupBy("STORE_ID").agg(
        F.count(F.lit(1)).cast("bigint").alias("transaction_lines"),
        F.countDistinct("household_key").cast("bigint").alias("distinct_households"),
        F.countDistinct("PRODUCT_ID").cast("bigint").alias("distinct_products"),
        F.countDistinct("BASKET_ID").cast("bigint").alias("distinct_baskets"),
        F.min("WEEK_NO").cast("int").alias("first_week_no"),
        F.max("WEEK_NO").cast("int").alias("last_week_no"),
        F.countDistinct("WEEK_NO").cast("int").alias("active_weeks"),
    )

    # Volume decile, so downstream work can talk about the store size distribution without
    # rediscovering it. F1 measured a 2,519x max/median ratio on store line counts — a real
    # grocery power law, not an artefact — and a decile is the cheapest way to make that
    # queryable rather than a fact everyone has to be told.
    by_volume = Window.orderBy(F.col("transaction_lines").desc())

    return (
        activity.join(promo_stores, on="STORE_ID", how="left")
        .withColumn("volume_decile", F.ntile(10).over(by_volume).cast("int"))
        .select(
            F.col("STORE_ID").cast("bigint").alias("store_id"),
            "transaction_lines",
            "distinct_households",
            "distinct_products",
            "distinct_baskets",
            "first_week_no",
            "last_week_no",
            "active_weeks",
            "volume_decile",
            F.col("promo_weeks_observed").isNotNull().alias("has_promo_exposure_data"),
            F.coalesce(F.col("promo_weeks_observed"), F.lit(0)).alias("promo_weeks_observed"),
            F.lit("derived from seed transaction_data; no store master exists").alias(
                "derivation_note"
            ),
        )
    )
