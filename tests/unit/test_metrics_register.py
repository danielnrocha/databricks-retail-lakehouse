"""MOD-005 — each KPI exists once, as a governed Unity Catalog metric definition.

The requirement's acceptance criterion has two halves: *each KPI in the metric register resolves to
exactly one UC Metrics definition*, and *no duplicate SQL*. The first half needs a workspace and
lives in `tests/integration/test_governance.py`. This module is the second half, and it runs
offline on every push — which is the right split, because duplicate definitions are a property of
the register itself and catching them should not depend on a warehouse being awake.

## What MOD-005 is actually afraid of

Not a missing metric. A *second* one. "Net sales" defined in a dashboard, then again in a notebook,
then again in a model's feature query, each with a slightly different filter — three numbers with
one name, discovered in a meeting where two of them are on the same slide. The defence is that a
KPI has exactly one definition and everything else composes from it, which is why
`avg_basket_amt` and `retail_discount_rate` are written as `MEASURE(...)` over other measures
rather than as their own `SUM(...)` expressions: composed, they cannot drift; restated, they will.

`normalised()` is whitespace-and-case only, not a SQL parser, so `SUM(a)+SUM(b)` and `SUM(b)+SUM(a)`
compare unequal. Stated so a pass is not read as stronger than it is.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# The registers live in `scripts/` rather than `src/` — nothing in the pipeline graph reads them,
# and `dng_domain` would otherwise meet ENV-001's `dng_` prefix scan, which the honest fix keeps
# out of the scanned tree rather than working around. `scripts/` is not an installed package, so
# the path goes on explicitly here instead of through a conftest that would hide it.
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from governance_register import (  # noqa: E402
    DOMAIN_OF,
    DOMAINS,
    KPIS,
    METRIC_VIEWS,
    Kpi,
    kpis_for,
    normalised,
)

VALID_UNITS = {"USD", "count", "units", "days", "ratio"}
DECISIONS = {"D1", "D2", "D3", "D4", "D5"}


# ---------------------------------------------------------------------------------------------
# MOD-005
# ---------------------------------------------------------------------------------------------
def test_no_duplicate_kpi_definitions() -> None:
    """One name per KPI, one measure slot per KPI, and one expression across the whole register.

    The third assertion is the load-bearing one. The first two catch a copy-paste; only the third
    catches the same number being defined twice under two names, which is the failure that produces
    two truths in a meeting.
    """
    names = [kpi.name for kpi in KPIS]
    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    assert not duplicate_names, f"KPI names declared more than once: {duplicate_names}"

    slots = [(kpi.view, kpi.measure) for kpi in KPIS]
    duplicate_slots = sorted({slot for slot in slots if slots.count(slot) > 1})
    assert not duplicate_slots, (
        f"two KPIs claim the same measure inside the same metric view: {duplicate_slots}. "
        "The later definition would silently overwrite the earlier one at publish time."
    )

    by_expression: dict[str, list[Kpi]] = {}
    for kpi in KPIS:
        by_expression.setdefault(normalised(kpi.expr), []).append(kpi)
    collisions = {expr: kpis for expr, kpis in by_expression.items() if len(kpis) > 1}
    assert not collisions, (
        "the same SQL is registered under more than one KPI, which is the duplicate definition "
        "MOD-005 exists to prevent — the two copies drift apart the first time one is edited:\n"
        + "\n".join(
            f"  {expr}\n" + "\n".join(f"    {k.name} (in {k.view})" for k in kpis)
            for expr, kpis in sorted(collisions.items())
        )
    )


def test_every_kpi_belongs_to_a_declared_metric_view() -> None:
    """A KPI pointing at a view that is not published resolves to zero definitions, not one."""
    declared = {view.name for view in METRIC_VIEWS}
    orphans = sorted({kpi.name for kpi in KPIS if kpi.view not in declared})
    assert not orphans, f"KPIs registered against an undeclared metric view: {orphans}"

    empty = sorted(view.name for view in METRIC_VIEWS if not kpis_for(view.name))
    assert not empty, (
        f"metric views with no measures: {empty}. An empty view publishes successfully and "
        "answers every question with nothing, which is worse than being absent."
    )


def test_derived_kpis_compose_rather_than_restate() -> None:
    """A measure built from other measures must reference them, not re-derive them.

    `avg_basket_amt` written as `SUM(sales_amt) / COUNT(DISTINCT basket_id)` would agree with
    `net_sales_amt` and `baskets` on the day it was written and would stop agreeing the first time
    either changed — a filter added to net sales, say. Composing through `MEASURE()` makes that
    impossible rather than unlikely.
    """
    measures_by_view = {
        view.name: {kpi.measure for kpi in kpis_for(view.name)} for view in METRIC_VIEWS
    }
    for kpi in KPIS:
        if "MEASURE(" not in kpi.expr.upper():
            continue
        referenced = {
            fragment.split(")")[0].strip() for fragment in kpi.expr.upper().split("MEASURE(")[1:]
        }
        available = {measure.upper() for measure in measures_by_view[kpi.view]}
        unknown = referenced - available
        assert not unknown, (
            f"{kpi.name} composes from {sorted(unknown)}, which is not a measure of {kpi.view}"
        )
        assert kpi.measure.upper() not in referenced, f"{kpi.name} references itself"


def test_every_kpi_states_a_unit_a_decision_and_a_definition() -> None:
    """MOD-006's discipline, applied to metrics rather than columns.

    A number without a unit is the class of defect that loses spacecraft, and a KPI that cannot
    name the decision it serves is a number somebody computed because the column was there — the
    same test `aggregates.py` applies to gold tables in its own docstring.
    """
    for kpi in KPIS:
        assert kpi.unit in VALID_UNITS, f"{kpi.name} declares unit {kpi.unit!r}"
        assert kpi.decisions, f"{kpi.name} names no decision it serves"
        assert set(kpi.decisions) <= DECISIONS, (
            f"{kpi.name} names decisions outside D1..D5: {sorted(set(kpi.decisions) - DECISIONS)}"
        )
        assert len(kpi.definition) > 40, (
            f"{kpi.name}'s definition is too thin to disambiguate it from a similar number"
        )
        if kpi.unit in {"USD", "ratio"}:
            assert kpi.expr.strip(), f"{kpi.name} has no expression"


# ---------------------------------------------------------------------------------------------
# The domain register's offline half. GOV-003's workspace assertions are in the integration suite.
# ---------------------------------------------------------------------------------------------
def test_every_asset_maps_to_exactly_one_declared_domain() -> None:
    declared = {domain.key for domain in DOMAINS}
    unknown = sorted({d for d in DOMAIN_OF.values() if d not in declared})
    assert not unknown, f"assets assigned to undeclared domains: {unknown}"

    # DOMAIN_OF is a dict, so "exactly one" is structural rather than asserted. This test exists to
    # fail loudly if it is ever widened to a list "just for the shared fact" — which is the change
    # a reviewer would wave through, and which turns GOV-003 into a different requirement.
    for asset, domain in DOMAIN_OF.items():
        assert isinstance(domain, str), (
            f"{asset} maps to {domain!r}. GOV-003 requires exactly one domain per asset; a "
            "collection here means the requirement was quietly relaxed rather than revised."
        )


def test_every_declared_domain_owns_something_and_says_why() -> None:
    """An unused domain in the policy is a value someone can tag with and nobody maintains."""
    used = set(DOMAIN_OF.values())
    unused = sorted({domain.key for domain in DOMAINS} - used)
    assert not unused, (
        f"domains declared but owning no asset: {unused}. They would still be accepted as tag "
        "values, so the policy would permit an assignment the register does not describe."
    )
    for domain in DOMAINS:
        assert len(domain.rationale) > 40, f"{domain.key} has no reviewable rationale"
        assert domain.owner, f"{domain.key} names no owner"
        if domain.key not in {"commercial_core", "platform_operations"}:
            assert domain.decisions, (
                f"{domain.key} is a business domain that names no decision from the North Star"
            )


def test_metric_views_are_themselves_registered_assets() -> None:
    """A metric view is a gold object, so GOV-003 applies to it too.

    Easy to miss: `information_schema.tables` lists a metric view with `table_type = METRIC_VIEW`,
    so a GOV-003 scan over gold picks it up and fails on it unless the register knows about it.
    """
    missing = sorted({view.name for view in METRIC_VIEWS} - set(DOMAIN_OF))
    assert not missing, f"metric views with no domain assignment: {missing}"
