# North Star — Retail Lakehouse & Agentic Data Platform

> **Status:** Living document. Every artifact in this repository must trace back to a
> requirement declared here. If code exists that no requirement asks for, the code is wrong.
> If a requirement has no test, the requirement is a wish.

---

## 1. Why this document exists first

This project is **spec-driven**, not prompt-driven. The distinction matters:

| Prompt-driven | Spec-driven |
|---|---|
| Human writes a prompt, reviews output, writes the next prompt. Human is the bottleneck and the only reviewer. | Human writes an executable specification. The agent loop generates, tests itself against the spec, and only escalates on ambiguity or exhausted retries. |
| Correctness is whatever the human noticed. | Correctness is what the spec asserts, enforced by tests. |
| Knowledge lives in a chat log that evaporates. | Knowledge lives in versioned artifacts: requirements, ADRs, tests, runbooks. |

The spec is the contract. The tests are the enforcement. The agent loop is the labour.
A human still owns the *goal definition* and the *exit criteria* — that is deliberately not delegated.

**Consequence for this repo:** `specs/REQUIREMENTS.md` holds numbered, testable requirements.
`specs/traceability.md` maps every requirement to the test(s) that prove it. A requirement with no
test row is a build failure, enforced in CI. This is the single most important quality gate here,
and it is enforced by machine rather than by discipline — because discipline does not survive
week three of a project.

---

## 2. The business, stated as decisions rather than as data

Vague framing ("build a retail data platform") produces vague architecture. The platform exists to
serve a finite set of decisions. Each decision has an owner, a latency tolerance, and a cost of
being wrong. Everything downstream — freshness SLOs, table layout, model choice — is derived from
this table, not from taste.

**Fictional operator:** *Northwind Grocers* — a regional grocery chain. Anonymised household-level
transaction history is real (dunnhumby, see ADR-0003); the operating model around it is constructed
so the platform has decisions to serve.

| # | Decision | Owner | Latency tolerance | Cost of being wrong |
|---|---|---|---|---|
| D1 | Which households receive which coupon in the next campaign wave | Category Marketing | Daily (T+1) | Wasted promo spend; margin erosion on customers who would have bought anyway |
| D2 | Which households are drifting toward lapse and need intervention | CRM | Weekly | Silent churn; LTV loss that is invisible until it is unrecoverable |
| D3 | Whether a live promotion is underperforming badly enough to pull mid-flight | Trade Planning | **Near real-time (minutes)** | A 2-week promo burns full budget before the weekly report says it failed |
| D4 | Whether today's store-level sales are anomalous (outage, shrink, mispriced SKU) | Store Ops | **Near real-time (minutes)** | Revenue leakage compounding per hour undetected |
| D5 | What a merchandiser can ask in natural language without filing a ticket | Merchandising | Interactive | Analyst queue becomes the bottleneck on every commercial question |

**D3 and D4 are the only reasons streaming exists in this architecture.** This is stated
explicitly because "we used streaming" is not an achievement — choosing streaming where batch
would do is a cost defect, and choosing batch where minutes matter is a business defect. The
architecture must be able to defend the boundary. See ADR-0005.

---

## 3. Capability map

```
                 ┌──────────────────────────────────────────────────────┐
   OPERATIONAL   │  Lakebase (Postgres OLTP)   +   Event landing zone   │
      PLANE      │  slowly-changing masters        high-volume baskets  │
                 └───────────────┬──────────────────────┬───────────────┘
                                 │ CDC                  │ Auto Loader
                 ┌───────────────▼──────────────────────▼───────────────┐
   BRONZE        │  Streaming tables · append-only · rescued data on    │
                 │  schema drift · full source lineage columns          │
                 └───────────────────────┬──────────────────────────────┘
                                         │ AUTO CDC (SCD1 + SCD2)
                 ┌───────────────────────▼──────────────────────────────┐
   SILVER        │  Conformed entities · DQX + UC-governed expectations │
                 │  quarantine on violation, never silent drop          │
                 └───────────────────────┬──────────────────────────────┘
                                         │
                 ┌───────────────────────▼──────────────────────────────┐
   GOLD          │  Star schema · Unity Catalog Metrics as governed KPI │
                 │  objects · liquid clustering · materialized views    │
                 └──────┬────────────────────┬───────────────────┬──────┘
                        │                    │                   │
                 ┌──────▼──────┐   ┌─────────▼────────┐  ┌───────▼───────┐
   ACTIVATION    │ ML: churn / │   │ Agent: grounded  │  │ Genie + BI    │
                 │ promo uplift│   │ NL over gold+docs│  │ dashboards    │
                 │ MLflow 3    │   │ MLflow 3 tracing │  │               │
                 └──────┬──────┘   └──────────────────┘  └───────────────┘
                        │ reverse ETL
                 ┌──────▼───────────────────────────────────────────────┐
                 │  Lakebase — scores served back to operational apps    │
                 └──────────────────────────────────────────────────────┘
```

The loop closing back into Lakebase is deliberate. A platform that only produces dashboards is a
reporting system. A platform that writes decisions back into the operational plane is
infrastructure. That round trip is what makes D1/D2 actionable rather than merely observable.

---

## 4. Engineering problems staged on purpose

A portfolio that only shows the happy path proves nothing. Each of these is **deliberately
induced** by the synthetic amplifier (see `generator/`), then diagnosed, then fixed — and the
before/after is captured as evidence, not asserted.

| Problem | How it is induced | What the fix demonstrates |
|---|---|---|
| **Data skew** | ~0.5% of `store_id` values carry ~40% of baskets (a real grocery pattern: flagship stores). Join on `store_id` produces one straggler task. | Reading the Spark UI to *prove* skew rather than guess it; AQE skew join; salting when AQE is insufficient; knowing why AQE sometimes cannot help. |
| **Small files** | Streaming micro-batches at 10s triggers over 24h produce tens of thousands of sub-MB files. | The distinction between auto-compaction, `OPTIMIZE`, and Predictive Optimization — and that running Predictive Optimization *and* a cron `OPTIMIZE` on the same table is a documented anti-pattern. |
| **Shuffle explosion** | A naive wide join of transactions × product × causal on unclustered tables. | Broadcast thresholds, join-order effects, and reading `shuffle read/write` from the query profile to justify the change with numbers. |
| **Late-arriving / out-of-order events** | Generator emits 3% of events with a lag of up to 6 hours. | Watermarks, and the explicit statement of what is dropped and why — the correctness/latency trade rather than pretending it is free. |
| **Schema drift** | Upstream adds and renames columns mid-stream at a scheduled point. | Auto Loader schema evolution + `_rescued_data`: pipeline survives, nothing is silently lost, and the drift is *observable*. |
| **Duplicate delivery** | CDC source replays a window (at-least-once semantics). | Idempotent merge keyed on a business key + sequence, proving exactly-once *effect* without claiming exactly-once *delivery*. |
| **SCD Type 2** | Product hierarchy reclassifications and household demographic updates arrive over time. | `AUTO CDC ... STORED AS SCD TYPE 2`, and — more importantly — the queries that are *wrong* if you forget point-in-time joins against a versioned dimension. |

That last row is the one most engineers get wrong: they build the SCD2 table correctly and then
join to it on the natural key without a validity-window predicate, silently fanning out every fact
row. The test suite asserts against exactly that failure mode.

---

## 5. Non-functional requirements

| ID | NFR | Target | Enforcement |
|---|---|---|---|
| NFR-1 | Bronze freshness (streaming path) | p95 < 2 min end-to-end | Pipeline event log assertion |
| NFR-2 | Gold freshness (batch path) | Available by 06:00 local for T+1 | Job SLA + alert |
| NFR-3 | Data quality | 0 rows silently dropped; every rejection lands in quarantine with a reason code | DQX + expectation audit table |
| NFR-4 | Reproducibility | Any commit deploys to a clean catalog and produces byte-identical gold for a fixed input snapshot | CI integration target |
| NFR-5 | Cost | Full daily pipeline within Free Edition quota (5 concurrent tasks, 1 active pipeline per type, 2X-Small SQL warehouse) | Quota-aware orchestration; documented in ADR-0002 |
| NFR-6 | Lineage | Every gold column traceable to source columns | Unity Catalog lineage + column-level docs |
| NFR-7 | Model quality | Churn model beats a recency-baseline on PR-AUC by a pre-registered margin | MLflow evaluation gate blocking promotion |
| NFR-8 | Agent quality | Grounded-ness and correctness scored by LLM judges against a curated eval set before any deployment | MLflow 3 scorers in CI |

NFR-4 deserves a note: reproducibility is the requirement most often claimed and least often
tested. Here it is tested by deploying to a throwaway catalog from a clean state and diffing gold
against a golden fixture. If that job is red, the platform is not reproducible, regardless of what
the README says.

---

## 6. What "done" means

This project is complete when a Databricks engineer who has never seen the repository can:

1. Clone it, run `make setup`, and deploy the full stack to their own Free Edition workspace with
   one command.
2. Read `docs/adr/` and understand not just what was chosen but **what was rejected and why** —
   including the choices that turned out to be wrong and were reversed.
3. Run the test suite locally without a Databricks connection (unit) and against a real workspace
   (integration).
4. Point at any number in the gold layer and walk the lineage back to a source file.
5. Break something on purpose — corrupt a source file, spike a skew key — and watch the platform
   detect it rather than propagate it.

Item 5 is the real bar. Anyone can demo a pipeline that works. The demonstrable skill is a
pipeline that **fails correctly**: loudly, in a quarantined way, with enough context to diagnose.

---

## 7. Explicit non-goals

Stating these prevents scope drift and, in a portfolio context, prevents the reviewer from
assuming an omission was an oversight:

- **Not** a Databricks feature tour. Features appear only where a decision in §2 requires them.
- **Not** multi-cloud. One metastore, one workspace (Free Edition constraint — ADR-0002).
- **Not** a real-time ML serving system with sub-100ms SLA. Model serving here is batch + on-demand.
- **Not** a production-grade security posture. Free Edition has no service principals, SSO, or
  private networking. Where a production design would differ, it is documented in-line rather than
  faked.

That last point is worth stating plainly: pretending Free Edition is production would be the
single easiest way to lose credibility with a senior reviewer. Naming the gap is stronger than
hiding it.

---

## 8. Reading order for a reviewer

1. This document.
2. `docs/adr/` — the decision record, in numeric order.
3. `specs/REQUIREMENTS.md` — what the system must do.
4. `specs/traceability.md` — proof that each requirement is tested.
5. `docs/architecture/` — the how, plus the measured findings per layer.
6. `docs/diagrams/` — the same story in one page, twice: once for the business, once for an
   engineer.

**Not written, and named rather than implied:** `docs/runbooks/` — what to do when it breaks. An
earlier version of this list pointed at it as though it existed. It does not, and a reading order
that sends a reviewer to an empty directory is the same defect this repository keeps finding
elsewhere: a claim that is easier to make than to check.
