"""Driver for the skew/spill lab.

    python -m retail_lakehouse.perf.cli load        # Part 1: build dng_dev.perf tables
    python -m retail_lakehouse.perf.cli probe       # platform capability probes
    python -m retail_lakehouse.perf.cli profile     # key skew + post-shuffle partition sizes
    python -m retail_lakehouse.perf.cli skew        # Part 2 join and aggregation variants
    python -m retail_lakehouse.perf.cli spill       # Part 3 spill induction and mitigation
    python -m retail_lakehouse.perf.cli backfill    # attach shuffle bytes from the system table

Results land in `data/perf/` as JSON so the write-up can be rebuilt without re-spending
warehouse quota. Free Edition quota is shared across the whole account; re-running a stage
because a number was not written down is the most expensive mistake available here.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from databricks.sdk import WorkspaceClient

from retail_lakehouse.perf import (
    calibration,
    metrics,
    platform_probe,
    runner,
    skew_lab,
    spill_lab,
    tables,
    warehouse,
)

RESULTS_DIR = Path(__file__).resolve().parents[3] / "data" / "perf"


def _write(name: str, payload: object) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"  wrote {path}")


def stage_load(client: WorkspaceClient, warehouse_id: str) -> None:
    tables.create(client, warehouse_id)
    facts = [
        asdict(tables.describe(client, warehouse_id, t))
        for t in (tables.TRANSACTIONS, tables.CAUSAL)
    ]
    for f in facts:
        print(
            f"  {f['name']}: {f['rows']:,} rows, {f['num_files']} files, "
            f"{f['size_bytes'] / 1024 / 1024:.1f} MiB, clustering={f['clustering_columns']}"
        )
    _write("tables.json", facts)


def stage_probe(client: WorkspaceClient, warehouse_id: str) -> None:
    results = platform_probe.run_probes(client, warehouse_id)
    for r in results:
        print(f"  {'OK  ' if r.succeeded else 'FAIL'} {r.name}: {r.message[:120]}")
    expected, observed = platform_probe.session_persists(client, warehouse_id)
    print(f"  session persistence: set={expected} read-back-in-new-statement={observed}")
    _write(
        "platform_probes.json",
        {
            "probes": [asdict(r) for r in results],
            "session_persistence": {"set_to": expected, "read_back": observed},
        },
    )


# Keys whose skew determines whether any mitigation is warranted. The composite key is the one
# the join under test actually uses; the single-column keys are what the dataset profile
# reported skew for, and the whole question is whether that skew survives compositing.
PROFILE_KEYS: list[tuple[str, str, str]] = [
    ("transactions", tables.TRANSACTIONS, "STORE_ID"),
    ("transactions", tables.TRANSACTIONS, "PRODUCT_ID"),
    ("transactions", tables.TRANSACTIONS, "PRODUCT_ID, STORE_ID, WEEK_NO"),
    ("causal", tables.CAUSAL, "STORE_ID"),
    ("causal", tables.CAUSAL, "PRODUCT_ID"),
    ("causal", tables.CAUSAL, "PRODUCT_ID, STORE_ID, WEEK_NO"),
]


def stage_profile(client: WorkspaceClient, warehouse_id: str) -> None:
    key_profiles = []
    for _, table, key in PROFILE_KEYS:
        profile = skew_lab.profile_key(client, warehouse_id, table, key)
        print(
            f"  {table.split('.')[-1]:12} {key:32} distinct={profile.distinct_keys:>10,} "
            f"max={profile.max_rows:>9,} med={profile.median_rows:>7,} "
            f"max/med={profile.max_over_median:>10,.1f}x"
        )
        key_profiles.append(asdict(profile) | {"max_over_median": profile.max_over_median})
    _write("key_profiles.json", key_profiles)

    partition_profiles = []
    for _, table, key in PROFILE_KEYS:
        for n in skew_lab.PARTITION_COUNTS:
            # bytes_per_row is filled in by the report once calibration has been backfilled;
            # the row distribution is the part that costs warehouse time, so it is captured now.
            p = skew_lab.profile_partitions(client, warehouse_id, table, key, n, bytes_per_row=0.0)
            partition_profiles.append(asdict(p))
            print(
                f"  {table.split('.')[-1]:12} {key:32} n={n:>5} "
                f"max_rows={p.max_rows:>10,} median_rows={p.median_rows:>9,} "
                f"max/med={p.max_over_median:.2f}x"
            )
    _write("partition_profiles.json", partition_profiles)


def stage_skew(client: WorkspaceClient, warehouse_id: str) -> None:
    variants = calibration.variants() + skew_lab.join_variants()
    summaries = runner.run_all(client, warehouse_id, variants, lab="skew")
    runner.dump(summaries, RESULTS_DIR / "skew_runs.json")


def stage_smj(client: WorkspaceClient, warehouse_id: str) -> None:
    summaries = runner.run_all(
        client, warehouse_id, skew_lab.shuffle_join_variants(), lab="skew-smj"
    )
    runner.dump(summaries, RESULTS_DIR / "smj_runs.json")


def stage_agg(client: WorkspaceClient, warehouse_id: str) -> None:
    summaries = runner.run_all(
        client, warehouse_id, skew_lab.aggregation_variants(), lab="skew-agg"
    )
    runner.dump(summaries, RESULTS_DIR / "agg_runs.json")


def stage_spill(client: WorkspaceClient, warehouse_id: str) -> None:
    variants = (
        spill_lab.null_result_variants()
        + spill_lab.width_sweep_variants()
        + spill_lab.mitigate_variants()
    )
    summaries = runner.run_all(client, warehouse_id, variants, lab="spill")
    runner.dump(summaries, RESULTS_DIR / "spill_runs.json")


def stage_backfill(client: WorkspaceClient, warehouse_id: str) -> None:
    """Attach `shuffle_read_bytes` and `read_io_cache_percent` from `system.query.history`.

    The system table lags the query history API by a variable amount on this workspace —
    measured at 349 s early in a session and still over 40 minutes for the last batch of a
    session — so this runs as a separate stage rather than inline, and is safe to re-run.
    """
    for name in ("skew_runs.json", "smj_runs.json", "agg_runs.json", "spill_runs.json"):
        path = RESULTS_DIR / name
        if not path.exists():
            continue
        summaries = runner.load(path)
        # One query for the whole file. A query against system.query.history costs ~6 s on this
        # warehouse; doing it per variant turned a 6-second backfill into a five-minute one.
        flat = [run for s in summaries for run in s.runs]
        enriched, found = metrics.backfill_from_system_table(client, warehouse_id, flat)
        by_id = {run.statement_id: run for run in enriched}
        for summary in summaries:
            summary.runs = [by_id.get(r.statement_id, r) for r in summary.runs]
        runner.dump(summaries, path)
        print(f"  {name}: {found}/{len(flat)} measured runs matched in system.query.history")


STAGES = {
    "load": stage_load,
    "probe": stage_probe,
    "profile": stage_profile,
    "skew": stage_skew,
    "agg": stage_agg,
    "smj": stage_smj,
    "spill": stage_spill,
    "backfill": stage_backfill,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=sorted(STAGES))
    args = parser.parse_args(argv)

    client = warehouse.workspace()
    warehouse_id, size = warehouse.resolve_warehouse(client)
    print(f"warehouse {warehouse_id} ({size})")
    STAGES[args.stage](client, warehouse_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
