"""Silver — the product dimension as SCD Type 2, built by AUTO CDC.

The change feed is synthetic, and that is stated everywhere it could be mistaken for real
--------------------------------------------------------------------------------------
The dunnhumby seed ships `product` as a **single static snapshot**. There is no history in it, so
there is nothing for an SCD Type 2 to track. Rather than pretend otherwise, this module derives a
two-version change feed from the snapshot and labels it: every row carries `change_reason` and
`is_synthetic_change`, the table comment says so, and the second version's `change_reason` is
literally `synthetic_reclassification`.

The alternative — quietly loading the snapshot once and calling the result "SCD Type 2" — would
produce a dimension where every key has exactly one version. Every point-in-time join would then
pass, including the broken one, because a fan-out of ×1 is indistinguishable from no fan-out.
A Type 2 dimension with no history does not test anything; it only looks like it does.

What the synthetic change models
--------------------------------
A merchandising hierarchy reclassification: `commodity_desc`, `sub_commodity_desc` and pack size
change while `product_id` stays the same. This is the most common real source of Type 2 versions
in grocery and it is the one with teeth — a fact from before the change belongs under the old
commodity, and a query that joins on `product_id` alone reports it under both.

Which products change is `product_id % 50 = 7`: deterministic, reproducible on a full refresh, and
independent of any hash implementation. About 2% of the catalogue.

Why a file stream rather than a batch read
------------------------------------------
`create_auto_cdc_flow` needs an append-only streaming source. A file-source stream over the seed
volume gives exactly that: the file is read once, tracked in the flow's checkpoint, and a re-run
finds nothing new — so the SCD2 history is stable across runs rather than being rebuilt with new
timestamps each time. The schema is declared explicitly rather than inferred, because streaming
file sources require it and because an inferred type is an unrecorded assumption.
"""

from __future__ import annotations

from pyspark import pipelines as dp
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

spark = SparkSession.getActiveSession()
assert spark is not None, "This module runs inside a Lakeflow pipeline."

CATALOG = spark.conf.get("dng.catalog")
SEED = spark.conf.get("dng.seed")
DATASET = "silver.dim_product_scd2"

# Types mirror the Parquet the seed loader wrote. Declared, not inferred: a streaming file source
# requires a schema, and stating it here means a silent upstream retype fails loudly instead of
# arriving as a NULL column.
PRODUCT_SCHEMA = (
    "PRODUCT_ID BIGINT, MANUFACTURER INT, DEPARTMENT STRING, BRAND STRING, "
    "COMMODITY_DESC STRING, SUB_COMMODITY_DESC STRING, CURR_SIZE_OF_PRODUCT STRING"
)

# The initial version predates every event in the fact table (bronze starts 2024-01-01), so every
# fact resolves to some version. If it did not, QLT-004 would fail for reasons that have nothing to
# do with data quality — an artefact of the fixture rather than a finding.
INITIAL_AT = "2023-01-01 00:00:00"
RECLASSIFIED_AT = "2025-01-01 00:00:00"

_CHANGES = F.expr("product_id % 50 = 7")

# F7: 15 seed products have a blank department. Mapping them to an explicit member means
# department roll-ups sum to the grand total by construction rather than by luck. Leaving them
# null is how a department report quietly stops reconciling with the total sitting next to it.
_UNCATEGORISED = "UNCATEGORISED"


def _blank_to(column: str, fallback: str):
    trimmed = F.trim(F.col(column))
    return F.when((trimmed == "") | trimmed.isNull(), F.lit(fallback)).otherwise(trimmed)


def _version(sequence: str, reason: str, *, reclassified: bool):
    """One version of a product row, as a struct the change feed explodes."""
    commodity = _blank_to("COMMODITY_DESC", _UNCATEGORISED)
    sub_commodity = _blank_to("SUB_COMMODITY_DESC", _UNCATEGORISED)
    size = _blank_to("CURR_SIZE_OF_PRODUCT", "UNKNOWN")

    if reclassified:
        commodity = F.concat(commodity, F.lit(" / RECLASSIFIED"))
        sub_commodity = F.concat(sub_commodity, F.lit(" / RECLASSIFIED"))
        size = F.concat(size, F.lit(" (REPACKED)"))

    return F.struct(
        commodity.alias("commodity_desc"),
        sub_commodity.alias("sub_commodity_desc"),
        size.alias("curr_size_of_product"),
        F.lit(reason).alias("change_reason"),
        F.lit(reclassified).alias("is_synthetic_change"),
        F.lit(sequence).cast("timestamp").alias("_cdc_ts"),
    )


@dp.temporary_view(name="product_change_feed")
@dp.expect_all_or_fail(
    {
        # Dimension rules are invariants, not filters. There is no useful "partially loaded
        # dimension" state, so a violation stops the update rather than quarantining a row and
        # leaving every downstream join to resolve against a hole.
        "product_id_present": "product_id IS NOT NULL",
        "department_never_null": "department IS NOT NULL",
        "brand_tier_is_binary": "brand_tier IN ('National', 'Private')",
    }
)
def product_change_feed() -> DataFrame:
    snapshot = (
        spark.readStream.format("parquet")
        .schema(PRODUCT_SCHEMA)
        .option("pathGlobFilter", "product.parquet")
        .load(SEED)
    )
    versions = F.array_compact(
        F.array(
            _version(INITIAL_AT, "initial_load", reclassified=False),
            F.when(
                _CHANGES, _version(RECLASSIFIED_AT, "synthetic_reclassification", reclassified=True)
            ),
        )
    )
    return snapshot.select(
        F.col("PRODUCT_ID").alias("product_id"),
        F.col("MANUFACTURER").alias("manufacturer_id"),
        _blank_to("DEPARTMENT", _UNCATEGORISED).alias("department"),
        # F5: the seed calls this BRAND, but it holds two values — National vs Private label.
        # It is a tier flag, not a brand name, and the real brand-ish dimension is
        # MANUFACTURER at 6,476 distinct values. Renaming it here is the whole fix: a gold
        # metric called "sales by brand" fed by the original name would answer a different
        # question than the one asked, and nothing would ever flag it.
        F.col("BRAND").alias("brand_tier"),
        F.explode(versions).alias("_v"),
    ).select("product_id", "manufacturer_id", "department", "brand_tier", "_v.*")


dp.create_streaming_table(
    name=DATASET,
    comment=(
        "Product dimension, SCD Type 2. The change feed is SYNTHETIC: the dunnhumby seed ships a "
        "single static snapshot with no history, so a second version is derived for products "
        "where product_id % 50 = 7, modelling a merchandising reclassification. Every row carries "
        "change_reason and is_synthetic_change. Join to facts on product_id AND the validity "
        "window (__START_AT <= transaction_ts < __END_AT) — joining on product_id alone fans out "
        "every fact by the number of versions and inflates every sum (MOD-003). brand_tier is the "
        "BRAND column of the seed, renamed: it is National-vs-Private label, not a brand name (F5)."
    ),
    table_properties={"quality": "silver"},
    # Point-in-time joins filter on the key and range-scan the window. ADR-0007 names
    # (product_id, is_current) as the keys; AUTO CDC does not produce an is_current column — the
    # generated pair is __START_AT/__END_AT, with a NULL end meaning current. The clustering key
    # follows what the join actually predicates on rather than what the ADR guessed before the
    # table existed.
    cluster_by=["product_id", "__START_AT"],
)

dp.create_auto_cdc_flow(
    target=DATASET,
    source="product_change_feed",
    keys=["product_id"],
    sequence_by=F.col("_cdc_ts"),
    except_column_list=["_cdc_ts"],
    stored_as_scd_type="2",
)
