"""Bytes-per-shuffled-row. **This measurement failed. The module is kept as the evidence.**

The plan was sound: post-shuffle partition size in bytes is the number AQE's skew rule tests
against, and it can be reconstructed as `rows_in_partition x bytes_per_shuffled_row`. So shuffle
exactly the projection the join under test carries, read `shuffle_read_bytes` back from
`system.query.history`, divide by the known row count. A `REPARTITION` hint naming the key
columns forces those columns to survive projection pushdown, so the payload is real.

Both statements ran and both returned `shuffle_read_bytes = 0`. So did every other statement:
across 325 non-null rows recorded during this lab, the maximum value of the column is zero,
while `spilled_local_bytes` and `written_bytes` on the same rows populate correctly. Shuffle
byte accounting is not reported for serverless SQL warehouses on this workspace.

The width is therefore computed from Spark's UnsafeRow layout instead — see
`skew_lab.SHUFFLE_WIDTH_BYTES` — and every conclusion that depends on it carries a break-even
figure showing how wrong the estimate would have to be to change the answer.

These variants stay in the run record because a failed measurement that is deleted looks like
a measurement that was never attempted.
"""

from __future__ import annotations

from dataclasses import dataclass

from retail_lakehouse.perf.runner import Variant
from retail_lakehouse.perf.tables import CAUSAL, TRANSACTIONS

# Arbitrary but fixed. The value does not affect bytes-per-row; it only has to be large enough
# that the shuffle is a real shuffle.
CALIBRATION_PARTITIONS = 200


@dataclass(frozen=True)
class ShuffleWidth:
    """Measured serialised width of one row as it crosses a shuffle boundary."""

    name: str
    projection: str
    rows: int
    shuffle_read_bytes: int

    @property
    def bytes_per_row(self) -> float:
        return self.shuffle_read_bytes / self.rows if self.rows else 0.0


# The join under test carries (PRODUCT_ID, STORE_ID, WEEK_NO, SALES_VALUE) from the fact side.
CAL_TRANSACTIONS = f"""
SELECT count(*) AS rows_shuffled, sum(SALES_VALUE) AS checksum
FROM (
  SELECT /*+ REPARTITION({CALIBRATION_PARTITIONS}, PRODUCT_ID, STORE_ID, WEEK_NO) */
         PRODUCT_ID, STORE_ID, WEEK_NO, SALES_VALUE
  FROM {TRANSACTIONS}
)
"""

# ...and (PRODUCT_ID, STORE_ID, WEEK_NO) from the promotion side.
CAL_CAUSAL = f"""
SELECT count(*) AS rows_shuffled
FROM (
  SELECT /*+ REPARTITION({CALIBRATION_PARTITIONS}, PRODUCT_ID, STORE_ID, WEEK_NO) */
         PRODUCT_ID, STORE_ID, WEEK_NO
  FROM {CAUSAL}
)
"""


def variants() -> list[Variant]:
    return [
        Variant(
            "CAL-transactions",
            CAL_TRANSACTIONS,
            "shuffle 2,595,732 rows x (PRODUCT_ID, STORE_ID, WEEK_NO, SALES_VALUE)",
        ),
        Variant(
            "CAL-causal",
            CAL_CAUSAL,
            "shuffle 36,786,524 rows x (PRODUCT_ID, STORE_ID, WEEK_NO)",
        ),
    ]
