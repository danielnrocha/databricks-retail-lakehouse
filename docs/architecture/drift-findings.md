# Staged-ingestion drift findings

Finding B1 in [`bronze-findings.md`](bronze-findings.md) recorded that a bulk backfill cannot
demonstrate schema evolution, because Auto Loader infers its initial schema from a **directory
sample** rather than from arrival order — so the post-drift shape becomes the starting schema and
nothing ever evolves. `_rescued_data` stayed at zero across 200,000 events for exactly that reason,
and the pipeline looked correct while proving nothing.

This document records the staged re-run that was supposed to settle ING-003 and ING-004: clear the
landing volume and the schema location, upload only the files preceding the first drift point, run,
then upload the next tranche and run again, so drift arrives in order.

**Result: ING-003 and ING-004 are both proven.** It took eight pipeline updates rather than four,
because six of them failed or stalled on platform capacity rather than on anything about the data.
That detour is recorded below rather than smoothed out, because it is the part a reader would
otherwise repeat.

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
| T2 additive | 100–199 | 100,000 | `0`, `1` | + `loyalty_tier` | 0 |
| T3 rename | 200–299 | 150,000 | `0`, `1`, `2` | + `transaction_time` | 0 |
| T4 retype | 300–399 | 200,000 | `0`, `1`, `2` | unchanged | **49,468** |

**T1 is the control, and it is the part the backfill never had.** One schema version, 50,000 rows,
`_rescued_data` null on every row, and no `loyalty_tier` anywhere. That is the state a backfill
cannot produce, because the sample would already have seen the post-drift files.

**T2 and T3 close ING-003.** Each additive change produced a new version under
`landing/_schema/_schemas/`, put the new column in the table, left the earlier rows null for it,
and completed. Additive drift did not fail the pipeline and was observable afterwards — which is
the requirement in the terms it was written in.

**T4 closes ING-004**, and the number is worth more than the pass:

```json
{"quantity": "2 units", "_file_path": "/Volumes/dng_dev/bronze/landing/events-000304.json"}
```

The generator retypes `quantity` from a number to `"<n> units"` from event 150,000. Right column
name, incompatible type — the case `_rescued_data` exists for, as distinct from the additive case,
which evolves the schema instead.

Row conservation holds exactly: 200,000 rows for 400 files × 500 events, matching
`_manifest.json`'s `total_events`. ING-004 asks for the count to be preserved, and it is.

### The rescued count was checked against the source, not just reported

49,468 rescued rows out of a 50,000-event tranche is a 532-row shortfall, and a shortfall that
looks like rounding is exactly the kind of number this project has been wrong about before. Counted
directly from the files on disk:

```
source files 300-399: 50000 events, 49468 with string quantity
```

An exact match. The 532 are events emitted *before* index 150,000 that were held back by the
late-arrival buffer and written into later files, so they carry the pre-drift numeric `quantity`
and correctly do not rescue. The instrument agrees with the source; had it not, the plausible
reading — "Auto Loader rescued about 99% of them" — would have been a fabricated explanation for a
real defect.

### What T3 shows that no requirement asks for

`trans_time` and `transaction_time` are now **both** columns in the table, and both are non-null
somewhere: `trans_time` for rows before event 100,000, `transaction_time` after. Nothing errored and
nothing rescued, because a rename is not a type conflict — it is one column ending and another
beginning.

That is the failure `silver-findings.md` already records halving a dashboard's data with no error,
visible here at its origin. A downstream query keyed on `trans_time` keeps returning rows and
silently stops covering the recent half of the stream. It is the most dangerous of the three drift
types and the only one that neither fails nor rescues.

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

## F-D2 — six of the eight updates failed on capacity, and "may be transient" was not

T3 did not fail on data. It failed on initialisation, five consecutive times:

```
[PYTHON_REPL_CREATION_FAILED] Failed to create Python REPL during pipeline initialization.
This issue may be transient. Try restarting your pipeline and contact Databricks for support
if this issue persists.
```

"May be transient" is what the message says; five identical failures in a row is what happened, and
a message that describes a possibility is not evidence about the case in front of you. A sixth
attempt after a pause reached `SETTING_UP_TABLES`, cancelled with the schema-change signature
above, and its auto-started successor then sat in `INITIALIZING` for **over thirty-five minutes**.
`databricks pipelines stop` timed out against it once, succeeded on a second call with a longer
window, and the pipeline returned to `IDLE`.

From `IDLE`, the identical T3 request completed in **52.5s** and T4 in **21.8s**. Nothing about the
request changed. This is the limitation already recorded in this project — an update stalling 49
minutes in `INITIALIZING` with cancel-and-resubmit getting capacity immediately — and the practical
rule it implies is worth stating: **a Free Edition pipeline update that has not left `INITIALIZING`
in a few minutes is waiting for capacity that is not coming. Cancel it and resubmit.** Retrying
without cancelling produced five failures and one thirty-five-minute stall; cancelling produced a
result in under a minute.

The SQL warehouse answered `SELECT 1` immediately throughout, which is how the fault was localised
to pipeline compute rather than to the account or its quota. Worth doing before concluding "the
environment is down", because the two have the same symptom from inside a pipeline.

## The state the workspace is left in

Deliberately not reverted, because this *is* the correct end state of the experiment.

| | |
|---|---|
| `/Volumes/dng_dev/bronze/landing` | all 400 event files, re-uploaded in tranche order. `_manifest.json` was deleted with the rest and is regenerable from `data/generated/stress/`. |
| `landing/_schema/_schemas/` | versions `0`, `1`, `2` |
| `dng_dev.bronze.basket_line_events_raw` | 200,000 rows; 49,468 with non-null `_rescued_data`; `trans_time`, `loyalty_tier` and `transaction_time` all present |
| pipeline `c677bdef` | `IDLE` |
| source files | all 400 remain in `data/generated/stress/` |

This replaces the state Finding B1 described, where a backfill had produced one schema version and
zero rescued rows across the same 200,000 events. Same input, same pipeline, different arrival
order, opposite result — which is the whole point of B1 and now has both halves on record.

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
