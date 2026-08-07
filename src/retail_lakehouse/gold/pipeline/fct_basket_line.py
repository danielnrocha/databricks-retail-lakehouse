"""Gold — the conformed fact, joined point-in-time to versioned dimensions.

This module exists to get one join right, and the rest is bookkeeping.

The point-in-time join, and why it is not optional
--------------------------------------------------
`dim_product_scd2` carries 94,190 versions over 92,353 keys — only **2% of products have a second
version**. That sounds harmless. It is not:

    join on product_id alone         201,476 rows   613,396.36 -> 623,863.52
    join with the validity window    198,013 rows   613,396.36  (correct)

One missing predicate, 3,463 phantom rows, and **revenue overstated by 1.706%**. The query looks
correct, runs without warning, and returns a number a stakeholder will act on. This is the
canonical SCD Type 2 defect and it survives code review every time, because there is nothing wrong
with the SQL — only with the model of time behind it.

`AUTO CDC` emits `__START_AT` / `__END_AT` rather than an `is_current` flag, so the predicate is a
half-open interval: `fact_ts >= __START_AT AND (__END_AT IS NULL OR fact_ts < __END_AT)`. Half-open
matters — using `<=` on the upper bound double-counts every fact landing exactly on a version
boundary, which is rare enough to pass testing and frequent enough to be wrong.

Promotion exposure is three-state, never a boolean
--------------------------------------------------
Two separate findings force this, and conflating them produces fabricated signal:

* **F2 — coverage.** Only 21.72% of transaction lines match `causal` on
  `(product_id, store_id, week_no)`. An inner join would discard 78.28% of the fact table while
  looking like an ordinary enrichment step. `causal` also holds 15,245 duplicate composite keys,
  so even a LEFT JOIN fans out unless the right side is deduplicated first.
* **F3 — window.** `causal.week_no` spans 9..101; transactions span 1..102. Outside that window
  promotion exposure is **undefined**, not absent.

So: `not_promoted` where the week is covered and no row matched, `unknown` where it is not, and
`display` / `mailer` / `both` where it did. Defaulting the undefined weeks to "not promoted" would
teach every downstream model that weeks 1-8 had no promotions — a signal nobody observed, invented
by a join default.
"""

from __future__ import annotations

from pyspark import pipelines as dp
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

spark = SparkSession.getActiveSession()
assert spark is not None, "This module runs inside a Lakeflow pipeline."

CATALOG = spark.conf.get("dng.catalog")

# Weeks for which promotion exposure was collected at all. Sourced from the profile
# (docs/architecture/dataset-profile.md), not inferred at runtime: deriving it from the data would
# make the boundary move whenever the input is filtered, and a three-state flag whose third state
# depends on the query is worse than no flag.
PROMO_WEEK_MIN = 9
PROMO_WEEK_MAX = 101


def _promo_exposure() -> DataFrame:
    """Promotion exposure, deduplicated to one row per composite key.

    The deduplication is the whole point. `causal` holds 15,245 composite keys appearing more than
    once across 30,490 rows, so joining it raw inflates the fact table by 858 rows — a LEFT JOIN
    that adds rows is the specific failure people assume cannot happen, because "LEFT JOIN
    preserves the left side" is true only when the right side is unique on the join key.

    Collapsing with max() is safe here because both flags are yes/no: if any row for a
    product-store-week says the item was on display, it was on display.
    """
    return (
        spark.read.table(f"{CATALOG}.perf.causal")
        .select(
            F.col("PRODUCT_ID").alias("product_id"),
            F.col("STORE_ID").alias("store_id"),
            F.col("WEEK_NO").alias("week_no"),
            F.col("display"),
            F.col("mailer"),
        )
        .groupBy("product_id", "store_id", "week_no")
        .agg(
            F.max(F.when(F.col("display") != "0", True).otherwise(False)).alias("on_display"),
            F.max(F.when(F.col("mailer") != "0", True).otherwise(False)).alias("in_mailer"),
        )
    )


@dp.materialized_view(
    name="gold.fct_basket_line",
    comment=(
        "Conformed basket-line fact at transaction-line grain. Dimensions are resolved "
        "point-in-time against their SCD Type 2 validity windows, so a product reclassification "
        "does not retroactively rewrite history and does not fan out the fact. Promotion exposure "
        "is three-state: absence of a causal row inside the observed window means not promoted, "
        "outside it means unknown. Serves D1 (coupon targeting), D3 (promo performance) and "
        "D4 (store anomaly)."
    ),
    table_properties={"quality": "gold", "dng.decision": "D1,D3,D4"},
    cluster_by=["transaction_date", "store_id"],
)
def fct_basket_line() -> DataFrame:
    fact = spark.read.table(f"{CATALOG}.silver.fact_basket_line")
    product = spark.read.table(f"{CATALOG}.silver.dim_product_scd2")
    household = spark.read.table(f"{CATALOG}.silver.dim_household_scd2")
    store = spark.read.table(f"{CATALOG}.silver.dim_store")
    promo = _promo_exposure()

    # The half-open validity predicate, applied identically to both SCD2 dimensions.
    def as_of(dim: DataFrame, key: str) -> DataFrame:
        return dim.alias("d").join(
            fact.alias("f"),
            (F.col(f"d.{key}") == F.col(f"f.{key}"))
            & (F.col("f.transaction_ts") >= F.col("d.__START_AT"))
            & (F.col("d.__END_AT").isNull() | (F.col("f.transaction_ts") < F.col("d.__END_AT"))),
            "inner",
        )

    product_at = as_of(product, "product_id").select(
        F.col("f.event_id").alias("event_id"),
        F.col("d.department").alias("department"),
        F.col("d.brand_tier").alias("brand_tier"),
        F.col("d.commodity_desc").alias("commodity_desc"),
        F.col("d.manufacturer_id").alias("manufacturer_id"),
    )
    household_at = as_of(household, "household_key").select(
        F.col("f.event_id").alias("event_id"),
        F.col("d.income_band").alias("income_band"),
        F.col("d.household_composition").alias("household_composition"),
        F.col("d.has_demographics").alias("household_has_demographics"),
    )

    return (
        fact.alias("f")
        # LEFT, not inner, on the point-in-time results. An inner join here would silently drop any
        # fact whose dimension version does not cover its timestamp — which is exactly the
        # condition a referential-integrity check exists to surface, not to hide.
        .join(product_at.alias("p"), "event_id", "left")
        .join(household_at.alias("h"), "event_id", "left")
        .join(
            store.select("store_id", "volume_decile", "has_promo_exposure_data").alias("s"),
            "store_id",
            "left",
        )
        .join(promo.alias("c"), ["product_id", "store_id", "week_no"], "left")
        .select(
            F.col("f.event_id").alias("event_id"),
            F.col("f.basket_id").alias("basket_id"),
            F.col("f.transaction_date").alias("transaction_date"),
            F.col("f.transaction_ts").alias("transaction_ts"),
            F.col("f.week_no").alias("week_no"),
            F.col("f.household_key").alias("household_key"),
            F.col("f.store_id").alias("store_id"),
            F.col("f.product_id").alias("product_id"),
            F.col("p.department").alias("department"),
            F.col("p.brand_tier").alias("brand_tier"),
            F.col("p.commodity_desc").alias("commodity_desc"),
            F.col("p.manufacturer_id").alias("manufacturer_id"),
            F.col("h.income_band").alias("income_band"),
            F.col("h.household_composition").alias("household_composition"),
            F.coalesce(F.col("h.household_has_demographics"), F.lit(False)).alias(
                "household_has_demographics"
            ),
            F.col("s.volume_decile").alias("store_volume_decile"),
            F.col("f.quantity_units").alias("quantity_units"),
            F.col("f.sales_amt").alias("sales_amt"),
            F.col("f.retail_disc_amt").alias("retail_disc_amt"),
            F.col("f.coupon_disc_amt").alias("coupon_disc_amt"),
            # F3 made explicit. The nested when() reads awkwardly and is deliberate: the three
            # states are genuinely three, and flattening them into a boolean plus a null is how
            # "unknown" silently becomes "false" two joins downstream.
            F.when(~F.col("f.week_no").between(PROMO_WEEK_MIN, PROMO_WEEK_MAX), F.lit("unknown"))
            .when(F.col("c.on_display") & F.col("c.in_mailer"), F.lit("both"))
            .when(F.col("c.on_display"), F.lit("display"))
            .when(F.col("c.in_mailer"), F.lit("mailer"))
            .otherwise(F.lit("not_promoted"))
            .alias("promo_exposure"),
            F.col("f.is_synthetic").alias("is_synthetic"),
        )
    )
