# Skew and spill performance lab

> Requirement **PRF-005**: every performance claim cites a measurement, states its input volume,
> and names what was held constant. A claim without those three is an opinion.
>
> Companion to [`perf-evidence.md`](perf-evidence.md), which records platform measurements. This
> document records *query* measurements: what skew and spill actually do to a 2X-Small serverless
> SQL warehouse on 39.4M rows of real grocery data, and which of the standard mitigations were
> worth applying.
>
> Raw per-run records live in `data/perf/*.json`, one entry per statement with its
> `statement_id`. Every table below is generated from them by
> `python -m retail_lakehouse.perf.report`. Regenerating the measurements is
> `python -m retail_lakehouse.perf.cli {load,probe,profile,skew,smj,agg,spill,backfill}`.

**Date:** 2026-08-06 · **Workspace:** Databricks Free Edition, serverless

---

## Held constant across every measurement in this document

| | |
|---|---|
| Compute | One serverless SQL warehouse, **2X-Small**, id `0eec48413abb124d`. Never resized. |
| Client | macOS, Databricks SDK 0.125.0, Statement Execution API, single session, no concurrency |
| Catalog / schema | `dng_dev.perf` — created by this lab, disposable, nothing downstream reads it |
| `dng_dev.perf.transactions` | **2,595,732 rows**, 12 columns, 1 file, **14.4 MiB**, unclustered |
| `dng_dev.perf.causal` | **36,786,524 rows**, 5 columns, 1 file, **32.0 MiB**, unclustered |
| Runs per variant | 4 — first discarded as warm-up, **median of the remaining 3** reported |
| IO cache | **100% on every measured run** (`read_io_cache_percent = 100`), verified per run |
| Result cache | Defeated per run and verified (`from_result_cache = false`, `read_bytes > 0`) |

Both tables were loaded straight from the Parquet seed with `CREATE OR REPLACE TABLE ... AS
SELECT *`. No `OPTIMIZE`, no `ZORDER`, no `CLUSTER BY`. A baseline you have already tuned is not
a baseline, and `test_lab_tables_are_unclustered` fails if that changes.

Every table reports the **min-max spread** of the three measured runs as a percentage of the
median. A 7% improvement against a 28% spread is not a result, and the spread column is there so
nobody has to take my word for which is which.

---

## Constraints: what this platform will not let you measure, and what it does instead

All of these were verified by running them, not read off a docs page. Verbatim errors are in
`data/perf/platform_probes.json`.

### C1 — No Spark configuration is settable on a serverless SQL warehouse

Databricks documents six confs as settable on serverless compute. **On a serverless SQL
warehouse, none of the six work**, and neither do the AQE knobs:

| `SET` statement | Result |
|---|---|
| `spark.databricks.execution.timeout` | `[CONFIG_NOT_AVAILABLE.WITHOUT_SUGGESTION] ... is not available. SQLSTATE: 42K0I` |
| `spark.sql.legacy.timeParserPolicy` | same |
| `spark.sql.session.timeZone` | same |
| `spark.sql.shuffle.partitions` | same |
| `spark.sql.ansi.enabled` | same |
| `spark.sql.files.maxPartitionBytes` | same |
| `spark.sql.adaptive.enabled` | same |
| `spark.sql.adaptive.skewJoin.enabled` | same |
| `spark.sql.adaptive.skewJoin.skewedPartitionFactor` | same |
| `spark.sql.adaptive.skewJoin.skewedPartitionThresholdInBytes` | same |
| `spark.sql.autoBroadcastJoinThreshold` | same |
| `use_cached_result` | **succeeds** |

The documented serverless-compute list applies to serverless *notebook* compute. A serverless
SQL warehouse is a different surface and the list does not transfer. This is the single most
consequential constraint on the lab: **the "before" condition can never be a config toggle.**
Every intervention below is a change to the SQL.

It also removes the obvious experimental design — flip `skewJoin.enabled` and diff — which is
what most write-ups on this topic do. That design is unavailable here, so the lab has to reason
about AQE from the outside, by measuring whether its documented trigger conditions are met.

### C2 — There is no session

`SET use_cached_result = false` succeeds. Reading it back in the next statement returns `true`.
The Statement Execution API opens a fresh session per request, so no session-scoped setting
survives to the statement it was meant to affect. Anything shaped like "set X, then run the
query" is unavailable.

### C3 — Result cache is not defeated by comment uniqueness

The standard trick — prefix each run with a unique comment — **does not work**. Two runs of the
same query differing only in `/* nonce=... */` both returned `result_from_cache = true`,
`read_bytes = 0`, `task_total_time_ms = 0`. Databricks normalises comments out of the cache key.

What does work is a varying literal in the outermost projection. Verified on the identical
query: `result_from_cache = false`, `read_bytes = 439,891` on both runs. The harness wraps every
statement as `SELECT <nonce> AS _perf_nonce, q.* FROM (<sql>) q`, and `runner.run_all` raises if
any measured run comes back cached. Had this gone unnoticed, every "optimised" variant would
have reported a sub-200 ms wall clock and the whole lab would have been a cache benchmark.

### C4 — No per-task durations, therefore no direct skew measurement

Databricks defines a skewed stage as **max task duration > 1.5x p75 task duration**. There is
no Spark UI on serverless and no surface — REST, system table, or Query Profile export —
exposing per-task durations. **That definition is unmeasurable here and this lab does not claim
to have measured it.**

The proxy used instead is

```
parallelism efficiency = total_task_duration_ms / execution_duration_ms
```

which is how many task-seconds the warehouse retired per wall-clock second. Evenly spread work
keeps every slot busy and the ratio tracks slot count; a stage that collapses onto one hot key
accumulates wall clock without accumulating task time, and the ratio falls. It detects the
*consequence* of skew (lost parallelism), not skew itself, and it is confounded by fixed
overhead — see the resolution limit below.

### C5 — `shuffle_read_bytes` is always zero

`system.query.history.shuffle_read_bytes` exists on this workspace and is non-null. Across
**325 statements** recorded during this lab its maximum value is **0**, while
`spilled_local_bytes` (17 non-zero) and `written_bytes` (15 non-zero) on the same rows populate
correctly. Shuffle byte accounting is simply not reported for serverless SQL warehouses here.

Two calibration statements were written specifically to measure bytes-per-shuffled-row
(`CAL-transactions`, `CAL-causal` in `data/perf/skew_runs.json`); both ran, both returned zero.
They are kept in the record because a deleted failed measurement looks like one that was never
attempted. Section 2 explains what replaced them and how far wrong it could be.

### C6 — `system.query.history` ingest lag is variable, and long

Measured by comparing `max(start_time)` in the table against `current_timestamp()`: **349 s**
early in a session, and **over 40 minutes** for the last batch of statements in one. The query
history REST API has the record within seconds, which is why the harness reads metrics from
there live and treats the system table as a separate, re-runnable backfill stage. All 84 measured
runs in this document are backfilled; the last batch took three attempts spread over an hour to
land, which is why the stage is idempotent rather than a one-shot.

### C7 — Caching APIs

`CACHE TABLE` and `UNCACHE TABLE` fail with `[NOT_SUPPORTED_WITH_DB_SQL] ... is not supported on
a SQL warehouse`. `CACHE SELECT` succeeds — it warms the *disk* IO cache, not a Spark memory
cache. Warm/cold is therefore not controllable, only observable, which is why every table above
reports `read_io_cache_percent` per run rather than claiming a cold baseline.

### C8 — Resolution limit of this warehouse

The smallest jobs in this lab (`A1`, `A2`: 2.6M rows, 582 output groups) finish in **400–600 ms
with a 22–41% run-to-run spread** and a parallelism proxy of 0.22–0.26. At that size the
measurement is dominated by fixed per-query overhead and **cannot resolve a skew effect at all**.
Any comparison below 1 second in this document is reported but not concluded from.

---

## Experiment 1 — Does the composite join key inherit the dataset's skew?

**Hypothesis.** [`dataset-findings.md` F1](dataset-findings.md) measures 2,519x max/median on
`STORE_ID` and 9,926x on `PRODUCT_ID`. The join under test keys on
`(PRODUCT_ID, STORE_ID, WEEK_NO)`. Compositing keys usually reduces skew; the question is by how
much, because the answer decides whether salting is warranted before anything is measured.

**Method.** Group each table by each candidate key, report the row-count distribution. Pure SQL,
one pass per key, no join involved.

### Measurements — key skew

Input: `transactions` 2,595,732 rows; `causal` 36,786,524 rows.

| Table | Key | Distinct keys | Rows | Max rows/key | Median | p99 | max/median |
|---|---|---:|---:|---:|---:|---:|---:|
| `transactions` | `STORE_ID` | 582 | 2,595,732 | 75,573 | 30 | 41,147 | **2,519.1x** |
| `transactions` | `PRODUCT_ID` | 92,339 | 2,595,732 | 29,778 | 3 | 365 | **9,926.0x** |
| `transactions` | `PRODUCT_ID, STORE_ID, WEEK_NO` | 2,370,784 | 2,595,732 | 21 | 1 | 3 | **21.0x** |
| `causal` | `STORE_ID` | 115 | 36,786,524 | 469,670 | 342,674 | 439,767 | **1.4x** |
| `causal` | `PRODUCT_ID` | 68,377 | 36,786,524 | 7,083 | 142 | 4,262 | **49.9x** |
| `causal` | `PRODUCT_ID, STORE_ID, WEEK_NO` | 36,771,279 | 36,786,524 | 2 | 1 | 1 | **2.0x** |

### What actually happened

**The composite key dissolves the skew.** 9,926x on `PRODUCT_ID` alone becomes **21x** on the
composite — and 21x on a key whose median is 1 row means the busiest key holds twenty-one rows.
On the `causal` side the maximum multiplicity is **2**.

The key-level ratio still overstates it, because what a shuffle sees is the *partition*, not the
key. Bucketing by `pmod(hash(key), N)` — Spark SQL's `hash()` is Murmur3, the same function
`HashPartitioner` uses, so this reproduces the real bucketing rather than modelling it:

| Table | Shuffle key | N | Max rows | Median rows | max/median |
|---|---|---:|---:|---:|---:|
| `transactions` | `STORE_ID` | 16 | 324,340 | 157,657 | 2.06x |
| `transactions` | `STORE_ID` | 64 | 205,619 | 31,741 | 6.48x |
| `transactions` | `STORE_ID` | 200 | 108,742 | 676 | 160.86x |
| `transactions` | `STORE_ID` | 1024 | 75,576 | 50 | **1,511.52x** |
| `transactions` | `PRODUCT_ID, STORE_ID, WEEK_NO` | 16 | 163,044 | 162,058 | 1.01x |
| `transactions` | `PRODUCT_ID, STORE_ID, WEEK_NO` | 64 | 41,084 | 40,554 | 1.01x |
| `transactions` | `PRODUCT_ID, STORE_ID, WEEK_NO` | 200 | 13,340 | 12,976 | 1.03x |
| `transactions` | `PRODUCT_ID, STORE_ID, WEEK_NO` | 1024 | 2,737 | 2,535 | **1.08x** |
| `causal` | `PRODUCT_ID, STORE_ID, WEEK_NO` | 1024 | 36,544 | 35,923 | **1.02x** |

(The full 24-row table across all six keys and four partition counts is in
`data/perf/partition_profiles.json`.)

At every partition count the composite key is within 8% of perfectly uniform. `STORE_ID` on its
own goes the other way — the more partitions you give it, the worse the ratio gets, because 582
keys spread across 1,024 buckets leaves most buckets nearly empty while the hot store still
occupies one on its own.

### What changed as a result

**Salting was removed from the plan for this join.** It was measured anyway, so the cost of
having applied it uncritically is on the record — see Experiment 2, variant V4: **8.9x slower**.
`test_composite_join_key_dissolves_the_skew` now fails if anyone reintroduces it on the grounds
that "the dataset is skewed". It is; not on this key.

This is the result I would most want a reviewer to notice. The dataset profile screams skew, the
join key it feeds is uniform, and the two facts are entirely compatible. Reading the profile and
reaching for a salt would have produced a 9x regression justified by a real measurement of the
wrong thing.

---

## Experiment 2 — Would AQE's skew join optimisation fire on this data?

**Hypothesis.** AQE's `OptimizeSkewedJoin` splits a partition only when **both** conditions hold:
larger than `skewedPartitionFactor` (default 5) times the median, **and** larger than
`skewedPartitionThresholdInBytes` (default 256 MB). The transaction table is 2.6M rows, so
post-shuffle partitions should fall far under 256 MB — meaning severe *logical* skew that AQE
silently declines to fix.

**Method.** Two parts. (a) Convert the measured per-partition row counts into bytes and test both
conditions. (b) Read the physical plan, because a condition test is meaningless if the plan does
not contain the operator the condition applies to.

### The bytes-per-row problem, stated plainly

Per C5, the width could not be measured. It is **computed** from Spark's UnsafeRow layout —
8-byte null bitmap plus 8 bytes per field in the fixed region, plus an 8-byte-aligned variable
region for strings:

| Shuffle payload | Fields | Estimated bytes/row |
|---|---:|---:|
| `transactions` join side: `(PRODUCT_ID, STORE_ID, WEEK_NO, SALES_VALUE)` | 4 | **40** |
| `causal` join side: `(PRODUCT_ID, STORE_ID, WEEK_NO)` | 3 | **32** |
| `causal` full row (2 one-character strings) | 5 | 64 |
| `transactions` full row | 12 | 104 |

The estimate ignores shuffle compression, so it is generous — and every conclusion here is
"even at this width the threshold is not reached", which is the safe direction to be wrong in.
To make the dependency falsifiable, the table below carries a **break-even** column: the
bytes-per-row at which that partition would reach 256 MB.

### Measurements — AQE trigger conditions

Input volumes as in "held constant". `Est. max partition` = max rows x estimated bytes/row.

| Table | Shuffle key | N | Max rows | max/median | Est. max partition | >5x median? | >256 MB? | **AQE splits?** | Break-even B/row |
|---|---|---:|---:|---:|---:|:---:|:---:|:---:|---:|
| `transactions` | `STORE_ID` | 64 | 205,619 | 6.48x | 7.8 MiB | yes | no | **no** | 1,305 |
| `transactions` | `STORE_ID` | 200 | 108,742 | 160.86x | 4.1 MiB | yes | no | **no** | 2,469 |
| `transactions` | `STORE_ID` | 1024 | 75,576 | 1,511.52x | 2.9 MiB | yes | no | **no** | 3,552 |
| `transactions` | `PRODUCT_ID` | 1024 | 37,083 | 17.66x | 1.4 MiB | yes | no | **no** | 7,239 |
| `transactions` | `PRODUCT_ID, STORE_ID, WEEK_NO` | 1024 | 2,737 | 1.08x | 107 KiB | no | no | **no** | 98,077 |
| `causal` | `STORE_ID` | 16 | 4,236,106 | 1.76x | 129.3 MiB | no | no | **no** | **63** |
| `causal` | `STORE_ID` | 64 | 2,409,770 | 3.63x | 73.5 MiB | no | no | **no** | 111 |
| `causal` | `PRODUCT_ID` | 1024 | 75,722 | 2.13x | 2.3 MiB | no | no | **no** | 3,545 |
| `causal` | `PRODUCT_ID, STORE_ID, WEEK_NO` | 1024 | 36,544 | 1.02x | 1.1 MiB | no | no | **no** | 7,346 |

**AQE would not split a single partition anywhere in this dataset, on any key, at any partition
count tested.**

The margins are not equal, and the one tight case is worth stating. `causal` shuffled on
`STORE_ID` at N=16 needs only **63 bytes/row** to reach 256 MB, and the *full* `causal` row is
estimated at 64 bytes — so carrying every column through that shuffle would put it at roughly
271 MB, over the line. It still would not split, because at 1.76x it fails the factor condition.
But it is a good illustration of something the folklore never mentions: **whether AQE engages
depends on your projection**, not just on your data.

### The plan does not contain a shuffle join at all

Testing AQE's conditions assumes the join is one AQE can optimise. `EXPLAIN FORMATTED` on the
baseline says otherwise:

```
PhotonGroupingAgg
+- PhotonBroadcastHashJoin Inner
   :- PhotonShuffleExchangeSource        <- transactions (14.4 MiB) built and broadcast
   +- PhotonScan parquet dng_dev.perf.causal   <- streamed, never shuffled by the join key
```

The planner broadcasts the 14.4 MiB fact side unprompted. `causal` is never shuffled on the join
key, and `OptimizeSkewedJoin` applies only to shuffle joins. **AQE skew handling is inapplicable
to this query by construction, before any threshold is consulted.** Adding an explicit
`/*+ BROADCAST(t) */` hint produces a byte-identical plan — which is exactly what the timings
show.

### Measurements — join variants

Median of 3 runs, warm-up discarded. All variants produce 115 output rows except V5.

| Variant | Intervention | exec ms | task ms | task/exec | read rows | spread |
|---|---|---:|---:|---:|---:|---:|
| `V1-baseline-join` | none (naive inner join) | **2,125** | 1,495 | 0.70 | 34,286,883 | 6% |
| `V2-broadcast` | `BROADCAST(t)` hint | 2,044 | 1,490 | 0.73 | 34,286,883 | 6% |
| `V3-preagg` | aggregate fact side to join grain first | 2,312 | 2,150 | 0.93 | 34,286,883 | 1% |
| `V4-salted` | 16-way salt, `causal` exploded 16x | **18,916** | 18,408 | 0.97 | 39,382,257 | 2% |
| `V5-left-join` | `LEFT JOIN` so uncovered stores survive | 2,367 | 3,467 | 1.47 | 39,382,256 | 10% |

| Variant | Intervention | exec ms | task ms | task/exec | read rows | spread |
|---|---|---:|---:|---:|---:|---:|
| `V6-smj-composite` | `SHUFFLE_MERGE`, uniform composite key | 3,189 | 8,334 | **2.59** | 39,382,256 | 6% |
| `V7-smj-store` | `SHUFFLE_MERGE` on `STORE_ID` (1,511x at N=1024) | 1,239 | 973 | 0.80 | 39,382,256 | 28% |
| `V8-smj-store-salted` | V7 with a 16-way salt | 1,146 | 1,490 | **1.37** | 39,382,257 | 13% |

V6–V8 force a sort-merge join so that a keyed shuffle exists at all; `EXPLAIN FORMATTED` confirms
`SortMergeJoin` with an exchange on both sides in all three. V7/V8 join the 2,595,732 fact rows
to a 115-row store-level summary of `causal`, so the fact volume is unchanged and the only thing
varying is the skew of the shuffle key.

### What actually happened

1. **The broadcast hint bought nothing** — 2,044 ms vs 2,125 ms, a 4% difference against a 6%
   spread, and an identical plan. The optimiser had already made the right choice. Reported as a
   null result rather than a 4% win, because it is one.

2. **Salting an unskewed key cost 8.9x** — 18,916 ms vs 2,125 ms, 2% spread on both. The salt
   forced a 16-fold explosion of the promotion side and turned a broadcast join into real work.
   This is the price of applying a mitigation on the strength of a profile rather than a
   measurement of the actual key.

3. **Pre-aggregation cost 9%** and moved the parallelism proxy from 0.70 to 0.93. The fact side
   only collapses 2,595,732 → 2,370,784 rows (1.09x), so the extra shuffle is not repaid. It is a
   good technique against the wrong grain.

4. **Forcing a sort-merge join cost 50%** — 3,189 ms vs 2,125 ms — while raising the parallelism
   proxy to 2.59, the highest in the lab. More parallelism, more total work, worse wall clock.
   That combination is the clearest demonstration in this document that the proxy measures
   parallelism and not goodness.

5. **On a genuinely skewed shuffle key, salting moved the proxy but not the clock.** V7 → V8:
   parallelism proxy 0.80 → 1.37, **+71%**, which is the mitigation working exactly as advertised.
   Wall clock 1,239 ms → 1,146 ms, **−7.5%** — inside V7's own 28% run-to-run spread. **No
   wall-clock improvement can be claimed.** At 2.6M rows and roughly one second of work, the
   straggler this fixes is worth less than the noise floor of the warehouse.

### What changed as a result

- The gold-layer join keeps the composite key, no salt, no broadcast hint. The plan the
  optimiser picks unaided is the one measured fastest.
- The lab's headline claim is stated as *"AQE would decline to split, and separately the plan
  contains nothing for it to split"* — two independent reasons, both measured. Only the first was
  hypothesised.

---

## Experiment 3 — Aggregation skew at matched volume

**Hypothesis.** `GROUP BY` on a 2,519x-skewed key should show lower parallelism than the same
aggregation on an even key at the same volume.

**Method.** Four aggregations. A1/A2 vary the mitigation at fixed input; A3/A4 hold input volume
at 36,786,524 rows and vary only the group key's skew (1.4x vs 49.9x).

| Variant | Input rows | Key skew | Groups | exec ms | task ms | task/exec | spread |
|---|---:|---:|---:|---:|---:|---:|---:|
| `A1-groupby-store` | 2,595,732 | 2,519x | 582 | 412 | 91 | 0.22 | 22% |
| `A2-salted-groupby` | 2,595,732 | salted 16-way | 582 | 456 | 118 | 0.26 | 41% |
| `A3-causal-by-store` | 36,786,524 | 1.4x | 115 | 1,128 | 725 | 0.64 | 4% |
| `A4-causal-by-product` | 36,786,524 | 49.9x | 68,377 | 1,254 | 935 | 0.75 | 2% |

A1/A2 both compute `sum(SALES_VALUE)`, `count(*)` and `count(DISTINCT BASKET_ID)`; A2 does it
through a two-stage roll-up salted on `hash(BASKET_ID)`, which keeps the distinct count exact
because a given basket hashes to exactly one bucket. Salting on a random value instead would
overcount baskets — the usual way this mitigation breaks in production.

### What actually happened — hypothesis not supported

At matched volume, the *more* skewed key (A4, 49.9x) ran with **higher** parallelism than the
even one (0.75 vs 0.64) and 11% more wall clock, which is fully explained by producing 68,377
groups instead of 115. Group-key skew is not visible in either metric at this scale.

A1 vs A2 is uninterpretable, not negative: at 412 ms with a 22% spread and a 41% spread on its
comparator, the pair sits inside the resolution limit of C8. Reporting "salting made the
aggregation 11% slower" from those numbers would be inventing a result.

### What changed as a result

Nothing in the pipeline. The finding is a **scale threshold**: skew mitigation on this warehouse
is unmeasurable below roughly one second of work, and the fact table is not large enough to get
there on a single-column group-by. That is a statement about the lab's resolution, and it is more
useful than a fabricated percentage.

---

## Experiment 4 — Inducing genuine disk spill

**Hypothesis.** 36,786,524 rows on a 2X-Small should spill on a global sort, a wide window join,
or a 36.8M-group aggregate.

**Method.** Run each candidate 4 times; spill is `spilled_local_bytes > 0` in
`system.query.history`. Nothing else counts as evidence.

### Measurements — the null result

Input: all 36,786,524 `causal` rows (N1 joins in all 2,595,732 transaction rows as well).

| Variant | What it does | exec ms | task ms | **spill** | spread |
|---|---|---:|---:|---:|---:|
| `N1-window-wide` | wide payload through a `PARTITION BY PRODUCT_ID, STORE_ID` window | 2,101 | 4,109 | **0** | 14% |
| `N2-global-rank` | `row_number() OVER (ORDER BY ...)` — one ordering, one task | 5,072 | 4,700 | **0** | 3% |
| `N3-highcard-agg` | 36,771,279 groups, one per row | 2,997 | 2,586 | **0** | 2% |
| `N4-collect-list` | 68,377 arrays, largest 7,083 structs | 2,522 | 2,124 | **0** | 16% |

**None of them spill a single byte.** `causal` is five narrow columns; 36.8M of them fit
comfortably. The obvious pressure sources do not work on this data at this size, and the smallest
warehouse available is still big enough. That is the honest starting point for the rest of the
section.

A second null result on the way there: putting a 1,024-byte payload column in the *projection*
rather than in the sort key produced 0 bytes of spill and a 1.5 s query, because projection
pushdown eliminated the column before the sort ever saw it. Spill pressure comes from what the
operator must hold, not from what the query mentions.

### Measurements — the width sweep

The sort key is widened by a stated number of filler bytes per row. This is synthetic and
labelled as such; it is synthetic in a direction real warehouses go anyway, since a production
promotion fact carries descriptive attributes rather than three integers. Everything else is held
constant: same table, same 36,786,524 rows, same global ordering, same warehouse.

| Variant | Sort key width | exec ms | task ms | task/exec | **spill** | spread |
|---|---:|---:|---:|---:|---:|---:|
| `W000-global-rank` | 10 B (native) | 5,093 | 4,703 | 0.92 | **0** | 6% |
| `W128-global-rank` | 138 B | 7,123 | 6,755 | 0.95 | **0** | 1% |
| `W256-global-rank` | 266 B | 19,735 | 19,458 | 0.98 | **58.2 MiB** | 7% |
| `W512-global-rank` | 522 B | 28,308 | 27,692 | 0.98 | **91.3 MiB** | 3% |

**Spill onset is between a 138-byte and a 266-byte sort key at 36,786,524 rows on a 2X-Small.**
The wall clock does not degrade gracefully across that boundary: 138 → 266 bytes is 1.9x the
bytes and **2.8x the time**, because past the threshold the sort is paying for disk round-trips
on top of the extra bytes. Every measured run of W256 and W512 spilled — not just the median —
which is asserted by `test_recorded_spill_evidence_is_internally_consistent`.

### Measurements — mitigations against W512

Baseline is W512: 28,308 ms, 91.3 MiB spilled. Each variant differs from it in exactly one
respect. The warehouse cannot be resized, so none of these is "add memory".

| Variant | Intervention | Rows into the sort | exec ms | **spill** | vs W512 | spread |
|---|---|---:|---:|---:|---:|---:|
| `W128-global-rank` | narrow the sort key 522 B → 138 B | 36,786,524 | 7,123 | **0** | **4.0x faster** | 1% |
| `M2-filtered` | `WHERE WEEK_NO BETWEEN 55 AND 101` | 18,959,270 | 6,122 | **0** | **4.6x faster** | 26% |
| `M3-preagg` | rank 3,934,433 `(PRODUCT_ID, STORE_ID)` pairs, not 36.8M fact rows | 36,786,524 read, 3,934,433 sorted | 2,259 | **0** | **12.5x faster** | 13% |
| `M1-partitioned` | `PARTITION BY STORE_ID` (115 partitions) | 36,786,524 | 30,558 | **118.7 MiB** | **1.08x slower**, 30% more spill | 4% |
| `M4-repartition` | `REPARTITION(1024)` ahead of the window | 36,786,524 | 35,502 | **99.1 MiB** | **1.25x slower**, 9% more spill | 0% |

### What actually happened

**Three interventions eliminated spill entirely.** All three reduce what the sort must hold, by
three different routes: fewer bytes per row, fewer rows, or a coarser grain. None of them touches
a config, because there is no config to touch.

**Two interventions made it worse, and both were meant to help.**

`M1-partitioned` is the one I got wrong. Partitioning a window is the standard cure for a global
ordering, and it produced **30% more spill** and slightly more wall clock. `STORE_ID` has 115
distinct values in `causal` distributed 1.4x, so each partition still holds ~320,000 rows of
522-byte key — and the partitioning adds a keyed shuffle that has to move the wide key across the
network, which the range-partitioned global sort did not. Splitting work into 115 pieces does not
help when the constraint is the width of each row rather than the count of them.

`M4-repartition` was expected to fail and did: **25% slower, 9% more spill**, with the
parallelism proxy at 1.35 — the extra partitions produced extra task time and nothing else. A
global `ORDER BY` has exactly one output partition by definition, so raising the input partition
count cannot reduce what the task owning the ordering must hold. Since `spark.sql.shuffle.
partitions` is unsettable (C1), the `REPARTITION` hint is the only lever on this platform that
resembles a partition-count knob, and this measures it as useless against this class of problem.

### What changed as a result

- Window functions in the pipeline must carry an explicit `PARTITION BY` **and** project only the
  columns the frame needs. The projection matters more than the partitioning: W128 (narrower key,
  same global ordering) beat M1 (partitioned, wide key) by 4.3x.
- "Increase the shuffle partitions" is off the standard remediation list for this platform. It is
  not merely unavailable as a config — as a hint, it is measurably counterproductive here.
- Filters that can move ahead of a sort do. 4.6x for a `WHERE` clause is the cheapest measurement
  in this document.

---

## Experiment 5 — Join coverage, and a correction to a prior finding

**Hypothesis.** From [`dataset-findings.md` F2](dataset-findings.md): an inner join to `causal`
drops 1.4% of transaction lines and 80% of stores — a small revenue loss hiding a large
dimensional one.

**Method.** `LEFT JOIN` the full fact table to `causal` on the composite key, count matched and
unmatched.

| | Measured |
|---|---:|
| Transaction lines in | 2,595,732 |
| Rows out of the LEFT JOIN | **2,596,590** |
| Lines with a promotion match | **564,632** |
| Match rate | **21.75%** |
| Distinct stores in | 582 |
| Distinct stores matched | **115** (19.8%) |

### What actually happened — the prior finding is right about stores and wrong about lines

The 1.4% figure is the loss from a **store-level** join: the 115 covered stores do carry 98.6% of
transaction lines. But the join under test keys on `(PRODUCT_ID, STORE_ID, WEEK_NO)`, and a
specific product was not necessarily on promotion in that store that week. **The composite-key
inner join drops 78.25% of transaction lines**, not 1.4%.

That reframes the failure mode rather than softening it. A 1.4% loss is the dangerous one because
nobody notices; a 78% loss will be noticed immediately. The genuinely dangerous artefact here is
the **store count**, which drops 582 → 115 under both joins.

There is a second finding hiding in the row count. The LEFT JOIN emits **2,596,590** rows from
2,595,732 inputs — **858 extra**. `causal` has 36,786,524 rows but only 36,771,279 distinct
composite keys, so 15,245 duplicate keys fan the fact table out. A join documented as "1:many"
is silently many:many, and the only symptom is a row count that is 0.03% high — well inside what
a reconciliation check with a tolerance would pass.

### What changed as a result

- The coverage figures cited for this join are the composite-key ones (21.75% of lines, 115 of
  582 stores), not F2's store-level ones. Two different joins, two different numbers, and
  quoting one for the other is exactly the class of error F2 was written to prevent.
- `causal` must be de-duplicated to its composite key before any join that claims 1:many
  cardinality, or the fan-out must be asserted. 858 rows is not a rounding error; it is a
  cardinality bug with a small blast radius today.
- `V5-left-join` costs **11% over the inner join** (2,367 ms vs 2,125 ms, 10% spread) and keeps
  all 582 stores. That is the price of correctness here, measured rather than argued.

---

## What could not be measured, and why

| Wanted | Why not |
|---|---|
| Per-task duration distribution — Databricks' own skew definition (max > 1.5x p75) | No surface on serverless exposes it: no Spark UI, no REST field, no system-table column. Substituted a documented proxy (C4) and never called it the same thing. |
| Bytes per shuffled row | `shuffle_read_bytes` is 0 for all 325 recorded statements while `spilled_local_bytes` populates on the same rows (C5). Computed from the UnsafeRow layout instead, with a break-even column on every conclusion that depends on it. |
| Actual `spark.sql.shuffle.partitions` in effect | `auto` on serverless and not readable — `SET spark.sql.shuffle.partitions` fails on read as well as write. Reported the distribution across N ∈ {16, 64, 200, 1024} instead of asserting one. |
| A cold-cache baseline | `CACHE TABLE` / `UNCACHE TABLE` are unsupported and `read_io_cache_percent` was 100 from the first run onward (C7). Warm is the only condition available, so it is stated as held constant rather than compared against. |
| AQE on/off A/B | No `spark.sql.adaptive.*` conf is settable (C1). Reasoned from AQE's documented trigger conditions against measured partition sizes, and separately showed the plan contains no shuffle join for it to act on. |
| Whether a 256 MB post-shuffle partition changes anything here | The hottest `STORE_ID` partition holds 75,576 rows; at 40 bytes/row it needs **88.8x** as many to reach 256 MB. Out of scope on a shared Free Edition quota; the break-even column states the requirement rather than guessing at the outcome. |

## Hypotheses that turned out false

| # | Hypothesis | Outcome |
|---|---|---|
| H1 | The composite join key inherits the dataset's 9,926x skew | **False.** 21x at key level, **1.08x** at partition level. |
| H2 | AQE declines to split because of the 256 MB byte condition | **True, but incomplete.** Also inapplicable: the plan is a broadcast hash join, so there is no shuffle join to optimise. |
| H3 | Broadcasting the small side is an improvement | **False.** Identical plan, 4% difference against a 6% spread. The optimiser was already doing it. |
| H4 | Salting the skewed key improves wall clock | **Not supported.** Parallelism proxy +71% (0.80 → 1.37); wall clock −7.5%, inside a 28% spread. |
| H5 | The 36.8M-row table will spill on a 2X-Small | **False** on native data — four different pressure sources, zero bytes. Spill required widening the sort key past ~138 bytes/row. |
| H6 | Partitioning the window relieves spill | **False.** 30% *more* spill and 8% more wall clock than the global ordering it replaced. |
| H7 | More shuffle partitions relieve spill | **False**, as expected: 25% slower, 9% more spill. Falsified deliberately. |
| H8 | Aggregation skew is visible at matched volume | **Not supported.** The more skewed key ran with higher parallelism; the effect is below the warehouse's resolution at this scale. |
| H9 | The composite-key inner join drops 1.4% of lines (from F2) | **False.** It drops **78.25%**. F2's figure is for a store-level join. |

## Quota

No quota limit was hit. `system.query.history` reports **366 statements** and **33.0 minutes of
execution time** on this warehouse across the session; that figure also covers the capability
probes and the system-table queries themselves, not just the measured variants.

Cost control was structural rather than incidental: a 180 s hard deadline with automatic
`cancel_execution` on every statement, each expensive variant priced with a single run before
committing four, and any candidate over ~30 s reduced in scope rather than left to run. One
statement was cancelled under that policy — an unbounded `STORE_ID`-only join whose intermediate
would have been on the order of 8x10^11 rows.

## Reproducing

```bash
export DATABRICKS_CONFIG_PROFILE=dng
export PYTHONPATH=src
.venv/bin/python -m retail_lakehouse.perf.cli load      # ~16 s
.venv/bin/python -m retail_lakehouse.perf.cli probe     # capability probes
.venv/bin/python -m retail_lakehouse.perf.cli profile   # key + partition distributions
.venv/bin/python -m retail_lakehouse.perf.cli skew      # V1-V5 + failed calibration
.venv/bin/python -m retail_lakehouse.perf.cli smj       # V6-V8 forced sort-merge
.venv/bin/python -m retail_lakehouse.perf.cli agg       # A1-A4
.venv/bin/python -m retail_lakehouse.perf.cli spill     # N1-N4, W000-W512, M1-M4  (~10 min)
sleep 420                                               # system table lag: 349 s to 40+ min
.venv/bin/python -m retail_lakehouse.perf.cli backfill
.venv/bin/python -m retail_lakehouse.perf.report        # regenerates every table above
```

`notebooks/perf/skew_and_spill_lab.sql` is the interactive counterpart: it re-derives the
data-shape measurements and shows the Query Profile for each variant, which is the only way to
check the physical-plan claims by hand.
