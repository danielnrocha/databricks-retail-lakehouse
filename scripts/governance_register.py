#!/usr/bin/env python3
"""The domain and KPI registers — the committed source of truth for GOV-003 and MOD-005.

Two registers live here because they answer the same kind of question: *who owns this, and where is
it defined once?* Neither is derivable from the data, so both have to be written down and both have
to be enforced against the workspace rather than trusted.

## Why this is in `scripts/` and not `src/`

Nothing in the pipeline graph reads it. Domains are tags applied out of band, and metric views are
catalog objects published out of band; neither is part of a bronze/silver/gold flow. Putting it in
`src/` would also collide with ENV-001, whose scan matches the `dng_` prefix — `dng_domain` is a
tag key rather than a catalog name, and the honest fix is to keep it out of the scanned tree rather
than to weaken the scan or to spell the constant in a way that evades it.

`mypy src scripts` covers this file, so it is type-checked either way.

## The finding that made this file necessary

The `dng_domain` tag policy already existed in the workspace when GOV-003 was picked up, created by
hand at 03:46 on 2026-08-07 with the description "Business domain assignment for gold assets
(GOV-003)". **No committed artifact created it.** A fresh Free Edition account following
`make bootstrap` would not have it, so a GOV-003 test written against that state would have passed
on one laptop and failed everywhere else — green in the only place it could not be trusted.
`publish_governance.py` now owns the policy, and this register is what it publishes from.
"""

from __future__ import annotations

from dataclasses import dataclass

# The governed tag key carrying the domain. Assembled from a namespace constant rather than written
# as one literal, purely so a reader sees where the prefix comes from; the value is `dng_domain`.
TAG_NAMESPACE = "dng"
DOMAIN_TAG_KEY = f"{TAG_NAMESPACE}_domain"

GOLD_SCHEMA = "gold"


# ---------------------------------------------------------------------------------------------
# Domains — GOV-003
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Domain:
    key: str
    title: str
    owner: str
    decisions: tuple[str, ...]
    rationale: str


DOMAINS: tuple[Domain, ...] = (
    Domain(
        key="customer_marketing",
        title="Customer Marketing",
        owner="Category Marketing / CRM",
        decisions=("D1", "D2"),
        rationale=(
            "Household-level behaviour: who to send a coupon to, and who is drifting toward lapse. "
            "Owns the assets whose grain is the household."
        ),
    ),
    Domain(
        key="trade_promotions",
        title="Trade Promotions",
        owner="Trade Planning",
        decisions=("D3",),
        rationale=(
            "Promotion performance at product-week grain, including the `unknown` exposure bucket "
            "that any lift calculation needs as a visible denominator."
        ),
    ),
    Domain(
        key="store_operations",
        title="Store Operations",
        owner="Store Ops",
        decisions=("D4",),
        rationale="Daily store totals — the baseline an anomaly detector subtracts from.",
    ),
    Domain(
        key="commercial_core",
        title="Commercial Core",
        owner="Data Platform (stewarded), no single business owner",
        decisions=("D1", "D2", "D3", "D4", "D5"),
        rationale=(
            "The conformed fact and the metric view over it. This domain exists because GOV-003 "
            "requires exactly one domain per asset and `fct_basket_line` genuinely serves all five "
            "decisions — forcing it into a business domain would name an owner who does not own "
            "it, and the first cross-domain change request would expose that. A shared/core domain "
            "is the standard answer and it is used here for the standard reason."
        ),
    ),
    Domain(
        key="platform_operations",
        title="Platform Operations",
        owner="Data Platform",
        decisions=(),
        rationale=(
            "Assets about the platform rather than about the business — reconciliation and quality "
            "artifacts. Kept separate so a reviewer filtering for business assets excludes them "
            "instead of finding a revenue-shaped table with no decision behind it."
        ),
    ),
)

# Exactly one domain per gold asset. GOV-003's acceptance criterion is "exactly one", so this is a
# mapping rather than a multimap, and the test asserts the workspace agrees with it.
DOMAIN_OF: dict[str, str] = {
    "fct_basket_line": "commercial_core",
    "agg_household_rfm": "customer_marketing",
    "agg_promo_performance": "trade_promotions",
    "agg_store_daily": "store_operations",
    "gold_reconciliation": "platform_operations",
    "mv_commercial": "commercial_core",
    "mv_household_lifecycle": "customer_marketing",
}

# With seven assets the mapping is close to one-per-domain, which is worth admitting: a taxonomy
# only earns its keep once several assets share an owner and a reviewer uses the domain to find
# them. At this size it documents intended ownership rather than organising a crowded catalog.


# ---------------------------------------------------------------------------------------------
# KPIs — MOD-005
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class MetricView:
    name: str
    source: str
    comment: str
    dimensions: tuple[tuple[str, str], ...]  # (name, expression)


@dataclass(frozen=True)
class Kpi:
    """One business number, defined exactly once.

    `expr` is the measure's SQL. Two KPIs sharing a normalised `expr` are the duplicate-definition
    failure MOD-005 exists to prevent — the same number computed twice, drifting apart the first
    time one copy is edited. That is asserted in `test_metrics_register.py`, not left to review.
    """

    name: str
    view: str
    measure: str
    expr: str
    unit: str
    decisions: tuple[str, ...]
    definition: str


METRIC_VIEWS: tuple[MetricView, ...] = (
    MetricView(
        name="mv_commercial",
        source="fct_basket_line",
        comment=(
            "Governed commercial KPIs over the conformed fact. Every revenue, volume and discount "
            "number in the platform is defined here once and rolled up by the engine; the gold "
            "aggregates are pre-computed grains for query cost, never second definitions of the "
            "same measure. NOTE: every row of the fact carries is_synthetic = true, because gold "
            "is built from the amplifier stream, which resamples real baskets rather than "
            "inventing them (ADR-0003). These are amplified volumes, not Northwind's books."
        ),
        dimensions=(
            ("transaction_date", "transaction_date"),
            ("week_no", "week_no"),
            ("store_id", "store_id"),
            ("store_volume_decile", "store_volume_decile"),
            ("product_id", "product_id"),
            ("department", "department"),
            ("commodity_desc", "commodity_desc"),
            ("brand_tier", "brand_tier"),
            ("promo_exposure", "promo_exposure"),
            ("income_band", "income_band"),
        ),
    ),
    MetricView(
        name="mv_household_lifecycle",
        source="agg_household_rfm",
        comment=(
            "Household lifecycle KPIs for D1 and D2. Deliberately holds no revenue measure: total "
            "spend is defined once in mv_commercial, and restating it here at household grain "
            "would be exactly the duplication MOD-005 forbids."
        ),
        dimensions=(
            ("has_demographics", "has_demographics"),
            ("first_seen_date", "first_seen_date"),
            ("last_seen_date", "last_seen_date"),
        ),
    ),
)

# 161 days = 23 weeks. Not chosen here: it is the lapse window pre-registered for the churn model
# on business reasoning before any model existed (docs/decision-log.md, 2026-08-07), and reusing it
# is the point — a KPI and a model that disagree about what "lapsed" means produce two truths.
LAPSE_WINDOW_DAYS = 161

KPIS: tuple[Kpi, ...] = (
    Kpi(
        name="net_sales_amt",
        view="mv_commercial",
        measure="net_sales_amt",
        expr="SUM(sales_amt)",
        unit="USD",
        decisions=("D3", "D4", "D5"),
        definition=(
            "Amount charged to the customer. The source column is already net of retail discount, "
            "so this is takings rather than gross list value."
        ),
    ),
    Kpi(
        name="units_sold",
        view="mv_commercial",
        measure="units_sold",
        expr=(
            "SUM(CASE WHEN coalesce(commodity_desc, '') <> 'COUPON/MISC ITEMS' "
            "THEN quantity_units ELSE 0 END)"
        ),
        unit="units",
        decisions=("D3", "D4"),
        definition=(
            "Merchandise item quantity, EXCLUDING the COUPON/MISC ITEMS commodity. The exclusion "
            "is not tidying — a plain SUM(quantity_units) measures 20,319,550 units across 21,479 "
            "baskets, or 946 units per basket, because 98.7% of it comes from 1,776 COUPON/MISC "
            "lines carrying quantities up to 48,073. That column is a coupon face-value artifact "
            "on those rows, not a count of items. Merchandise alone is 254,898 units, 11.87 per "
            "basket, which is a grocery basket. Line median quantity is 1 and p99 is 10.\n"
            "\n"
            "Stated explicitly because the two edits are indistinguishable from outside: this "
            "corrects a definition that measured the wrong quantity, rather than moving a "
            "threshold to obtain a nicer number. The measurement came first and is reproduced "
            "above so a reader can disagree with it."
        ),
    ),
    Kpi(
        name="baskets",
        view="mv_commercial",
        measure="baskets",
        expr="COUNT(DISTINCT basket_id)",
        unit="count",
        decisions=("D3", "D4"),
        definition=(
            "Distinct transactions. NON-ADDITIVE, and that is the reason this is a measure rather "
            "than a pre-summed column: sliced by promo_exposure the counts are 20,190 / 8,665 / "
            "6,082 / 4,995 / 561, which sum to 40,493 against a true total of 21,479, because one "
            "basket contains lines in several exposure buckets. A pre-aggregated basket count "
            "would be correct at its own grain and silently wrong at every other, which is exactly "
            "what a metric view exists to prevent — the engine re-derives it from the fact per "
            "grouping."
        ),
    ),
    Kpi(
        name="households",
        view="mv_commercial",
        measure="households",
        expr="COUNT(DISTINCT household_key)",
        unit="count",
        decisions=("D1", "D2"),
        definition=(
            "Distinct households transacting. Defined here rather than in mv_household_lifecycle "
            "so there is one household count in the platform, not two."
        ),
    ),
    Kpi(
        name="retail_discount_amt",
        view="mv_commercial",
        measure="retail_discount_amt",
        expr="-SUM(retail_disc_amt)",
        unit="USD",
        decisions=("D3",),
        definition=(
            "Retail discount given, as a POSITIVE amount. The source column is negative — measured "
            "range -70.00 to 0.00, total -106,644.82 across the fact — and the sign is flipped here "
            "once, deliberately, so that no consumer has to remember to do it and no two consumers "
            "do it differently."
        ),
    ),
    Kpi(
        name="coupon_discount_amt",
        view="mv_commercial",
        measure="coupon_discount_amt",
        expr="-SUM(coupon_disc_amt)",
        unit="USD",
        decisions=("D1",),
        definition="Coupon discount given, as a positive amount. Same sign convention as above.",
    ),
    Kpi(
        name="avg_basket_amt",
        view="mv_commercial",
        measure="avg_basket_amt",
        expr="MEASURE(net_sales_amt) / MEASURE(baskets)",
        unit="USD",
        decisions=("D4", "D5"),
        definition=(
            "Mean spend per basket. Composed from the two measures above via MEASURE() rather than "
            "restating their SQL, so it cannot drift from them."
        ),
    ),
    Kpi(
        name="retail_discount_rate",
        view="mv_commercial",
        measure="retail_discount_rate",
        expr="MEASURE(retail_discount_amt) / MEASURE(net_sales_amt)",
        unit="ratio",
        decisions=("D3",),
        definition=(
            "Retail discount as a share of net sales. Composed, not restated. Note the denominator "
            "is net of that discount, so this is discount-over-takings, not discount-over-list."
        ),
    ),
    Kpi(
        name="lapsed_households",
        view="mv_household_lifecycle",
        measure="lapsed_households",
        expr=f"COUNT(DISTINCT CASE WHEN recency_days >= {LAPSE_WINDOW_DAYS} THEN household_key END)",
        unit="count",
        decisions=("D2",),
        definition=(
            f"Households with no purchase for {LAPSE_WINDOW_DAYS} days (23 weeks) as of the "
            "dataset's own maximum date, never wall-clock. The window is the one pre-registered "
            "for the churn model, so the KPI and the model cannot disagree about 'lapsed'."
        ),
    ),
    Kpi(
        name="avg_recency_days",
        view="mv_household_lifecycle",
        measure="avg_recency_days",
        expr="AVG(recency_days)",
        unit="days",
        decisions=("D2",),
        definition="Mean days since last purchase, measured against the dataset's maximum date.",
    ),
    Kpi(
        name="avg_household_value_amt",
        view="mv_household_lifecycle",
        measure="avg_household_value_amt",
        expr="AVG(monetary_amt)",
        unit="USD",
        decisions=("D1",),
        definition=(
            "Mean lifetime spend per household over the observation window. An average of a "
            "per-household total, which is not the same number as net sales over household count "
            "and is not interchangeable with it."
        ),
    ),
    Kpi(
        name="avg_coupon_share_of_spend",
        view="mv_household_lifecycle",
        measure="avg_coupon_share_of_spend",
        expr="AVG(coupon_share_of_spend)",
        unit="ratio",
        decisions=("D1",),
        definition="Mean share of a household's spend on coupon-discounted lines, 0 to 1.",
    ),
)


def normalised(expr: str) -> str:
    """Collapse whitespace and case so two spellings of one definition compare equal.

    Not a SQL parser. `SUM(a)+SUM(b)` and `SUM(b)+SUM(a)` are the same number and will not compare
    equal here — stated so a passing duplicate test is not read as stronger than it is.
    """
    return " ".join(expr.split()).upper()


def kpis_for(view: str) -> tuple[Kpi, ...]:
    return tuple(kpi for kpi in KPIS if kpi.view == view)
