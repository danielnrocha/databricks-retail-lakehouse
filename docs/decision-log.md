# Decision log

Chronological. ADRs record *what* was decided and why; this log records *when* a decision changed
and what forced the change. The two answer different questions — an ADR read cold cannot tell you
whether it was written from evidence or from assumption.

Entries are append-only.

---

### 2026-08-06 — Platform tier: Free Edition, permanently, not a trial

**Forced by:** wanting an environment that still exists after the first presentation.

A 14-day Premium trial would give multi-workspace isolation, service principals, and the newest
preview features. It would also expire, leaving a public repository nobody — including its
author — can run. Free Edition is permanent and, after checking the current limitation matrix,
supports the CLI, Declarative Automation Bundles, git folders, Unity Catalog, Lakeflow pipelines,
Auto Loader, MLflow 3, model serving, Genie, Apps, and Lakebase.

The gaps that remain (one workspace, one metastore, no service principals, no account console,
5 concurrent job tasks, one active pipeline per type, 2X-Small warehouse) are documented rather
than worked around. See [ADR-0002](adr/ADR-0002-environment-isolation.md).

---

### 2026-08-06 — Dataset: dunnhumby *The Complete Journey*

**Forced by:** needing genuine business semantics, not volume.

Rejected: generic large public datasets (volume without meaning), basedosdados (aggregated, not
basket-level), Databricks sample datasets (every portfolio uses them), fully synthetic (a model
trained on a rule you wrote recovers the rule you wrote).

See [ADR-0003](adr/ADR-0003-dataset-selection.md).

---

### 2026-08-06 — **Reversal:** the amplifier no longer injects skew

**Forced by:** profiling the data instead of assuming its shape.

ADR-0003 was written before download, planning to inject store skew synthetically. The seed's
native skew turned out to be extreme — top 10% of stores at 69.3% of lines, max/median 2,519×,
and `PRODUCT_ID` worse still at 9,926×. `causal_data` at 36.8M rows already makes the wide join
expensive.

Injecting skew on top of real skew would have produced a mitigation validated only against
manufactured distributions. The amplifier's scope is now streaming volume and event ordering.

This is the first reversal in the project and it happened roughly two hours in, which is the
argument for profiling early: the cost of this change was editing one ADR. Three weeks later it
would have been rewriting a generator and re-running a performance lab.

Evidence: [`architecture/dataset-findings.md`](architecture/dataset-findings.md).

---

### 2026-08-06 — Traceability gate treats PLANNED as valid

**Forced by:** the first run of `scripts/check_traceability.py` failing on 48 requirements whose
tests did not exist yet.

The initial gate required every mapped test file to exist. That made CI red from the first commit
and offered only bad ways out: write 48 empty placeholder test files (a suite that looks green
while asserting nothing), or merge everything at once.

Revised: `PLANNED` rows register intent and skip the file check; `PASSING` rows claim proof and
must have the file. The gate still fails on an unmapped requirement or a stale row.

The general lesson, recorded because it will recur: a quality gate that cannot be satisfied
incrementally will be disabled, and a disabled gate is worse than a lenient one.

---

### 2026-08-06 — Rate parameters state their units, and a test enforces them

**Forced by:** the same defect shipping three times in one sitting, in three different parameters,
each passing review because the code matched the variable name.

| Parameter | Intended | Implemented as | Configured | Observed |
|---|---|---|---|---|
| `DuplicateDelivery.fraction` | share of output | per-basket probability of replaying the whole window | 0.5% | **73%** |
| `LateArrival.beyond_watermark_fraction` | share of output | share of *late* events | 0.200% | **0.003%** |
| `SchemaDrift.*_at_event` | a point inside the run | an absolute offset, unchecked against run length | fires at 250k | **never fired** at 200k |

The first was caught only because a manual run printed an absurd number. The second would have
gone unnoticed — 0.003% against 0.200% reads like sampling noise unless you compare it to what was
asked for. The third made a run look *clean*, which is the worst failure of the three: a clean
result gets read as evidence about the pipeline when it is evidence about the config.

The duplicate flood also cascaded into the sampler's territory, dropping observed store coverage
from 582 to 400 and the top-decile share from 67% to 53%. A skew experiment run on that stream
would have measured the emitter's bug and attributed it to grocery retail.

**Changes:** every `*_fraction` is now documented as a share of the total stream; impossible
combinations are rejected in `__post_init__`; drift thresholds beyond the run length raise rather
than silently no-op; and `test_configured_rates_match_observed_rates` asserts configured against
observed for every rate in one place.

The general lesson, which is the reason this entry exists: **"the feature fires" and "the feature
fires at the configured rate" are different claims.** A test asserting the first passes happily
while the second is wrong by two orders of magnitude. Existence checks are the cheapest test to
write and the easiest to mistake for coverage.

---

### 2026-08-07 — The ML gate refused the model, and the model stayed refused

The household-lapse model scored PR-AUC 0.1420 against a recency-only baseline at 0.3846 — a
relative lift of −63.1% against a required +10%. Not registered.

The obvious move was to shorten the outcome window from day 547 to day 660, where the base rate
rises from 3.0% to 12.4% and the problem becomes learnable. That is choosing the experiment to fit
the desired result, so it was not done and is recorded as not done. Twenty-three weeks was chosen
on business reasoning before any model existed: seven weeks of grocery silence means someone went
on holiday.

The deeper finding is that the evaluation was underpowered from the start — 76 positives, 19 in
the test split — so it could not have resolved the difference either way. And the base rate is
structural: dunnhumby selected 2,500 *frequent shoppers*, so the dataset's own inclusion criteria
make its churn problem nearly unlearnable.

---

### 2026-08-07 — The agent eval failed, and the fix was to the specification, not the threshold

First run: unanswerable 100%, trap 100%, empty 100%, **answerable 33%**. The agent handled every
hard case and failed the easy ones, because the expectations were written as exhaustive lists and
the judge read extra *correct* columns as non-compliance.

Lowering `THRESHOLDS` from 90% to 80% would have been tuning. Rewriting "report X and Y" as "MUST
INCLUDE X and Y; extra facts from the same tool are welcome" fixes a statement that said something
other than what it meant. The thresholds were not touched.

The honest caveat, recorded because it is the shape of p-hacking: the correction was made *after*
seeing a failure. The only defence is that the change is inspectable and the bar did not move.

---

### 2026-08-07 — mypy had been failing CI for four runs

Root cause was three-layered: `python_version = "3.11"` while everything actually runs 3.12, so
mypy rejected numpy's own bundled stubs; missing stubs for pandas/sklearn/pyarrow/openai; and CI
installing a dependency subset that excluded `agents`, so it type-checked less code than a
developer has.

Fixing those surfaced 81 errors. 46 were in Lakeflow pipeline modules, which mypy **structurally
cannot** analyse — they import `pyspark.pipelines`, which exists only inside the Databricks
runtime. Those are excluded, with the config comment stating explicitly that this excludes code
the tool cannot check rather than code the tool dislikes. The second kind of exclusion is how a
type gate quietly stops gating.

The remaining 35 were genuine and were fixed. The dominant pattern was the SDK typing every id and
manifest as optional, narrowed five slightly different ways in five modules — which is how one of
them ends up being the copy that does not check.

---

### 2026-08-07 — Four ADRs were never written, and the rule meant to prevent fiction produced silence

**Forced by:** a sweep for dangling references while updating the documentation.

`docs/adr/README.md` listed ADR-0006, 0008, 0010 and 0011 as *Pending (Phase 5 / 8 / 9)*. Those
phases are finished — silver, ML and the agentic layer are all merged. The status was not stale in
the ordinary sense; it asserted something false about the project's own state, in the index a
reviewer opens first.

The rule that produced it is good and is kept: an ADR written retroactively describes the choice
you can still justify, not the one you actually faced. What it did not anticipate is a phase
*finishing* without its record. The gap is now labelled **Not written — shipped** and the index
names, per ADR, where the reasoning was captured contemporaneously — because assembling an ADR from
evidence recorded at the time is a different act from reconstructing one from memory, and only the
second is the thing the rule forbids.

Two related dangling claims fixed in the same pass. The North Star's reading order sent a reviewer
to `docs/runbooks/`, which does not exist and never did. And ADR-0007 pointed at ADR-0008 as though
it could be read. Both are now stated as gaps rather than as pointers.

The pattern, for the third time in this project: **the failure mode of a rule is what it does when
nobody applies it.** A gate whose skip renders green, an existence check that passes while the rate
is wrong, and now a documentation rule whose correct application produces an empty directory.

---

### 2026-08-07 — `recency_days` was null on every row, and GOV-001 passed on it

**Forced by:** writing a lapse KPI for MOD-005 and querying the column it depends on.

`agg_household_rfm.recency_days` was NULL on all 2,337 rows while `first_seen_date` and
`last_seen_date` were populated. The cause is a one-line shape error with a non-obvious mechanism:

```python
as_of = fact.agg(F.max("transaction_date")).collect()[0][0]   # eager, at graph-plan time
... F.datediff(F.lit(as_of), F.max("transaction_date"))
```

A Lakeflow function body is evaluated when the graph is **planned**, and at that moment
`fct_basket_line` is being built by the same update and holds no data. `collect()` returned `None`,
`F.lit(None)` typed the anchor as null, and `datediff` propagated null through every row **without
erroring**. Fixed by cross-joining a single-row aggregate instead of collecting one. After the
rebuild: 0 nulls, range 0–691, mean 133.9, and 665 of 2,337 households lapsed at the 161-day window.

Two things make this worth a log entry rather than a commit message.

**GOV-001 passed on this column.** It carries a comment — "Days between the household's last
transaction and the dataset's own maximum date" — describing behaviour it did not have. *Every gold
column is documented* and *every gold column is correct* are different claims, and the first is the
one this project had a gate for. Documentation coverage is not data quality, and a fully documented
null column is the cleanest possible demonstration of that.

**An eager action inside a declarative definition is a shape error, not a logic error.** Nothing
about the line is wrong in isolation; it is wrong because of *when* it runs. No test failed, no
exception was raised, and review would have to reconstruct the graph's evaluation order to see it.
The general rule: in a declarative pipeline, a value derived from a table in the same graph must be
expressed as part of the query, never collected into Python.

---

### 2026-08-07 — GOV-003 and MOD-005 implemented; `units_sold` was measuring coupons

Both features had been verified available (below). Implemented now: five domains over seven gold
assets via a governed tag policy that `scripts/publish_governance.py` owns — closing the gap where
the policy existed only because a session created it by hand — and twelve KPIs across two metric
views. Evidence in [`architecture/governance-findings.md`](architecture/governance-findings.md).

Two corrections were forced by looking at the numbers rather than at the structure.

`units_sold` was registered as `SUM(quantity_units)`. It published cleanly, resolved cleanly, and
returned 20,319,550 units across 21,479 baskets — **946 units per basket**. 98.7% of it came from
1,776 `COUPON/MISC ITEMS` lines carrying quantities up to 48,073, where the column holds a coupon
face value rather than a count of items. Merchandise alone is 254,898 units, 11.87 per basket.
**This is a mis-stated definition being corrected, not a threshold being moved to obtain a nicer
number** — the measurement came first and is reproduced in the KPI's definition text so a reader
can disagree with it. Saying which of the two it is remains mandatory, because from outside they
are identical.

The second was my own test failing on my own register: `test_every_kpi_states_a_unit_a_decision_and_a_definition`
rejected `baskets` for a 22-character definition. The fix was to write the definition, not to lower
the bound — and writing it surfaced something worth keeping. `COUNT(DISTINCT basket_id)` sliced by
`promo_exposure` gives 20,190 / 8,665 / 6,082 / 4,995 / 561, summing to **40,493 against a true
total of 21,479**, because one basket has lines in several exposure buckets. That non-additivity is
the concrete argument for a governed metric definition over a well-named summary table: the engine
re-derives the count per grouping, where a pre-aggregate would be right at one grain and quietly
wrong at every other.

---

### 2026-08-07 — **Correction:** ING-004 was not blocked; the pipeline needed cancelling, not retrying

The entry below, written the same afternoon, concluded that ING-004 could not be proven on this
tier. That conclusion was wrong, and the original text is kept because the reason it was wrong is
the useful part.

Once the stalled update was **cancelled** — not retried — the pipeline returned to `IDLE` and the
identical T3 request completed in 52.5s and T4 in 21.8s. Nothing about the request changed. Five
`PYTHON_REPL_CREATION_FAILED` retries and a thirty-five-minute `INITIALIZING` stall had all been
attempts to push through a queue that was never going to move; one cancel produced a result in
under a minute. The rule this yields, which is now in
[`architecture/drift-findings.md`](architecture/drift-findings.md) F-D2: **a Free Edition pipeline
update that has not left `INITIALIZING` within a few minutes is waiting for capacity that is not
coming — cancel and resubmit.** This project had already recorded a 49-minute stall with the same
resolution and did not apply it.

Results: T3 evolved to a third schema version with `transaction_time` present; T4 rescued **49,468
rows** carrying `{"quantity": "2 units", …}`, with the row count preserved at exactly 200,000. Both
requirements are now `PASSING` against `tests/integration/test_schema_drift.py`.

The 49,468 was checked against the source rather than reported: files 300–399 hold 50,000 events of
which exactly 49,468 carry a string `quantity`, the 532 difference being pre-retype events pushed
into later files by the late-arrival buffer. The convenient explanation — "roughly all of them were
rescued" — would have been an invented rationalisation for a number nobody had verified, and this
project has now been wrong that way often enough that the check is not optional.

What stands from the original entry: the harness finding about `CANCELED` (F-D1), and the warning
that an unearned zero reads exactly like a clean result. The latter is what made the wrong
conclusion legible as provisional rather than final.

---

### 2026-08-07 — Staged ingestion proved ING-003 and was stopped by capacity before ING-004

**Forced by:** Finding B1 — a backfill cannot demonstrate schema evolution, because Auto Loader
samples the directory rather than reading in arrival order, so the post-drift shape becomes the
initial schema.

The landing volume and the schema location were cleared, files were uploaded one tranche per drift
point, and only `basket_line_events_raw` was refreshed via `refresh_selection` so silver and gold
did not rebuild over the 36.8M-row causal table on each run.

T1 (pre-drift, 50,000 rows) gave the control the backfill never had: exactly one version under
`_schema/_schemas/`, `_rescued_data` null on every row, no `loyalty_tier`. T2 evolved it — two
versions, the new column present, the earlier rows null for it, run completed. **ING-003 is
measured.** T3 and T4 are not: five consecutive `PYTHON_REPL_CREATION_FAILED` initialisation
failures, then a sixth attempt whose auto-restarted update sat in `INITIALIZING` past twenty-five
minutes with `databricks pipelines stop` timing out against it. The SQL warehouse stayed healthy
throughout, so this is pipeline compute, not the account. Stopped there rather than keep spending a
shared daily quota whose exhaustion removes all compute until the next day.

**The finding worth keeping is about the harness.** Schema evolution does not present as a failed
update or a completed one — the flow terminates, the update goes to **`CANCELED`**, and Lakeflow
auto-starts a successor with `cause: SCHEMA_CHANGE`. The first driver treated `CANCELED` as
terminal and reported a failure on the one event ING-003 exists to prove is survivable. Any
orchestration polling for `COMPLETED` inherits that, and so would a test asserting the update
completed: it would go red exactly when the pipeline behaved correctly.

**And the negative result must not be misread.** `_rescued_data` is still zero, and that is *not*
evidence that nothing gets rescued — it is evidence that the tranche which would populate it was
never ingested. An unearned zero reads exactly like a clean result, which is the same trap as the
drift scenario that never fired at 200k events (2026-08-06). ING-004 stays `PLANNED`, and so does
ING-003, because a measurement in a findings document is not a test.

Evidence: [`architecture/drift-findings.md`](architecture/drift-findings.md).

---

### 2026-08-07 — UC Domains and UC Metric Views are both available; four negative probes said otherwise

**Forced by:** GOV-003 and MOD-005 having sat `PLANNED` with "availability unverified", which is a
polite way of recording that nobody had run the command.

Both features exist on Free Edition and both are enforced rather than cosmetic. A metric view
refuses `SELECT measure_column` without `MEASURE()`; a governed domain tag refuses a value outside
its policy's allowed list. Evidence in
[`architecture/governance-findings.md`](architecture/governance-findings.md).

The reason this is in the log rather than only in the findings file is the search that preceded it.
Domains returned *nothing* from the CLI, nothing from the SDK, and `No API found` from four REST
paths — `/api/2.1/unity-catalog/domains`, `/api/2.0/unity-catalog/domains`,
`/api/2.1/unity-catalog/data-domains`, `/api/2.0/lineage-tracking/domains`. Four negative results
agreeing with each other read like a conclusion. They were four spellings of one guess, and the
feature was at `/api/2.0/domains` the whole time.

The transferable rule: **agreement among probes is not independent evidence when the probes share
an assumption.** Enumerate the surface before concluding from its silence. This is the same error
as the perf lab's comment-based cache busting — 84 internally consistent runs, all meaningless —
in a different costume.

A second finding fell out of it and is a fault of this repository rather than the platform. The
`dng_domain` tag policy and its domain record already existed in the workspace, created by hand at
03:46 the same morning, described as "Business domain assignment for gold assets (GOV-003)".
**No committed artifact creates them.** A fresh account following `make bootstrap` would not have
them, so GOV-003 would have passed here and failed everywhere else — green on the author's machine
only, which is worse than red.

---

### 2026-08-07 — The prod bundle target had never validated, and nothing could have noticed

**Forced by:** running `databricks bundle validate -t prod` for the first time, as the instrument
for ENV-002.

```
Error: target with 'mode: production' cannot include a pipeline with 'development: true'
```

`resources/bronze_pipeline.yml` had pinned `development: true` since the file was written. Every
prod deploy would have died at its first validate step. The reason it went unnoticed for the whole
life of the file is worth more than the fix: CI's `bundle` job is gated on `vars.DATABRICKS_HOST
!= ''`, that variable is unset, and a skipped job renders as a *green* check. The gate that would
have caught this was configured in a way that made its absence look like success.

The fix is to delete the field rather than to make it conditional. `mode: development` sets
`pipelines_development: true` through its presets and `mode: production` leaves it false, so
restating the value in the resource only created a way for the resource and the target to
disagree. `test_no_pipeline_resource_pins_development_mode` now fails if it comes back.

The general lesson, which is the second time this project has hit it: a gate whose skip condition
is indistinguishable from a pass is not a gate. The traceability gate learned the same thing in a
different shape on 2026-08-06.

---

### 2026-08-07 — The `test` target moved to `mode: production`

**Forced by:** ENV-003 claiming more than the configuration supported.

ENV-003 says "the deployed artifact is the tested artifact". Under `mode: development` the `test`
target did not resolve to the same shape prod would receive. Measured, by diffing the resolved
configurations leaf by leaf, eight keys existed only on test:

| Key | test (before) | prod |
|---|---|---|
| `presets.name_prefix` | `[dev daniel_rocha] ` | absent |
| `presets.pipelines_development` | `true` | absent |
| `presets.trigger_pause_status` | `PAUSED` | absent |
| `presets.jobs_max_concurrent_runs` | `4` | absent |
| `presets.tags.dev` / `pipelines.*.tags.dev` | `daniel_rocha` | absent |
| `bundle.deployment.lock.enabled` | `false` | absent (locking on) |
| `pipelines.dng_medallion.development` | `true` | absent (false) |
| `pipelines.dng_medallion.name` | `[dev daniel_rocha] dng-medallion-test` | `dng-medallion-prod` |

Two of those change behaviour rather than labelling. A development-mode pipeline reuses compute
between updates and does not retry, so a failure mode prod would recover from was never exercised
on test. And the deployment lock — which serialises concurrent deploys — was off on test and on in
prod, so the one target where two deploys could collide was the one without the guard.

With `test` at `mode: production`, the two resolved configurations now differ *only* by a
`test`→`prod` substring substitution, and every leaf path is present in both.
`test_test_and_prod_differ_only_in_the_environment_axis` asserts exactly that, and it fails on
the old configuration — checked, not assumed.

The cost is accepted rather than waved away: production mode refuses to deploy a dirty tree, so
`test` is no longer usable as an interactive edit-and-deploy loop. That loop is what `dev` is for.
A promotion gate that accepts uncommitted changes is not a promotion gate.

---

### 2026-08-07 — An ENV-003 assertion passed while the property it named was false

**Forced by:** running the negative control instead of trusting a green test.

`test_the_tested_sha_is_an_output_of_the_test_deploy_not_a_constant` originally asserted that the
step emitting `tested_sha` *contained* the string `git rev-parse HEAD`. Mutating the workflow so
the emitted line read `echo "sha=$EXPECTED_SHA" >> "$GITHUB_OUTPUT"` — the exact defect the test
exists to catch, an output recording what the workflow *intended* to check out rather than what it
did — left the test green, because the derivation was still elsewhere in the same script.

The strengthened version captures the variable assigned from `$(git rev-parse HEAD)` and requires
that same variable to be the one written to `$GITHUB_OUTPUT`. Both mutations now fail.

This is the third appearance of one pattern, and it is recorded again because the repetition is
the finding: **an existence check inside the right step is still an existence check.** GEN-005 went
green at 73% duplicates against a configured 0.5% for the same reason. The defence is not "write
better assertions" — it is to run the mutation and watch the test fail before believing it.

---

### 2026-08-06 — Catalogs are created via SQL DDL, not the Unity Catalog REST API

**Forced by:** the REST path failing on an account with Default Storage.

`databricks catalogs create dng_dev` returns:

> Metastore storage root URL does not exist. Default Storage is enabled in your account. You can
> use the UI to create a new catalog using Default Storage, or please provide a storage location.

The REST endpoint requires an explicit `storage_root`. A Free Edition account has no metastore
storage root to point at and cannot create one. The SQL DDL path (`CREATE CATALOG IF NOT EXISTS`
executed through the Statement Execution API) resolves Default Storage automatically and succeeds.

Worth recording because the natural assumption — that the REST API and the SQL surface are
equivalent views of the same operation — is wrong here, and the error message points you at the
UI, which is the one option that cannot be scripted. `scripts/bootstrap_catalogs.py` uses SQL
throughout and explains why in its module docstring.

The script also refuses to drop any of the three environment catalogs. An unguarded `--drop` in a
bootstrap script is one typo away from deleting prod, and bootstrap scripts get run in a hurry.

---

### 2026-08-06 — Seed loads as Parquet via the SDK; `databricks fs cp` abandoned

**Forced by:** `fs cp` taking over ten minutes on a 6.4 MB file.

Measured and isolated in [`architecture/perf-evidence.md`](architecture/perf-evidence.md) (PE-001):
the SDK's `files.upload` is **>200× faster** than `databricks fs cp` for the same file in the same
session. Parquet conversion is a separate, additive win — 15.6× on storage across the seed, 19× on
the 36.8M-row promotion table.

Recording the isolation matters more than the numbers. The first version of this change moved from
"CSV via CLI" to "Parquet via SDK" and looked like a 40× improvement attributable to Parquet. It
was not. Changing two variables and crediting the interesting one is the most common way a
performance story becomes folklore.

One counter-result kept deliberately: `campaign_desc` (30 rows) got **larger** as Parquet, 0.3×.
Blanket format rules are wrong at small scale, and here the cost of being wrong is a few kilobytes
— which is precisely why the rule survives to places where it is expensive.

---

### 2026-08-06 — SHAP removed from the ML dependency set

**Forced by:** `shap` → `numba` → `llvmlite 0.36`, which refuses to build on Python 3.12
(`only versions >=3.6,<3.10 are supported`), taking the whole environment with it.

Explainability is covered by LightGBM's native gain importance plus
`sklearn.inspection.permutation_importance`, neither of which adds a dependency. Taking a fragile
transitive chain for a capability no stakeholder has asked for is a bad trade; revisit only if
per-prediction attributions become a requirement.

Related: LightGBM on Apple Silicon needs the OpenMP runtime (`brew install libomp`) or it fails at
import with a `dlopen` error on `libomp.dylib`. Documented as a prerequisite rather than worked
around.

---

### 2026-08-06 — Discovered but deferred: *Let's Get Sort-of-Real*

dunnhumby also publishes a 4.3 GB dataset covering 117 weeks, ~300M transactions and 47M baskets,
split across nine parts, plus a 50K-customer sample at 417 MB.

Not adopted as the primary seed: it lacks the campaign, coupon, and demographic tables, so it
cannot support the coupon-targeting (D1) or churn (D2) decisions. Registered in
`scripts/fetch_data.py` as `real-50k` for the performance lab if the Complete Journey's 2.6M lines
prove insufficient to make a query plan hurt.

Deferred rather than dismissed: Free Edition runs a 2X-Small warehouse with a daily quota that a
4.3 GB shuffle would plausibly exhaust, taking the whole environment down for the rest of the day.
Worth testing on a bounded subset before committing.
