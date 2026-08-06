# ADR-0004 — Lakeflow Declarative Pipelines over hand-rolled Structured Streaming

**Status:** Accepted · **Date:** 2026-08-06 · **Reversal cost:** Medium

---

## Context

Bronze ingestion needs to read files from a landing volume incrementally, survive schema change,
preserve everything it cannot parse, and be re-runnable. There are three credible ways to do that
on Databricks, and the choice is genuinely contested — this is not a case where one option is
obviously right.

## Options considered

### A. Hand-rolled Structured Streaming with `readStream` + `writeStream` + explicit checkpoints
Maximum control. You own the checkpoint, the trigger, the sink, the retry semantics.

**Rejected**, but it is the option most worth taking seriously, because the reasons it is
attractive are real:

- You end up writing, by hand, the things the framework already does: checkpoint placement,
  restart-safety, schema-location management, table property maintenance, and a dependency graph
  between tables that you will otherwise encode as job ordering.
- Checkpoint management is the specific trap. A checkpoint is an invisible piece of state whose
  deletion silently reprocesses everything and whose *presence* silently skips data if you point a
  new query at an old one. In a declarative pipeline the framework owns that lifecycle; hand-rolled,
  it becomes a directory somebody eventually "cleans up".
- The dependency graph is the other. With three layers and a dozen tables, "which table is stale
  because its upstream failed?" is a question the framework answers and a hand-rolled job does not.

Where A would win: a pipeline needing `foreachBatch` with genuinely custom sink logic, or arbitrary
stateful processing that the declarative API cannot express. Neither applies here.

### B. Plain batch — `COPY INTO` or a scheduled `MERGE`
Simplest possible thing.

**Rejected.** `COPY INTO` is idempotent and pleasant for a one-off load, but it has no schema
evolution story comparable to Auto Loader's, no rescued-data column, and no incremental file
discovery that scales past a directory listing. It is the right tool for the seed load — and is
effectively what `scripts/upload_seed.py` plus a `CREATE TABLE AS` does — and the wrong tool for a
continuous feed.

### C. Lakeflow Spark Declarative Pipelines + Auto Loader ✅

**Chosen.** What it actually buys, in order of how much it matters:

1. **Auto Loader's incremental file discovery** — no directory listing that degrades as the landing
   zone grows, and no bookkeeping table of "files I already read".
2. **`_rescued_data`** — the only mechanism that makes "nothing is dropped" a property of the
   platform rather than a promise in a code review.
3. **Managed schema location and checkpoints** — the invisible state has an owner.
4. **A declared dependency graph** — table freshness and failure propagation are visible rather
   than inferred from job ordering.
5. **The event log as a governed table** — `ops.pipeline_events`. Without it, "what did the
   pipeline do last Tuesday?" is answerable only by clicking.

## Decision

Lakeflow Spark Declarative Pipelines, using the **`from pyspark import pipelines as dp`** module
rather than `import dlt`.

That import choice deserves its own line, because it dates the code. Delta Live Tables became
Lakeflow Spark Declarative Pipelines; `dlt` continues to work indefinitely and Databricks has been
explicit that migration is optional. The new module is used here because it is the open-source SDP
API that Spark 4.1 ships — code targeting the durable interface ages better than code targeting a
compatibility shim, and the cost of choosing it now is zero.

`@dp.table` creates streaming tables; `@dp.materialized_view` creates materialized views. The old
single `@dlt.table` decorator conflated the two, which made the streaming/batch distinction a
property of the query rather than a declared intent.

## Consequences

**Positive**
- Bronze is ~60 lines including comments, and most of it is configuration of behaviour that would
  otherwise be code.
- Restart safety, incremental discovery, and rescue are platform properties, not review items.
- Row conservation was verified on the first run: 200,000 in, 200,000 out, zero loss.

**Negative — and the second one is serious**

- **Pipeline modules cannot be imported locally.** `pyspark.pipelines` does not exist in the
  installed PySpark, so `src/retail_lakehouse/bronze/events.py` cannot be unit-tested by importing
  it. Mitigation is structural rather than clever: transformation logic that deserves a unit test
  lives in plain importable modules, and the pipeline module stays a thin declaration. This is a
  real constraint that pushes toward good structure, but it is a constraint.

- **The framework's conveniences hide state you still need to understand.** The first bronze run
  demonstrated this precisely: `--full-refresh-all` rebuilt every table and produced the *same*
  schema and the *same* zero rescued rows, because Auto Loader's schema location is a separate
  artifact with a separate lifecycle that a full refresh does not touch. Someone who believes the
  framework owns all the state will lose an hour to that. See
  [`bronze-findings.md`](../architecture/bronze-findings.md), B3.

- **One active pipeline per type on Free Edition.** Bronze, silver and gold therefore share one
  pipeline graph rather than being independently schedulable. On a paid tier they would be split,
  because independent failure domains are worth real money.

## Reversal cost: Medium

The declared tables are Delta tables like any other, so consumers are unaffected. Reverting means
rewriting the ingestion as explicit streaming queries and taking ownership of checkpoints and
schema locations — perhaps a day's work for bronze, more once silver's `AUTO CDC` is in place,
since a hand-written SCD Type 2 `MERGE` is materially harder than the declarative form.
