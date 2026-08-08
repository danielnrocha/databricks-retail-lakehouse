# ADR-0007 — Liquid clustering + Predictive Optimization; no partitioning, no Z-ORDER

**Status:** Accepted · **Date:** 2026-08-06 · **Reversal cost:** Medium

---

## Context

Table layout is where most Databricks platforms quietly rot. The failure is rarely dramatic: a
partition column chosen in month one produces a hundred thousand sub-megabyte files by month
twelve, queries slow by 5×, and nobody can point at the commit that caused it because no single
commit did.

Three forces have to be balanced:

1. **File size.** Too small → per-file overhead dominates and the driver spends its life listing.
   Too large → poor parallelism and expensive rewrites.
2. **Data skipping.** The layout must let the engine prune files without reading them.
3. **Write amplification.** Every reorganisation rewrites data. A layout that needs constant
   rewriting costs more than the scans it saves.

The streaming path in this platform makes force 1 acute: a 30s trigger produces 2,880 commits per
day per table, and the amplifier deliberately makes many of them small.

## Options considered

### A. Hive-style partitioning on `transaction_date`
The reflex choice, and it is what most teams migrating from Hive/Synapse bring with them.

**Rejected.** Grocery transaction volume per day is, at seed scale, a few megabytes. Partitioning
by day produces a directory per day holding files far below the ~128MB–1GB target — the textbook
small-files generator. Worse, it is a *structural* commitment: the partition column is encoded in
the physical path, so changing your mind means rewriting the table.

Partitioning is still correct when a partition holds ≥ ~1GB and the column is low-cardinality and
near-universally filtered. Neither holds here. Recording this matters because the *rule* is what
transfers; the conclusion is table-specific.

### B. Partitioning + Z-ORDER
The pre-2024 Databricks best practice, and still the most common thing in production.

**Rejected.** Z-ORDER is a *batch reorganisation*: it rewrites the affected files each run, so
incremental data is not clustered until the next `OPTIMIZE ZORDER BY`. On a streaming table this
means either constant expensive rewrites or persistently unclustered recent data — which is the
data most queries want. Z-ORDER also cannot be changed incrementally: altering the Z-ORDER columns
means a full rewrite.

### C. Liquid clustering + Predictive Optimization ✅

**Chosen.**

- Clustering keys are **metadata, not physical layout**, so they can be changed with
  `ALTER TABLE ... CLUSTER BY` without rewriting history.
- Clustering is incremental — new data is clustered as it lands, not on a separate reorg pass.
- It handles skewed and high-cardinality keys, which is exactly the `store_id` situation the
  amplifier creates.
- Predictive Optimization runs `OPTIMIZE`, `VACUUM`, and `ANALYZE` on Unity Catalog managed tables
  based on observed access patterns, removing the maintenance-cron class of bug entirely.

## Decision

| Table | Clustering keys | Rationale |
|---|---|---|
| `bronze.basket_line_events` | `AUTO` | Ingest-only; let Databricks infer from query patterns rather than guess before any query exists. |
| `silver.fact_basket_line` | `transaction_date`, `store_id` | The two predicates present in essentially every downstream query. |
| `gold.fct_basket_line` | `date_key`, `product_key` | BI access is date-scoped then product-sliced. |
| `silver.dim_product_scd2` | `product_id`, `__START_AT` | Point-in-time joins predicate on the key and the validity window (ADR-0008 was never written — see `README.md` in this directory). |
| `gold.promo_performance_rt` | `AUTO` | Streaming, small, query pattern still emerging. |

> **Corrected 2026-08-06.** The `dim_product_scd2` row above originally specified
> `(product_id, is_current)`. There is no `is_current` column: `AUTO CDC ... STORED AS SCD TYPE 2`
> emits `__START_AT` / `__END_AT`, and currency is expressed as `__END_AT IS NULL`. The clustering
> key now names what the point-in-time join actually predicates on.
>
> A worked example written before the code exists will contain a column that does not. The rule the
> ADR states was right; the illustration was invented. Worth leaving visible, because an ADR is
> trusted precisely to the extent its examples were checked against something real.

Rules enforced by test (`tests/unit/test_table_properties.py`):

1. **No `PARTITIONED BY` anywhere.** Grep-level assertion on DDL.
2. **No manual `OPTIMIZE` on a Predictive-Optimization-enabled table.** Running both on the same
   table is a documented anti-pattern: they contend, and the manual run reverses PO's decisions
   while still being billed. The test asserts the two sets are disjoint.
3. **`delta.autoOptimize.autoCompact` on streaming sink tables only.** Auto-compaction runs
   synchronously after write on the writing cluster — desirable for high-frequency small writes,
   pure overhead for a daily batch write.
4. **`VACUUM` retention ≥ 7 days.** Shorter breaks time travel and — more dangerously — can delete
   files a concurrent long-running reader still needs.

### The measurement obligation

None of the above is asserted without evidence. `docs/architecture/perf-evidence.md` records, for
each claim, the query profile before and after: file count, bytes scanned, shuffle read/write,
task-time skew ratio, and wall clock, with the input volume stated. A layout claim without a query
profile is an opinion.

This is also the honest answer to "how do I know liquid clustering helped?" — you do not, until you
look. The most common failure mode with liquid clustering is enabling it, changing nothing about
the query, and assuming the benefit.

## Consequences

**Positive**
- Clustering keys are revisable, so an early wrong guess is cheap.
- Maintenance is automatic and access-pattern-driven rather than cron-and-hope.
- The small-files problem is addressed at three layers — auto-compaction on write, PO's `OPTIMIZE`,
  and trigger sizing — instead of being fought at one.

**Negative**
- Liquid clustering requires Unity Catalog managed tables; external tables are excluded. Acceptable
  here since everything is UC-managed, but it is a real constraint for teams with external tables.
- Predictive Optimization is a black box. When it does *not* run, the diagnosis path is thinner than
  with a cron you own. Mitigated by monitoring `system.storage.predictive_optimization_operations_history`
  and alerting when a hot table goes un-optimised beyond a threshold.
- Auto-compaction taxes the writing cluster. On the 30s-trigger stream this is measurable; it is
  accepted because the alternative — unbounded small files — is worse, and the trade is recorded
  with numbers rather than assumed.

## Reversal cost: Medium

Clustering keys change for free. Abandoning liquid clustering entirely for partitioning means a
full table rewrite plus downstream invalidation — which is the asymmetry that justified choosing
the revisable option first.
