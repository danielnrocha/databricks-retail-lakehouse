# Staged-ingestion drift findings

Finding B1 in [`bronze-findings.md`](bronze-findings.md) recorded that a bulk backfill cannot
demonstrate schema evolution, because Auto Loader infers its initial schema from a **directory
sample** rather than from arrival order — so the post-drift shape becomes the starting schema and
nothing ever evolves. `_rescued_data` stayed at zero across 200,000 events for exactly that reason,
and the pipeline looked correct while proving nothing.

This document records the staged re-run that was supposed to settle ING-003 and ING-004: clear the
landing volume and the schema location, upload only the files preceding the first drift point, run,
then upload the next tranche and run again, so drift arrives in order.

**Result: ING-003 is proven. ING-004 is not.** The experiment was stopped by a platform failure,
not by a result, and that is recorded below rather than smoothed over.

---

## Setup

The generator writes 400 files of 500 events each. `_manifest.json` fixes the drift points by event
index, which maps to file index at 500 events per file:

| drift | from event | first file | effect on the record |
|---|---|---|---|
| add `loyalty_tier` | 50,000 | `events-000100` | a field appears that the schema does not have |
| rename `trans_time` → `transaction_time` | 100,000 | `events-000200` | one field disappears, another appears |
| retype a field | 150,000 | `events-000300` | a value no longer matches the inferred type |

The reader is `cloudFiles` with `schemaEvolutionMode = addNewColumns`,
`rescuedDataColumn = _rescued_data`, `inferColumnTypes = true`, and a `schemaHints` of
`event_ts TIMESTAMP`.

Only `basket_line_events_raw` was refreshed, via `refresh_selection` in the updates API rather than
a whole-pipeline run. That is what made the experiment affordable: the medallion is one graph
(ADR-0004), and a full update would have rebuilt silver and gold over the 2.6M-row transaction seed
and the 36.8M-row causal table on every tranche.

## What was measured

| stage | files | rows | `_schema/_schemas/` | drift columns present | rescued rows |
|---|---|---|---|---|---|
| T1 pre-drift | 0–99 | 50,000 | `0` | `trans_time` | 0 |
| T2 additive | 100–199 | 100,000 | `0`, `1` | `trans_time`, `loyalty_tier` | 0 |
| T3 rename | 200–299 | — | — | — | — |
| T4 retype | 300–399 | — | — | — | — |

**T1 is the control, and it is the part the backfill never had.** One schema version, 50,000 rows,
`_rescued_data` null on every row, and no `loyalty_tier` anywhere. That is the state a backfill
cannot produce, because the sample would already have seen the post-drift files.

**T2 closes ING-003.** A second schema version appeared under `landing/_schema/_schemas/`, the new
column is in the table, the pre-drift 50,000 rows are null for it, and the run completed. Additive
drift did not fail the pipeline and was observable afterwards — which is the requirement, stated in
the terms it was written in.

## F-D1 — schema evolution presents as a **cancelled** update, not a failed or a completed one

The most useful thing this experiment produced is not in the table above.

When Auto Loader meets an unknown column under `addNewColumns`, the flow does not adapt in place.
It terminates, the update goes to **`CANCELED`**, and Lakeflow starts a *successor* update with
`cause: SCHEMA_CHANGE`:

```
WARN  Flow 'dng_dev.bronze.basket_line_events_raw' has encountered a schema change during
      execution and terminated. A new update using the new schema will be automatically started.
INFO  Update fc274e has been cancelled due to a schema change in
      dng_dev.bronze.basket_line_events_raw, and will be restarted.
```

The first driver written for this experiment treated `CANCELED` as terminal and reported
`FAILED_STATE: CANCELED` — a false failure, on the one event the requirement exists to prove is
survivable. Any orchestration that polls for `COMPLETED` and treats the other terminal states as
errors will do the same: page someone every time a schema evolves, and — worse — a test asserting
"the update completed" fails precisely when the pipeline behaved correctly.

The correct shape is to follow the successor chain: on `CANCELED`, look for a later update with
`cause: SCHEMA_CHANGE` and continue watching that one.

This is a measurement-instrument failure of the same family as the others in this project. The
harness was wrong about the shape of a correct result, and its output was indistinguishable from a
real defect.

## F-D2 — the experiment was stopped by platform capacity, and ING-004 is unproven

T3 did not fail on data. It failed on initialisation, five consecutive times:

```
[PYTHON_REPL_CREATION_FAILED] Failed to create Python REPL during pipeline initialization.
This issue may be transient. Try restarting your pipeline and contact Databricks for support
if this issue persists.
```

"May be transient" is what the message says; five identical failures in a row is what happened. A
sixth attempt after a pause got further — it reached `SETTING_UP_TABLES`, cancelled with the
schema-change signature above, and its auto-started successor then sat in `INITIALIZING` for over
fifteen minutes. `databricks pipelines stop` timed out against it. The SQL warehouse stayed healthy
throughout (`SELECT 1` returned immediately), so this is pipeline compute specifically, not the
account.

That matches a limitation already recorded in this project — an update stalling 49 minutes in
`INITIALIZING`, with cancel-and-resubmit getting capacity immediately — and it is the reason the
staged experiment stops here rather than continuing to burn a shared daily quota whose exhaustion
takes all compute down until tomorrow.

**Consequences, stated plainly:**

- **ING-003** — additive drift does not fail the pipeline. **Proven** at T2.
- **ING-004** — a type-incompatible field lands in `_rescued_data` with the row count preserved.
  **Not proven.** It needs T4, and T4 needs the retype tranche to be ingested.
- The rename case (T3) is also unmeasured. It is interesting for a reason ING-003 does not cover:
  a rename is an *addition plus a disappearance*, so the old column silently becomes null for new
  rows rather than erroring. That is the failure mode `silver-findings.md` already records halving
  a dashboard's data with no error, and it deserves its own measurement.

`_rescued_data` remaining at zero here is **not** evidence that nothing is rescued. It is evidence
that the tranche which would populate it was never ingested. The distinction matters because a zero
that has not been earned reads exactly like a clean result — the same trap as the drift scenario
that never fired at 200k events, recorded in the decision log on 2026-08-06.

## Reproducing

`scripts/` does not own this yet; the driver lived in a scratch file for the run. Turning it into a
committed script is the obvious next step and is deliberately not claimed as done. The sequence is:

1. Delete every file under `/Volumes/<catalog>/bronze/landing`, **including `_schema/`** —
   `--full-refresh-all` does not reset `cloudFiles.schemaLocation`, which has its own lifecycle.
2. Upload `events-000000` … `events-000099`.
3. `POST /api/2.0/pipelines/<id>/updates` with
   `{"full_refresh_selection": ["basket_line_events_raw"]}`.
4. Assert one version under `landing/_schema/_schemas/` and `_rescued_data` null on all rows.
5. Upload the next 100 files, update with `refresh_selection`, follow the `SCHEMA_CHANGE`
   successor chain, and re-assert.

Steps 4 and 5 are the whole point. A run that only checks the final state cannot tell an evolution
from a backfill, which is how B1 happened in the first place.
