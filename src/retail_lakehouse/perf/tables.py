"""Part 1 — lab tables in `dng_dev.perf`.

Loaded unclustered and unoptimised on purpose. A baseline you have already tuned is not a
baseline; every later improvement has to be measurable against the state a table arrives in.
"""

from __future__ import annotations

from dataclasses import dataclass

from databricks.sdk import WorkspaceClient

from retail_lakehouse.perf import warehouse

SCHEMA = "dng_dev.perf"
SEED_VOLUME = "/Volumes/dng_dev/bronze/seed"

TRANSACTIONS = f"{SCHEMA}.transactions"
CAUSAL = f"{SCHEMA}.causal"

CREATE_SCHEMA = (
    "CREATE SCHEMA IF NOT EXISTS dng_dev.perf "
    "COMMENT 'Skew and spill performance lab. Tables here are disposable and rebuildable "
    "from the seed volume; nothing downstream depends on them.'"
)

# CTAS straight off the Parquet seed. No CLUSTER BY, no ZORDER, no OPTIMIZE — see module docstring.
LOAD_STATEMENTS: dict[str, str] = {
    TRANSACTIONS: f"""
        CREATE OR REPLACE TABLE {TRANSACTIONS}
        COMMENT 'dunnhumby transaction_data, 2,595,732 rows. Unclustered baseline.'
        AS SELECT * FROM parquet.`{SEED_VOLUME}/transaction_data.parquet`
    """,
    CAUSAL: f"""
        CREATE OR REPLACE TABLE {CAUSAL}
        COMMENT 'dunnhumby causal_data, 36,786,524 rows. Unclustered baseline; the spill engine.'
        AS SELECT * FROM parquet.`{SEED_VOLUME}/causal_data.parquet`
    """,
}


@dataclass(frozen=True)
class TableFacts:
    """Physical facts about a lab table. Every measurement table in the report cites these."""

    name: str
    rows: int
    num_files: int
    size_bytes: int
    clustering_columns: str

    @property
    def size_mib(self) -> float:
        return self.size_bytes / (1024 * 1024)


def create(client: WorkspaceClient, warehouse_id: str) -> None:
    warehouse.execute(client, warehouse_id, CREATE_SCHEMA, bust_cache=False, label="ddl")
    for name, sql in LOAD_STATEMENTS.items():
        print(f"  loading {name} ...", flush=True)
        warehouse.execute(client, warehouse_id, sql, bust_cache=False, label="load", wait="50s")


def describe(client: WorkspaceClient, warehouse_id: str, table: str) -> TableFacts:
    """Read physical layout from `DESCRIBE DETAIL` plus an exact row count.

    Row count is a separate `count(*)` rather than `numRecords` from the Delta stats, because
    the stats field is absent on tables written without stats collection and a silently missing
    volume figure would undermine every claim built on it.
    """
    # DESCRIBE DETAIL cannot be wrapped in a subquery on Databricks SQL, so its columns are
    # resolved by name from the response manifest rather than by position — the position of
    # `clusteringColumns` has moved between runtime versions.
    detail = warehouse.execute(
        client, warehouse_id, f"DESCRIBE DETAIL {table}", bust_cache=False, label="describe"
    )
    values = detail.as_dicts()[0]
    num_files = values.get("numFiles")
    size_bytes = values.get("sizeInBytes")
    clustering = values.get("clusteringColumns")
    rows = warehouse.scalar(client, warehouse_id, f"SELECT count(*) FROM {table}") or "0"
    return TableFacts(
        name=table,
        rows=int(rows),
        num_files=int(num_files or 0),
        size_bytes=int(size_bytes or 0),
        clustering_columns=clustering if clustering not in ("[]", "null", None) else "none",
    )
