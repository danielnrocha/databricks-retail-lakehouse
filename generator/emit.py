"""Event emitter — turns resampled baskets into a landing-zone event stream.

Two clocks, and conflating them is the mistake this module exists to avoid:

* **event time** — when the transaction happened at the till. Carried on the event.
* **arrival time** — when the file containing it lands. Implied by file order.

In a healthy feed these march together. Late arrival is precisely the case where they diverge, and
a generator that only models one clock cannot produce late data at all — it can only produce data
with a *timestamp* that looks late, which every watermark handles trivially because the event is
still in the right file. That is the difference between testing a watermark and testing a string.

So delayed events are genuinely held back and written to a **later file**, out of order relative
to their event time. That is what makes ING-006 and ING-007 meaningful assertions.

The emitter records ground truth for everything it did — how many events were held, how many were
pushed past the watermark, which files carry drifted schemas — into a sidecar manifest. Without
that, a downstream test asserting "late events were counted" has nothing to compare against and
degenerates into asserting that some number is non-zero.
"""

from __future__ import annotations

import heapq
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from generator.config import GeneratorConfig
from generator.sampler import Basket, BasketSampler

# Mean seconds between baskets on the synthetic clock. Chosen so the default 1M-event run spans a
# plausible number of store-days rather than compressing two years into an afternoon.
INTER_ARRIVAL_SECONDS = 0.75


@dataclass
class EmissionManifest:
    """Ground truth about what the generator actually did.

    Written next to the events. Every stress-scenario assertion downstream compares against this
    rather than against a hard-coded expectation, so changing a config knob does not silently
    invalidate a test.
    """

    total_events: int = 0
    files_written: int = 0
    late_events: int = 0
    beyond_watermark_events: int = 0
    duplicate_events: int = 0
    drift_added_column_from: int | None = None
    drift_renamed_from: int | None = None
    drift_retyped_from: int | None = None
    first_event_ts: str | None = None
    last_event_ts: str | None = None
    config: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class _Pending:
    """An event held back to arrive out of order."""

    release_at: float
    sequence: int
    payload: dict[str, Any]

    def __lt__(self, other: _Pending) -> bool:
        # heapq needs a total order; sequence breaks ties so the heap stays deterministic.
        return (self.release_at, self.sequence) < (other.release_at, other.sequence)


class EventEmitter:
    def __init__(self, config: GeneratorConfig, sampler: BasketSampler) -> None:
        self._config = config
        self._sampler = sampler
        self._rng = np.random.default_rng(config.random_seed)
        self._day_one = datetime.fromisoformat(config.day_one).replace(tzinfo=UTC)

        # A drift threshold beyond the run length means drift silently never happens, and the
        # run looks clean for a reason that has nothing to do with the pipeline. Fail loudly:
        # a scenario you asked for and did not get is worse than one you never enabled, because
        # you will read the clean result as evidence.
        if config.drift.enabled:
            latest = max(
                config.drift.add_column_at_event,
                config.drift.rename_at_event,
                config.drift.retype_at_event,
            )
            if latest >= config.total_events:
                raise ValueError(
                    f"Schema drift is enabled with its last stage at event {latest:,}, but only "
                    f"{config.total_events:,} events will be emitted. The drift would never fire "
                    "and the run would look clean. Lower the drift thresholds or raise "
                    "total_events."
                )
        self.manifest = EmissionManifest(
            config={
                "total_events": config.total_events,
                "random_seed": config.random_seed,
                "store_skew_multiplier": config.store_skew_multiplier,
                "late_enabled": config.late.enabled,
                "drift_enabled": config.drift.enabled,
                "duplicates_enabled": config.duplicates.enabled,
                "events_per_file": config.files.events_per_file,
            }
        )

    # -- event construction ---------------------------------------------------------------

    def _event_time(self, basket: Basket) -> datetime:
        """Business time, reconstructed from the seed's relative DAY and HHMM.

        DAY=1 is anchored to a Monday so day-of-week structure survives. Calendar seasonality is
        NOT recoverable and must not be implied — see ADR-0003 and data/README.md.
        """
        hours, minutes = divmod(basket.trans_time, 100)
        return self._day_one + timedelta(days=basket.day - 1, hours=hours, minutes=minutes)

    def _to_events(self, basket: Basket, synthetic_basket_id: int) -> list[dict[str, Any]]:
        event_ts = self._event_time(basket)
        return [
            {
                # A stable, content-derived id. Random ids would make the duplicate-delivery
                # scenario undetectable by any dedupe keyed on identity, which would be testing
                # nothing.
                "event_id": f"{synthetic_basket_id}-{line_no}",
                "basket_id": synthetic_basket_id,
                "source_basket_id": basket.source_basket_id,
                "line_no": line_no,
                "household_key": basket.household_key,
                "store_id": basket.store_id,
                "product_id": line.product_id,
                "quantity": line.quantity,
                "sales_value": round(line.sales_value, 2),
                "retail_disc": round(line.retail_disc, 2),
                "coupon_disc": round(line.coupon_disc, 2),
                "coupon_match_disc": round(line.coupon_match_disc, 2),
                "trans_time": basket.trans_time,
                "week_no": basket.week_no,
                "event_ts": event_ts.isoformat(),
                # GEN-005. Applied at source, never inferred later. A label added downstream by
                # guessing is a label that will eventually be wrong, and MLR-003 depends on it.
                "is_synthetic": True,
            }
            for line_no, line in enumerate(basket.lines, start=1)
        ]

    # -- stress scenarios -----------------------------------------------------------------

    def _apply_drift(self, event: dict[str, Any], index: int) -> dict[str, Any]:
        drift = self._config.drift
        if not drift.enabled:
            return event

        if index >= drift.add_column_at_event:
            if self.manifest.drift_added_column_from is None:
                self.manifest.drift_added_column_from = index
            # Benign if the reader tolerates unknown columns. Included because "benign" is a
            # claim the pipeline should have to demonstrate.
            event[drift.add_column_name] = ("gold", "silver", "bronze", "none")[index % 4]

        if index >= drift.rename_at_event:
            if self.manifest.drift_renamed_from is None:
                self.manifest.drift_renamed_from = index
            # The dangerous one. A reader keyed on the old name sees null and carries on. No
            # error, no rescue, just a column that quietly stops having values.
            event[drift.rename_to] = event.pop(drift.rename_from)

        if index >= drift.retype_at_event:
            if self.manifest.drift_retyped_from is None:
                self.manifest.drift_retyped_from = index
            # The one Auto Loader's _rescued_data is for: right name, incompatible type.
            event[drift.retype_column] = f"{event[drift.retype_column]} units"

        return event

    def _lag_seconds(self) -> tuple[float, bool]:
        """Draw an arrival lag. Returns (seconds, is_beyond_watermark).

        This runs only for events already selected as late, so the beyond-watermark probability
        must be rescaled from a share of the total stream to a share of late events. Using the
        configured value directly here would silently divide it by `late.fraction` — the bug that
        produced 0.003% observed against 0.200% configured.
        """
        late = self._config.late
        conditional = late.beyond_watermark_fraction / max(late.fraction, 1e-12)
        if self._rng.random() < conditional:
            # Deliberately past any sane watermark, so ING-007 has something to count.
            return float(late.max_lag_seconds * self._rng.uniform(1.5, 4.0)), True
        # Log-uniform: many slightly-late events, few very late. A uniform draw would produce an
        # unrealistically fat tail and make any watermark look bad — a strawman argument for a
        # longer one.
        log_low, log_high = np.log(late.min_lag_seconds), np.log(late.max_lag_seconds)
        return float(np.exp(self._rng.uniform(log_low, log_high))), False

    # -- main loop ------------------------------------------------------------------------

    def stream(self) -> Iterator[dict[str, Any]]:
        """Yield events in *arrival* order, which is not event order once lateness is on."""
        config = self._config
        clock = 0.0
        emitted = 0
        sequence = 0
        pending: list[_Pending] = []
        replay_buffer: list[dict[str, Any]] = []
        synthetic_basket_id = 0

        while emitted < config.total_events:
            # Release anything whose hold has expired, before adding new arrivals.
            while pending and pending[0].release_at <= clock:
                yield heapq.heappop(pending).payload
                emitted += 1
                if emitted >= config.total_events:
                    return

            synthetic_basket_id += 1
            basket = self._sampler.draw(config.store_skew_multiplier)
            events = self._to_events(basket, synthetic_basket_id)

            for event in events:
                sequence += 1
                event = self._apply_drift(event, emitted)

                if config.duplicates.enabled:
                    replay_buffer.append(dict(event))
                    if len(replay_buffer) > config.duplicates.replay_window_events:
                        replay_buffer.pop(0)

                if config.late.enabled and self._rng.random() < config.late.fraction:
                    lag, beyond = self._lag_seconds()
                    self.manifest.late_events += 1
                    if beyond:
                        self.manifest.beyond_watermark_events += 1
                    heapq.heappush(pending, _Pending(clock + lag, sequence, event))
                    continue

                yield event
                emitted += 1
                if emitted >= config.total_events:
                    return

            # A consumer restarting from a stale offset replays a contiguous window. Scattered
            # duplicates are caught by any dedupe; a replayed window is what actually breaks
            # naive watermark-scoped deduplication.
            #
            # The trigger probability is derived from the target duplicate SHARE rather than
            # being the share itself. One replay contributes `replay_window_events` duplicates,
            # so to hit `fraction` of the output the replay must fire roughly once every
            # `replay_window_events / fraction` events. Treating the share as the per-basket
            # trigger probability inflates duplicates by the window size — a factor of hundreds.
            if config.duplicates.enabled and replay_buffer:
                events_per_trigger = config.duplicates.replay_window_events / max(
                    config.duplicates.fraction, 1e-12
                )
                trigger_probability = len(events) / events_per_trigger
                if self._rng.random() < trigger_probability:
                    for event in list(replay_buffer):
                        yield dict(event)
                        self.manifest.duplicate_events += 1
                        emitted += 1
                        if emitted >= config.total_events:
                            return

            clock += float(self._rng.exponential(INTER_ARRIVAL_SECONDS))

        # Anything still held when the target count is reached is simply never delivered, which
        # is also what happens to a real feed when you stop reading it.

    def write(self, output_dir: Path | None = None) -> EmissionManifest:
        """Write the stream as newline-delimited JSON files plus a manifest."""
        target = output_dir or self._config.output_dir
        target.mkdir(parents=True, exist_ok=True)

        per_file = self._config.files.events_per_file
        buffer: list[str] = []
        file_index = 0
        first_ts: str | None = None
        last_ts: str | None = None

        def flush() -> None:
            nonlocal file_index, buffer
            if not buffer:
                return
            path = target / f"events-{file_index:06d}.json"
            path.write_text("".join(buffer), encoding="utf-8")
            self.manifest.files_written += 1
            file_index += 1
            buffer = []

        for event in self.stream():
            ts = event["event_ts"]
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
            buffer.append(json.dumps(event, separators=(",", ":")) + "\n")
            self.manifest.total_events += 1
            if len(buffer) >= per_file:
                flush()
        flush()

        self.manifest.first_event_ts = first_ts
        self.manifest.last_event_ts = last_ts
        (target / "_manifest.json").write_text(self.manifest.to_json(), encoding="utf-8")
        return self.manifest
