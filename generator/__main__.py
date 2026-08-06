"""CLI for the event amplifier.

    python -m generator --events 200000 --env dev            # generate and upload
    python -m generator --events 5000 --local-only           # generate locally, inspect first
    python -m generator --events 200000 --env dev --clean    # control condition, no stress

`--clean` is not a convenience flag. Every performance claim in this project compares a stressed
run against a control at identical volume, and a control you have to hand-assemble by unsetting
six knobs is a control that will eventually be assembled wrong.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from generator.config import FileSizing, GeneratorConfig, SchemaDrift
from generator.emit import EventEmitter
from generator.sampler import BasketSampler

REPO_ROOT = Path(__file__).resolve().parents[1]


def _scaled_drift(total_events: int) -> SchemaDrift:
    """Place the three drift stages at 25%, 50% and 75% of the run.

    Absolute offsets tuned for a 1M-event run silently never fire on a 200k run, which makes the
    output look clean for a reason that has nothing to do with the pipeline. Scaling them to the
    run length removes the whole failure mode rather than guarding against it.
    """
    return SchemaDrift(
        enabled=True,
        add_column_at_event=max(1, total_events // 4),
        rename_at_event=max(2, total_events // 2),
        retype_at_event=max(3, (total_events * 3) // 4),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=200_000)
    parser.add_argument("--env", default="dev", choices=("dev", "test", "prod"))
    parser.add_argument("--events-per-file", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20260806)
    parser.add_argument("--skew-multiplier", type=float, default=1.0)
    parser.add_argument("--clean", action="store_true", help="control condition: no stress")
    parser.add_argument("--local-only", action="store_true", help="do not upload")
    parser.add_argument(
        "--seed-dir",
        type=Path,
        default=REPO_ROOT / "data" / "interim" / "seed-parquet",
        help="falls back to the committed fixture when the full seed is absent",
    )
    args = parser.parse_args()

    seed_dir = args.seed_dir
    if not (seed_dir / "transaction_data.parquet").exists():
        seed_dir = REPO_ROOT / "data" / "fixtures"
        print(f"full seed not found; using fixture at {seed_dir.relative_to(REPO_ROOT)}")

    output_dir = REPO_ROOT / "data" / "generated" / ("clean" if args.clean else "stress")
    config = GeneratorConfig(
        seed_dir=seed_dir,
        output_dir=output_dir,
        total_events=args.events,
        random_seed=args.seed,
        store_skew_multiplier=args.skew_multiplier,
        drift=_scaled_drift(args.events),
        files=FileSizing(events_per_file=args.events_per_file),
    )
    if args.clean:
        config = config.clean()

    sampler = BasketSampler(seed_dir, random_seed=args.seed)
    print(
        f"seed: {sampler.basket_count:,} baskets / {sampler.line_count:,} lines "
        f"({sampler.mean_basket_size:.1f} lines per basket)"
    )

    started = time.monotonic()
    manifest = EventEmitter(config, sampler).write()
    elapsed = time.monotonic() - started

    print(f"\n{'events':<26}{manifest.total_events:>12,}")
    print(f"{'files':<26}{manifest.files_written:>12,}")
    print(
        f"{'late':<26}{manifest.late_events:>12,}  ({manifest.late_events / manifest.total_events:.2%})"
    )
    print(
        f"{'beyond watermark':<26}{manifest.beyond_watermark_events:>12,}"
        f"  ({manifest.beyond_watermark_events / manifest.total_events:.3%})"
    )
    print(
        f"{'duplicates':<26}{manifest.duplicate_events:>12,}"
        f"  ({manifest.duplicate_events / manifest.total_events:.2%})"
    )
    print(f"{'event time span':<26}{manifest.first_event_ts} .. {manifest.last_event_ts}")
    print(f"{'generated in':<26}{elapsed:>11.1f}s")

    if args.local_only:
        print(f"\nwritten to {output_dir.relative_to(REPO_ROOT)} (not uploaded)")
        return 0

    # Imported here so that --local-only works with no SDK configured. A generator that needs
    # workspace credentials to write files locally is a generator nobody can experiment with.
    from databricks.sdk import WorkspaceClient

    client = WorkspaceClient()
    volume = f"/Volumes/dng_{args.env}/bronze/landing"
    files = [*sorted(output_dir.glob("events-*.json")), output_dir / "_manifest.json"]

    started = time.monotonic()
    for index, path in enumerate(files, start=1):
        with path.open("rb") as handle:
            client.files.upload(f"{volume}/{path.name}", handle, overwrite=True)
        if index % 50 == 0 or index == len(files):
            print(f"  uploaded {index}/{len(files)}", end="\r", file=sys.stderr)
    print(f"\nuploaded {len(files)} files to {volume} in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
