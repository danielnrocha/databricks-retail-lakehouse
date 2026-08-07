"""The reviewed quality ruleset — the only rules that are ever enforced.

This module is the source of truth in git. `publish.py` writes it to `<catalog>.ops.dq_rules`,
and the silver pipeline modules read *that table* at graph-construction time. Two consequences
worth being explicit about:

* The pipeline never imports this file. Lakeflow source files are executed individually and
  sibling imports are not reliable across runtimes, so the Unity Catalog table is the interface.
  That is not a workaround dressed up as a design — a governed table that several pipelines can
  read, with history and grants, is what QLT-001 actually asks for.
* A rule that is not published cannot fire. `publish.py` is therefore a deploy step, not an
  optional convenience, and the pipeline fails loudly if the table is missing rather than
  silently running with no quality gate.

Severity semantics, chosen deliberately:

| severity | effect                                                         |
|----------|----------------------------------------------------------------|
| `error`  | row is routed to the quarantine table; it does not reach silver |
| `warn`   | row passes; the violation is counted in the pipeline event log  |

There is no `drop`. NFR-3 says zero rows are silently discarded, and a drop is exactly that.

Why `warn` exists at all: quarantining a row is a decision to exclude it from every downstream
number. That is the right call for a row that is internally incoherent, and the wrong call for a
row that is merely unusual. Finding F6 is the canonical example — `QUANTITY` reaching 89,638 is a
weight-priced item in grams, not an error, and a range rule generated from the interquartile range
would have quarantined real revenue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "warn"]
Review = Literal["accepted", "amended", "authored"]

#: Bumped whenever the ruleset changes. Published alongside each rule so a quarantine row can be
#: traced to the exact ruleset version that rejected it — "which rule was in force in March?" is
#: otherwise unanswerable once a rule has been edited.
RULESET_VERSION = "1.0.0"


@dataclass(frozen=True)
class QualityRule:
    """One rule. `expression` is SQL that must evaluate to TRUE for an acceptable row.

    NULL is not a violation, matching Lakeflow expectation semantics: a rule about a column says
    nothing about rows where that column is absent. Absence is the job of a separate presence
    rule, and conflating the two produces rules that fire for two different reasons and are
    therefore undiagnosable.
    """

    name: str
    dataset: str
    expression: str
    severity: Severity
    tag: str
    origin: str
    review: Review
    rationale: str


# ---------------------------------------------------------------------------------------------
# silver.fact_basket_line
# ---------------------------------------------------------------------------------------------

_FACT = "silver.fact_basket_line"

FACT_RULES: tuple[QualityRule, ...] = (
    QualityRule(
        name="event_id_present",
        dataset=_FACT,
        expression="event_id IS NOT NULL",
        severity="error",
        tag="completeness",
        origin="dqx_profiler_candidate",
        review="accepted",
        rationale=(
            "The dedupe key. A row with no event_id cannot be made idempotent, so a replay would "
            "duplicate it silently."
        ),
    ),
    QualityRule(
        name="transaction_time_reconciled",
        dataset=_FACT,
        expression="transaction_time_hhmm IS NOT NULL",
        severity="error",
        tag="completeness",
        origin="dqx_profiler_candidate",
        review="accepted",
        rationale=(
            "Upstream renamed trans_time to transaction_time mid-stream. Each column is populated "
            "for the half of the timeline it existed in, so a query filtering on either one sees "
            "roughly 50% of the data and looks healthy doing it. This rule fires if the coalesce "
            "ever fails to find a value in either column, which is the signal that a third name "
            "has appeared. The profiler proposed the null check; it could not have proposed the "
            "reason, because the reconciliation happens before profiling sees the column."
        ),
    ),
    QualityRule(
        name="transaction_time_is_valid_hhmm",
        dataset=_FACT,
        expression="transaction_time_hhmm BETWEEN 0 AND 2359 AND transaction_time_hhmm % 100 < 60",
        severity="error",
        tag="validity",
        origin="dqx_profiler_candidate",
        review="amended",
        rationale=(
            "The profiler proposed the observed min/max range. Amended to also reject impossible "
            "minute values: HHMM is a packed integer, so 1373 is inside the observed range and is "
            "still not a time."
        ),
    ),
    QualityRule(
        name="quantity_parsed",
        dataset=_FACT,
        expression="quantity_units IS NOT NULL",
        severity="error",
        tag="validity",
        origin="dqx_profiler_candidate",
        review="accepted",
        rationale=(
            "Upstream retyped quantity from an integer to strings like '12 units'. Silver parses "
            "the leading numeral; a row whose quantity does not parse is a shape nobody has seen "
            "yet and must not be summed."
        ),
    ),
    QualityRule(
        name="quantity_non_negative",
        dataset=_FACT,
        expression="quantity_units >= 0",
        severity="error",
        tag="validity",
        origin="dqx_profiler_candidate",
        review="amended",
        rationale=(
            "AMENDS the profiler's interquartile range rule. QUANTITY legitimately reaches 89,638 "
            "because weight-priced items are expressed in grams (finding F6). A bounded range "
            "would quarantine real revenue at a rate slow enough that nobody would notice for "
            "months. Only the sign is asserted, because only the sign is actually impossible."
        ),
    ),
    QualityRule(
        name="revenue_requires_quantity",
        dataset=_FACT,
        expression="quantity_units > 0 OR sales_amt = 0",
        severity="error",
        tag="consistency",
        origin="domain_knowledge",
        review="authored",
        rationale=(
            "A line that charged money for nothing is internally incoherent: it inflates revenue "
            "while contributing no units, so revenue-per-unit and basket composition both drift. "
            "A zero-quantity zero-value line is a void and is allowed through."
        ),
    ),
    QualityRule(
        name="sales_amt_present",
        dataset=_FACT,
        expression="sales_amt IS NOT NULL",
        severity="error",
        tag="completeness",
        origin="dqx_profiler_candidate",
        review="accepted",
        rationale=(
            "The revenue measure. A null here would be summed as zero by every aggregate and "
            "would therefore understate revenue without ever showing up as a missing row."
        ),
    ),
    QualityRule(
        name="sales_amt_non_negative",
        dataset=_FACT,
        expression="sales_amt >= 0",
        severity="error",
        tag="validity",
        origin="dqx_profiler_candidate",
        review="amended",
        rationale=(
            "Zero is explicitly permitted — a fully coupon-offset line is legitimate (F6). The "
            "profiler's lower bound came from the observed minimum, which happened to be zero; "
            "keeping it as a strict '> 0' would have been correct by accident and wrong the first "
            "time a refund appeared."
        ),
    ),
    QualityRule(
        name="discount_amounts_present",
        dataset=_FACT,
        expression=(
            "retail_disc_amt IS NOT NULL AND coupon_disc_amt IS NOT NULL "
            "AND coupon_match_disc_amt IS NOT NULL"
        ),
        severity="error",
        tag="completeness",
        origin="dqx_profiler_candidate",
        review="amended",
        rationale=(
            "AMENDS three separate null candidates into one. The three discount columns are a "
            "single ledger — gross less retail less coupon less coupon-match — and a row missing "
            "any one of them cannot be reconciled, so splitting them into three rules would "
            "produce three quarantine reasons for one defect."
        ),
    ),
    QualityRule(
        name="retail_discount_is_not_a_surcharge",
        dataset=_FACT,
        expression="retail_disc_amt <= 0",
        severity="error",
        tag="validity",
        origin="dqx_profiler_candidate",
        review="amended",
        rationale=(
            "AMENDS the profiler's fitted range to the only bound that is actually a law: a "
            "discount that adds money is a sign error. It is rare — two rows in 200,000 — which "
            "is exactly why it needs a rule rather than an eyeball: a sign error at this rate is "
            "invisible in any aggregate and corrupts the discount ledger permanently. The "
            "profiler's lower bound (-4.23, from a 1,000-row sample) would have rejected every "
            "discount larger than that, and the real minimum is -70.00."
        ),
    ),
    QualityRule(
        name="coupon_discounts_are_not_surcharges",
        dataset=_FACT,
        expression="coupon_disc_amt <= 0 AND coupon_match_disc_amt <= 0",
        severity="error",
        tag="validity",
        origin="dqx_profiler_candidate",
        review="amended",
        rationale=(
            "Same amendment as retail_discount_is_not_a_surcharge, applied to the coupon side. "
            "Currently zero violations — which is the point of F7: a rule that has never fired is "
            "an untested rule, so this one is exercised by injected cases in the unit suite "
            "rather than trusted because production is quiet."
        ),
    ),
    QualityRule(
        name="zero_sales_has_offsetting_discount",
        dataset=_FACT,
        expression=(
            "sales_amt > 0 OR retail_disc_amt < 0 OR coupon_disc_amt < 0 "
            "OR coupon_match_disc_amt < 0 OR quantity_units = 0"
        ),
        severity="warn",
        tag="consistency",
        origin="domain_knowledge",
        review="authored",
        rationale=(
            "WARN, not error, and the reasoning is the point. A zero-value line with a positive "
            "quantity and no discount to explain it cannot be reconciled — but quarantining it "
            "protects zero revenue while removing a real line from basket-size and "
            "items-per-visit metrics. Recording it is strictly better than dropping it. If the "
            "count moves, something upstream changed."
        ),
    ),
    QualityRule(
        name="store_id_present",
        dataset=_FACT,
        expression="store_id IS NOT NULL",
        severity="error",
        tag="completeness",
        origin="dqx_profiler_candidate",
        review="accepted",
        rationale="Foreign key to silver.dim_store. An orphan fact cannot be attributed to a site.",
    ),
    QualityRule(
        name="product_id_present",
        dataset=_FACT,
        expression="product_id IS NOT NULL",
        severity="error",
        tag="completeness",
        origin="dqx_profiler_candidate",
        review="accepted",
        rationale="Foreign key to silver.dim_product_scd2. Required by QLT-004.",
    ),
    QualityRule(
        name="household_key_present",
        dataset=_FACT,
        expression="household_key IS NOT NULL",
        severity="error",
        tag="completeness",
        origin="dqx_profiler_candidate",
        review="accepted",
        rationale="Foreign key to silver.dim_household_scd2. Required by QLT-004.",
    ),
    QualityRule(
        name="transaction_ts_present",
        dataset=_FACT,
        expression="transaction_ts IS NOT NULL",
        severity="error",
        tag="completeness",
        origin="dqx_profiler_candidate",
        review="accepted",
        rationale=(
            "The point-in-time join key. Without it a fact row cannot be attached to any "
            "dimension version, so it is unusable rather than merely incomplete."
        ),
    ),
    QualityRule(
        name="transaction_ts_not_in_future",
        dataset=_FACT,
        expression="transaction_ts <= current_timestamp()",
        severity="error",
        tag="validity",
        origin="dqx_profiler_candidate",
        review="amended",
        rationale=(
            "AMENDS the profiler's fitted window. It proposed a lower bound of 2024-01-30, taken "
            "from a 1,000-row sample; the table actually starts on 2024-01-01, so that rule would "
            "have quarantined the first month of history on its first run. Only the upper bound "
            "survives, and only because a future event time is genuinely impossible: it would "
            "resolve against a dimension version that does not exist yet."
        ),
    ),
    QualityRule(
        name="week_no_within_seed_window",
        dataset=_FACT,
        expression="week_no BETWEEN 1 AND 102",
        severity="warn",
        tag="validity",
        origin="dqx_profiler_candidate",
        review="amended",
        rationale=(
            "WARN, and the bound is widened. The profiler proposed 5-102 from its sample; the "
            "seed actually spans 1-102, so the proposed rule would have quarantined the first "
            "four weeks. Even corrected, the bound is a property of this snapshot rather than of "
            "the business, so enforcing it as an error would make the pipeline reject the first "
            "week of genuinely new data."
        ),
    ),
)

# ---------------------------------------------------------------------------------------------
# Dimensions
#
# Dimension rules are invariants rather than filters: a dimension row that violates one indicates
# the generator or the seed is broken, and continuing would corrupt every join downstream. They
# are enforced as fail-the-update expectations on the change-feed views, not routed to quarantine,
# because there is no meaningful "partially loaded dimension" state to recover from.
# ---------------------------------------------------------------------------------------------

DIMENSION_RULES: tuple[QualityRule, ...] = (
    QualityRule(
        name="product_id_present",
        dataset="silver.dim_product_scd2",
        expression="product_id IS NOT NULL",
        severity="error",
        tag="completeness",
        origin="domain_knowledge",
        review="authored",
        rationale="The SCD2 business key. A null key would collapse every version into one group.",
    ),
    QualityRule(
        name="department_never_null",
        dataset="silver.dim_product_scd2",
        expression="department IS NOT NULL",
        severity="error",
        tag="completeness",
        origin="domain_knowledge",
        review="authored",
        rationale=(
            "15 seed products have a blank department (F7). Silver maps them to UNCATEGORISED so "
            "department roll-ups sum to the grand total by construction. This rule asserts the "
            "mapping actually happened — without it the roll-up quietly stops reconciling."
        ),
    ),
    QualityRule(
        name="brand_tier_is_binary",
        dataset="silver.dim_product_scd2",
        expression="brand_tier IN ('National', 'Private')",
        severity="error",
        tag="validity",
        origin="domain_knowledge",
        review="authored",
        rationale=(
            "The seed column is named BRAND but holds a two-valued tier flag (F5). It is renamed "
            "brand_tier in silver; this rule pins the domain so a third value cannot arrive and "
            "be quietly aggregated as if it were a brand name."
        ),
    ),
    QualityRule(
        name="household_key_present",
        dataset="silver.dim_household_scd2",
        expression="household_key IS NOT NULL",
        severity="error",
        tag="completeness",
        origin="domain_knowledge",
        review="authored",
        rationale=(
            "The SCD2 business key. A null would collapse every household's versions into one "
            "group, so a single household would appear to have changed demographics repeatedly."
        ),
    ),
    QualityRule(
        name="demographics_flag_matches_payload",
        dataset="silver.dim_household_scd2",
        expression="has_demographics = (age_band IS NOT NULL)",
        severity="error",
        tag="consistency",
        origin="domain_knowledge",
        review="authored",
        rationale=(
            "Only 801 of 2,500 households have demographics (F4). The gap is modelled as nullable "
            "attributes plus an explicit flag, so downstream code can distinguish 'unknown' from "
            "'not applicable'. The flag and the payload must agree or the distinction is a lie."
        ),
    ),
    QualityRule(
        name="store_id_present",
        dataset="silver.dim_store",
        expression="store_id IS NOT NULL",
        severity="error",
        tag="completeness",
        origin="domain_knowledge",
        review="authored",
        rationale="The dimension key, derived from observed transactions — there is no store master.",
    ),
    QualityRule(
        name="store_activity_is_positive",
        dataset="silver.dim_store",
        expression="transaction_lines > 0",
        severity="error",
        tag="validity",
        origin="domain_knowledge",
        review="authored",
        rationale=(
            "The dimension is derived from transactions, so a store with no lines cannot exist by "
            "construction. If one appears, the derivation is wrong, not the data."
        ),
    ),
)

REVIEWED_RULES: tuple[QualityRule, ...] = FACT_RULES + DIMENSION_RULES

#: Every silver table this ruleset governs. QLT-001 asserts the two sets are equal, so adding a
#: silver table without adding a rule is a test failure rather than a discovery in production.
GOVERNED_DATASETS: tuple[str, ...] = (
    "silver.fact_basket_line",
    "silver.dim_product_scd2",
    "silver.dim_household_scd2",
    "silver.dim_store",
)


def rules_for(dataset: str, severity: Severity | None = None) -> tuple[QualityRule, ...]:
    """Rules governing one dataset, optionally filtered by severity."""
    return tuple(
        rule
        for rule in REVIEWED_RULES
        if rule.dataset == dataset and (severity is None or rule.severity == severity)
    )
