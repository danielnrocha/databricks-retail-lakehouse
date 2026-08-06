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
