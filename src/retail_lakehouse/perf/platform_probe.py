"""What the platform will and will not let you do — verified, not read off a docs page.

Every claim in the report's constraints section comes from here, with the error string the
platform actually returned. The list of confs is the one Databricks documents as settable on
serverless compute; the point of the probe is that a serverless *SQL warehouse* is a different
surface from serverless *notebook* compute and the documented list does not transfer.
"""

from __future__ import annotations

from dataclasses import dataclass

from databricks.sdk import WorkspaceClient

from retail_lakehouse.perf import warehouse

# The six confs Databricks documents as settable on serverless compute, plus the AQE knobs a
# skew experiment would reach for first.
CONF_PROBES: dict[str, str] = {
    "spark.databricks.execution.timeout": "SET spark.databricks.execution.timeout = 3600",
    "spark.sql.legacy.timeParserPolicy": "SET spark.sql.legacy.timeParserPolicy = LEGACY",
    "spark.sql.session.timeZone": "SET spark.sql.session.timeZone = 'UTC'",
    "spark.sql.shuffle.partitions": "SET spark.sql.shuffle.partitions = 200",
    "spark.sql.ansi.enabled": "SET spark.sql.ansi.enabled = true",
    "spark.sql.files.maxPartitionBytes": "SET spark.sql.files.maxPartitionBytes = 33554432",
    "spark.sql.adaptive.enabled": "SET spark.sql.adaptive.enabled = false",
    "spark.sql.adaptive.skewJoin.enabled": "SET spark.sql.adaptive.skewJoin.enabled = true",
    "spark.sql.adaptive.skewJoin.skewedPartitionFactor": (
        "SET spark.sql.adaptive.skewJoin.skewedPartitionFactor = 2"
    ),
    "spark.sql.adaptive.skewJoin.skewedPartitionThresholdInBytes": (
        "SET spark.sql.adaptive.skewJoin.skewedPartitionThresholdInBytes = 8388608"
    ),
    "spark.sql.autoBroadcastJoinThreshold": "SET spark.sql.autoBroadcastJoinThreshold = -1",
    "use_cached_result": "SET use_cached_result = false",
}

# Caching APIs the Databricks docs say are unsupported on serverless. Confirmed here so the
# report can say what the failure actually looks like.
CACHE_PROBES: dict[str, str] = {
    "CACHE TABLE": "CACHE TABLE dng_dev.perf.transactions",
    "CACHE SELECT": "CACHE SELECT * FROM dng_dev.perf.transactions",
    "UNCACHE TABLE": "UNCACHE TABLE dng_dev.perf.transactions",
}


@dataclass(frozen=True)
class ProbeResult:
    name: str
    statement: str
    succeeded: bool
    message: str


def run_probes(client: WorkspaceClient, warehouse_id: str) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for name, statement in {**CONF_PROBES, **CACHE_PROBES}.items():
        ok, message = warehouse.try_execute(client, warehouse_id, statement, label="capability")
        results.append(
            ProbeResult(
                name=name,
                statement=statement,
                succeeded=ok,
                message=message.strip().replace("\n", " ")[:220],
            )
        )
    return results


def session_persists(client: WorkspaceClient, warehouse_id: str) -> tuple[str, str]:
    """Set a session parameter, then read it back in a *separate* statement.

    If the Statement Execution API kept a session between calls, this would return the value
    that was set. It does not, and that single fact removes every session-scoped lever from
    the lab.
    """
    warehouse.execute(
        client, warehouse_id, "SET use_cached_result = false", bust_cache=False, label="probe"
    )
    read_back = warehouse.execute(
        client, warehouse_id, "SET use_cached_result", bust_cache=False, label="probe"
    )
    row = read_back.rows[0] if read_back.rows else ["use_cached_result", "?"]
    return "false", str(row[1])
