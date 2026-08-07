"""Type 2 dimension semantics: how to build them, and how to join to them without breaking.

`AUTO CDC ... STORED AS SCD TYPE 2` builds the tables in production. This module is not a
replacement for it. It exists for two things the platform cannot do for you:

1. **`point_in_time_join`** — the join that is correct, next to `naive_key_join`, the one almost
   everyone writes. The difference is one predicate and roughly 100% of the revenue number.
2. **`build_scd2_from_change_feed`** — a reference implementation of the window-closing AUTO CDC
   performs, so the semantics can be asserted on a local Spark session in milliseconds instead of
   through a pipeline run. If the reference and the platform ever disagree, that is worth knowing;
   silently trusting the platform means you would not find out.
"""

from __future__ import annotations

from pyspark.sql import Column, DataFrame, Window
from pyspark.sql import functions as F

#: The validity-window columns `AUTO CDC ... STORED AS SCD TYPE 2` generates. Open rows carry a
#: NULL end. Named as constants because a typo in a string literal here produces a join that
#: returns zero rows, which reads exactly like "no data for that period".
START_COL = "__START_AT"
END_COL = "__END_AT"


def validity_predicate(
    fact_time: Column,
    start: Column,
    end: Column,
) -> Column:
    """`[start, end)` containment, half-open, with NULL end meaning "still open".

    Half-open on purpose. With a closed upper bound, a fact landing at the exact instant of a
    dimension change matches both versions, and the fan-out is invisible because it affects a
    handful of rows rather than all of them — the worst kind of bug, since it survives every
    spot-check.
    """
    return (fact_time >= start) & (end.isNull() | (fact_time < end))


def point_in_time_join(
    facts: DataFrame,
    dim: DataFrame,
    *,
    key: str,
    fact_time: str,
    how: str = "inner",
) -> DataFrame:
    """Join a fact to a Type 2 dimension as of the fact's own event time.

    The result carries both sides' columns; the key appears twice, so callers select explicitly
    rather than relying on `*`. That is deliberate — a `SELECT *` across a dimension join is how
    an ambiguous column silently resolves to the wrong side.
    """
    f = facts.alias("f")
    d = dim.alias("d")
    condition = (F.col(f"f.{key}") == F.col(f"d.{key}")) & validity_predicate(
        F.col(f"f.{fact_time}"), F.col(f"d.{START_COL}"), F.col(f"d.{END_COL}")
    )
    return f.join(d, condition, how)


def naive_key_join(facts: DataFrame, dim: DataFrame, *, key: str) -> DataFrame:
    """The bug, written down so it can be asserted against rather than described.

    Joining on the natural key alone against a Type 2 dimension multiplies every fact row by the
    number of versions that key has. Revenue, units, basket counts — every additive measure
    inflates, and none of them looks obviously wrong, because the inflation factor varies by key
    and averages out to something plausible.

    Kept in the shipped source rather than inside the test, because the point is that this is
    *ordinary-looking code*. A reviewer who has seen it here recognises it in a pull request.
    """
    f = facts.alias("f")
    d = dim.alias("d")
    return f.join(d, F.col(f"f.{key}") == F.col(f"d.{key}"), "inner")


def overlapping_windows(dim: DataFrame, *, key: str) -> DataFrame:
    """Rows whose validity windows intersect for the same key. MOD-002 expects this to be empty.

    Implemented as a self-join rather than as a lead/lag scan on purpose: lead/lag only detects
    overlaps between *adjacent* versions, and the interesting corruption — a late-arriving change
    inserted out of sequence — produces an overlap with a version two steps away.
    """
    far_future = F.lit("9999-12-31 00:00:00").cast("timestamp")
    left = dim.alias("l")
    right = dim.alias("r")
    return left.join(
        right,
        (F.col(f"l.{key}") == F.col(f"r.{key}"))
        & (F.col(f"l.{START_COL}") < F.col(f"r.{START_COL}"))
        & (F.col(f"l.{START_COL}") < F.coalesce(F.col(f"r.{END_COL}"), far_future))
        & (F.col(f"r.{START_COL}") < F.coalesce(F.col(f"l.{END_COL}"), far_future)),
        "inner",
    ).select(
        F.col(f"l.{key}").alias(key),
        F.col(f"l.{START_COL}").alias("left_start"),
        F.col(f"l.{END_COL}").alias("left_end"),
        F.col(f"r.{START_COL}").alias("right_start"),
        F.col(f"r.{END_COL}").alias("right_end"),
    )


def current_row_violations(dim: DataFrame, *, key: str) -> DataFrame:
    """Keys that do not have exactly one open version. MOD-001 expects this to be empty."""
    return (
        dim.groupBy(key)
        .agg(F.sum(F.when(F.col(END_COL).isNull(), 1).otherwise(0)).alias("open_versions"))
        .filter(F.col("open_versions") != 1)
    )


def build_scd2_from_change_feed(
    changes: DataFrame,
    *,
    keys: list[str],
    sequence_col: str,
    tracked_columns: list[str],
) -> DataFrame:
    """Reference implementation of the SCD2 window-closing AUTO CDC performs.

    Two steps, and the first is the one people forget:

    1. **Collapse no-op changes.** A change record whose tracked attributes match the previous
       version is not a version. Skipping this produces a dimension that grows a row per CDC event
       rather than per business change — technically "correct" history, and useless: every
       point-in-time join then has more versions to scan, and `is_current` churns without anything
       having changed.
    2. **Close the previous window at the next version's start.** Half-open intervals, so the last
       version stays open with a NULL end.
    """
    ordering = Window.partitionBy(*keys).orderBy(sequence_col)
    tracked = F.struct(*[F.col(column) for column in tracked_columns])

    changed_only = (
        changes.withColumn("_tracked", tracked)
        .withColumn("_previous", F.lag("_tracked").over(ordering))
        .filter(F.col("_previous").isNull() | (F.col("_tracked") != F.col("_previous")))
        .drop("_tracked", "_previous")
    )

    return (
        changed_only.withColumn(START_COL, F.col(sequence_col))
        .withColumn(END_COL, F.lead(sequence_col).over(ordering))
        .withColumn("is_current", F.col(END_COL).isNull())
    )
