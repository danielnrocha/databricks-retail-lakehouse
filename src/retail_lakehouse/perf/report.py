"""Render measurement tables from the recorded run files.

The tables in `docs/architecture/perf-lab.md` are generated from `data/perf/*.json` so that
every number in the write-up can be traced to a `statement_id` in `system.query.history`. A
figure that cannot be regenerated from a recorded statement does not belong in the document.
"""

from __future__ import annotations

import json
from pathlib import Path

from retail_lakehouse.perf.metrics import VariantSummary
from retail_lakehouse.perf.runner import load
from retail_lakehouse.perf.skew_lab import (
    AQE_SKEW_FACTOR,
    AQE_SKEW_THRESHOLD_BYTES,
    SHUFFLE_WIDTH_BYTES,
)

RESULTS_DIR = Path(__file__).resolve().parents[3] / "data" / "perf"
MIB = 1024 * 1024


def _fmt_bytes(value: float) -> str:
    if value <= 0:
        return "0"
    if value < MIB:
        return f"{value / 1024:,.0f} KiB"
    return f"{value / MIB:,.1f} MiB"


def timing_table(summaries: list[VariantSummary], intervention: dict[str, str]) -> str:
    """Median-of-3 timings with the min-max spread and the skew proxy.

    `shuffle read` is omitted deliberately: it is 0 for every statement on this platform (see
    `skew_lab.SHUFFLE_WIDTH_BYTES`), and a column of zeros would read as "no shuffle happened"
    rather than "not reported".
    """
    header = (
        "| Variant | Intervention | exec ms | task ms | task/exec | spill | "
        "read rows | produced rows | IO cache | spread |\n"
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    lines = [header]
    for s in summaries:
        cache = (
            f"{s.runs[0].read_io_cache_percent}%"
            if s.runs and s.runs[0].read_io_cache_percent is not None
            else "n/a"
        )
        lines.append(
            f"| `{s.variant}` | {intervention.get(s.variant, '')} "
            f"| {s.execution_duration_ms:,.0f} | {s.total_task_duration_ms:,.0f} "
            f"| {s.parallelism_efficiency:.2f} | {_fmt_bytes(s.spilled_local_bytes)} "
            f"| {s.read_rows:,.0f} | {s.produced_rows:,.0f} | {cache} | {s.spread_pct:.0f}% |"
        )
    return "\n".join(lines)


def key_profile_table() -> str:
    rows = json.loads((RESULTS_DIR / "key_profiles.json").read_text(encoding="utf-8"))
    lines = [
        "| Table | Key | Distinct keys | Rows | Max rows/key | Median | p99 | max/median |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        table = r["table"].split(".")[-1]
        lines.append(
            f"| `{table}` | `{r['key']}` | {r['distinct_keys']:,} | {r['total_rows']:,} "
            f"| {r['max_rows']:,} | {r['median_rows']:,} | {r['p99_rows']:,} "
            f"| **{r['max_over_median']:,.1f}x** |"
        )
    return "\n".join(lines)


def partition_table(bytes_per_row: dict[str, int]) -> str:
    """Post-shuffle partition sizes against both AQE skew conditions.

    The last column is the honest one: how many bytes a single shuffled row would have to be
    for this partition to reach 256 MB. It converts the estimated-width dependency into a
    falsifiable statement — if the real width is anywhere near that number, the conclusion is
    wrong and this table says so.
    """
    rows = json.loads((RESULTS_DIR / "partition_profiles.json").read_text(encoding="utf-8"))
    lines = [
        "| Table | Shuffle key | N | Max rows | Median rows | max/median | "
        "Est. max partition | >5x median? | >256 MB? | AQE splits? | Break-even B/row |",
        "|---|---|---:|---:|---:|---:|---:|:---:|:---:|:---:|---:|",
    ]
    for r in rows:
        table = r["table"].split(".")[-1]
        bpr = bytes_per_row.get(table, 0)
        max_bytes = r["max_rows"] * bpr
        ratio = r["max_rows"] / r["median_rows"] if r["median_rows"] else float("inf")
        factor_ok = ratio > AQE_SKEW_FACTOR
        byte_ok = max_bytes > AQE_SKEW_THRESHOLD_BYTES
        break_even = AQE_SKEW_THRESHOLD_BYTES / r["max_rows"] if r["max_rows"] else 0.0
        lines.append(
            f"| `{table}` | `{r['key']}` | {r['num_partitions']} | {r['max_rows']:,} "
            f"| {r['median_rows']:,} | {ratio:,.2f}x | {_fmt_bytes(max_bytes)} "
            f"| {'yes' if factor_ok else 'no'} | {'yes' if byte_ok else 'no'} "
            f"| **{'yes' if factor_ok and byte_ok else 'no'}** | {break_even:,.0f} |"
        )
    return "\n".join(lines)


def main() -> None:
    """Print every generated table, in the order the write-up uses them."""
    print("### Key skew\n")
    print(key_profile_table())
    bpr = SHUFFLE_WIDTH_BYTES
    print("\n### Estimated bytes per shuffled row (UnsafeRow layout)\n")
    for table, value in bpr.items():
        print(f"- `{table}`: {value} bytes/row")
    print("\n### Post-shuffle partitions\n")
    print(partition_table(bpr))
    for name in ("skew_runs.json", "agg_runs.json", "spill_runs.json"):
        path = RESULTS_DIR / name
        if not path.exists():
            continue
        print(f"\n### {name}\n")
        print(timing_table(load(path), {}))


if __name__ == "__main__":
    main()
