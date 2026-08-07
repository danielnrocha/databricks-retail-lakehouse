"""Part 3 — the spill experiment.

Spill is proven by `spilled_local_bytes > 0` (`spill_to_disk_bytes` in the query history API).
Nothing else counts: a query being slow is not evidence of spill, and on serverless there is no
Spark UI to appeal to.

The warehouse is fixed at 2X-Small, so "add memory" is not a lever. That is deliberate — it
forces the interventions to be the ones a data engineer controls in code: how wide the rows
crossing an operator are, how many rows reach it, and whether the operator is forced onto a
single task.

**The first result was a null result.** None of the obvious pressure sources on the native seed
spill a single byte: a wide window join, a global ordering of all 36,786,524 `causal` rows, and
a 36,771,279-group hash aggregate all report `spilled_local_bytes = 0`. `causal` is five narrow
columns; 36.8M of them fit. Those queries are kept as `null_result_variants` rather than deleted,
because "we could not induce spill this way" is the finding that justifies everything after it.

To reach memory pressure the sort key is widened by a stated number of bytes per row. This is
synthetic and labelled as such — but it is synthetic in a direction real warehouses go anyway
(a production promotion fact carries descriptive attributes, not three integers), and it turns
"does it spill?" into a measured threshold: at 36.8M rows, this warehouse spills somewhere
between a 128-byte and a 512-byte sort key.
"""

from __future__ import annotations

from retail_lakehouse.perf.runner import Variant
from retail_lakehouse.perf.tables import CAUSAL, TRANSACTIONS

# Promotion coverage runs weeks 9-101 (dataset finding F3). 55-101 is the back half of that
# window, so the filtered variant drops rows without leaving the meaningful range.
FILTER_WEEK_FROM = 55
FILTER_WEEK_TO = 101


def _padded_key(width: int) -> str:
    """A sort key of `width` filler bytes plus a zero-padded PRODUCT_ID.

    The filler has to be in the *sort key*, not merely in the projection. A wide column that the
    sort only carries gets eliminated by projection pushdown once an aggregate sits on top —
    measured: a 1024-byte payload column outside the key produced 0 bytes of spill and a 1.5 s
    query over the full table, because the sort never saw it.
    """
    if width == 0:
        return "lpad(cast(PRODUCT_ID AS string), 10, '0')"
    return f"concat(repeat('x', {width}), lpad(cast(PRODUCT_ID AS string), 10, '0'))"


def global_rank(width: int) -> str:
    """Global `row_number()`: one ordering, therefore one task holding all 36.8M rows."""
    return f"""
SELECT max(rn) AS max_rank
FROM (
  SELECT row_number() OVER (ORDER BY {_padded_key(width)}, STORE_ID, WEEK_NO) AS rn
  FROM {CAUSAL}
)
"""


# ---------------------------------------------------------------------------------------
# Null results: obvious pressure sources on the native seed that do not spill.
# ---------------------------------------------------------------------------------------

WINDOW_WIDE = f"""
SELECT count(*) AS ranked_rows, sum(rn) AS checksum
FROM (
  SELECT c.*,
         t.SALES_VALUE,
         t.household_key,
         t.BASKET_ID,
         row_number() OVER (
           PARTITION BY c.PRODUCT_ID, c.STORE_ID
           ORDER BY c.WEEK_NO, t.SALES_VALUE DESC
         ) AS rn
  FROM {CAUSAL} c
  JOIN {TRANSACTIONS} t
    ON c.PRODUCT_ID = t.PRODUCT_ID AND c.STORE_ID = t.STORE_ID AND c.WEEK_NO = t.WEEK_NO
)
"""

HIGH_CARD_AGG = f"""
SELECT count(*) AS groups, sum(n) AS checksum
FROM (
  SELECT PRODUCT_ID, STORE_ID, WEEK_NO, count(*) AS n, max(display) AS d, max(mailer) AS m
  FROM {CAUSAL}
  GROUP BY PRODUCT_ID, STORE_ID, WEEK_NO
)
"""

# Builds one array per product: 68,377 groups, the largest holding 7,083 structs.
COLLECT_PER_PRODUCT = f"""
SELECT count(*) AS groups, max(sz) AS largest_array
FROM (
  SELECT PRODUCT_ID, size(collect_list(struct(STORE_ID, WEEK_NO, display, mailer))) AS sz
  FROM {CAUSAL}
  GROUP BY PRODUCT_ID
)
"""


# ---------------------------------------------------------------------------------------
# Width sweep: the induction experiment and the projection mitigation in one axis.
# ---------------------------------------------------------------------------------------

SWEEP_WIDTHS = (0, 128, 256, 512)


# ---------------------------------------------------------------------------------------
# Mitigations, all measured against the 512-byte global rank. Each changes one thing.
# ---------------------------------------------------------------------------------------

PARTITIONED_WINDOW = f"""
SELECT max(rn) AS max_rank
FROM (
  SELECT row_number() OVER (
           PARTITION BY STORE_ID
           ORDER BY {_padded_key(512)}, WEEK_NO
         ) AS rn
  FROM {CAUSAL}
)
"""

FILTERED = f"""
SELECT max(rn) AS max_rank
FROM (
  SELECT row_number() OVER (ORDER BY {_padded_key(512)}, STORE_ID, WEEK_NO) AS rn
  FROM {CAUSAL}
  WHERE WEEK_NO BETWEEN {FILTER_WEEK_FROM} AND {FILTER_WEEK_TO}
)
"""

# Rank one row per (PRODUCT_ID, STORE_ID) instead of one per fact row: same question
# ("order product-store pairs"), a fraction of the rows through the sort.
PREAGGREGATED = f"""
SELECT max(rn) AS max_rank
FROM (
  SELECT row_number() OVER (ORDER BY {_padded_key(512)}, STORE_ID) AS rn
  FROM (
    SELECT PRODUCT_ID, STORE_ID, count(*) AS n
    FROM {CAUSAL}
    GROUP BY PRODUCT_ID, STORE_ID
  )
)
"""

# The only lever on this platform that resembles `spark.sql.shuffle.partitions`. Included to
# be falsified: a global ordering has exactly one output partition by definition, so raising
# the input partition count cannot reduce the memory held by the task that owns the ordering.
REPARTITIONED = f"""
SELECT max(rn) AS max_rank
FROM (
  SELECT row_number() OVER (ORDER BY {_padded_key(512)}, STORE_ID, WEEK_NO) AS rn
  FROM (SELECT /*+ REPARTITION(1024, PRODUCT_ID) */ * FROM {CAUSAL})
)
"""


def null_result_variants() -> list[Variant]:
    """Pressure sources on the native seed. All expected — and measured — to spill nothing."""
    return [
        Variant("N1-window-wide", WINDOW_WIDE, "wide payload through a partitioned window"),
        Variant("N2-global-rank", global_rank(0), "global ordering, native 10-byte key"),
        Variant("N3-highcard-agg", HIGH_CARD_AGG, "36,771,279 groups, one per row"),
        Variant("N4-collect-list", COLLECT_PER_PRODUCT, "68,377 arrays, largest 7,083 elements"),
    ]


def width_sweep_variants() -> list[Variant]:
    """Global rank over all 36,786,524 rows at four sort-key widths."""
    return [
        Variant(
            f"W{width:03d}-global-rank",
            global_rank(width),
            f"sort key = {width} filler bytes + 10-byte PRODUCT_ID",
        )
        for width in SWEEP_WIDTHS
    ]


def mitigate_variants() -> list[Variant]:
    """Interventions against W512. Each differs from it in exactly one respect."""
    return [
        Variant("M1-partitioned", PARTITIONED_WINDOW, "PARTITION BY STORE_ID (115 partitions)"),
        Variant(
            "M2-filtered",
            FILTERED,
            f"WHERE WEEK_NO BETWEEN {FILTER_WEEK_FROM} AND {FILTER_WEEK_TO}",
        ),
        Variant("M3-preagg", PREAGGREGATED, "rank (PRODUCT_ID, STORE_ID) pairs, not fact rows"),
        Variant("M4-repartition", REPARTITIONED, "REPARTITION(1024) ahead of the global window"),
    ]
