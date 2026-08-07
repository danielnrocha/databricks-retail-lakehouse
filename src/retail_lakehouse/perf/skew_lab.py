"""Part 2 — the skew experiment.

Two things are measured here and they are not the same thing:

1. **Logical key skew** — how unevenly rows distribute across a join/group key. Pure SQL,
   costs almost nothing, and is a property of the data.
2. **Post-shuffle partition size in bytes** — what AQE actually looks at. Spark's shuffle uses
   `HashPartitioner`, i.e. `pmod(murmur3_hash(key), numPartitions)`, and Spark SQL's `hash()`
   function *is* Murmur3, so the bucketing below is a faithful reproduction rather than a
   model of one. Only the bytes-per-row factor is estimated, and it is calibrated against a
   real shuffle rather than guessed.

The reason (2) exists: AQE's `OptimizeSkewedJoin` requires a partition to be both larger than
`skewedPartitionFactor` (5) times the median *and* larger than
`skewedPartitionThresholdInBytes` (256 MB). A dataset can be violently skewed and still never
trip the second condition, in which case AQE declines to do anything and the "AQE handles skew"
folklore quietly fails. That is the headline hypothesis.
"""

from __future__ import annotations

from dataclasses import dataclass

from databricks.sdk import WorkspaceClient

from retail_lakehouse.perf import warehouse
from retail_lakehouse.perf.runner import Variant
from retail_lakehouse.perf.tables import CAUSAL, TRANSACTIONS

# AQE skew-join thresholds, from the Spark defaults Databricks ships. Both must hold.
AQE_SKEW_FACTOR = 5
AQE_SKEW_THRESHOLD_BYTES = 256 * 1024 * 1024

# Shuffle partition counts to evaluate the post-shuffle distribution at. `spark.sql.shuffle.
# partitions` is `auto` on serverless and cannot be read back (see perf-lab.md, Constraint C1),
# so the distribution is reported across a range that brackets any plausible value instead of
# being asserted at one.
PARTITION_COUNTS = (16, 64, 200, 1024)

JOIN_KEYS = ("PRODUCT_ID", "STORE_ID", "WEEK_NO")

# ---------------------------------------------------------------------------------------
# Shuffle row width.
#
# This was supposed to be measured. `system.query.history.shuffle_read_bytes` exists on this
# workspace and is non-null, and it reads 0 for every one of the 325 statements recorded during
# this lab — while `spilled_local_bytes` and `written_bytes` populate correctly on the same
# rows. Shuffle byte accounting is simply not reported for serverless SQL warehouses here, and
# the calibration statements (`CAL-transactions`, `CAL-causal`) that were written to measure it
# returned nothing usable. They are kept in the run record as evidence of the gap.
#
# So the width is *computed*, from Spark's UnsafeRow layout, and labelled as an estimate
# everywhere it is used:
#
#   8 bytes null bitmap (one word per 64 fields)
# + 8 bytes per field in the fixed region, regardless of declared type
# + the variable-length region, 8-byte aligned, for strings
#
# Both `display` and `mailer` in `causal` are 1-character strings, so each costs one 8-byte
# word in the variable region on top of its 8-byte fixed slot.
#
# The estimate is deliberately generous: it ignores shuffle compression, which only makes the
# real number smaller. Every conclusion below is of the form "even at this width, the threshold
# is not reached", so an overestimate is the safe direction to be wrong in.
# ---------------------------------------------------------------------------------------
UNSAFE_ROW_HEADER_BYTES = 8
UNSAFE_ROW_FIELD_BYTES = 8

# Bytes per row for the projection each shuffle actually carries.
SHUFFLE_WIDTH_BYTES: dict[str, int] = {
    # (PRODUCT_ID, STORE_ID, WEEK_NO, SALES_VALUE) — the fact side of the join under test.
    "transactions": UNSAFE_ROW_HEADER_BYTES + 4 * UNSAFE_ROW_FIELD_BYTES,
    # (PRODUCT_ID, STORE_ID, WEEK_NO) — the promotion side.
    "causal": UNSAFE_ROW_HEADER_BYTES + 3 * UNSAFE_ROW_FIELD_BYTES,
}

# Full-row widths, used for the "what if you carry everything" bound.
FULL_ROW_WIDTH_BYTES: dict[str, int] = {
    "transactions": UNSAFE_ROW_HEADER_BYTES + 12 * UNSAFE_ROW_FIELD_BYTES,
    "causal": UNSAFE_ROW_HEADER_BYTES + 5 * UNSAFE_ROW_FIELD_BYTES + 2 * 8,
}


@dataclass(frozen=True)
class KeyProfile:
    """Row-count distribution across the distinct values of one key expression."""

    table: str
    key: str
    distinct_keys: int
    total_rows: int
    max_rows: int
    median_rows: int
    p99_rows: int

    @property
    def max_over_median(self) -> float:
        return self.max_rows / self.median_rows if self.median_rows else float("inf")


@dataclass(frozen=True)
class PartitionProfile:
    """Post-shuffle partition distribution at a given partition count."""

    table: str
    key: str
    num_partitions: int
    max_rows: int
    median_rows: int
    total_rows: int
    bytes_per_row: float

    @property
    def max_bytes(self) -> float:
        return self.max_rows * self.bytes_per_row

    @property
    def median_bytes(self) -> float:
        return self.median_rows * self.bytes_per_row

    @property
    def max_over_median(self) -> float:
        return self.max_rows / self.median_rows if self.median_rows else float("inf")

    @property
    def trips_factor_condition(self) -> bool:
        return self.max_over_median > AQE_SKEW_FACTOR

    @property
    def trips_byte_condition(self) -> bool:
        return self.max_bytes > AQE_SKEW_THRESHOLD_BYTES

    @property
    def aqe_would_split(self) -> bool:
        """AQE splits a partition only when *both* conditions hold."""
        return self.trips_factor_condition and self.trips_byte_condition


_KEY_PROFILE_SQL = """
WITH per_key AS (
  SELECT {key_expr} AS k, count(*) AS n
  FROM {table}
  GROUP BY {key_expr}
)
SELECT count(*)                            AS distinct_keys,
       sum(n)                              AS total_rows,
       max(n)                              AS max_rows,
       cast(percentile(n, 0.5)  AS bigint) AS median_rows,
       cast(percentile(n, 0.99) AS bigint) AS p99_rows
FROM per_key
"""

# `hash()` in Spark SQL is Murmur3, the same function HashPartitioner uses, so this reproduces
# the real post-shuffle bucketing rather than approximating it.
_PARTITION_PROFILE_SQL = """
WITH per_partition AS (
  SELECT pmod(hash({key_expr}), {n}) AS p, count(*) AS n
  FROM {table}
  GROUP BY pmod(hash({key_expr}), {n})
)
SELECT max(n)                             AS max_rows,
       cast(percentile(n, 0.5) AS bigint) AS median_rows,
       sum(n)                             AS total_rows
FROM per_partition
"""


def profile_key(
    client: WorkspaceClient, warehouse_id: str, table: str, key_expr: str
) -> KeyProfile:
    result = warehouse.execute(
        client,
        warehouse_id,
        _KEY_PROFILE_SQL.format(table=table, key_expr=key_expr),
        bust_cache=False,
        label="key-profile",
    )
    distinct_keys, total_rows, max_rows, median_rows, p99_rows = result.rows[0]
    return KeyProfile(
        table=table,
        key=key_expr,
        distinct_keys=int(distinct_keys or 0),
        total_rows=int(total_rows or 0),
        max_rows=int(max_rows or 0),
        median_rows=int(median_rows or 0),
        p99_rows=int(p99_rows or 0),
    )


def profile_partitions(
    client: WorkspaceClient,
    warehouse_id: str,
    table: str,
    key_expr: str,
    num_partitions: int,
    bytes_per_row: float,
) -> PartitionProfile:
    result = warehouse.execute(
        client,
        warehouse_id,
        _PARTITION_PROFILE_SQL.format(table=table, key_expr=key_expr, n=num_partitions),
        bust_cache=False,
        label="partition-profile",
    )
    max_rows, median_rows, total_rows = result.rows[0]
    return PartitionProfile(
        table=table,
        key=key_expr,
        num_partitions=num_partitions,
        max_rows=int(max_rows or 0),
        median_rows=int(median_rows or 0),
        total_rows=int(total_rows or 0),
        bytes_per_row=bytes_per_row,
    )


def hottest_key_bytes(profile: KeyProfile, bytes_per_row: float) -> float:
    """Shuffle bytes carried by the single busiest key.

    This is the hard floor on post-shuffle partition size: a hash partitioner cannot split one
    key across two partitions, so no increase in `spark.sql.shuffle.partitions` gets a partition
    below this. Comparing it to 256 MB answers "could AQE ever fire here?" without needing to
    know what the partition count actually is.
    """
    return profile.max_rows * bytes_per_row


# --------------------------------------------------------------------------------------
# Join variants. All interventions are code-level: no Spark conf on a serverless SQL
# warehouse is settable (Constraint C1), so a config-toggle A/B is not available even in
# principle.
# --------------------------------------------------------------------------------------

_ON_COMPOSITE = " AND ".join(f"t.{c} = c.{c}" for c in JOIN_KEYS)

# Every variant is an inner join, so all of them drop the same 1.4% of transaction lines that
# belong to the 467 stores absent from causal_data. Holding that constant across variants is
# the point: the row loss is a data-coverage fact, not a difference between the variants, and
# variant V5 exists to price what happens when you refuse to accept it.
BASELINE_JOIN = f"""
SELECT t.STORE_ID, sum(t.SALES_VALUE) AS sales, count(*) AS matched_lines
FROM {TRANSACTIONS} t
JOIN {CAUSAL} c ON {_ON_COMPOSITE}
GROUP BY t.STORE_ID
"""

BROADCAST_JOIN = f"""
SELECT /*+ BROADCAST(t) */ t.STORE_ID, sum(t.SALES_VALUE) AS sales, count(*) AS matched_lines
FROM {TRANSACTIONS} t
JOIN {CAUSAL} c ON {_ON_COMPOSITE}
GROUP BY t.STORE_ID
"""

# Pre-aggregate the fact side to the join grain before joining. 2,595,732 lines collapse to
# 2,370,784 distinct (PRODUCT_ID, STORE_ID, WEEK_NO) triples — a 1.09x reduction, which is
# small, and the variant is here to measure whether that is worth the extra shuffle at all.
PREAGG_JOIN = f"""
WITH t AS (
  SELECT PRODUCT_ID, STORE_ID, WEEK_NO,
         sum(SALES_VALUE) AS sales, count(*) AS lines
  FROM {TRANSACTIONS}
  GROUP BY PRODUCT_ID, STORE_ID, WEEK_NO
)
SELECT t.STORE_ID, sum(t.sales) AS sales, sum(t.lines) AS matched_lines
FROM t
JOIN {CAUSAL} c ON {_ON_COMPOSITE}
GROUP BY t.STORE_ID
"""

# Salt the store dimension of the key. The fact side gets a random salt in [0, 16); the causal
# side is exploded 16-fold to match. This is the textbook mitigation, applied here to a key
# whose skew has already been measured — see the report for whether it was warranted.
SALT_BUCKETS = 16
SALTED_JOIN = f"""
WITH t AS (
  SELECT PRODUCT_ID, STORE_ID, WEEK_NO, SALES_VALUE,
         cast(pmod(hash(BASKET_ID, PRODUCT_ID), {SALT_BUCKETS}) AS int) AS salt
  FROM {TRANSACTIONS}
),
c AS (
  SELECT c.PRODUCT_ID, c.STORE_ID, c.WEEK_NO, s.salt
  FROM {CAUSAL} c
  CROSS JOIN (SELECT explode(sequence(0, {SALT_BUCKETS - 1})) AS salt) s
)
SELECT t.STORE_ID, sum(t.SALES_VALUE) AS sales, count(*) AS matched_lines
FROM t
JOIN c ON {_ON_COMPOSITE} AND t.salt = c.salt
GROUP BY t.STORE_ID
"""

# Left join, so the 467 stores with no promotion coverage survive. Same shuffle, different
# semantics — measured so the cost of correctness is a number rather than an argument.
LEFT_JOIN = f"""
SELECT t.STORE_ID,
       sum(t.SALES_VALUE) AS sales,
       count(c.PRODUCT_ID) AS matched_lines,
       count(*) AS all_lines
FROM {TRANSACTIONS} t
LEFT JOIN {CAUSAL} c ON {_ON_COMPOSITE}
GROUP BY t.STORE_ID
"""

# Aggregation-only, no join. STORE_ID on the fact table is the key with 2,519x max/median, so
# if a single-key straggler is visible anywhere on this dataset it should be visible here.
# `count(DISTINCT BASKET_ID)` is deliberate: a distinct count cannot be reduced to a partial
# sum, so the hot key's whole distinct set has to be held by one task.
STORE_AGGREGATION = f"""
SELECT STORE_ID,
       sum(SALES_VALUE) AS sales,
       count(*) AS lines,
       count(DISTINCT BASKET_ID) AS baskets
FROM {TRANSACTIONS}
GROUP BY STORE_ID
"""

# The same three output columns, computed through a salted two-stage roll-up. Salting on
# `hash(BASKET_ID)` keeps the distinct count exact: a given BASKET_ID hashes to exactly one
# bucket, so per-bucket distinct counts are disjoint and summable. Getting that wrong — salting
# on a random value — would silently overcount baskets, which is the usual way this mitigation
# breaks in production.
SALTED_STORE_AGGREGATION = f"""
WITH partial AS (
  SELECT STORE_ID,
         pmod(hash(BASKET_ID), {SALT_BUCKETS}) AS salt,
         sum(SALES_VALUE) AS sales,
         count(*) AS lines,
         count(DISTINCT BASKET_ID) AS baskets
  FROM {TRANSACTIONS}
  GROUP BY STORE_ID, pmod(hash(BASKET_ID), {SALT_BUCKETS})
)
SELECT STORE_ID, sum(sales) AS sales, sum(lines) AS lines, sum(baskets) AS baskets
FROM partial
GROUP BY STORE_ID
"""

# Volume-matched pair. Both read all 36,786,524 rows of `causal` on the same warehouse; the
# only thing that differs is how skewed the group key is (1.4x vs 49.9x max/median). This is
# the controlled version of the question the fact-table aggregation is too small to answer.
CAUSAL_BY_STORE = f"""
SELECT STORE_ID, count(*) AS n, max(display) AS d, max(mailer) AS m
FROM {CAUSAL}
GROUP BY STORE_ID
"""

CAUSAL_BY_PRODUCT = f"""
SELECT PRODUCT_ID, count(*) AS n, max(display) AS d, max(mailer) AS m
FROM {CAUSAL}
GROUP BY PRODUCT_ID
"""


# --------------------------------------------------------------------------------------
# Forced sort-merge joins.
#
# `EXPLAIN FORMATTED` on V1 shows `PhotonBroadcastHashJoin`: the planner broadcasts the
# 14.4 MiB fact side on its own, so `causal` is never shuffled by the join key and AQE's skew
# handling — which only applies to shuffle joins — cannot engage regardless of thresholds.
# V2's explicit BROADCAST hint produces a byte-identical plan.
#
# A skew experiment against a plan that contains no keyed shuffle measures nothing, so the
# joins below force `SHUFFLE_MERGE`. V6 keeps the uniform composite key; V7 switches to
# STORE_ID, whose post-shuffle partitions are skewed 1,511x at N=1024, against a 115-row
# store-level summary of `causal` so the row count stays bounded. V8 salts V7.
# --------------------------------------------------------------------------------------

SMJ_COMPOSITE = f"""
SELECT /*+ SHUFFLE_MERGE(t, c) */
       t.STORE_ID, sum(t.SALES_VALUE) AS sales, count(*) AS matched_lines
FROM {TRANSACTIONS} t
JOIN {CAUSAL} c ON {_ON_COMPOSITE}
GROUP BY t.STORE_ID
"""

# One row per store, so the join cannot explode; the only thing under test is how 2,595,732
# fact rows distribute across shuffle partitions keyed on a 2,519x-skewed column.
_STORE_PROMO = f"""
  SELECT STORE_ID, count(*) AS promo_rows, max(WEEK_NO) AS last_week
  FROM {CAUSAL}
  GROUP BY STORE_ID
"""

SMJ_STORE = f"""
SELECT /*+ SHUFFLE_MERGE(t, s) */
       t.STORE_ID, sum(t.SALES_VALUE) AS sales, count(*) AS lines, max(s.promo_rows) AS promo_rows
FROM {TRANSACTIONS} t
JOIN ({_STORE_PROMO}) s ON t.STORE_ID = s.STORE_ID
GROUP BY t.STORE_ID
"""

SMJ_STORE_SALTED = f"""
WITH s AS (
  SELECT p.STORE_ID, p.promo_rows, x.salt
  FROM ({_STORE_PROMO}) p
  CROSS JOIN (SELECT explode(sequence(0, {SALT_BUCKETS - 1})) AS salt) x
),
t AS (
  SELECT STORE_ID, SALES_VALUE, pmod(hash(BASKET_ID, PRODUCT_ID), {SALT_BUCKETS}) AS salt
  FROM {TRANSACTIONS}
)
SELECT /*+ SHUFFLE_MERGE(t, s) */
       t.STORE_ID, sum(t.SALES_VALUE) AS sales, count(*) AS lines, max(s.promo_rows) AS promo_rows
FROM t
JOIN s ON t.STORE_ID = s.STORE_ID AND t.salt = s.salt
GROUP BY t.STORE_ID
"""


def join_variants() -> list[Variant]:
    return [
        Variant("V1-baseline-join", BASELINE_JOIN, "none (naive inner join)"),
        Variant("V2-broadcast", BROADCAST_JOIN, "BROADCAST hint on the 14.4 MiB fact side"),
        Variant("V3-preagg", PREAGG_JOIN, "aggregate the fact side to join grain first"),
        Variant("V4-salted", SALTED_JOIN, f"{SALT_BUCKETS}-way salt, causal side exploded"),
        Variant("V5-left-join", LEFT_JOIN, "LEFT JOIN so uncovered stores survive"),
    ]


def shuffle_join_variants() -> list[Variant]:
    """Forced sort-merge joins — the only plans in this lab that shuffle on the join key."""
    return [
        Variant("V6-smj-composite", SMJ_COMPOSITE, "SHUFFLE_MERGE, uniform composite key"),
        Variant("V7-smj-store", SMJ_STORE, "SHUFFLE_MERGE on STORE_ID (1,511x at N=1024)"),
        Variant("V8-smj-store-salted", SMJ_STORE_SALTED, f"V7 with a {SALT_BUCKETS}-way salt"),
    ]


def aggregation_variants() -> list[Variant]:
    return [
        Variant("A1-groupby-store", STORE_AGGREGATION, "none (naive GROUP BY STORE_ID)"),
        Variant(
            "A2-salted-groupby",
            SALTED_STORE_AGGREGATION,
            f"two-stage roll-up with a {SALT_BUCKETS}-way salt",
        ),
        Variant("A3-causal-by-store", CAUSAL_BY_STORE, "36.8M rows, key max/median 1.4x"),
        Variant("A4-causal-by-product", CAUSAL_BY_PRODUCT, "36.8M rows, key max/median 49.9x"),
    ]
