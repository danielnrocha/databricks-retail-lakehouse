"""Empirical basket sampler.

The central honesty constraint of this project lives in this file.

The amplifier **resamples observed baskets**; it does not synthesise shopping behaviour from a
generative rule. A whole real basket — its store, its line items, its quantities, its prices, its
discounts — is drawn as a unit and re-stamped with a new identity and timestamp.

Why this matters, concretely: if the generator instead drew "a household, then N products weighted
by popularity, then a price", it would have invented a joint distribution. Every correlation in the
output would be one the author put there, and any model trained on it would recover the author's
assumptions and report them as accuracy. Resampling whole baskets means the co-purchase structure,
the store mix, the price/discount relationship, and the skew are all the real ones, because they
are literally the observed ones.

The price paid, stated so nobody has to discover it: resampling cannot produce behaviour the seed
never exhibited. No novel co-purchase pairs, no new stores, no unseen price points. The amplifier
increases *volume and arrival dynamics*, not *variety*. That is exactly what is needed for the
streaming, file-sizing, and skew work — and exactly what disqualifies amplified data from
evaluating a model, which is why MLR-003 forbids it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq


@dataclass(frozen=True)
class BasketLine:
    product_id: int
    quantity: int
    sales_value: float
    retail_disc: float
    coupon_disc: float
    coupon_match_disc: float


@dataclass(frozen=True)
class Basket:
    """One observed shopping trip, drawn intact from the seed."""

    source_basket_id: int
    household_key: int
    store_id: int
    day: int
    trans_time: int
    week_no: int
    lines: tuple[BasketLine, ...]

    @property
    def line_count(self) -> int:
        return len(self.lines)


class BasketSampler:
    """Draws whole baskets from the seed's observed population.

    Sampling baskets uniformly reproduces the store, product, and household distributions exactly,
    because each basket carries its own. There is no separate skew model and no skew parameter —
    the skew comes along for free because it is real. That is the whole design.
    """

    def __init__(self, seed_dir: Path, *, random_seed: int = 0) -> None:
        table = pq.read_table(
            seed_dir / "transaction_data.parquet",
            columns=[
                "household_key",
                "BASKET_ID",
                "DAY",
                "PRODUCT_ID",
                "QUANTITY",
                "SALES_VALUE",
                "STORE_ID",
                "RETAIL_DISC",
                "TRANS_TIME",
                "WEEK_NO",
                "COUPON_DISC",
                "COUPON_MATCH_DISC",
            ],
        )
        self._rng = np.random.default_rng(random_seed)

        basket_ids = table.column("BASKET_ID").to_numpy()
        # Sort by basket so each basket's lines are contiguous; then a single pass finds the
        # boundaries. Grouping 2.6M rows with a dict of lists costs about 40x more memory and is
        # noticeably slower, which matters because this runs on every generator invocation.
        order = np.argsort(basket_ids, kind="stable")
        self._order = order
        sorted_ids = basket_ids[order]
        boundaries = np.flatnonzero(np.diff(sorted_ids)) + 1
        self._starts = np.concatenate(([0], boundaries))
        self._ends = np.concatenate((boundaries, [len(sorted_ids)]))

        self._cols = {
            name: table.column(name).to_numpy()
            for name in (
                "household_key",
                "BASKET_ID",
                "DAY",
                "PRODUCT_ID",
                "QUANTITY",
                "SALES_VALUE",
                "STORE_ID",
                "RETAIL_DISC",
                "TRANS_TIME",
                "WEEK_NO",
                "COUPON_DISC",
                "COUPON_MATCH_DISC",
            )
        }

    @property
    def basket_count(self) -> int:
        return len(self._starts)

    @property
    def line_count(self) -> int:
        return len(self._order)

    @property
    def mean_basket_size(self) -> float:
        return self.line_count / self.basket_count

    def basket_at(self, index: int) -> Basket:
        rows = self._order[self._starts[index] : self._ends[index]]
        first = rows[0]
        col = self._cols
        return Basket(
            source_basket_id=int(col["BASKET_ID"][first]),
            household_key=int(col["household_key"][first]),
            store_id=int(col["STORE_ID"][first]),
            day=int(col["DAY"][first]),
            trans_time=int(col["TRANS_TIME"][first]),
            week_no=int(col["WEEK_NO"][first]),
            lines=tuple(
                BasketLine(
                    product_id=int(col["PRODUCT_ID"][r]),
                    quantity=int(col["QUANTITY"][r]),
                    sales_value=float(col["SALES_VALUE"][r]),
                    retail_disc=float(col["RETAIL_DISC"][r]),
                    coupon_disc=float(col["COUPON_DISC"][r]),
                    coupon_match_disc=float(col["COUPON_MATCH_DISC"][r]),
                )
                for r in rows
            ),
        )

    def draw(self, store_skew_multiplier: float = 1.0) -> Basket:
        """Draw one basket.

        `store_skew_multiplier` of 1.0 samples uniformly, which preserves the observed
        distribution exactly. Above 1.0 it biases toward baskets in already-busy stores, to stress
        beyond observed skew. It cannot invent skew that is not present — at multiplier 1.0 the
        weighting term vanishes entirely.
        """
        if store_skew_multiplier == 1.0:
            return self.basket_at(int(self._rng.integers(self.basket_count)))

        if not hasattr(self, "_skew_weights"):
            self._build_skew_weights()
        weights = self._skew_weights**store_skew_multiplier  # type: ignore[has-type]
        probabilities = weights / weights.sum()
        return self.basket_at(int(self._rng.choice(self.basket_count, p=probabilities)))

    def _build_skew_weights(self) -> None:
        store_per_basket = np.array([self._cols["STORE_ID"][self._order[s]] for s in self._starts])
        _, inverse, counts = np.unique(store_per_basket, return_inverse=True, return_counts=True)
        # Weight each basket by its store's share, normalised so multiplier 1.0 is a no-op.
        self._skew_weights = (counts[inverse] / counts.mean()).astype(np.float64)
