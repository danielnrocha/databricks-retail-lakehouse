"""Publish the reviewed ruleset to `<catalog>.ops.dq_rules` — Layer B's storage.

Why the rules live in a Unity Catalog table rather than in the pipeline source
---------------------------------------------------------------------------
Lakeflow's January 2026 release notes announce that expectations can be stored and managed in
Unity Catalog tables, "version-controlled, auditable quality rules that can be shared across
multiple pipelines". As of writing there is **no API, SQL clause or documented table schema** for
that anywhere in the product documentation — only the release-note sentence. What *is* documented
is the reusable-expectations pattern: put the rules in a table you own, read them at graph
construction, and pass them to `dp.expect_all(...)`. That is what this module implements, and it
is described as what it is rather than as a native feature.

Independent of the feature question, the table is the right interface here for a mechanical
reason. Lakeflow executes each pipeline source file on its own; importing a sibling module is not
reliable across runtimes, so the pipeline cannot simply `import rules`. A governed table decouples
the two and buys grants, history and cross-pipeline sharing at the same time.

The table is `REPLACE`d on every publish, so Delta history is the version log: `DESCRIBE HISTORY`
answers "which rules were in force on the 3rd?", and every quarantine row carries the
`ruleset_version` that rejected it.

Usage::

    PYTHONPATH=src python -m retail_lakehouse.quality.publish --catalog <catalog> --warehouse <id>

The catalog is deliberately spelled `<catalog>` even in this example. ENV-001 is enforced by a
static scan of `src/`, and a scan cannot tell a docstring from an assignment — which is the right
behaviour, because neither can a hurried copy-paste.
"""

from __future__ import annotations

import argparse
import sys
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

from retail_lakehouse.quality.rules import REVIEWED_RULES, RULESET_VERSION, QualityRule

TABLE = "ops.dq_rules"

DDL = """
CREATE TABLE IF NOT EXISTS {catalog}.{table} (
    rule_name       STRING  NOT NULL COMMENT 'Stable identifier. Appears verbatim on quarantine rows.',
    dataset         STRING  NOT NULL COMMENT 'Fully qualified silver dataset, schema-qualified.',
    expression      STRING  NOT NULL COMMENT 'SQL predicate. TRUE means the row is acceptable.',
    severity        STRING  NOT NULL COMMENT 'error routes the row to quarantine; warn records it only.',
    tag             STRING  NOT NULL COMMENT 'Quality dimension: completeness, validity, consistency.',
    origin          STRING  NOT NULL COMMENT 'dqx_profiler_candidate or domain_knowledge.',
    review          STRING  NOT NULL COMMENT 'accepted, amended or authored. QLT-005 audit trail.',
    rationale       STRING  NOT NULL COMMENT 'Why this rule exists, in business terms.',
    ruleset_version STRING  NOT NULL COMMENT 'Version of the ruleset this row belongs to.',
    published_at    TIMESTAMP NOT NULL
)
COMMENT 'Governed data quality rules for the silver layer (QLT-001). Written by retail_lakehouse.quality.publish from the reviewed ruleset in git; read by the silver pipeline at graph construction. Delta history is the version log.'
"""


def _sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def insert_statement(catalog: str, rules: tuple[QualityRule, ...]) -> str:
    columns = (
        "rule_name, dataset, expression, severity, tag, origin, review, rationale, "
        "ruleset_version, published_at"
    )
    values = ",\n    ".join(
        "("
        + ", ".join(
            [
                _sql_literal(rule.name),
                _sql_literal(rule.dataset),
                _sql_literal(rule.expression),
                _sql_literal(rule.severity),
                _sql_literal(rule.tag),
                _sql_literal(rule.origin),
                _sql_literal(rule.review),
                _sql_literal(rule.rationale),
                _sql_literal(RULESET_VERSION),
                "current_timestamp()",
            ]
        )
        + ")"
        for rule in rules
    )
    return f"INSERT OVERWRITE {catalog}.{TABLE} ({columns}) VALUES\n    {values}"


def run(client: WorkspaceClient, warehouse_id: str, statement: str) -> None:
    result = client.statement_execution.execute_statement(
        statement=statement, warehouse_id=warehouse_id, wait_timeout="50s"
    )
    while result.status and result.status.state in (
        StatementState.PENDING,
        StatementState.RUNNING,
    ):
        time.sleep(2)
        result = client.statement_execution.get_statement(result.statement_id)
    if result.status and result.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"{result.status.state}: {result.status.error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Required, with no default. The catalog reaches this command from the bundle target the same
    # way it reaches the pipeline (ADR-0002); a default would be the first step towards a dev run
    # writing to prod.
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--warehouse", required=True, help="SQL warehouse id.")
    parser.add_argument("--profile", default=None, help="Databricks CLI profile.")
    args = parser.parse_args()

    client = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()

    run(client, args.warehouse, DDL.format(catalog=args.catalog, table=TABLE))
    run(client, args.warehouse, insert_statement(args.catalog, REVIEWED_RULES))

    by_severity: dict[str, int] = {}
    for rule in REVIEWED_RULES:
        by_severity[rule.severity] = by_severity.get(rule.severity, 0) + 1
    print(
        f"published {len(REVIEWED_RULES)} rules (v{RULESET_VERSION}) to "
        f"{args.catalog}.{TABLE}: {by_severity}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
