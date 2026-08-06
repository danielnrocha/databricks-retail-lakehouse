# Requirements

> Every requirement here is **testable**. If you cannot describe the assertion that would fail,
> it is not a requirement — it is an aspiration, and it belongs in the North Star instead.
>
> `specs/traceability.md` maps each ID to its test(s). CI fails on any ID with no mapped test.
> That gate is what makes this document load-bearing rather than decorative.

**Priority:** `MUST` = the platform is incorrect without it. `SHOULD` = the platform is weaker
without it. `MAY` = deliberate stretch, cut first under pressure.

---

## ING — Ingestion

| ID | Pri | Requirement | Acceptance criterion |
|---|---|---|---|
| ING-001 | MUST | Bronze ingests new landing-zone files without reprocessing prior files | Two consecutive runs over a landing zone with one new file process exactly that file's row count |
| ING-002 | MUST | Every bronze row carries source lineage | `_source_file`, `_ingest_ts`, `_pipeline_run_id` non-null on 100% of rows |
| ING-003 | MUST | Additive schema drift does not fail the pipeline | Injecting a new column mid-stream: pipeline completes; new column present; pre-drift rows null for it |
| ING-004 | MUST | Non-conforming fields are captured, never dropped | A row with a type-incompatible field lands with populated `_rescued_data`; total row count is preserved |
| ING-005 | MUST | Duplicate delivery from CDC does not duplicate bronze rows | Replaying a CDC window leaves the target row count unchanged |
| ING-006 | SHOULD | Late-arriving events within the watermark are incorporated | An event emitted with 5h lag (watermark 6h) appears in the aggregate |
| ING-007 | MUST | Events beyond the watermark are counted, not silently discarded | An event with 7h lag increments `ops.dropped_late_events`; the aggregate is unchanged |

ING-007 is the requirement that separates a real streaming implementation from a demo. Dropping
late data is legitimate; dropping it *invisibly* is a data-loss bug wearing a watermark costume.

## GEN — Synthetic amplifier

The amplifier is the one component whose defects would be invisible everywhere else: distorted
synthetic data produces confident, wrong conclusions in the performance lab and the ML layer, and
nothing downstream would flag it. These requirements make the "resamples reality, does not invent
it" claim falsifiable.

| ID | Pri | Requirement | Acceptance criterion |
|---|---|---|---|
| GEN-001 | MUST | Resampling preserves the observed store distribution | Total variation distance from the source distribution is within 1.5× the measured multinomial noise floor, and top-decile share is preserved within 3 points |
| GEN-002 | MUST | Baskets are drawn intact, never reassembled | A drawn basket's line count, store, product set, and value total match its source basket exactly |
| GEN-003 | MUST | Generation is deterministic given a seed | Identical seeds produce identical draw sequences; different seeds do not |
| GEN-004 | SHOULD | Every stress scenario can be disabled independently | A control run with all scenarios off produces no late, duplicate, or drifted events |
| GEN-005 | MUST | Synthetic rows are labelled at source | Every emitted event carries `is_synthetic = true`, so MLR-003 can be enforced downstream |

GEN-005 is what makes MLR-003 (evaluation never touches synthetic rows) checkable rather than
aspirational. A label applied later, by inference, is a label that will be wrong.

## QLT — Data quality

| ID | Pri | Requirement | Acceptance criterion |
|---|---|---|---|
| QLT-001 | MUST | Every silver table has explicit quality rules, versioned in Unity Catalog | Each silver table resolves to ≥1 expectation row in the UC-governed rules table |
| QLT-002 | MUST | Violating rows are quarantined with a machine-readable reason | Quarantine row carries `rule_name`, `rule_expression`, `failed_at`; count reconciles with the drop |
| QLT-003 | MUST | Row conservation across silver | `input = passed + quarantined` per batch, asserted, no tolerance |
| QLT-004 | MUST | Referential integrity between fact and dimensions | Zero fact rows whose FK has no dimension match valid at the fact's event time |
| QLT-005 | SHOULD | Rules are generated from profiling, then human-reviewed, never auto-applied | Generated candidates land in a review file; only reviewed rules reach the UC rules table |
| QLT-006 | MUST | Quality metrics are time series, not point checks | `ops.dq_metrics` has one row per table per run with pass rate and violation counts |
| QLT-007 | SHOULD | Quality regression blocks promotion | Pass rate below the registered baseline fails the CI quality gate |

QLT-005 exists because DQX's profiler is good enough to be dangerous: auto-generated rules encode
whatever anomalies were present at profiling time as *law*. Profiling proposes; a human disposes.

## MOD — Modelling

| ID | Pri | Requirement | Acceptance criterion |
|---|---|---|---|
| MOD-001 | MUST | Product and household dimensions are SCD Type 2 | An attribute change produces a new version; the prior version gets a closed validity window; exactly one row per key has `is_current = true` |
| MOD-002 | MUST | No overlapping validity windows | Zero rows where the same key has intersecting `[valid_from, valid_to)` intervals |
| MOD-003 | MUST | Fact-to-dimension joins are point-in-time correct | Joining a fact to a key with N historical versions returns exactly one dimension row |
| MOD-004 | MUST | Gold row counts are stable under re-run | Re-running gold on identical input produces identical row counts and identical aggregate checksums |
| MOD-005 | SHOULD | Business KPIs exist once, as governed Unity Catalog Metrics | Each KPI in the metric register resolves to exactly one UC Metrics definition; no duplicate SQL |
| MOD-006 | MUST | Currency and units are explicit | Every monetary column ends in `_amt` and carries a UC comment naming its currency |

MOD-003 is the SCD Type 2 trap: the naive join on natural key alone silently fans out every fact
row by the number of dimension versions, inflating every sum in the warehouse. The test asserts a
1:1 cardinality, so the bug cannot ship.

## PRF — Performance

| ID | Pri | Requirement | Acceptance criterion |
|---|---|---|---|
| PRF-001 | MUST | Skew is detected before it is fixed | A recorded query profile shows max-task-time / median-task-time > 5 on the unmitigated join |
| PRF-002 | MUST | Skew mitigation is proven, not asserted | Post-mitigation profile shows the ratio < 2 at identical input volume |
| PRF-003 | MUST | Streaming sinks stay above the small-file floor | Median file size in the streaming sink > 16 MB over a 24h window |
| PRF-004 | SHOULD | Shuffle volume reduced on the wide join | Post-optimisation `shuffle write` at least 40% below baseline, same input |
| PRF-005 | MUST | Performance claims cite measurements | Every claim in `perf-evidence.md` carries before/after metrics and input volume |
| PRF-006 | MUST | No table is subject to both Predictive Optimization and a manual OPTIMIZE schedule | The two sets are disjoint |

## ENV — Environments and CI/CD

| ID | Pri | Requirement | Acceptance criterion |
|---|---|---|---|
| ENV-001 | MUST | No catalog name is hard-coded in source | Static scan of `src/` finds zero literal catalog names |
| ENV-002 | MUST | Environment is injected by bundle target only | Deploying to `test` writes exclusively to `dng_test` |
| ENV-003 | MUST | The deployed artifact is the tested artifact | Prod deploy references the git SHA that passed the test target |
| ENV-004 | MUST | A clean deploy is reproducible | Deploy to an empty catalog from a fixed input snapshot yields gold matching the golden fixture |
| ENV-005 | SHOULD | Failed deploys are recoverable | A deliberately broken deploy is rolled back by redeploying the prior SHA, with no manual cleanup |
| ENV-006 | MUST | Unit tests run without a Databricks connection | `pytest tests/unit` passes with no configured workspace |

ENV-006 matters more than it looks: a test suite that needs a workspace is a test suite that gets
skipped. Fast, connectionless unit tests are what keep TDD viable past week one.

## MLR — Machine learning

| ID | Pri | Requirement | Acceptance criterion |
|---|---|---|---|
| MLR-001 | MUST | Every training run is reproducible from its logged artifacts | Re-running from a logged run's params + data version reproduces the metric within tolerance |
| MLR-002 | MUST | The model beats a stated naive baseline | PR-AUC exceeds the recency-baseline by the pre-registered margin, on held-out **real seed** data |
| MLR-003 | MUST | Evaluation never touches synthetic rows | Evaluation dataset contains zero rows with `is_synthetic = true` |
| MLR-004 | MUST | Promotion is gated on evaluation | A model failing MLR-002 cannot transition to the `champion` alias |
| MLR-005 | SHOULD | Data drift and model drift are distinguished | Monitoring reports feature-distribution drift separately from performance decay |
| MLR-006 | SHOULD | Training features are point-in-time correct | No feature in the training set uses information unavailable at the prediction timestamp |

MLR-006 is leakage, the single most common and most invisible ML defect. It is a `MUST` in spirit
and a `SHOULD` only because fully proving it requires a temporal-join audit that is scoped as a
stretch.

## AGT — Agentic layer

| ID | Pri | Requirement | Acceptance criterion |
|---|---|---|---|
| AGT-001 | MUST | Agent answers are grounded in governed data | Every factual claim traces to a tool call against a Unity Catalog object |
| AGT-002 | MUST | Every agent interaction is traced | MLflow trace exists per request with inputs, tool calls, and outputs |
| AGT-003 | MUST | Quality is scored before deployment | LLM judges score groundedness, correctness, and relevance against a curated eval set |
| AGT-004 | MUST | Deployment is gated on judge scores | Scores below threshold block the deploy |
| AGT-005 | SHOULD | Production traces are sampled and scored continuously | Scheduled scorers run against a sample of live traces |
| AGT-006 | MUST | The agent refuses rather than guesses | For a question with no supporting data, the agent states the gap instead of producing a number |

AGT-006 is the requirement most agent demos fail. An agent that always answers is worse than one
that sometimes declines, because its confident wrong answers are indistinguishable from its right
ones.

## GOV — Governance

| ID | Pri | Requirement | Acceptance criterion |
|---|---|---|---|
| GOV-001 | MUST | Every gold column is documented | Zero gold columns with a null Unity Catalog comment |
| GOV-002 | MUST | Lineage resolves from gold to source | Each gold table's upstream lineage terminates at a bronze table or landing volume |
| GOV-003 | SHOULD | Assets are organised into business domains | Every gold table belongs to exactly one UC Domain |
| GOV-004 | SHOULD | Business terms are defined once | Terms used in metric names resolve to a glossary entry |

---

## Change protocol

Requirements are versioned with the code. Adding one requires adding a traceability row in the same
commit. Removing one requires a note in `docs/decision-log.md` explaining what changed about the
problem — not about the difficulty.
