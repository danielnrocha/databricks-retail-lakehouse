"""The quality ruleset and the review gate between profiling and enforcement.

QLT-001 — every silver table resolves to at least one rule.
QLT-005 — no profiler-generated rule reaches the enforced set without a recorded decision.

`test_generated_rules_require_review` is the load-bearing one. It parses the decision table in
`docs/quality/rule-review.md` and fails if a candidate has no decision, if a decision names a rule
that does not exist, or if a profiler-derived rule appears in `rules.py` without a row explaining
how it got there. It is deliberately a *file* check rather than a code check: the artefact QLT-005
asks for is a review someone can read, and a decision recorded only as a Python constant is not
a review, it is a comment.

The rule expressions are also executed against injected violations, because F7's point holds for
rules as much as for data: a rule that has never seen a violation is an untested rule, and several
of these have zero violations in the current seed.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from retail_lakehouse.quality.rules import (
    GOVERNED_DATASETS,
    REVIEWED_RULES,
    RULESET_VERSION,
    rules_for,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CANDIDATES_FILE = REPO_ROOT / "docs" / "quality" / "dqx-candidate-rules.yml"
REVIEW_FILE = REPO_ROOT / "docs" / "quality" / "rule-review.md"

VALID_DECISIONS = {"ACCEPT", "AMEND", "REJECT"}


@pytest.fixture(scope="module")
def spark() -> SparkSession:
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    session = (
        SparkSession.builder.master("local[2]")
        .appName("test-quality-rules")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "2")
        .getOrCreate()
    )
    yield session
    session.stop()


@pytest.fixture(scope="module")
def candidates() -> list[dict]:
    document = yaml.safe_load(CANDIDATES_FILE.read_text())
    return document["candidate_checks"]


@pytest.fixture(scope="module")
def review() -> dict[str, tuple[str, str]]:
    """`{candidate_name: (decision, enforced_as)}` parsed from the review table.

    Parsed rather than duplicated into a YAML sidecar on purpose. Two files holding the same
    decisions drift, and the one that drifts is always the one nobody reads.
    """
    pattern = re.compile(r"^\|\s*`([a-z0-9_]+)`\s*\|\s*(\w+)\s*\|\s*(.*?)\s*\|")
    decisions: dict[str, tuple[str, str]] = {}
    for line in REVIEW_FILE.read_text().splitlines():
        match = pattern.match(line)
        if not match:
            continue
        candidate, decision, enforced = match.groups()
        if decision not in VALID_DECISIONS:
            continue
        decisions[candidate] = (decision, enforced.strip("` "))
    return decisions


# ---------------------------------------------------------------------------------------------
# QLT-001
# ---------------------------------------------------------------------------------------------
def test_every_silver_table_has_rules():
    for dataset in GOVERNED_DATASETS:
        assert rules_for(dataset), f"{dataset} has no quality rules."


def test_governed_datasets_and_ruleset_agree():
    """Neither direction is allowed to drift.

    A silver table with no rule is an ungated table. A rule for a table that no longer exists is
    a rule that will never fire and will be trusted anyway.
    """
    in_rules = {rule.dataset for rule in REVIEWED_RULES}
    assert in_rules == set(GOVERNED_DATASETS)


def test_rule_names_are_unique_within_a_dataset():
    seen: set[tuple[str, str]] = set()
    for rule in REVIEWED_RULES:
        key = (rule.dataset, rule.name)
        assert key not in seen, f"Duplicate rule name {rule.name} on {rule.dataset}."
        seen.add(key)


def test_every_rule_states_why_it_exists():
    """A rule with no rationale cannot be reviewed, only obeyed."""
    for rule in REVIEWED_RULES:
        assert len(rule.rationale) > 40, f"{rule.name} has no usable rationale."


def test_no_rule_silently_drops_rows():
    """NFR-3. `error` quarantines and `warn` records; there is no severity that deletes."""
    assert {rule.severity for rule in REVIEWED_RULES} <= {"error", "warn"}


def test_ruleset_version_is_semantic():
    assert re.fullmatch(r"\d+\.\d+\.\d+", RULESET_VERSION)


# ---------------------------------------------------------------------------------------------
# QLT-005 — the review gate
# ---------------------------------------------------------------------------------------------
def test_generated_rules_require_review(candidates, review):
    candidate_names = {check["name"] for check in candidates}
    enforced_names = {rule.name for rule in REVIEWED_RULES}

    undecided = candidate_names - set(review)
    assert not undecided, (
        f"Profiler candidates with no recorded review decision: {sorted(undecided)}"
    )

    unknown = set(review) - candidate_names
    assert not unknown, (
        f"Review rows for candidates the profiler did not generate: {sorted(unknown)}"
    )

    for candidate, (decision, enforced_as) in sorted(review.items()):
        if decision == "REJECT":
            assert enforced_as in {"—", "-", ""}, (
                f"{candidate} is REJECTed but names an enforced rule."
            )
            continue
        assert enforced_as in enforced_names, (
            f"{candidate} was {decision}ed as '{enforced_as}', which is not in the ruleset."
        )

    # And the other direction: nothing profiler-derived may appear in the enforced set without a
    # row in the review table. This is the assertion that actually stops a candidate being pasted
    # into rules.py during a hurried afternoon.
    reviewed_targets = {enforced for _, enforced in review.values()}
    for rule in REVIEWED_RULES:
        if rule.origin == "dqx_profiler_candidate":
            assert rule.name in reviewed_targets, (
                f"{rule.name} claims profiler origin but has no review row."
            )


def test_the_naive_quantity_range_was_rejected(candidates, review):
    """A regression guard on finding F6, named because this is the exact rule that would hurt.

    The profiler fits a range to `quantity_units` from a 1,000-row sample. Weight-priced items
    express quantity in grams and reach five figures, so the fitted upper bound quarantines real
    revenue at a rate slow enough that nobody notices for months. If a future regeneration
    silently promotes this candidate, this test is what stops it.
    """
    fitted = next(
        check
        for check in candidates
        if check["check"]["function"] == "is_in_range"
        and check["check"]["arguments"]["column"] == "quantity_units"
    )
    upper = fitted["check"]["arguments"]["max_limit"]
    assert upper < 89_638, (
        "The fixture no longer demonstrates the problem; regenerate and re-review."
    )

    decision, enforced_as = review[fitted["name"]]
    assert decision == "AMEND"
    assert enforced_as == "quantity_non_negative"

    enforced = {rule.name: rule.expression for rule in rules_for("silver.fact_basket_line")}
    assert enforced["quantity_non_negative"] == "quantity_units >= 0"
    assert "BETWEEN" not in enforced["quantity_non_negative"].upper()


def test_identifier_range_rules_were_all_rejected(candidates, review):
    """Ranges over identifiers are meaningless and were rejected as a class, not one by one."""
    for column in ("store_id", "product_id", "household_key"):
        name = f"{column}_isnt_in_range"
        assert name in review, f"{name} is missing from the review table."
        assert review[name][0] == "REJECT"


# ---------------------------------------------------------------------------------------------
# The rules themselves, executed. F7: a rule that has never fired is an untested rule.
# ---------------------------------------------------------------------------------------------
def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


FACT_SCHEMA = (
    "event_id STRING, store_id BIGINT, product_id BIGINT, household_key BIGINT, week_no INT, "
    "quantity_units BIGINT, transaction_time_hhmm INT, sales_amt DOUBLE, retail_disc_amt DOUBLE, "
    "coupon_disc_amt DOUBLE, coupon_match_disc_amt DOUBLE, transaction_ts TIMESTAMP"
)


def _row(**overrides):
    base = {
        "event_id": "ok",
        "store_id": 364,
        "product_id": 1004906,
        "household_key": 2375,
        "week_no": 1,
        "quantity_units": 1,
        "transaction_time_hhmm": 1631,
        "sales_amt": 1.39,
        "retail_disc_amt": -0.6,
        "coupon_disc_amt": 0.0,
        "coupon_match_disc_amt": 0.0,
        "transaction_ts": _ts("2025-01-01 16:31:00"),
    }
    base.update(overrides)
    return tuple(base[name.split()[0]] for name in FACT_SCHEMA.split(", "))


# (rule name, the row that must fail it). Every error-severity fact rule appears here, including
# the ones with zero violations in the current seed — especially those.
VIOLATIONS = [
    ("event_id_present", {"event_id": None}),
    ("transaction_time_reconciled", {"transaction_time_hhmm": None}),
    ("transaction_time_is_valid_hhmm", {"transaction_time_hhmm": 1373}),
    ("transaction_time_is_valid_hhmm", {"transaction_time_hhmm": 2401}),
    ("quantity_parsed", {"quantity_units": None}),
    ("quantity_non_negative", {"quantity_units": -1}),
    ("revenue_requires_quantity", {"quantity_units": 0, "sales_amt": 4.5}),
    ("sales_amt_present", {"sales_amt": None}),
    ("sales_amt_non_negative", {"sales_amt": -1.0}),
    ("discount_amounts_present", {"coupon_disc_amt": None}),
    ("retail_discount_is_not_a_surcharge", {"retail_disc_amt": 3.99}),
    ("coupon_discounts_are_not_surcharges", {"coupon_disc_amt": 0.5}),
    ("store_id_present", {"store_id": None}),
    ("product_id_present", {"product_id": None}),
    ("household_key_present", {"household_key": None}),
    ("transaction_ts_present", {"transaction_ts": None}),
    ("transaction_ts_not_in_future", {"transaction_ts": _ts("2099-01-01 00:00:00")}),
    ("zero_sales_has_offsetting_discount", {"sales_amt": 0.0, "retail_disc_amt": 0.0}),
    ("week_no_within_seed_window", {"week_no": 103}),
]


@pytest.mark.parametrize(("rule_name", "overrides"), VIOLATIONS, ids=lambda v: str(v)[:60])
def test_each_rule_rejects_the_row_it_is_meant_to(spark: SparkSession, rule_name, overrides):
    rule = next(r for r in rules_for("silver.fact_basket_line") if r.name == rule_name)
    frame = spark.createDataFrame([_row(**overrides)], FACT_SCHEMA)
    verdict = frame.select(F.expr(f"({rule.expression}) IS FALSE").alias("violated")).collect()[0]
    assert verdict["violated"], f"{rule_name} did not reject {overrides}."


def test_a_clean_row_violates_nothing(spark: SparkSession):
    """The other half of the contract. A rule that rejects everything also rejects nothing useful."""
    frame = spark.createDataFrame([_row()], FACT_SCHEMA)
    for rule in rules_for("silver.fact_basket_line"):
        verdict = frame.select(F.expr(f"({rule.expression}) IS FALSE").alias("violated")).collect()[
            0
        ]
        assert not verdict["violated"], f"{rule.name} rejected a legitimate row."


def test_weight_priced_and_coupon_offset_lines_survive(spark: SparkSession):
    """F6, as an assertion rather than a note in a document.

    A 48,073-gram weight-priced line and a fully coupon-offset zero-value line are both legitimate
    and both are what a fitted range rule would have quarantined.
    """
    rows = [
        _row(event_id="weighted", quantity_units=48073, sales_amt=13.55),
        _row(event_id="coupon_offset", sales_amt=0.0, coupon_disc_amt=-2.5),
    ]
    frame = spark.createDataFrame(rows, FACT_SCHEMA)
    for rule in rules_for("silver.fact_basket_line", severity="error"):
        failures = frame.filter(F.expr(f"({rule.expression}) IS FALSE")).count()
        assert failures == 0, f"{rule.name} quarantined a legitimate line."
