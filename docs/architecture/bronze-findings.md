# Bronze — what the first ingestion actually proved

200,000 events, 400 files, `dng_dev.bronze.basket_line_events_raw`. Row count exact, zero loss.
Three findings, none of which were the ones the pipeline was built to demonstrate.

---

## B1 — Schema drift is only observable if you were already watching

**What happened.** The generator staged three drift events at 25%, 50% and 75% of the stream: a
new column, a rename, and a type change. All 401 files were uploaded to the landing volume, then
the pipeline ran once. Result: **`_rescued_data` was null on all 200,000 rows.**

**Why.** Auto Loader infers its schema from a *sample of files in the directory*, not from the
first file in arrival order. All the drifted files were already sitting there. So the post-drift
shape **was** the initial schema:

```
schema version 0 (the only version):
  quantity          string     <- inferred as string on the first pass
  transaction_time  long       <- the renamed column, present from v0
  loyalty_tier      string     <- the added column, present from v0
```

Nothing evolved, because nothing changed after inference. Nothing was rescued, because nothing
failed to fit. The pipeline was correct; the *experiment* was wrong.

**Why this is the most useful thing in the run.** It generalises well past this repo: **a
backfill cannot demonstrate schema evolution.** If you load history in bulk and then declare your
ingestion "drift-tested", you have tested nothing — the drift was flattened into the initial
inference before the first row landed. Drift is a property of *arrival over time*, and testing it
requires ingesting in the order the events actually arrived.

**Consequence:** the drift scenario needs staged ingestion — run the pipeline, upload the next
tranche, run again. That is also how it happens in production, which is the point.

## B2 — A schema hint is a class of drift you have chosen not to be told about

The first version of `events.py` carried `.option("cloudFiles.schemaHints", "quantity STRING")`,
on the reasoning that widening a numeric column defensively is harmless.

It is not harmless. The retype drift turns `quantity` from `12` into `"12 units"`, and
pre-declaring the column as a string meant those values were accepted as ordinary data rather than
rescued. The hint silently disabled the exact detection the column existed to demonstrate.

The rule that came out of it: **hint the ambiguous, never the merely inconvenient.** Every schema
hint is an assertion that you already know the type, and therefore an instruction not to be told
when you are wrong.

(The hint was removed. `quantity` is still a string in the current table — see B1: the sample
already contained drifted files, so inference reached the same answer on its own.)

## B3 — `--full-refresh-all` does not reset the schema location

After removing the hint, `databricks bundle run --full-refresh-all` rebuilt the table from
scratch and produced **the same schema and the same zero rescued rows**.

A full refresh truncates and rebuilds the *tables*. Auto Loader's schema tracking lives in
`cloudFiles.schemaLocation` — here a single file at `/Volumes/.../landing/_schema/_schemas/0` —
and is a **separate artifact with a separate lifecycle**. It survives the refresh, so the pipeline
rebuilds using the schema it had already decided on.

This is the kind of thing that produces an hour of confusion: you reset everything, and the old
behaviour persists. Resetting inference genuinely requires deleting the schema location, which is
a deliberate act, not a flag.

---

## What did work, and is worth showing

**The rename is a silent data-halving.** This is the failure mode the config docstring predicted,
and it reproduced exactly:

| | rows |
|---|---:|
| total | 200,000 |
| `trans_time` populated | 100,637 |
| `transaction_time` populated | 99,363 |
| neither populated | **0** |

No error. No rescue. No warning. One field became two, each populated for the half of the timeline
it existed in. **A dashboard filtering on `trans_time` silently reports 50.3% of the data** and
looks completely healthy doing it.

This is more dangerous than a type error precisely because nothing fails. A rescued row shows up
in a count; a null shows up as "no data for that period", which is a plausible business answer.

**Row conservation held.** 200,000 in, 200,000 out, across 400 files, with `_source_file`,
`_source_file_ts`, `_ingest_ts` and `_pipeline_id` populated on every row (ING-002). The
`ingest_health` materialized view makes "the pipeline ran" and "the pipeline ingested what it
should have" separately answerable, which is the distinction that matters at 03:00.
