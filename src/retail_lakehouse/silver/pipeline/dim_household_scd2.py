"""Silver — the household dimension as SCD Type 2, built by AUTO CDC.

The modelling decision that matters here is the 68% gap
-------------------------------------------------------
2,500 households transact. 801 of them — 32% — have demographic attributes (finding F4). That gap
is a property of the business, not a defect: demographics come from a loyalty profile that most
households never complete. It is therefore modelled, not filled in and not filtered out.

The dimension is keyed on **household**, and every transacting household has a row. Demographic
attributes are nullable, and `has_demographics` says which is which. Three consequences follow,
and all three are the reason this shape was chosen:

* Referential integrity is satisfiable. QLT-004 asks for zero fact rows whose foreign key has no
  dimension row valid at the fact's event time. A dimension containing only the 801 would orphan
  68% of the fact table — and the temptation would then be to inner-join it away, which silently
  drops two thirds of the households from every household-level metric.
* "No demographics" and "demographics unknown" stay distinguishable. Imputing a modal age band
  would make the dimension complete and the model wrong, and nothing downstream could tell.
* The churn model can use behavioural features for 100% of households and treat demographics as
  measurable optional lift (F4), rather than being trained on a self-selected third.

The change feed is synthetic, and labelled
------------------------------------------
As with `dim_product_scd2`, the seed is a static snapshot with no history. Three versions are
derived, each labelled in `change_reason`:

1. `household_universe` — every transacting household, no demographics, from the start of time.
2. `synthetic_demographic_enrolment` — the 801 gain their attributes partway through the window.
3. `synthetic_demographic_update` — about 5% of those report a household-size change later.

Version 1 predating the fact window is what makes the point-in-time join interesting: a purchase
made before enrolment correctly resolves to a version with no demographics. Backfilling attributes
to the beginning of time would be the classic Type 2 mistake — it makes historical analysis look
tidy by attributing knowledge the business did not have yet, which is leakage (MLR-006).
"""

from __future__ import annotations

from pyspark import pipelines as dp
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

spark = SparkSession.getActiveSession()
assert spark is not None, "This module runs inside a Lakeflow pipeline."

CATALOG = spark.conf.get("dng.catalog")
SEED = spark.conf.get("dng.seed")
DATASET = "silver.dim_household_scd2"

# Only the key is projected from the 2.6M-row transaction file. A streaming file source projects
# the declared schema and ignores everything else, so this reads one column rather than twelve.
TRANSACTION_KEY_SCHEMA = "household_key INT"

# The seed's own column names are anonymised — `classification_1` through `classification_5` —
# and so are the values. The semantics below are inferred from two independent signals that agree:
# column position against the documented dunnhumby layout, and cardinality (3 marital codes, 12
# income levels, 5 household sizes, 6 composition groups). Note that positions 5 and 6 are named
# classification_5 and classification_4 respectively, so name order is NOT position order — which
# is exactly why the inference is recorded here rather than assumed.
HH_DEMOGRAPHIC_SCHEMA = (
    "classification_1 STRING, classification_2 STRING, classification_3 STRING, "
    "HOMEOWNER_DESC STRING, classification_5 STRING, classification_4 STRING, "
    "KID_CATEGORY_DESC STRING, household_key BIGINT"
)

UNIVERSE_AT = "2023-01-01 00:00:00"
ENROLLED_AT = "2024-07-01 00:00:00"
UPDATED_AT = "2025-03-01 00:00:00"

_UPDATES = F.expr("household_key % 20 = 3")


def _feed_row(
    *,
    age_band,
    marital_status_code,
    income_band,
    homeowner_desc,
    household_composition,
    household_size_code,
    kid_category_desc,
    has_demographics,
    change_reason: str,
    is_synthetic_change: bool,
    cdc_ts: str,
) -> list:
    """The change-feed column list, in one place.

    The two source streams are unioned, so their projections have to agree exactly on names,
    order and type. Building both from one function is the cheap way to guarantee that; the
    alternative is a `unionByName` that silently succeeds with a column pair transposed.
    """
    return [
        age_band.alias("age_band"),
        marital_status_code.alias("marital_status_code"),
        income_band.alias("income_band"),
        homeowner_desc.alias("homeowner_desc"),
        household_composition.alias("household_composition"),
        household_size_code.alias("household_size_code"),
        kid_category_desc.alias("kid_category_desc"),
        has_demographics.alias("has_demographics"),
        F.lit(change_reason).alias("change_reason"),
        F.lit(is_synthetic_change).alias("is_synthetic_change"),
        F.lit(cdc_ts).cast("timestamp").alias("_cdc_ts"),
    ]


_NULL_STRING = F.lit(None).cast("string")


@dp.temporary_view(name="household_change_feed")
@dp.expect_all_or_fail(
    {
        "household_key_present": "household_key IS NOT NULL",
        "demographics_flag_matches_payload": "has_demographics = (age_band IS NOT NULL)",
    }
)
def household_change_feed() -> DataFrame:
    # Version 1 — the household universe. Derived from the seed transactions rather than from the
    # event stream, because the business universe is "households that shop here", not "households
    # that appear in the sample the amplifier happened to draw".
    universe = (
        spark.readStream.format("parquet")
        .schema(TRANSACTION_KEY_SCHEMA)
        .option("pathGlobFilter", "transaction_data.parquet")
        .load(SEED)
        # Stateful dedup rather than an aggregation: `distinct` on a stream is an aggregation and
        # produces complete-mode output, which an append-only CDC source cannot consume.
        .dropDuplicates(["household_key"])
        .select(
            F.col("household_key").cast("bigint").alias("household_key"),
            *_feed_row(
                age_band=_NULL_STRING,
                marital_status_code=_NULL_STRING,
                income_band=_NULL_STRING,
                homeowner_desc=_NULL_STRING,
                household_composition=_NULL_STRING,
                household_size_code=_NULL_STRING,
                kid_category_desc=_NULL_STRING,
                has_demographics=F.lit(False),
                change_reason="household_universe",
                is_synthetic_change=False,
                cdc_ts=UNIVERSE_AT,
            ),
        )
    )

    demographics = (
        spark.readStream.format("parquet")
        .schema(HH_DEMOGRAPHIC_SCHEMA)
        .option("pathGlobFilter", "hh_demographic.parquet")
        .load(SEED)
    )

    enrolled = demographics.select(
        F.col("household_key"),
        *_feed_row(
            age_band=F.col("classification_1"),
            marital_status_code=F.col("classification_2"),
            income_band=F.col("classification_3"),
            homeowner_desc=F.col("HOMEOWNER_DESC"),
            household_composition=F.col("classification_5"),
            household_size_code=F.col("classification_4"),
            kid_category_desc=F.col("KID_CATEGORY_DESC"),
            has_demographics=F.lit(True),
            change_reason="synthetic_demographic_enrolment",
            is_synthetic_change=True,
            cdc_ts=ENROLLED_AT,
        ),
    )

    # A household grows. Size and kid category move together, because a change to one without the
    # other would be an internally inconsistent version — and a dimension that generates
    # impossible states teaches the wrong lesson about what Type 2 history looks like.
    updated = demographics.filter(_UPDATES).select(
        F.col("household_key"),
        *_feed_row(
            age_band=F.col("classification_1"),
            marital_status_code=F.col("classification_2"),
            income_band=F.col("classification_3"),
            homeowner_desc=F.col("HOMEOWNER_DESC"),
            household_composition=F.col("classification_5"),
            household_size_code=F.lit("5+"),
            kid_category_desc=F.lit("3+"),
            has_demographics=F.lit(True),
            change_reason="synthetic_demographic_update",
            is_synthetic_change=True,
            cdc_ts=UPDATED_AT,
        ),
    )

    return universe.unionByName(enrolled).unionByName(updated)


dp.create_streaming_table(
    name=DATASET,
    comment=(
        "Household dimension, SCD Type 2. Covers every household in the seed transactions; "
        "demographic attributes are populated for the 801 of 2,500 that have them (F4) and are "
        "NULL otherwise, with has_demographics distinguishing 'no profile' from 'unknown'. The "
        "change feed is SYNTHETIC: enrolment and a later household-size update are derived from a "
        "static snapshot, labelled in change_reason and is_synthetic_change. Join to facts on "
        "household_key AND the validity window; a purchase before enrolment correctly resolves to "
        "a version with no demographics."
    ),
    table_properties={"quality": "silver"},
    cluster_by=["household_key", "__START_AT"],
)

dp.create_auto_cdc_flow(
    target=DATASET,
    source="household_change_feed",
    keys=["household_key"],
    sequence_by=F.col("_cdc_ts"),
    except_column_list=["_cdc_ts"],
    stored_as_scd_type="2",
)
