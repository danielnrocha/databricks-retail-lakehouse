#!/usr/bin/env python3
"""Publish the domain and KPI registers into Unity Catalog — GOV-003 and MOD-005.

Idempotent. Creates or updates the governed `dng_domain` tag policy from `DOMAINS`, assigns exactly
one domain to every gold asset from `DOMAIN_OF`, and creates one metric view per entry in
`METRIC_VIEWS` carrying the measures registered against it.

    python3 scripts/publish_governance.py --catalog dng_dev
    python3 scripts/publish_governance.py --catalog dng_dev --check

`--check` mutates nothing and exits non-zero on any gap, so it is usable as a gate.

## Why the tag policy is created here rather than by hand

A tag with no policy behind it is accepted by Unity Catalog with any value at all — measured:
`ALTER TABLE ... SET TAGS ('made_up_key' = 'anything')` succeeds. A *policy-backed* key rejects a
value outside its allowed list:

    [INVALID_PARAMETER_VALUE] Tag value not_a_real_domain is not an allowed value for tag policy
    key dng_domain. Allowed values: [customer_marketing, trade_promotions, store_operations]

So the policy is the entire difference between a domain and a free-text label, and it is exactly
the part that was previously created by hand and therefore existed nowhere in the repository. This
script is the fix for that finding, not an optional convenience.

## Why metric views are published here rather than by the pipeline

They are catalog objects over gold, not a flow in the medallion graph. Building them in the
pipeline would put a governance concern inside a data path where Free Edition already forces
bronze, silver and gold to share one graph (ADR-0004) — one more thing whose failure stops
ingestion.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState
from databricks.sdk.service.tags import TagPolicy, Value

from governance_register import (
    DOMAIN_OF,
    DOMAIN_TAG_KEY,
    DOMAINS,
    GOLD_SCHEMA,
    METRIC_VIEWS,
    kpis_for,
)

WAREHOUSE_NAME = "Serverless Starter Warehouse"


@dataclass(frozen=True)
class Gap:
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


def _warehouse(client: WorkspaceClient) -> str:
    for warehouse in client.warehouses.list():
        if warehouse.name == WAREHOUSE_NAME and warehouse.id:
            return warehouse.id
    raise SystemExit(f"no warehouse named {WAREHOUSE_NAME!r}")


def _run(client: WorkspaceClient, warehouse: str, sql: str) -> tuple[bool, str]:
    response = client.statement_execution.execute_statement(
        statement=sql, warehouse_id=warehouse, wait_timeout="50s"
    )
    status = response.status
    if status and status.state == StatementState.SUCCEEDED:
        return True, "SUCCEEDED"
    message = status.error.message if status and status.error else str(status)
    return False, message or "unknown error"


def _rows(client: WorkspaceClient, warehouse: str, sql: str) -> list[list[str | None]]:
    response = client.statement_execution.execute_statement(
        statement=sql, warehouse_id=warehouse, wait_timeout="50s"
    )
    status = response.status
    if not status or status.state != StatementState.SUCCEEDED:
        message = status.error.message if status and status.error else str(status)
        raise SystemExit(f"query failed: {message}\n{sql}")
    result = response.result
    return [list(row) for row in (result.data_array or [])] if result else []


def _escape(text: str) -> str:
    """Single quotes in generated DDL are not escaped by any layer below this one.

    Three separate places in this repository have produced PARSE_SYNTAX_ERROR from an apostrophe in
    prose. Every literal goes through here.
    """
    return text.replace("'", "''")


# ---------------------------------------------------------------------------------------------
# GOV-003
# ---------------------------------------------------------------------------------------------
def sync_tag_policy(client: WorkspaceClient, *, check: bool) -> list[Gap]:
    """Make the governed tag policy match `DOMAINS` exactly."""
    wanted = sorted(domain.key for domain in DOMAINS)
    existing = next(
        (p for p in client.tag_policies.list_tag_policies() if p.tag_key == DOMAIN_TAG_KEY), None
    )

    policy = TagPolicy(
        tag_key=DOMAIN_TAG_KEY,
        description="Business domain assignment for gold assets (GOV-003).",
        values=[Value(name=key) for key in wanted],
    )

    if existing is None:
        if check:
            return [Gap("tag-policy-missing", f"{DOMAIN_TAG_KEY} does not exist")]
        client.tag_policies.create_tag_policy(tag_policy=policy)
        print(f"created tag policy {DOMAIN_TAG_KEY} with {len(wanted)} values")
        return []

    have = sorted(value.name for value in (existing.values or []) if value.name)
    if have == wanted:
        return []
    if check:
        return [
            Gap(
                "tag-policy-drift",
                f"{DOMAIN_TAG_KEY} allows {have}, register declares {wanted}",
            )
        ]
    client.tag_policies.update_tag_policy(
        tag_key=DOMAIN_TAG_KEY, tag_policy=policy, update_mask="values,description"
    )
    print(f"updated tag policy {DOMAIN_TAG_KEY}: {have} -> {wanted}")
    return []


def gold_assets(client: WorkspaceClient, warehouse: str, catalog: str) -> list[str]:
    """Every gold object a domain must be assigned to.

    `__materialization%` is Lakeflow's internal backing storage for a materialized view. Excluded
    for the same reason GOV-001 excludes it: it is an implementation detail of an asset already in
    the list, and tagging it would double-count every materialized view.
    """
    return [
        str(row[0])
        for row in _rows(
            client,
            warehouse,
            "SELECT table_name FROM system.information_schema.tables "
            f"WHERE table_catalog = '{catalog}' AND table_schema = '{GOLD_SCHEMA}' "
            "AND table_name NOT LIKE '__materialization%' ORDER BY table_name",
        )
    ]


def assigned_domains(client: WorkspaceClient, warehouse: str, catalog: str) -> dict[str, list[str]]:
    """Asset -> the domain values currently tagged on it. A list, so 'exactly one' is checkable."""
    assignments: dict[str, list[str]] = {}
    for row in _rows(
        client,
        warehouse,
        "SELECT table_name, tag_value FROM system.information_schema.table_tags "
        f"WHERE catalog_name = '{catalog}' AND schema_name = '{GOLD_SCHEMA}' "
        f"AND tag_name = '{DOMAIN_TAG_KEY}'",
    ):
        assignments.setdefault(str(row[0]), []).append(str(row[1]))
    return assignments


def sync_domains(
    client: WorkspaceClient, warehouse: str, catalog: str, *, check: bool
) -> list[Gap]:
    gaps: list[Gap] = []
    assets = gold_assets(client, warehouse, catalog)

    unregistered = [asset for asset in assets if asset not in DOMAIN_OF]
    gaps += [
        Gap("asset-not-in-register", f"{asset} exists in gold but no domain is declared for it")
        for asset in unregistered
    ]

    missing_assets = [name for name in DOMAIN_OF if name not in assets]
    gaps += [
        Gap("register-names-missing-asset", f"{name} is in the register but not in {catalog}.gold")
        for name in missing_assets
    ]

    current = assigned_domains(client, warehouse, catalog)
    for asset in assets:
        wanted = DOMAIN_OF.get(asset)
        if wanted is None:
            continue
        have = current.get(asset, [])
        if have == [wanted]:
            continue
        if check:
            gaps.append(Gap("domain-mismatch", f"{asset} tagged {have}, register says [{wanted}]"))
            continue
        ok, message = _run(
            client,
            warehouse,
            f"ALTER TABLE {catalog}.{GOLD_SCHEMA}.{asset} "
            f"SET TAGS ('{DOMAIN_TAG_KEY}' = '{_escape(wanted)}')",
        )
        print(f"  {'ok  ' if ok else 'FAIL'} {asset} -> {wanted}" + ("" if ok else f"  {message}"))
        if not ok:
            gaps.append(Gap("domain-assign-failed", f"{asset}: {message}"))
    return gaps


# ---------------------------------------------------------------------------------------------
# MOD-005
# ---------------------------------------------------------------------------------------------
def metric_view_ddl(view_name: str, catalog: str) -> str:
    view = next(v for v in METRIC_VIEWS if v.name == view_name)
    lines = [
        f"CREATE OR REPLACE VIEW {catalog}.{GOLD_SCHEMA}.{view.name}",
        f"COMMENT '{_escape(view.comment)}'",
        "WITH METRICS",
        "LANGUAGE YAML",
        "AS $$",
        "version: 0.1",
        f"source: {catalog}.{GOLD_SCHEMA}.{view.source}",
        "dimensions:",
    ]
    for name, expr in view.dimensions:
        lines += [f"  - name: {name}", f"    expr: {expr}"]
    lines.append("measures:")
    for kpi in kpis_for(view.name):
        lines += [f"  - name: {kpi.measure}", f"    expr: {kpi.expr}"]
    lines.append("$$")
    return "\n".join(lines)


def published_metric_views(client: WorkspaceClient, warehouse: str, catalog: str) -> set[str]:
    return {
        str(row[0])
        for row in _rows(
            client,
            warehouse,
            "SELECT table_name FROM system.information_schema.tables "
            f"WHERE table_catalog = '{catalog}' AND table_schema = '{GOLD_SCHEMA}' "
            "AND table_type = 'METRIC_VIEW'",
        )
    }


def sync_metric_views(
    client: WorkspaceClient, warehouse: str, catalog: str, *, check: bool
) -> list[Gap]:
    gaps: list[Gap] = []
    published = published_metric_views(client, warehouse, catalog)

    for view in METRIC_VIEWS:
        if check:
            if view.name not in published:
                gaps.append(Gap("metric-view-missing", f"{view.name} is not published"))
            continue
        ok, message = _run(client, warehouse, metric_view_ddl(view.name, catalog))
        measures = len(kpis_for(view.name))
        print(
            f"  {'ok  ' if ok else 'FAIL'} {view.name} ({measures} measures)"
            + ("" if ok else f"  {message}")
        )
        if not ok:
            gaps.append(Gap("metric-view-failed", f"{view.name}: {message}"))

    stray = published - {view.name for view in METRIC_VIEWS}
    gaps += [
        Gap("unregistered-metric-view", f"{name} is published but not in the register")
        for name in sorted(stray)
    ]
    return gaps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report gaps without changing anything; exits non-zero if any exist",
    )
    args = parser.parse_args()

    client = WorkspaceClient()
    warehouse = _warehouse(client)

    print(f"{'Checking' if args.check else 'Publishing'} governance for {args.catalog}\n")

    print("tag policy")
    gaps = sync_tag_policy(client, check=args.check)

    print("\nmetric views")
    # Metric views before domains: a view that does not exist yet cannot be tagged, and DOMAIN_OF
    # names them. Publishing in the other order makes the first run of a clean catalog report two
    # spurious gaps.
    gaps += sync_metric_views(client, warehouse, args.catalog, check=args.check)

    print("\ndomains")
    gaps += sync_domains(client, warehouse, args.catalog, check=args.check)

    if gaps:
        print(f"\n{len(gaps)} gap(s):", file=sys.stderr)
        for gap in gaps:
            print(f"  - {gap}", file=sys.stderr)
        return 1

    print(f"\nClean — {len(DOMAIN_OF)} assets, {len(METRIC_VIEWS)} metric views.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
