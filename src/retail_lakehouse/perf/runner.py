"""Repeated-execution harness: run a variant N+1 times, discard the first, keep the rest."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path

from databricks.sdk import WorkspaceClient

from retail_lakehouse.perf import warehouse
from retail_lakehouse.perf.metrics import RunMetrics, VariantSummary, fetch_rest_metrics

# One warm-up plus three measured runs. Three is not a large sample, but Free Edition quota is
# shared across the account and an exhausted quota stops all compute for the day. The report
# publishes the min-max spread alongside every median so a reader can see how thin the sample is.
WARMUP_RUNS = 1
MEASURED_RUNS = 3

# Query history is written asynchronously; the record is normally there on the first poll but
# occasionally needs a second.
METRIC_POLL_ATTEMPTS = 8
METRIC_POLL_SLEEP_S = 3.0


class Variant:
    """A named SQL statement under test, plus what makes it different from the baseline."""

    def __init__(self, name: str, sql: str, intervention: str) -> None:
        self.name = name
        self.sql = sql
        self.intervention = intervention


def _collect_metrics(
    client: WorkspaceClient,
    statement_id: str,
    variant: str,
    run_index: int,
    window_start_ms: int,
) -> RunMetrics | None:
    for _ in range(METRIC_POLL_ATTEMPTS):
        found = fetch_rest_metrics(
            client, statement_id, variant, run_index, window_start_ms=window_start_ms
        )
        if found is not None and found.execution_duration_ms > 0:
            return found
        time.sleep(METRIC_POLL_SLEEP_S)
    return None


def run_variant(
    client: WorkspaceClient,
    warehouse_id: str,
    variant: Variant,
    *,
    lab: str,
    measured_runs: int = MEASURED_RUNS,
    warmup_runs: int = WARMUP_RUNS,
) -> VariantSummary:
    """Execute one variant `warmup_runs + measured_runs` times and summarise the measured tail.

    The warm-up exists to move the Delta log, file listing and IO cache into a steady state.
    It is retained in `VariantSummary.discarded` rather than thrown away, because the gap
    between run 1 and run 2 is itself evidence about the IO cache.
    """
    summary = VariantSummary(variant=variant.name)
    total = warmup_runs + measured_runs
    for index in range(total):
        window_start_ms = int(time.time() * 1000) - 60_000
        result = warehouse.execute(
            client,
            warehouse_id,
            variant.sql,
            tags={"lab": lab, "variant": variant.name, "run": str(index)},
            label=f"{lab}/{variant.name}/run{index}",
        )
        metrics = _collect_metrics(
            client, result.statement_id, variant.name, index, window_start_ms
        )
        if metrics is None:
            continue
        if index < warmup_runs:
            summary.discarded.append(metrics)
        else:
            summary.runs.append(metrics)
    return summary


def run_all(
    client: WorkspaceClient,
    warehouse_id: str,
    variants: Iterable[Variant],
    *,
    lab: str,
) -> list[VariantSummary]:
    summaries: list[VariantSummary] = []
    for variant in variants:
        print(f"  running {lab}/{variant.name} ...", flush=True)
        summary = run_variant(client, warehouse_id, variant, lab=lab)
        cached = [r for r in summary.runs if r.from_result_cache]
        if cached:
            # Would silently turn a measurement into a cache lookup. Fail loudly instead.
            raise RuntimeError(
                f"{variant.name}: {len(cached)} measured run(s) served from result cache"
            )
        print(
            f"    median exec {summary.execution_duration_ms:.0f} ms "
            f"| task {summary.total_task_duration_ms:.0f} ms "
            f"| eff {summary.parallelism_efficiency:.2f} "
            f"| spill {summary.spilled_local_bytes:.0f} B "
            f"| spread {summary.spread_pct:.0f}%",
            flush=True,
        )
        summaries.append(summary)
    return summaries


def dump(summaries: list[VariantSummary], path: Path) -> None:
    """Persist raw per-run records so the report can be rebuilt without re-spending quota."""
    payload = [
        {
            "variant": s.variant,
            "runs": [asdict(r) for r in s.runs],
            "discarded": [asdict(r) for r in s.discarded],
        }
        for s in summaries
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load(path: Path) -> list[VariantSummary]:
    """Rehydrate summaries written by `dump`."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    summaries: list[VariantSummary] = []
    for entry in payload:
        summaries.append(
            VariantSummary(
                variant=entry["variant"],
                runs=[RunMetrics(**r) for r in entry["runs"]],
                discarded=[RunMetrics(**r) for r in entry["discarded"]],
            )
        )
    return summaries
