"""Layer A — run the DQX profiler and write *candidate* rules to a review file.

This is deliberately a standalone command, not a pipeline step. QLT-005 requires that generated
rules never reach production without a human decision, and the cheapest way to guarantee that is
to make generation and enforcement different programs with a reviewed file between them.

The profiler runs against a **local extract** of the bronze table on a local Spark session. Two
reasons, in order of importance:

1. Profiling is exploratory. Running it against the warehouse on every iteration burns Free
   Edition quota that the pipeline needs, and quota exhaustion stops all compute for the day.
2. It keeps the loop honest. The extract is a file with a row count you can state, so the
   candidate file can record exactly what was profiled rather than "the table, at some point".

Produce the extract with a `SELECT * FROM <catalog>.bronze.basket_line_events_raw` pulled through
the SQL warehouse to Parquet; the header of the output file records the row count so a reviewer
can tell whether the sample was big enough to trust.

Usage::

    PYTHONPATH=src python -m retail_lakehouse.quality.profile_candidates \\
        --source /path/to/bronze_events.parquet \\
        --out docs/quality/dqx-candidate-rules.yml
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------------------------
# The projection the profiler sees.
#
# Profiling raw bronze would be useless for the columns that matter: `quantity` is a STRING there
# (the retype drift turned `12` into `"12 units"`), so a profiler cannot propose a numeric rule
# for it at all — and the numeric rule it *would* propose is the entire point of QLT-005.
#
# So the profiler is shown the numeric quantity and the reconciled transaction time that silver
# produces. This is a projection, not a reimplementation of the silver transform: it exists to put
# the profiler in front of the same value distribution the rules will govern.
# ---------------------------------------------------------------------------------------------
SILVER_PROJECTION = """
SELECT
    event_id,
    store_id,
    product_id,
    household_key,
    week_no,
    CAST(regexp_extract(quantity, '^\\\\s*([0-9]+(?:\\\\.[0-9]+)?)', 1) AS DOUBLE) AS quantity_units,
    COALESCE(trans_time, transaction_time)                                        AS transaction_time_hhmm,
    sales_value                                                                   AS sales_amt,
    retail_disc                                                                   AS retail_disc_amt,
    coupon_disc                                                                   AS coupon_disc_amt,
    coupon_match_disc                                                             AS coupon_match_disc_amt,
    event_ts                                                                      AS transaction_ts
FROM events
"""


def _spark() -> Any:
    """A local Spark session. No workspace connection, no quota consumed."""
    from pyspark.sql import SparkSession

    # PySpark refuses to run when the driver and worker interpreters differ by a minor version,
    # and the worker default is whatever `python3` resolves to on PATH — which on a machine with
    # several interpreters is rarely the one running this module.
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    return (
        SparkSession.builder.master("local[*]")
        .appName("dqx-profile-candidates")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "8")
        .getOrCreate()
    )


def generate_candidates(source: Path, dataset: str) -> dict[str, Any]:
    """Profile `source` and return the candidate document, ready to serialise."""
    from databricks.labs.dqx.profiler.generator import DQGenerator
    from databricks.labs.dqx.profiler.profiler import DQProfiler
    from databricks.sdk import WorkspaceClient

    spark = _spark()
    events = spark.read.parquet(str(source))
    events.createOrReplaceTempView("events")
    projected = spark.sql(SILVER_PROJECTION)
    row_count = projected.count()

    ws = WorkspaceClient()
    summary, profiles = DQProfiler(ws, spark).profile(projected)
    checks = DQGenerator(ws, spark).generate_dq_rules(profiles)

    return {
        "provenance": {
            "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "generator": "databricks-labs-dqx DQProfiler + DQGenerator",
            "source_extract": source.name,
            "rows_profiled": row_count,
            "target_dataset": dataset,
        },
        "status": "UNREVIEWED — NOT APPLIED",
        "column_summary": {
            column: {
                key: (str(value) if isinstance(value, dt.date | dt.datetime) else value)
                for key, value in stats.items()
                if key in ("count", "null_count", "min", "max", "mean", "stddev", "25%", "75%")
            }
            for column, stats in summary.items()
        },
        "candidate_checks": checks,
    }


HEADER = """\
# DQX profiler output — CANDIDATES ONLY. NOTHING HERE IS ENFORCED.
#
# Regenerate with:
#   PYTHONPATH=src python -m retail_lakehouse.quality.profile_candidates --source <extract.parquet>
#
# This file is machine-written. Do not hand-edit it: edits would make the next regeneration look
# like a change in the data when it was a change in the file.
#
# The review decision for every candidate below lives in docs/quality/rule-review.md, and the
# rules actually enforced live in src/retail_lakehouse/quality/rules.py. QLT-005 is the assertion
# that no candidate crosses that gap without a recorded decision; the test that enforces it is
# tests/unit/test_quality_rules.py::test_generated_rules_require_review.
#
# The reason this gate exists is visible in the candidates themselves: the profiler derives a
# bounded range for quantity_units from the interquartile spread, and that rule would quarantine
# legitimate revenue, because weight-priced items express quantity in grams and reach five figures
# (finding F6 in docs/architecture/dataset-findings.md).
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Parquet extract to profile.")
    parser.add_argument(
        "--dataset", default="silver.fact_basket_line", help="Dataset the candidates target."
    )
    parser.add_argument("--out", type=Path, default=Path("docs/quality/dqx-candidate-rules.yml"))
    args = parser.parse_args()

    if not args.source.exists():
        print(f"No such extract: {args.source}", file=sys.stderr)
        return 1

    document = generate_candidates(args.source, args.dataset)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(HEADER + yaml.safe_dump(document, sort_keys=False, width=100))

    checks = document["candidate_checks"]
    print(f"{len(checks)} candidate rules -> {args.out}")
    print("Review them in docs/quality/rule-review.md before anything is published.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
