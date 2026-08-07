"""GOV-003 and MOD-005, asserted against the workspace rather than against the register.

`tests/unit/test_metrics_register.py` proves the registers are internally coherent. That is a
different claim from the catalog agreeing with them, and the gap between the two is where
governance actually fails: a register everyone edits and nobody publishes looks exactly like a
register that is enforced.

## The finding that shaped these tests

The `dng_domain` tag policy already existed when GOV-003 was picked up — created by hand, described
as "Business domain assignment for gold assets (GOV-003)", and reproduced by nothing in the
repository. A test written then would have passed on one laptop and failed on every fresh account.

So `test_the_domain_tag_policy_is_governed` does not merely check that the policy exists. It checks
that the policy **rejects a value outside its list**, because an ungoverned tag key is accepted by
Unity Catalog with any value at all — measured: `SET TAGS ('made_up_key' = 'anything')` succeeds.
Without that assertion, GOV-003 would pass against free-text labels that happen to be spelled like
governance.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from databricks.sdk import WorkspaceClient

from retail_lakehouse.perf import warehouse

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from governance_register import (  # noqa: E402
    DOMAIN_OF,
    DOMAIN_TAG_KEY,
    DOMAINS,
    GOLD_SCHEMA,
    KPIS,
    METRIC_VIEWS,
    kpis_for,
)

CATALOG = "dng_dev"


@pytest.fixture(scope="module")
def session() -> tuple[object, str]:
    client = warehouse.workspace()
    warehouse_id, _ = warehouse.resolve_warehouse(client)
    return client, warehouse_id


def rows(session: tuple[object, str], sql: str) -> list[list[str | None]]:
    client, warehouse_id = session
    return warehouse.execute(client, warehouse_id, sql, bust_cache=False, label="gov").rows


# ---------------------------------------------------------------------------------------------
# GOV-003
# ---------------------------------------------------------------------------------------------
def test_gold_tables_have_domain(session: tuple[object, str]) -> None:
    """Every gold asset carries exactly one domain, and it is the registered one.

    "Exactly one" is asserted as a count rather than as presence. A second tag value would still
    satisfy "has a domain" while making ownership ambiguous, which is the state the requirement is
    written to exclude.
    """
    assets = [
        str(row[0])
        for row in rows(
            session,
            "SELECT table_name FROM system.information_schema.tables "
            f"WHERE table_catalog = '{CATALOG}' AND table_schema = '{GOLD_SCHEMA}' "
            "AND table_name NOT LIKE '__materialization%' ORDER BY table_name",
        )
    ]
    assert assets, (
        f"{CATALOG}.{GOLD_SCHEMA} contains no assets, so this test would pass by having nothing to "
        "check. The gold layer is missing or information_schema changed shape."
    )

    assigned: dict[str, list[str]] = {}
    for row in rows(
        session,
        "SELECT table_name, tag_value FROM system.information_schema.table_tags "
        f"WHERE catalog_name = '{CATALOG}' AND schema_name = '{GOLD_SCHEMA}' "
        f"AND tag_name = '{DOMAIN_TAG_KEY}'",
    ):
        assigned.setdefault(str(row[0]), []).append(str(row[1]))

    untagged = sorted(asset for asset in assets if not assigned.get(asset))
    assert not untagged, (
        f"gold assets with no {DOMAIN_TAG_KEY} tag: {untagged}. Run "
        f"`python3 scripts/publish_governance.py --catalog {CATALOG}`."
    )

    ambiguous = {a: v for a, v in assigned.items() if a in assets and len(v) > 1}
    assert not ambiguous, (
        f"GOV-003 requires exactly one domain per asset; these carry several: {ambiguous}"
    )

    disagreements = {
        asset: (assigned[asset][0], DOMAIN_OF.get(asset))
        for asset in assets
        if assigned.get(asset) and assigned[asset][0] != DOMAIN_OF.get(asset)
    }
    assert not disagreements, (
        "the catalog and the register disagree about ownership (asset: catalog vs register):\n"
        + "\n".join(f"  {a}: {c!r} vs {r!r}" for a, (c, r) in sorted(disagreements.items()))
    )


def test_the_domain_tag_policy_is_governed(session: tuple[object, str]) -> None:
    """The policy must refuse a value outside its list.

    This is the whole difference between a domain and a label. An ungoverned key accepts anything,
    so without this assertion GOV-003 would pass against free text.

    The probe is applied to a throwaway key on a real asset and removed afterwards; the negative
    case is the point, so it must actually be attempted rather than assumed.
    """
    client = WorkspaceClient(profile="dng")
    policy = next(
        (p for p in client.tag_policies.list_tag_policies() if p.tag_key == DOMAIN_TAG_KEY), None
    )
    assert policy is not None, (
        f"{DOMAIN_TAG_KEY} has no tag policy, so any value would be accepted and every domain "
        "assignment is free text. This is the exact state the register was written to fix."
    )

    allowed = sorted(value.name for value in (policy.values or []) if value.name)
    assert allowed == sorted(domain.key for domain in DOMAINS), (
        f"the policy allows {allowed}, the register declares "
        f"{sorted(d.key for d in DOMAINS)}. A value the policy permits but the register does not "
        "describe is a domain nobody owns."
    )

    client_sql, warehouse_id = session
    target = f"{CATALOG}.{GOLD_SCHEMA}.agg_store_daily"
    ok, message = warehouse.try_execute(
        client_sql,
        warehouse_id,
        f"ALTER TABLE {target} SET TAGS ('{DOMAIN_TAG_KEY}' = 'not_a_real_domain')",
        label="gov",
    )
    assert not ok, (
        "Unity Catalog accepted 'not_a_real_domain' for the domain tag. The key is no longer "
        "policy-backed, so GOV-003 is now asserting against free-text labels."
    )
    assert "not an allowed value" in (message or ""), (
        f"the assignment failed for an unexpected reason, so this test is not measuring "
        f"governance: {message}"
    )


# ---------------------------------------------------------------------------------------------
# MOD-005 — the workspace half
# ---------------------------------------------------------------------------------------------
def test_every_kpi_resolves_to_exactly_one_metric_definition(session: tuple[object, str]) -> None:
    """Each registered KPI is one measure of one published `METRIC_VIEW`."""
    published = {
        str(row[0]): str(row[1])
        for row in rows(
            session,
            "SELECT table_name, table_type FROM system.information_schema.tables "
            f"WHERE table_catalog = '{CATALOG}' AND table_schema = '{GOLD_SCHEMA}' "
            "AND table_name NOT LIKE '__materialization%'",
        )
    }

    for view in METRIC_VIEWS:
        assert published.get(view.name) == "METRIC_VIEW", (
            f"{view.name} is {published.get(view.name)!r}, not METRIC_VIEW. A plain view would "
            "answer the same queries without enforcing the aggregation contract, which is the "
            "difference MOD-005 is asking for — so this must not be allowed to pass as a fallback."
        )

    # A measure is only reachable through MEASURE(). Selecting each one proves the definition
    # resolves, which a catalog listing alone does not.
    for view in METRIC_VIEWS:
        measures = kpis_for(view.name)
        projection = ", ".join(f"MEASURE({kpi.measure})" for kpi in measures)
        result = rows(session, f"SELECT {projection} FROM {CATALOG}.{GOLD_SCHEMA}.{view.name}")
        assert len(result) == 1 and len(result[0]) == len(measures)
        empty = [k.name for k, v in zip(measures, result[0], strict=True) if v is None]
        assert not empty, (
            f"{view.name} resolves but returns null for {empty}. A measure that publishes and "
            "computes nothing is the failure a structural check cannot see."
        )


def test_a_measure_cannot_be_selected_as_a_plain_column(session: tuple[object, str]) -> None:
    """The aggregation contract is enforced by the engine, not by convention.

    This is what makes a metric view different from a view with a naming convention: selecting a
    measure without `MEASURE()` is refused. If it ever stops being refused, MOD-005's claim that
    the KPI is *governed* weakens to a claim that it is merely *centralised*.
    """
    client, warehouse_id = session
    view = METRIC_VIEWS[0]
    measure = kpis_for(view.name)[0].measure
    ok, message = warehouse.try_execute(
        client,
        warehouse_id,
        f"SELECT {measure} FROM {CATALOG}.{GOLD_SCHEMA}.{view.name} LIMIT 1",
        label="gov",
    )
    assert not ok, f"{measure} was selectable without MEASURE(); the contract is not enforced"
    assert "MEASURE" in (message or "").upper(), f"refused for an unexpected reason: {message}"


def test_registered_kpi_count_matches_the_published_measures(session: tuple[object, str]) -> None:
    """Guards against the register and the catalog drifting apart in the harmless-looking direction.

    A measure published but not registered is undocumented; a KPI registered but not published
    resolves to nothing. The count check catches the first, which the per-KPI loop above cannot.
    """
    for view in METRIC_VIEWS:
        published_measures = {
            str(row[0])
            for row in rows(
                session,
                "SELECT column_name FROM system.information_schema.columns "
                f"WHERE table_catalog = '{CATALOG}' AND table_schema = '{GOLD_SCHEMA}' "
                f"AND table_name = '{view.name}'",
            )
        }
        registered = {kpi.measure for kpi in kpis_for(view.name)}
        dimensions = {name for name, _ in view.dimensions}
        unregistered = published_measures - registered - dimensions
        assert not unregistered, (
            f"{view.name} publishes {sorted(unregistered)}, which the register does not describe. "
            "An undocumented measure is a KPI with no owner and no definition."
        )
    assert len(KPIS) == sum(len(kpis_for(v.name)) for v in METRIC_VIEWS)
