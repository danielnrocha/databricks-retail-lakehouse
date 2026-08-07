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
