# Silver — what building the conformed layer actually proved

Five silver datasets, three ops audit tables, 25 governed quality rules. The numbers below are
measured on `dng_dev` after update `6e581132`, not estimated.

| table | rows | note |
|---|---:|---|
| `silver.fact_basket_line` | 198,013 | one row per `event_id` |
| `silver.fact_basket_line_quarantine` | 6 | with the rule that rejected each |
| `silver.dim_product_scd2` | 94,190 | 92,353 keys, 1,837 with a second version |
| `silver.dim_household_scd2` | 3,340 | 2,500 keys, 801 enrolled, 39 later updated |
| `silver.dim_store` | 582 | 115 with promotion exposure data |

Row conservation, asserted by the pipeline rather than by this document:

```
200,000 bronze = 1,981 duplicates collapsed + 198,013 passed + 6 quarantined
```

Eight findings. Three changed the design — S2, S3 and S7 — and the rest are things that would
have cost an afternoon each to rediscover. S1 is the one worth reading if you only read one.

---

## S1 — The DQX profiler's candidate rules would have quarantined 23.6% of revenue

**Measured.** DQX 0.15.0 generated 23 rules from a 200,000-row extract of the bronze events, all
at `criticality: error`. Applying them without review:

| | value |
|---|---:|
| rows quarantined | 17,927 of 200,000 (9.0%) |
| **revenue quarantined** | **£146,099.63 of £619,942.65 (23.6%)** |
| stores affected | 211 of 260 |
| households affected | 1,886 of 2,337 |

The reviewed ruleset quarantines **6 rows**.

**Interpretation.** The failure is not that the profiler is bad. It is that DQX profiles a
**sample** — 1,000 rows by default — and derives each range from that sample's spread. Every
bound is a statement about 0.5% of the data presented as a statement about all of it. Six of the
eleven generated range rules have a bound the full table already violates on day one:

| column | profiled bound | actual extreme |
|---|---|---|
| `quantity_units` | ≤ 3,893.82 | 48,073 |
| `sales_amt` | ≤ 13.66 | 210.00 |
| `retail_disc_amt` | ≥ −4.23 | −70.00 |
| `transaction_ts` | ≥ 2024-01-30 | 2024-01-01 |
| `week_no` | ≥ 5 | 1 |
| `transaction_time_hhmm` | 495–2355 | 0–2359 |

**The tell is worth memorising.** The upper bound on `quantity_units` is `3893.8152094863253` — a
count of items with thirteen decimal places. Nobody writes that by hand, and nobody skims a
generated file closely enough to notice. A fitted bound on a discrete column is a fitted bound,
and fitted bounds encode last Tuesday rather than the business rule.

**What changed.** `docs/quality/rule-review.md` records a decision for all 23 candidates: 8
accepted, 12 amended, 3 rejected. They collapse into 16 enforced rules — several null checks merge —
alongside 9 rules no profiler could have proposed. `tests/unit/test_quality_rules.py::test_generated_rules_require_review`
fails the build if a candidate has no decision, if a decision names a rule that does not exist, or
if a profiler-derived rule reaches the enforced set without a review row. That is QLT-005 made
mechanical rather than cultural.

The three rejections are all range rules over identifiers — `store_id`, `product_id`,
`household_key`. A numeric range over an identifier rejects store 40000 for being *large*, not for
not existing. The correct check is referential integrity, and it lives in `ops.join_coverage`.

---

## S2 — F6 is right about the direction and wrong about the mechanism

**Measured.** `dataset-findings.md` F6 says a `SALES_VALUE` of 0 "is a fully coupon-offset line".
In the 200,000-row event stream:

| | rows |
|---|---:|
| `sales_amt = 0` | 1,415 |
| of those, with **no** discount of any kind | 766 |
| of those 766, with a positive quantity | 62 |

So the majority of zero-value lines have nothing offsetting them, and 62 of them charged nothing
for something.

**Interpretation.** The finding's conclusion — do not quarantine zero-value lines — still holds.
Its stated reason does not, and the difference matters: a rule written to "validate zero-value
lines against a matching coupon discount", as F6 proposes, would quarantine 766 rows.

**What changed.** `zero_sales_has_offsetting_discount` is a **`warn`** rule, not an `error` one,
and it excludes zero-quantity voids. It fires on 62 rows and quarantines none. The reasoning
generalises past this column: quarantining a row protects downstream numbers from it, and these
rows carry zero revenue — so quarantine protects nothing while removing 62 real lines from
basket-size and items-per-visit metrics. **Quarantine is for rows that are unusable, not for rows
that are unexplained.**

The two rules that *do* quarantine were both authored, not generated, and both assert a
relationship between columns that a single-column profiler cannot see:

| rule | rows | what it catches |
|---|---:|---|
| `revenue_requires_quantity` | 4 | charged money, zero units |
| `retail_discount_is_not_a_surcharge` | 2 | a discount that adds money |

One row, `19336-24`, fails both — 6 quarantined rows from 7 violations. It is a `0 units` line
with `sales_amt = 0.77` and `retail_disc_amt = +0.77`.

Two rows in 200,000 is a 0.001% defect rate. That is exactly why it needs a rule: a sign error at
that rate is invisible in every aggregate and corrupts the discount ledger permanently.

---

## S3 — A static snapshot cannot demonstrate SCD Type 2, and the failure is silent

The dunnhumby seed ships `product` and `hh_demographic` as single snapshots. Loading either once
into an `AUTO CDC ... STORED AS SCD TYPE 2` target produces a dimension where every key has
exactly one version.

**Why that is worse than useless.** Every point-in-time test then passes — including the broken
join. A fan-out of ×1 is indistinguishable from no fan-out, so a naive join on the natural key
alone returns exactly the right row count, and the test suite goes green on a query that will
inflate every measure in the warehouse the moment a real change arrives.

This is the same class of error as B1 in `bronze-findings.md`: the pipeline was correct and the
*experiment* was wrong.

**What changed.** Both dimensions are fed a synthesised change feed, labelled at source. Every row
carries `change_reason` and `is_synthetic_change`; the table comments say so in the first sentence;
and the reclassified rows are literally suffixed `/ RECLASSIFIED`. Product: 1,837 of 92,353 keys
gain a second version at 2025-01-01, halfway through the fact window. Household: all 2,500 exist
from 2023-01-01, 801 gain demographics at 2024-07-01, and 39 of those change household size at
2025-03-01.

The enrolment date is the interesting one. Backfilling demographics to the beginning of time would
make historical analysis look tidier and would attribute knowledge the business did not have yet —
leakage, and precisely what MLR-006 exists to prevent. A purchase made before enrolment resolves,
correctly, to a version with `has_demographics = false`.

**Measured on the deployed tables**, joining 198,013 fact rows to `dim_product_scd2`:

| join | rows | revenue |
|---|---:|---:|
| fact table alone | 198,013 | £613,396.36 |
| point-in-time (`key AND window`) | 198,013 | £613,396.36 |
| **naive (`key` only)** | **201,476** | **£623,863.52** |

One missing predicate, **+3,463 rows and +£10,467.16 — revenue overstated by 1.71%**. And that is
with only 2% of product keys carrying a second version. On a real merchandising hierarchy where
reclassifications accumulate over years, the same query returns a number that is wrong by a
multiple, and every row in it looks correct.

1.71% is the dangerous magnitude. It is too small to notice and too large to ignore — well inside
the range a business would attribute to seasonality, and permanent.

The unit suite proves the assertion is not vacuous rather than trusting it. Against a fixture where
one key has three change records and two versions, `naive_key_join` turns 6 fact rows into 11 and
overstates revenue by 71% (210 → 360). The point-in-time join returns 6.

---

## S4 — `AUTO CDC` does not produce `is_current`, so ADR-0007's clustering key does not exist

**Measured.** ADR-0007 specifies `silver.dim_product_scd2` clustered by `(product_id,
is_current)`. `AUTO CDC ... STORED AS SCD TYPE 2` generates `__START_AT` and `__END_AT`; "current"
is `__END_AT IS NULL`. There is no `is_current` column to cluster on.

**Interpretation.** The ADR was written before the table existed, which is the normal and correct
order — but it means the key was chosen from a mental model of the output rather than from the
output. The predicate a point-in-time join actually issues is an equality on the key plus a range
scan over the window, so the honest key is `(product_id, __START_AT)`.

Clustering on `__END_AT` would be worse than the alternative, not better: it is NULL for 92,353 of
94,190 rows, so it produces one enormous cluster and a scattering of tiny ones.

**What changed.** Both Type 2 dimensions cluster on `(key, __START_AT)`. ADR-0007's decision table
should be corrected; the *rule* it states — cluster on what the query predicates on — is what
produced this deviation, so the ADR is right and its worked example is stale.

---

## S5 — A pipeline cannot observe its own completion, and AUTO CDC does not report `num_output_rows`

Two separate discoveries about `ops.dq_metrics`, which projects the pipeline event log into one
row per dataset per update.

**First: it trails by one update.** `ops.dq_metrics` was computed at 00:08:43; the
`fact_basket_line` flow completed at 00:08:54. The materialized view read the event log before the
events describing that run had been written, so update N's metrics only become visible during
update N+1. This is not a scheduling bug to fix — the completion event for the last flow is
necessarily written after the last flow completes, so no ordering within the update can help. For
a daily pipeline it means the quality report is T+1, which is worth stating out loud rather than
discovering when someone asks why today's row is missing.

**Second: the obvious metric is always NULL for the tables that matter.** AUTO CDC flows report
`num_upserted_rows` and `num_deleted_rows`, not `num_output_rows` — reasonably, since an upsert has
no single output count. A metrics view reading only `num_output_rows` shows every CDC-fed table
writing nothing, forever, and looks perfectly healthy doing it.

**Third, and this one was a bug in the first version of the view.** A `flow_progress` event is
emitted repeatedly as a flow advances, and its metrics are **cumulative for the batch**, not
increments. Summing them over-counts by however many progress events the platform happened to
emit. The first version of `ops.dq_metrics` reported 990,065 rows written to `fact_basket_line` —
which is 198,013 × 5, and 198,013 was the correct answer.

What makes this worth recording is that the wrong number was not obviously wrong. It was large,
plausible, monotonic across runs, and in the right ballpark for a busy pipeline. It was caught by
dividing it by the row count and getting a suspiciously round 5, not by anything structural.
The fix is to collapse each `(flow, batch_id)` to its high-water mark before summing across
batches — `max` within a batch because the reports are cumulative, `sum` across batches because a
streaming flow genuinely processes several.

Corrected output for update `6e581132`:

| dataset | `output_rows` | `upserted_rows` | rules | passes | failures | pass rate |
|---|---:|---:|---:|---:|---:|---:|
| `silver.fact_basket_line` | — | 198,013 | 5 | 993,965 | 62 | 99.9938% |
| `silver.fact_basket_line_quarantine` | — | 6 | 0 | 0 | 0 | — |
| `silver.dim_product_scd2` | — | 94,190 | 3 | 282,570 | 0 | 100% |
| `silver.dim_household_scd2` | — | 3,340 | 2 | 6,680 | 0 | 100% |
| `silver.dim_store` | 582 | — | 2 | 1,164 | 0 | 100% |
| `ops.join_coverage` | 4 | — | 2 | 8 | 0 | 100% |
| `ops.row_conservation` | 1 | — | 2 | 2 | 0 | 100% |

`passes` counts rule *evaluations*, not rows — 1,164 is 582 rows × 2 rules — which is why the
per-rule map matters more than the total.

The event log also carries an internal placeholder flow named
`pipelines.flowTimeMetrics.missingFlowName`, which is not a dataset. Filtering to names in the
pipeline's catalog keeps the grain honest.

**What did work, first time.** Expectations are recorded per rule per flow with exact pass and
fail counts, including for expectations attached to views:

```
zero_sales_has_offsetting_discount  passed 199,932  failed 62   (dataset: basket_line_valid)
week_no_within_seed_window          passed 199,994  failed 0
gate_held_event_id                  passed 198,013  failed 0
row_conservation                    passed 1        failed 0
point_in_time_join_does_not_fan_out passed 4        failed 0
no_orphan_facts                     passed 4        failed 0
```

The last three are the interesting ones: they are single-row assertions on audit tables, and they
fail the update rather than logging a warning.

---

## S6 — Expectations in Unity Catalog: the release note exists, the API does not

Lakeflow's January 2026 release notes say: *"You can now store and manage data quality expectations
directly in Unity Catalog tables, centralizing data quality rules with your data governance
framework. This enables version-controlled, auditable quality rules that can be shared across
multiple pipelines."*

**What we could not find.** No API, no SQL clause, no required table schema, no preview flag, in
any of the three expectations pages in the documentation (`ldp/expectations`,
`ldp/expectation-patterns`, `ldp/developer/ldp-python-ref-expectations`). The release-note bullet
carries no link. `ALTER TABLE ... ADD CONSTRAINT` contains no `EXPECT` clause. The `data-quality`
CLI group is about monitors, not expectations.

**What we did instead**, and it is described as what it is rather than as a native feature: the
documented *reusable expectations* pattern. `retail_lakehouse.quality.publish` writes the reviewed
ruleset from git to `<catalog>.ops.dq_rules`; the silver modules read that table at
graph-construction time and turn it into both physical quarantine routing and `dp.expect_all`
telemetry. Delta history on the rules table is the version log, and every quarantine row carries
the `ruleset_version` that rejected it.

This works on Free Edition. Whether it is *the* feature the release note describes is unknown, and
claiming so would be unverifiable.

The table is not merely a convenience, either. Lakeflow executes each pipeline source file on its
own, so a pipeline module cannot reliably `import` a sibling. A governed table is the only
interface between the two that also comes with grants, history, and cross-pipeline sharing.

---

## S7 — The household dimension has to cover 2,500, or referential integrity is unsatisfiable

F4 measures that 801 of 2,500 households have demographics. The tempting shape is a dimension of
801 rows. It makes QLT-004 impossible: 68% of the fact table would be orphaned, and the reflex fix
— inner-join it away — silently deletes two thirds of the households from every household-level
metric while the revenue total stays plausible.

**What changed.** The dimension is keyed on *household*, contains every household in the seed
transactions, and carries demographics as nullable attributes plus an explicit
`has_demographics` flag. A rule asserts the flag and the payload agree, so "no profile" and
"unknown" stay distinguishable — imputing a modal age band would make the dimension complete and
the model wrong, and nothing downstream could tell.

Measured coverage of the three referential joins, all point-in-time where the dimension is Type 2:

| join | fact rows | joined rows | unmatched |
|---|---:|---:|---:|
| → `dim_product_scd2` | 198,013 | 198,013 | 0 |
| → `dim_household_scd2` | 198,013 | 198,013 | 0 |
| → `dim_store` | 198,013 | 198,013 | 0 |

`joined_rows = fact_rows` is the assertion, not `unmatched = 0`. Zero orphans says nothing about
fan-out; equal row counts say both at once.

And the join that is *not* a referential-integrity join, which is F2 measured rather than
described:

| join | row match | key match |
|---|---:|---:|
| → `causal_data` store coverage | 98.49% | 44.23% (115 of 260) |

That asymmetry is the whole finding. An inner join here loses 1.5% of lines — which nobody
notices — and 56% of the stores in the fact table, which destroys any store-count metric sitting
next to it.

---

## S8 — Two Lakeflow packaging constraints that shape the source tree

Worth recording because both cost a deploy cycle and neither is discoverable from the docs.

1. **`libraries.glob.include` rejects single-asterisk patterns.** `silver/*.py` returns
   *"Single asterisk glob pattern is not supported in included path ... Use a double asterisk"*.
2. **A `libraries` list may contain globs or explicit `file` entries, but not both.** Mixing them
   returns *"Either glob or notebook/file field under libraries in the pipeline settings should be
   set."*

Together they mean a directory is either entirely in the pipeline graph or entirely out of it.
Since the pure Spark helpers must be importable by the unit suite and must *not* execute as
pipeline sources, the split has to be physical: `silver/pipeline/` runs on a pipeline,
`silver/lib/` runs on a laptop.

A third one, cheaper but sillier: `create_streaming_table(schema=...)` takes raw DDL and does not
escape it, so a column comment containing an apostrophe — `'12 units'` — terminates the string
literal and produces a parse error 1,400 characters into a generated statement.

---

## Verified on the deployed tables

`DESCRIBE DETAIL` on all five silver tables, after update `6e581132`:

| table | `partitionColumns` | `clusteringColumns` |
|---|---|---|
| `fact_basket_line` | `[]` | `["transaction_date","store_id"]` |
| `fact_basket_line_quarantine` | `[]` | `["rule_name","transaction_date"]` |
| `dim_product_scd2` | `[]` | `["product_id","__START_AT"]` |
| `dim_household_scd2` | `[]` | `["household_key","__START_AT"]` |

No partitioning anywhere, as ADR-0007 requires.

Column documentation is uneven and the gap is deliberate rather than accidental:
`fact_basket_line` (25 columns) and `fact_basket_line_quarantine` (30 columns) have **zero**
uncommented columns, because both declare an explicit schema — which is also what carries the
currency on every `_amt` column (MOD-006). The three dimensions and the three ops tables have no
column comments at all, because they do not declare a schema. Adding one to each is
straightforward; it was traded against the risk of a schema mismatch failing an update, on a
quota where each failed update is expensive. It is the clearest piece of unfinished work in this
layer.

---

## Re-running changes nothing, which is the point

Update `676a8a41` re-ran the whole graph over unchanged input. Every count is identical:

| | update `6e581132` | update `676a8a41` |
|---|---:|---:|
| `fact_basket_line` | 198,013 | 198,013 |
| `fact_basket_line_quarantine` | 6 | 6 |
| `dim_product_scd2` | 94,190 | 94,190 |
| `dim_household_scd2` | 3,340 | 3,340 |
| `dim_store` | 582 | 582 |
| revenue | £613,396.36 | £613,396.36 |

Row conservation holds identically on both. This is ING-005 and half of MOD-004 (identical row
counts; identical aggregate checksums are the other half and belong with gold).

The dimensions not growing matters as much as the fact table not growing. Their change feeds come
from file streams over the seed volume, so a second run finds no new files, `AUTO CDC` receives
nothing, and no spurious versions appear. A change feed rebuilt from scratch each run — the
obvious implementation — would have added a version per key per run and looked like a working
SCD2 for about a week.

---

## What Free Edition cost, stated plainly

Getting these four updates through took longer than building them. One update sat in
`INITIALIZING` for 49 minutes with **no further events in the log**, on a workspace where the
preceding update reached `INITIALIZING` 1.3 seconds after creation and completed the whole graph
in 79 seconds. `databricks pipelines stop` then took several minutes to return, because the
update it was cancelling had never started.

Free Edition serverless capacity is shared across the account and is not guaranteed. There is no
queue-position signal and no capacity metric to point at, so the only honest description is that
the update did not start — not that it was slow, and not that it failed. Cancelling and
resubmitting got capacity immediately, which suggests the stall is per-update rather than
account-wide, but that is inference from one observation and is not worth more than that.

The practical consequence for anyone running this: budget for a bounded number of pipeline runs
per day and make each one count. Every number in this document was obtained in four updates, and
that was deliberate.
