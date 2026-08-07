"""Idempotent deduplication semantics — the reference for what AUTO CDC SCD Type 1 does.

The pipeline does not call this. It uses `dp.create_auto_cdc_flow(..., stored_as_scd_type="1")`,
which is an upsert keyed on a business key and ordered by a sequence: replaying a window rewrites
the same rows rather than appending them. This module states the same contract in a form a local
test can assert, because "the merge is idempotent" is a claim, and a claim without an assertion is
a hope.

The distinction the north star insists on: this proves exactly-once *effect*, not exactly-once
*delivery*. The source may deliver a row any number of times; the target converges to one row per
key regardless. That is achievable. Exactly-once delivery is not, and claiming it is a tell.
"""

from __future__ import annotations

from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F


def latest_per_key(events: DataFrame, *, keys: list[str], sequence_cols: list[str]) -> DataFrame:
    """One row per key: the last by `sequence_cols`, deterministically.

    `sequence_cols` is a list rather than a single column for a specific reason. A duplicate
    replay usually arrives with an identical logical timestamp, so a single-column sequence leaves
    the winner undefined and the output non-reproducible — which breaks MOD-004 (identical row
    counts *and* identical checksums on re-run) while leaving the row count perfectly correct.
    A tie-breaker on file path costs nothing and makes the result a function of the input.
    """
    ordering = Window.partitionBy(*keys).orderBy(*[F.col(c).desc() for c in sequence_cols])
    return (
        events.withColumn("_rank", F.row_number().over(ordering))
        .filter(F.col("_rank") == 1)
        .drop("_rank")
    )
