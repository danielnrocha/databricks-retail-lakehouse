"""Emitter tests: the stress scenarios must be controllable and truthfully reported.

The control-condition test (GEN-004) is the one that earns its keep. Without it, a "clean" run
could quietly contain late or duplicate events, and every A/B comparison in the performance lab
would be comparing two contaminated conditions while reporting a difference as if it meant
something.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from generator.config import (
    DuplicateDelivery,
    FileSizing,
    GeneratorConfig,
    LateArrival,
    SchemaDrift,
)
from generator.emit import EventEmitter
from generator.sampler import BasketSampler

FIXTURE = Path(__file__).resolve().parents[2] / "data" / "fixtures"

pytestmark = pytest.mark.unit


def _emitter(tmp_path: Path, config: GeneratorConfig) -> EventEmitter:
    return EventEmitter(config, BasketSampler(FIXTURE, random_seed=config.random_seed))


def _read_events(directory: Path) -> list[dict]:
    events: list[dict] = []
    for path in sorted(directory.glob("events-*.json")):
        events.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    return events


def test_every_event_labelled_synthetic(tmp_path: Path) -> None:
    """GEN-005: the synthetic label is applied at source, on every event, always.

    MLR-003 (evaluation never touches synthetic rows) is only enforceable if this holds without
    exception. A label that is right 99.9% of the time is a label that puts synthetic rows in a
    held-out evaluation set.
    """
    config = GeneratorConfig(
        total_events=5_000,
        output_dir=tmp_path,
        # Drift thresholds default to a 1M-event run; scaled down here so the guard in
        # EventEmitter.__init__ is satisfied and drift actually fires within the run.
        drift=SchemaDrift(
            enabled=True,
            add_column_at_event=1_000,
            rename_at_event=2_000,
            retype_at_event=3_000,
        ),
        files=FileSizing(events_per_file=800),
    )
    _emitter(tmp_path, config).write()

    events = _read_events(tmp_path)
    assert len(events) == 5_000
    unlabelled = [e for e in events if e.get("is_synthetic") is not True]
    assert not unlabelled, f"{len(unlabelled)} events missing is_synthetic"


def test_control_run_has_no_stress_events(tmp_path: Path) -> None:
    """GEN-004: with every scenario disabled, the output is clean and in order.

    'Clean' is asserted three ways, because each stress scenario fails differently and a single
    assertion would let two of them through.
    """
    config = GeneratorConfig(total_events=5_000, output_dir=tmp_path).clean()
    manifest = _emitter(tmp_path, config).write()

    assert manifest.late_events == 0
    assert manifest.beyond_watermark_events == 0
    assert manifest.duplicate_events == 0
    assert manifest.drift_added_column_from is None
    assert manifest.drift_renamed_from is None
    assert manifest.drift_retyped_from is None

    events = _read_events(tmp_path)

    # No duplicate identities.
    ids = Counter(e["event_id"] for e in events)
    assert not [k for k, n in ids.items() if n > 1], "duplicate event_id in a clean run"

    # One stable schema throughout.
    schemas = {tuple(sorted(e)) for e in events}
    assert len(schemas) == 1, f"clean run produced {len(schemas)} distinct schemas"

    # Arrival order matches event order within each basket; nothing was held back.
    assert "transaction_time" not in events[0], "renamed column leaked into a clean run"


def test_late_events_arrive_out_of_order(tmp_path: Path) -> None:
    """Lateness must be real: a delayed event lands in a *later file* than its event time implies.

    Emitting an event in its correct position with a backdated timestamp would satisfy any naive
    check while testing nothing — every watermark handles that case trivially because the event
    is already where it belongs.
    """
    config = GeneratorConfig(
        total_events=20_000,
        output_dir=tmp_path,
        late=LateArrival(enabled=True, fraction=0.10),
        drift=SchemaDrift(enabled=False),
        duplicates=DuplicateDelivery(enabled=False),
        files=FileSizing(events_per_file=1_000),
    )
    manifest = _emitter(tmp_path, config).write()

    assert manifest.late_events > 0, "lateness enabled but nothing was held back"

    # Find at least one event whose event_ts precedes the max event_ts already seen in an
    # earlier file — i.e. genuine out-of-order arrival, not merely an old timestamp.
    out_of_order = 0
    high_water = ""
    for path in sorted(tmp_path.glob("events-*.json")):
        batch = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for event in batch:
            if event["event_ts"] < high_water:
                out_of_order += 1
        high_water = max([high_water, *(e["event_ts"] for e in batch)])

    assert out_of_order > 0, "no event arrived out of order — lateness is cosmetic"


def test_schema_drift_is_staged_and_recorded(tmp_path: Path) -> None:
    """Drift happens at a known point and the manifest says where.

    Three distinct failure modes, deliberately separated: an added column (benign if tolerated),
    a renamed column (silently nulls a field, no error anywhere), and a retyped column (the
    _rescued_data case). Collapsing them into one 'drift' event would hide that the middle one is
    the dangerous one.
    """
    config = GeneratorConfig(
        total_events=6_000,
        output_dir=tmp_path,
        late=LateArrival(enabled=False),
        duplicates=DuplicateDelivery(enabled=False),
        drift=SchemaDrift(
            enabled=True,
            add_column_at_event=1_000,
            rename_at_event=3_000,
            retype_at_event=5_000,
        ),
        files=FileSizing(events_per_file=1_000),
    )
    manifest = _emitter(tmp_path, config).write()
    events = _read_events(tmp_path)

    assert manifest.drift_added_column_from is not None
    assert manifest.drift_renamed_from is not None
    assert manifest.drift_retyped_from is not None

    assert "loyalty_tier" not in events[500]
    assert "loyalty_tier" in events[2_000]

    assert "trans_time" in events[2_000]
    assert "transaction_time" in events[4_000]
    assert "trans_time" not in events[4_000], "rename left the old column behind"

    assert isinstance(events[4_000]["quantity"], int)
    assert isinstance(events[5_500]["quantity"], str), "retype did not take effect"


def test_duplicate_delivery_replays_a_contiguous_window(tmp_path: Path) -> None:
    """At-least-once delivery, modelled as a window replay rather than scattered duplicates."""
    config = GeneratorConfig(
        total_events=20_000,
        output_dir=tmp_path,
        late=LateArrival(enabled=False),
        drift=SchemaDrift(enabled=False),
        duplicates=DuplicateDelivery(enabled=True, fraction=0.05, replay_window_events=200),
        files=FileSizing(events_per_file=2_000),
    )
    manifest = _emitter(tmp_path, config).write()
    events = _read_events(tmp_path)

    assert manifest.duplicate_events > 0
    counts = Counter(e["event_id"] for e in events)
    repeated = {k: n for k, n in counts.items() if n > 1}
    assert repeated, "duplicates enabled but every event_id is unique"

    # The assertion that matters, and the one whose absence let a real bug through.
    #
    # The first version of this test asserted only `duplicate_events > 0`. It passed while the
    # emitter was producing 73% duplicates against a configured 0.5% — the parameter was being
    # interpreted as a per-basket replay probability rather than a share of output, inflating it
    # by the window size. "The feature is present" and "the feature is correct" are different
    # claims, and only weak tests confuse them.
    observed_rate = manifest.duplicate_events / manifest.total_events
    assert observed_rate == pytest.approx(config.duplicates.fraction, rel=0.6), (
        f"duplicate share {observed_rate:.2%} does not match the configured "
        f"{config.duplicates.fraction:.2%} — the rate parameter's units are wrong"
    )

    # A duplicate must be byte-identical to its original. A duplicate that differs is not a
    # duplicate — it is a correction, and it needs entirely different handling.
    by_id: dict[str, list[dict]] = {}
    for event in events:
        by_id.setdefault(event["event_id"], []).append(event)
    for event_id in list(repeated)[:50]:
        first, *rest = by_id[event_id]
        for other in rest:
            assert other == first, f"replayed {event_id} differs from the original"


def test_stress_scenarios_do_not_distort_the_store_distribution(tmp_path: Path) -> None:
    """The scenarios must perturb *arrival*, never *content*.

    This is the end-to-end version of GEN-001. The sampler can be provably correct while the
    emitter still ruins the distribution downstream — which is exactly what happened: an
    over-firing replay flooded the output with copies of a narrow window, dropping observed
    store coverage from 582 to 400 and the top-decile share from 67% to 53%.

    A skew experiment run on that stream would have measured the emitter's bug and attributed it
    to grocery retail. Asserting on the *emitted* distribution, not just the sampler's, is what
    closes that gap.
    """

    def emitted_profile(config: GeneratorConfig, out: Path) -> tuple[int, float]:
        _emitter(out, config).write(out)
        observed = Counter(e["store_id"] for e in _read_events(out))
        ordered = sorted(observed.values(), reverse=True)
        head = sum(ordered[: max(1, len(ordered) // 10)]) / sum(ordered)
        return len(observed), head

    events = 60_000
    base = GeneratorConfig(
        total_events=events, output_dir=tmp_path, files=FileSizing(events_per_file=5_000)
    )

    # The control run is the baseline, not a chosen constant.
    #
    # A first version of this test asserted "at least 90% of stores appear". It failed at 73% —
    # and 73% was correct. With 582 stores, a long tail where the smallest have a single basket,
    # and ~6,700 baskets drawn, coupon-collector statistics alone leave roughly a third of the
    # singleton stores unseen. The threshold sat below the achievable floor, exactly like the
    # earlier total-variation mistake in test_generator_sampler.py.
    #
    # What is actually being asked is a *relative* question: does turning stress scenarios on
    # reduce variety compared with leaving them off, at identical volume? That has a measurable
    # control, so it gets one.
    clean_stores, clean_head = emitted_profile(base.clean(), tmp_path / "clean")
    stress_stores, stress_head = emitted_profile(
        GeneratorConfig(
            total_events=events,
            output_dir=tmp_path,
            drift=SchemaDrift(enabled=False),
            files=FileSizing(events_per_file=5_000),
        ),
        tmp_path / "stress",
    )

    assert stress_stores >= clean_stores * 0.95, (
        f"stress run saw {stress_stores} stores against {clean_stores} clean at identical "
        "volume — a scenario is crowding out real variety"
    )
    assert stress_head == pytest.approx(clean_head, abs=0.05), (
        f"top-decile store share moved from {clean_head:.1%} clean to {stress_head:.1%} under "
        "stress; the scenarios must perturb arrival, not content"
    )


def test_configured_rates_match_observed_rates(tmp_path: Path) -> None:
    """Every rate parameter means a share of the total stream, and is measured to prove it.

    This test exists because the same defect shipped twice in one sitting, in two different
    parameters, and both times the code matched the variable name:

    * `duplicates.fraction` was a per-basket trigger probability. Configured 0.5%, observed 73%.
    * `late.beyond_watermark_fraction` was a share of *late* events, not of the stream.
      Configured 0.200%, observed 0.003%.

    The first was caught only because a manual run printed an obviously absurd number. The second
    would have passed unnoticed — 0.003% against 0.200% reads like sampling noise unless you
    compare it to what was asked for. A per-scenario existence check ("some duplicates appeared")
    catches neither.

    Asserting configured-versus-observed for *every* rate, in one place, is the cheap general
    defence. Tolerances are wide because these are stochastic, but they are far tighter than the
    60x and 150x errors they exist to catch.
    """
    config = GeneratorConfig(
        total_events=120_000,
        output_dir=tmp_path,
        late=LateArrival(enabled=True, fraction=0.03, beyond_watermark_fraction=0.002),
        duplicates=DuplicateDelivery(enabled=True, fraction=0.01, replay_window_events=500),
        drift=SchemaDrift(enabled=False),
        files=FileSizing(events_per_file=10_000),
    )
    manifest = _emitter(tmp_path, config).write()
    total = manifest.total_events

    assert manifest.late_events / total == pytest.approx(config.late.fraction, rel=0.25), (
        f"late share {manifest.late_events / total:.3%} vs configured {config.late.fraction:.3%}"
    )
    assert manifest.beyond_watermark_events / total == pytest.approx(
        config.late.beyond_watermark_fraction, rel=0.6
    ), (
        f"beyond-watermark share {manifest.beyond_watermark_events / total:.4%} vs configured "
        f"{config.late.beyond_watermark_fraction:.4%} — the parameter is being applied "
        "conditionally rather than as a share of the stream"
    )
    assert manifest.duplicate_events / total == pytest.approx(
        config.duplicates.fraction, rel=0.6
    ), (
        f"duplicate share {manifest.duplicate_events / total:.3%} vs configured "
        f"{config.duplicates.fraction:.3%}"
    )


def test_beyond_watermark_cannot_exceed_late(tmp_path: Path) -> None:
    """An event cannot be beyond the watermark without being late. Reject the impossible config."""
    with pytest.raises(ValueError, match="cannot be beyond the watermark"):
        LateArrival(enabled=True, fraction=0.01, beyond_watermark_fraction=0.05)


def test_drift_beyond_run_length_is_rejected(tmp_path: Path) -> None:
    """A scenario you asked for and silently did not get is worse than one you never enabled.

    Found by running the generator for fewer events than the default drift thresholds: the
    manifest reported no drift and the run looked clean, which reads as evidence about the
    pipeline when it is really evidence about the config.
    """
    config = GeneratorConfig(
        total_events=1_000,
        output_dir=tmp_path,
        drift=SchemaDrift(enabled=True, add_column_at_event=250_000),
    )
    with pytest.raises(ValueError, match="would never fire"):
        _emitter(tmp_path, config)
