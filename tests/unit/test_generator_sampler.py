"""The amplifier's honesty tests.

These exist because the project's central claim about synthetic data — "it resamples reality, it
does not invent it" — is otherwise unfalsifiable marketing. If the sampler distorted the store
distribution, every skew result in the performance lab would be measuring an artefact of the
generator rather than a property of grocery retail.

Runs offline against a committed fixture (ENV-006). No workspace, no network.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest
from generator.sampler import BasketSampler

FIXTURE = Path(__file__).resolve().parents[2] / "data" / "fixtures"

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def sampler() -> BasketSampler:
    return BasketSampler(FIXTURE, random_seed=20260806)


@pytest.fixture(scope="module")
def source_store_shares() -> dict[int, float]:
    table = pq.read_table(FIXTURE / "transaction_data.parquet", columns=["STORE_ID", "BASKET_ID"])
    stores = table.column("STORE_ID").to_numpy()
    baskets = table.column("BASKET_ID").to_numpy()
    # Share is measured per basket, not per line: the sampler draws baskets, so that is the
    # population whose distribution must be preserved. Comparing per-line shares would fail for
    # a correct sampler whenever basket size correlates with store.
    _, first = np.unique(baskets, return_index=True)
    counts = Counter(int(s) for s in stores[first])
    total = sum(counts.values())
    return {store: n / total for store, n in counts.items()}


def test_baskets_are_drawn_intact(sampler: BasketSampler) -> None:
    """GEN-002: a drawn basket is a whole observed trip, not a reassembled one.

    Every line must belong to the same source basket, which is what makes co-purchase structure
    real rather than modelled.
    """
    table = pq.read_table(
        FIXTURE / "transaction_data.parquet",
        columns=["BASKET_ID", "PRODUCT_ID", "SALES_VALUE", "STORE_ID"],
    )
    by_basket: dict[int, list[tuple[int, float]]] = {}
    stores: dict[int, int] = {}
    for bid, pid, val, sid in zip(
        table.column("BASKET_ID").to_pylist(),
        table.column("PRODUCT_ID").to_pylist(),
        table.column("SALES_VALUE").to_pylist(),
        table.column("STORE_ID").to_pylist(),
        strict=True,
    ):
        by_basket.setdefault(bid, []).append((pid, val))
        stores[bid] = sid

    for index in range(0, sampler.basket_count, max(1, sampler.basket_count // 200)):
        basket = sampler.basket_at(index)
        expected = by_basket[basket.source_basket_id]

        assert basket.line_count == len(expected), (
            f"basket {basket.source_basket_id} drawn with {basket.line_count} lines, "
            f"source has {len(expected)}"
        )
        assert basket.store_id == stores[basket.source_basket_id]
        assert sorted(line.product_id for line in basket.lines) == sorted(p for p, _ in expected)
        assert sum(line.sales_value for line in basket.lines) == pytest.approx(
            sum(v for _, v in expected), rel=1e-9
        )


def _total_variation(a: dict[int, float], b: dict[int, float]) -> float:
    return 0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in set(a) | set(b))


def test_uniform_draw_preserves_store_distribution(
    sampler: BasketSampler, source_store_shares: dict[int, float]
) -> None:
    """GEN-001: the observed store distribution survives resampling.

    The threshold is derived, not chosen. A first version of this test asserted
    `tvd < 0.02` — a number picked because it looked small. It failed at 0.026, and the failure
    was the test's fault: with 582 stores and 60,000 draws, multinomial noise alone produces a
    total variation distance of roughly 0.024. The threshold sat *below the noise floor*, so no
    sampler, however correct, could ever have passed.

    That is the more common failure than a flaky test: a threshold that is unsatisfiable by
    construction, "fixed" by loosening it until it goes green. Loosening a number you cannot
    justify converts a real assertion into a decorative one.

    So the baseline is measured instead. Drawing the same number of samples directly from the
    source population gives the null distribution — what an ideal sampler would score. The
    assertion is that the real sampler is not meaningfully worse than that ideal.
    """
    draws = 60_000
    rng = np.random.default_rng(7)

    stores = np.array(list(source_store_shares))
    probabilities = np.array([source_store_shares[s] for s in stores])

    # Null: a perfect sampler drawing from the true distribution.
    ideal_counts = rng.multinomial(draws, probabilities)
    ideal_shares = {int(s): c / draws for s, c in zip(stores, ideal_counts, strict=True)}
    noise_floor = _total_variation(ideal_shares, source_store_shares)

    observed = Counter(sampler.draw().store_id for _ in range(draws))
    observed_shares = {store: n / draws for store, n in observed.items()}
    tvd = _total_variation(observed_shares, source_store_shares)

    # 1.5x the measured noise floor. A sampler with real bias — say, one that dropped or
    # reweighted the head — moves TVD by far more than 50%; the store mix here is skewed enough
    # that any systematic distortion is large, not marginal.
    assert tvd <= noise_floor * 1.5, (
        f"total variation {tvd:.4f} exceeds 1.5x the {noise_floor:.4f} noise floor "
        "— the sampler distorts the store mix beyond sampling error"
    )


def test_uniform_draw_preserves_skew_shape(
    sampler: BasketSampler, source_store_shares: dict[int, float]
) -> None:
    """GEN-001: the head of the distribution — the part that causes stragglers — is preserved.

    TVD alone can hide a systematic flattening of the head while the tail absorbs the difference,
    and the head is precisely what the performance lab depends on.
    """
    draws = 60_000
    observed = Counter(sampler.draw().store_id for _ in range(draws))

    def top_decile_share(shares: dict[int, float]) -> float:
        ordered = sorted(shares.values(), reverse=True)
        cut = max(1, len(ordered) // 10)
        return sum(ordered[:cut]) / sum(ordered)

    source_head = top_decile_share(source_store_shares)
    observed_head = top_decile_share({s: n / draws for s, n in observed.items()})

    assert observed_head == pytest.approx(source_head, abs=0.03), (
        f"top-decile store share drifted: source {source_head:.1%}, resampled {observed_head:.1%}"
    )


def test_generation_is_deterministic() -> None:
    """GEN-003: identical seeds produce identical output.

    Without this, every performance comparison is confounded by a different input and every
    downstream test is flaky for reasons nobody will trace back to here.
    """
    first = BasketSampler(FIXTURE, random_seed=42)
    second = BasketSampler(FIXTURE, random_seed=42)
    third = BasketSampler(FIXTURE, random_seed=43)

    a = [first.draw().source_basket_id for _ in range(500)]
    b = [second.draw().source_basket_id for _ in range(500)]
    c = [third.draw().source_basket_id for _ in range(500)]

    assert a == b, "same seed produced different draws"
    assert a != c, "different seeds produced identical draws — the seed is being ignored"
