"""ING-003 and ING-004 — schema drift survives ingestion and nothing is silently dropped.

## What these tests are, and what they deliberately are not

They are **regression guards on a measurement**, in the same shape as `test_skew_lab.py`: each one
re-derives a number that `drift-findings.md` depends on, so if the platform or the data changes
underneath the document, a test goes red rather than the document quietly becoming false.

They do **not** re-run the staged ingestion. That would mean deleting the landing volume and the
schema location, re-uploading 400 files in four tranches, and driving four pipeline updates — and
the run they are guarding needed eight updates to get four, because six failed or stalled on Free
Edition capacity (`drift-findings.md` F-D2). A test that expensive and that flaky would be disabled
within a month, and a disabled test protects nothing.

The cost of that choice, stated so it is not discovered later: **these tests would pass against a
correctly-drifted table produced by any means.** They assert the end state is right, not that it
was reached by arrival-ordered ingestion. The staging is what `drift-findings.md` documents and
what the reproduction steps there re-create. If the bronze table is rebuilt by a backfill, F-D1's
condition returns — one schema version, zero rescued rows — and
`test_incompatible_field_lands_in_rescued_data` fails, which is the outcome that matters.

Requires an authenticated workspace. Every query reads at most 200,000 rows and returns one row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from databricks.sdk import WorkspaceClient

from retail_lakehouse.perf import warehouse

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = REPO_ROOT / "data" / "generated" / "stress"

TABLE = "dng_dev.bronze.basket_line_events_raw"
SCHEMAS = "/Volumes/dng_dev/bronze/landing/_schema/_schemas"

# From data/generated/stress/_manifest.json. Restated here rather than read from the volume,
# because the manifest lives in the landing zone the staged run deletes — a fixture that the
# experiment erases cannot be the experiment's own oracle.
TOTAL_EVENTS = 200_000
RETYPE_FROM_EVENT = 150_000
EVENTS_PER_FILE = 500

# Drift columns, in the order they enter the stream.
ORIGINAL_COLUMN = "trans_time"
ADDED_COLUMN = "loyalty_tier"
RENAMED_TO = "transaction_time"


@pytest.fixture(scope="module")
def session() -> tuple[object, str]:
    client = warehouse.workspace()
    warehouse_id, _ = warehouse.resolve_warehouse(client)
    return client, warehouse_id


@pytest.fixture(scope="module")
def columns(session: tuple[object, str]) -> set[str]:
    client, warehouse_id = session
    rows = warehouse.execute(
        client,
        warehouse_id,
        "SELECT column_name FROM dng_dev.information_schema.columns "
        "WHERE table_schema = 'bronze' AND table_name = 'basket_line_events_raw'",
        bust_cache=False,
        label="drift",
    ).rows
    return {row[0] for row in rows}


def schema_versions() -> list[str]:
    """Versions Auto Loader has written under `cloudFiles.schemaLocation`.

    More than one means the schema changed *after* ingestion began, which is the thing a backfill
    cannot produce: a directory-sampled inference sees the post-drift shape first and never
    evolves.
    """
    client = WorkspaceClient(profile="dng")
    return sorted(entry.name for entry in client.files.list_directory_contents(SCHEMAS))


# ---------------------------------------------------------------------------------------------
# ING-003
# ---------------------------------------------------------------------------------------------
def test_additive_column_survives(session: tuple[object, str], columns: set[str]) -> None:
    """An added column appears, earlier rows are null for it, and the pipeline completed.

    The null check is the half that is easy to omit and is the reason the requirement says
    "pre-drift rows null for it": a column that is fully populated would mean the whole table was
    re-read under the new schema, which is a backfill wearing an evolution's clothes.
    """
    versions = schema_versions()
    assert len(versions) > 1, (
        f"Auto Loader has written {versions} under {SCHEMAS}. A single version means the schema "
        "never evolved — the state Finding B1 describes, where a backfill let the post-drift shape "
        "become the initial inference and nothing ever drifted."
    )

    assert ADDED_COLUMN in columns, (
        f"{ADDED_COLUMN} is not a column of {TABLE}, so additive drift did not reach the table"
    )

    client, warehouse_id = session
    populated, empty = (
        int(value or 0)
        for value in warehouse.execute(
            client,
            warehouse_id,
            f"SELECT sum(CASE WHEN {ADDED_COLUMN} IS NOT NULL THEN 1 ELSE 0 END), "
            f"       sum(CASE WHEN {ADDED_COLUMN} IS NULL THEN 1 ELSE 0 END) FROM {TABLE}",
            bust_cache=False,
            label="drift",
        ).rows[0]
    )
    assert populated > 0, f"{ADDED_COLUMN} exists but is null on every row"
    assert empty > 0, (
        f"{ADDED_COLUMN} is populated on all {populated} rows. Rows ingested before the column "
        "appeared must be null for it; a fully-populated column means the table was re-read under "
        "the new schema rather than evolved."
    )


def test_rename_is_survived_but_not_signalled(
    session: tuple[object, str], columns: set[str]
) -> None:
    """The dangerous case, asserted as the hazard it is rather than as a pass.

    A rename is one column ending and another beginning. Auto Loader evolves for the new name and
    has nothing to complain about for the old one, so both columns exist, both are non-null
    somewhere, and *nothing anywhere reports a problem*. A downstream query keyed on the old name
    keeps returning rows and silently stops covering the recent stream — the failure
    `silver-findings.md` records halving a dashboard with no error.

    This test exists so that behaviour is pinned rather than rediscovered.
    """
    assert {ORIGINAL_COLUMN, RENAMED_TO} <= columns, (
        f"expected both {ORIGINAL_COLUMN} and {RENAMED_TO} to survive the rename; columns has "
        f"{sorted(columns & {ORIGINAL_COLUMN, RENAMED_TO})}"
    )

    client, warehouse_id = session
    old, new = (
        int(value or 0)
        for value in warehouse.execute(
            client,
            warehouse_id,
            f"SELECT sum(CASE WHEN {ORIGINAL_COLUMN} IS NOT NULL THEN 1 ELSE 0 END), "
            f"       sum(CASE WHEN {RENAMED_TO} IS NOT NULL THEN 1 ELSE 0 END) FROM {TABLE}",
            bust_cache=False,
            label="drift",
        ).rows[0]
    )
    assert old > 0 and new > 0, (
        f"{ORIGINAL_COLUMN} non-null on {old} rows, {RENAMED_TO} on {new}. Both must be populated "
        "somewhere, or the rename did not arrive mid-stream and this test is watching the wrong "
        "table state."
    )
    assert old + new == TOTAL_EVENTS, (
        f"{old} + {new} = {old + new}, expected {TOTAL_EVENTS}. Every row carries the timestamp "
        "under exactly one of the two names; anything else means rows were lost or duplicated "
        "across the rename."
    )


# ---------------------------------------------------------------------------------------------
# ING-004
# ---------------------------------------------------------------------------------------------
def expected_rescued_rows() -> int:
    """Count the retyped events in the source files on disk.

    Derived from the input rather than pinned as a literal. The staged run measured 49,468 rescued
    rows against a 50,000-event tranche, and a 532-row shortfall is exactly the kind of nearly-right
    number this project has been wrong about before — it is the late-arrival buffer pushing
    pre-drift events into later files, not a rescue failure. Recomputing it here means the test
    fails if the generator's config changes rather than encoding one run's answer as law.
    """
    retyped = 0
    for path in sorted(SOURCE_FILES.glob("events-*.json")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            if isinstance(json.loads(line).get("quantity"), str):
                retyped += 1
    return retyped


def test_incompatible_field_lands_in_rescued_data(session: tuple[object, str]) -> None:
    """A type-incompatible field is captured in `_rescued_data`, and no row is lost.

    ING-004 has two halves and the second is the one that makes it a data-loss test rather than a
    feature test: the total row count must be preserved. A pipeline that quarantined the bad rows
    would satisfy "rescued data is populated" and fail the requirement.
    """
    client, warehouse_id = session
    rows, rescued = (
        int(value or 0)
        for value in warehouse.execute(
            client,
            warehouse_id,
            "SELECT count(*), sum(CASE WHEN _rescued_data IS NOT NULL THEN 1 ELSE 0 END) "
            f"FROM {TABLE}",
            bust_cache=False,
            label="drift",
        ).rows[0]
    )

    assert rows == TOTAL_EVENTS, (
        f"{TABLE} holds {rows:,} rows, expected {TOTAL_EVENTS:,}. ING-004 requires the row count to "
        "be preserved through the retype; a shortfall is silent data loss."
    )

    expected = expected_rescued_rows()
    assert expected > 0, (
        "no event in data/generated/stress/ carries a string `quantity`, so the retype never fired "
        "in the source and a zero rescued count below would prove nothing about the pipeline"
    )
    assert rescued == expected, (
        f"{rescued:,} rows carry _rescued_data; the source files contain {expected:,} events with "
        "a retyped `quantity`. These must match exactly — a smaller number means fields were "
        "dropped instead of rescued, a larger one means something other than the retype is being "
        "rescued and the finding attributes it to the wrong cause."
    )


def test_rescued_payload_names_the_offending_field_and_file(session: tuple[object, str]) -> None:
    """`_rescued_data` has to be diagnostic, not merely non-null.

    A rescued column that says "something did not fit" is a smoke alarm with no address. The
    requirement's value is that drift becomes *observable*, which means the payload must identify
    the field and the file it arrived in.
    """
    client, warehouse_id = session
    payload = warehouse.execute(
        client,
        warehouse_id,
        f"SELECT _rescued_data FROM {TABLE} WHERE _rescued_data IS NOT NULL LIMIT 1",
        bust_cache=False,
        label="drift",
    ).rows[0][0]

    rescued = json.loads(payload)
    assert "quantity" in rescued, (
        f"the rescued payload does not name the retyped column: {payload[:200]}"
    )
    assert isinstance(rescued["quantity"], str), (
        "the rescued value is not the string form the generator produced; the retype under test is "
        f"not the drift being captured: {payload[:200]}"
    )
    assert "_file_path" in rescued, (
        "the rescued payload carries no source file, so a diagnosis cannot get from the symptom to "
        f"the input that caused it: {payload[:200]}"
    )
    assert rescued["_file_path"].endswith(".json")

    file_index = int(rescued["_file_path"].rsplit("-", 1)[1].removesuffix(".json"))
    assert file_index >= RETYPE_FROM_EVENT // EVENTS_PER_FILE, (
        f"rescued row came from file {file_index}, which precedes the retype point at event "
        f"{RETYPE_FROM_EVENT:,}. Something other than the staged drift is being rescued."
    )
