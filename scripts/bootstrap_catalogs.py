#!/usr/bin/env python3
"""Create the three environment catalogs, their schemas, and the landing volumes.

Idempotent by construction (`IF NOT EXISTS` throughout), so it is safe to re-run and safe to run
from CI. Bootstrap scripts that can only be run once are how environments drift: the second person
to join the project runs a slightly different sequence by hand.

Why SQL rather than the Unity Catalog REST API
-----------------------------------------------
`POST /api/2.1/unity-catalog/catalogs` (and `databricks catalogs create`) fails on an account with
Default Storage enabled:

    Metastore storage root URL does not exist. Default Storage is enabled in your account.

The REST path requires an explicit `storage_root`, which a Free Edition account does not have and
cannot create. The SQL DDL path resolves Default Storage automatically. This is not documented
prominently and costs an afternoon if you assume the API and the SQL surface are equivalent.

Usage:
    python3 scripts/bootstrap_catalogs.py                 # all environments
    python3 scripts/bootstrap_catalogs.py --env dev
    python3 scripts/bootstrap_catalogs.py --dry-run
    python3 scripts/bootstrap_catalogs.py --drop dng_probe # explicit, single, named
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

# Environments are catalogs, not workspaces — Free Edition allows one metastore.
# See docs/adr/ADR-0002-environment-isolation.md for why this is a legitimate design and not
# merely a workaround.
ENVIRONMENTS = ("dev", "test", "prod")
CATALOG_PREFIX = "dng"

SCHEMAS: dict[str, str] = {
    "bronze": "Raw ingest. Append-only, source-faithful, no business logic. Never queried by BI.",
    "silver": "Conformed entities. Quality-enforced, deduplicated, SCD2 where history matters.",
    "gold": "Dimensional model and governed KPIs. The only layer consumers should read.",
    "ops": (
        "Platform observability: data-quality metrics, quarantine, pipeline events, "
        "join coverage, streaming/batch reconciliation. Per-environment on purpose — a shared "
        "ops schema lets dev runs pollute prod dashboards."
    ),
}

VOLUMES: dict[str, tuple[str, str]] = {
    # volume name -> (schema, comment)
    "landing": (
        "bronze",
        "Event landing zone consumed by Auto Loader. Written by the synthetic amplifier.",
    ),
    "checkpoints": (
        "ops",
        "Structured Streaming checkpoints. Deleting one silently reprocesses from scratch.",
    ),
    "seed": (
        "bronze",
        "dunnhumby seed CSVs (CC BY 4.0). Loaded once; the source of every distribution.",
    ),
}


@dataclass
class Runner:
    client: WorkspaceClient
    warehouse_id: str
    dry_run: bool = False

    def sql(self, statement: str) -> None:
        if self.dry_run:
            print(f"  [dry-run] {statement}")
            return
        result = self.client.statement_execution.execute_statement(
            warehouse_id=self.warehouse_id,
            statement=statement,
            wait_timeout="50s",
        )
        state = result.status.state if result.status else None
        if state != StatementState.SUCCEEDED:
            message = result.status.error.message if result.status and result.status.error else "?"
            raise RuntimeError(f"FAILED: {statement}\n  {message}")
        print(f"  ok  {statement.splitlines()[0][:96]}")


def resolve_warehouse(client: WorkspaceClient) -> str:
    """Pick the first available SQL warehouse.

    Free Edition allows exactly one, so 'first' is unambiguous here. On a paid tier this would be
    a required parameter rather than a guess — silently picking a warehouse is how a bootstrap
    ends up billed to the wrong team.
    """
    warehouses = list(client.warehouses.list())
    if not warehouses:
        raise RuntimeError("No SQL warehouse available. Start one in the workspace UI first.")
    if len(warehouses) > 1:
        names = ", ".join(w.name or "?" for w in warehouses)
        print(f"WARNING: {len(warehouses)} warehouses found ({names}); using the first.")
    chosen = warehouses[0]
    print(f"warehouse: {chosen.name} ({chosen.cluster_size}, {chosen.state})")
    assert chosen.id is not None
    return chosen.id


def bootstrap_environment(runner: Runner, env: str) -> None:
    catalog = f"{CATALOG_PREFIX}_{env}"
    print(f"\n=== {catalog} ===")

    runner.sql(
        f"CREATE CATALOG IF NOT EXISTS {catalog} "
        f"COMMENT 'Retail lakehouse — {env} environment. Managed by scripts/bootstrap_catalogs.py; "
        f"do not create objects here by hand.'"
    )

    for schema, comment in SCHEMAS.items():
        runner.sql(
            f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema} COMMENT '{comment.replace(chr(39), '')}'"
        )

    for volume, (schema, comment) in VOLUMES.items():
        runner.sql(
            f"CREATE VOLUME IF NOT EXISTS {catalog}.{schema}.{volume} "
            f"COMMENT '{comment.replace(chr(39), '')}'"
        )

    # Tags make the environment queryable from system tables, which is what makes a
    # "which objects belong to prod?" question answerable without a naming convention.
    runner.sql(
        f"ALTER CATALOG {catalog} SET TAGS ('environment' = '{env}', 'project' = 'dng-retail')"
    )


def verify(client: WorkspaceClient) -> int:
    print("\n=== verification ===")
    existing = {c.name for c in client.catalogs.list()}
    problems = 0
    for env in ENVIRONMENTS:
        catalog = f"{CATALOG_PREFIX}_{env}"
        if catalog not in existing:
            print(f"  MISSING catalog {catalog}")
            problems += 1
            continue
        schemas = {s.name for s in client.schemas.list(catalog_name=catalog)}
        missing = set(SCHEMAS) - schemas
        if missing:
            print(f"  {catalog}: missing schemas {sorted(missing)}")
            problems += 1
        else:
            print(f"  {catalog}: {len(SCHEMAS)} schemas ok")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=ENVIRONMENTS, help="bootstrap a single environment")
    parser.add_argument("--dry-run", action="store_true", help="print statements, execute nothing")
    parser.add_argument("--drop", metavar="CATALOG", help="drop one named catalog (destructive)")
    args = parser.parse_args()

    client = WorkspaceClient()
    warehouse_id = resolve_warehouse(client)
    runner = Runner(client=client, warehouse_id=warehouse_id, dry_run=args.dry_run)

    if args.drop:
        # Guard rail: refuse to drop anything that is not clearly ours and clearly disposable.
        # A bootstrap script with an unguarded drop is one typo away from deleting prod.
        if not args.drop.startswith(f"{CATALOG_PREFIX}_") or args.drop in {
            f"{CATALOG_PREFIX}_{e}" for e in ENVIRONMENTS
        }:
            print(
                f"Refusing to drop {args.drop!r}: only non-environment "
                f"{CATALOG_PREFIX}_* catalogs may be dropped by this script.",
                file=sys.stderr,
            )
            return 1
        runner.sql(f"DROP CATALOG IF EXISTS {args.drop} CASCADE")
        return 0

    targets = [args.env] if args.env else list(ENVIRONMENTS)
    for env in targets:
        bootstrap_environment(runner, env)

    if args.dry_run:
        return 0
    return 1 if verify(client) else 0


if __name__ == "__main__":
    sys.exit(main())
