"""Configuration for the event amplifier.

Every stress scenario is a named, defaulted, documented knob. Two reasons:

1. A scenario you cannot turn off cannot be used as a control. The performance lab needs to run
   the *same* pipeline with and without late arrival to attribute a difference to it.
2. A scenario buried in code as a magic constant is invisible to the reviewer, who then cannot
   tell an induced failure from a real one.

Note what is NOT here: a skew knob that manufactures skew. The seed's native store and product
skew is severe (69.3% of lines in the top 10% of stores; max/median 2,519x) and is preserved by
resampling rather than replaced. See docs/architecture/dataset-findings.md, finding F1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LateArrival:
    """Events that reach the landing zone after their event time.

    Real feeds are late for mundane reasons: a store's uplink drops, a batch retries, a mobile
    POS syncs when it reconnects. The watermark decision in ADR-0005 is only defensible against a
    measured lag distribution, so the generator produces one.

    **Every `*_fraction` here is a share of the TOTAL event stream**, never a conditional share.

    That sentence exists because this codebase got the same defect wrong twice in one sitting.
    `DuplicateDelivery.fraction` was implemented as a per-basket trigger probability and produced
    73% duplicates against a configured 0.5%. Then `beyond_watermark_fraction` was implemented as
    a share *of late events*, yielding 0.003% observed against 0.200% configured — a 60x
    discrepancy that looked like noise rather than a bug.

    Both survived code review because the code matched the variable name; only the units were
    wrong. Rate parameters are worth stating units for explicitly, and worth asserting on with a
    test that compares configured against observed, because "the feature fires" is not evidence
    that "the feature fires at the configured rate".
    """

    enabled: bool = True
    # Share of TOTAL events emitted out of order.
    fraction: float = 0.03
    # Lag is drawn log-uniformly between these bounds: many slightly-late events, few very late
    # ones. A uniform draw would produce an unrealistically fat tail and make any watermark look
    # bad, which would be a strawman argument for a longer watermark.
    min_lag_seconds: int = 60
    max_lag_seconds: int = 6 * 3600
    # Share of TOTAL events pushed beyond any sane watermark, so ING-007 (late events are
    # counted, never silently dropped) has something to count. Must be <= `fraction`, since an
    # event can only be beyond the watermark if it is late at all.
    beyond_watermark_fraction: float = 0.002

    def __post_init__(self) -> None:
        if self.enabled and self.beyond_watermark_fraction > self.fraction:
            raise ValueError(
                f"beyond_watermark_fraction ({self.beyond_watermark_fraction}) exceeds "
                f"fraction ({self.fraction}). Both are shares of the total stream, and an event "
                "cannot be beyond the watermark without being late."
            )


@dataclass(frozen=True)
class SchemaDrift:
    """Upstream changes shape mid-stream, without telling anyone.

    Modelled as three separate events because they fail differently: an added column is benign if
    the reader tolerates it, a renamed column silently nulls a field, and a retyped column is the
    one that lands in _rescued_data.
    """

    enabled: bool = True
    add_column_at_event: int = 250_000
    add_column_name: str = "loyalty_tier"
    rename_at_event: int = 500_000
    rename_from: str = "trans_time"
    rename_to: str = "transaction_time"
    retype_at_event: int = 750_000
    retype_column: str = "quantity"


@dataclass(frozen=True)
class DuplicateDelivery:
    """At-least-once delivery, which is what real CDC and real queues give you.

    Exactly-once *delivery* is mostly a marketing claim; exactly-once *effect* via idempotent
    merge is achievable and is what ING-005 asserts.
    """

    enabled: bool = True

    # Share of the OUTPUT stream that is duplicated. This is the quantity anyone reasoning about
    # the feed actually cares about ("about 0.5% of what we receive is a repeat"), and it is what
    # the emitter targets.
    #
    # An earlier version defined this as "probability, per basket, of replaying the whole window".
    # That reads plausibly and is badly wrong: with a 5,000-event window and ~22,000 baskets, a
    # 0.5% per-basket probability produced 110 replays of 5,000 events each — 73% of the output
    # was duplicates. It also distorted the store distribution the sampler works hard to preserve,
    # so a skew measurement taken on that stream would have been measuring the bug.
    #
    # The lesson generalises past this file: a rate parameter whose units are not the units the
    # reader assumes is a defect that survives review, because the code matches the variable name.
    fraction: float = 0.005

    # Replay a contiguous window rather than scattering duplicates. A scattered duplicate is
    # caught by any dedupe; a replayed window is what actually happens when a consumer restarts
    # from a stale offset, and it is the case that breaks naive watermark-scoped deduplication.
    replay_window_events: int = 500


@dataclass(frozen=True)
class FileSizing:
    """Controls the small-file problem directly.

    Defaults are chosen to *cause* the problem, because the default run should exhibit what the
    platform claims to solve. A demo that only runs in its own best case proves nothing.
    """

    events_per_file: int = 500
    # Wall-clock pacing between files. Set to 0 for a bulk backfill.
    seconds_between_files: float = 0.0


@dataclass(frozen=True)
class GeneratorConfig:
    seed_dir: Path = Path("data/interim/seed-parquet")
    output_dir: Path = Path("data/generated/events")

    total_events: int = 1_000_000
    # Deterministic by default. A generator whose output changes between runs makes every
    # downstream test flaky and every performance comparison meaningless.
    random_seed: int = 20260806

    # Anchors DAY=1 to a Monday so day-of-week structure survives. Calendar seasonality is
    # NOT recoverable from this dataset and must not be implied -- see ADR-0003.
    day_one: str = "2024-01-01"

    # Multiplier applied to the seed's observed store concentration. 1.0 preserves reality.
    # Above 1.0 stress-tests beyond observed skew; it does not create skew that is not there.
    store_skew_multiplier: float = 1.0

    late: LateArrival = field(default_factory=LateArrival)
    drift: SchemaDrift = field(default_factory=SchemaDrift)
    duplicates: DuplicateDelivery = field(default_factory=DuplicateDelivery)
    files: FileSizing = field(default_factory=FileSizing)

    def clean(self) -> GeneratorConfig:
        """A config with every stress scenario disabled — the control condition."""
        return GeneratorConfig(
            seed_dir=self.seed_dir,
            output_dir=self.output_dir,
            total_events=self.total_events,
            random_seed=self.random_seed,
            day_one=self.day_one,
            store_skew_multiplier=self.store_skew_multiplier,
            late=LateArrival(enabled=False),
            drift=SchemaDrift(enabled=False),
            duplicates=DuplicateDelivery(enabled=False),
            files=FileSizing(events_per_file=100_000),
        )
