# What would be different in production

Free Edition is not a production posture, and pretending otherwise is the fastest way to lose a
senior reviewer. This document names every place the design here departs from what it would be on
a paid tier, what the gap actually costs, and what would replace it.

A waiver is a documented gap. A silent omission is a lie. Everything below is a waiver.

---

## 1. Environment isolation is logical, not physical

**Here.** Three catalogs on one metastore in one workspace (ADR-0002). A principal with
`ALL PRIVILEGES` on the metastore can reach every environment.

**Production.** Workspace per environment, catalogs bound to workspaces, so a dev principal cannot
authenticate against prod at all. Unity Catalog *catalog bindings* would restrict each catalog to
its workspace, making the isolation enforceable rather than conventional.

**What the gap costs.** A mistake that would be impossible in production is merely unlikely here.
The `ENV-001` lint rule (no literal catalog names in `src/`) exists precisely because it is the
last line of defence when the platform is not providing one. In production that rule would still
be worth having; here it is load-bearing.

## 2. No service principals

**Here.** Everything runs as `daniel.rocha@dadosnagringa.com` via OAuth U2M. Free Edition has no
account console and no service principals.

**Production.** One service principal per environment, with workload identity federation for CI/CD
so no long-lived secret exists anywhere. `run_as` set to the environment's service principal, and
the human's identity used only for interactive development.

**What the gap costs.** Three things that genuinely matter: audit trails attribute pipeline
activity to a person rather than a system; a person leaving the company breaks production; and the
blast radius of a compromised laptop includes prod. This is the single largest gap in the whole
project.

## 3. Deploy authentication

**Here.** OAuth U2M from a developer machine. CI has no credentials, so the `bundle` job in
`.github/workflows/ci.yml` validates offline and is marked `continue-on-error`.

**Production.** GitHub Actions with OIDC federation to a Databricks service principal — no secret
in GitHub at all. Deploy to prod gated on a passing test-target deploy of the same commit SHA
(ENV-003), with the SHA recorded in the deployment.

## 4. One pipeline for the whole medallion

**Here.** Free Edition allows one active pipeline per type, so bronze, silver and gold share one
graph (ADR-0004).

**Production.** At minimum two pipelines: a continuous streaming pipeline for the real-time path
(D3/D4 in the North Star) and a triggered batch pipeline for the dimensional build. The reason is
not tidiness — it is independent failure domains and independent schedules. A failure in the daily
SCD Type 2 build should not stop store-anomaly detection, and here it would.

**Related.** ADR-0005 rejected a Kappa architecture *specifically* because of this constraint. On a
paid tier the streaming-only design becomes viable and is arguably the better answer. That is
recorded so the reasoning is understood as tier-dependent rather than as a belief about Kappa.

## 5. Compute is a 2X-Small serverless SQL warehouse, and nothing else

**Here.** No cluster sizing, no instance types, no autoscaling policy, no GPU. Photon is on because
serverless enables it, not because it was chosen.

**Production.** Warehouse sized to the workload with a scaling policy; job compute separate from
interactive compute so an analyst's query cannot slow a pipeline; and — the one that matters most
for this project — **the ability to fix disk spill by adding memory**, which is Databricks' own
first recommendation for spill and is unavailable here.

**Consequence for the performance work.** Every optimisation in the performance lab has to be a
*code-level* intervention, because the configuration levers do not exist: only six Spark properties
are settable on serverless, and none of them are `spark.sql.adaptive.*`. That is a real constraint,
though it produces a more defensible narrative than toggling the optimizer off would have.

## 6. Observability is thinner than it looks

**Here.** No Spark UI at all on serverless. Evidence comes from `system.query.history` and the
Query Profile, which expose per-query and per-operator aggregates but **no per-task metrics**.

**Production.** Same on serverless — this is not a Free Edition limitation but a serverless one, and
it is worth stating clearly because it surprises people migrating from classic clusters. Databricks'
own documented skew threshold ("max task duration more than 50% above the 75th percentile") is
measurable only in the Spark UI, which serverless does not have. A team that standardises on
serverless inherits this gap regardless of what they pay.

Classic compute would restore the Spark UI. Whether that justifies classic compute is a real
architectural question and not an obvious yes.

## 7. Quota exhaustion takes down everything

**Here.** Exceeding the Free Edition fair-use quota shuts down all compute in the account for the
rest of the day — including prod. This is why `var.sample_pct` defaults to `0.05` in dev: the
expensive path is opt-in.

**Production.** Budget policies and per-workload cost attribution, where overspend produces an alert
and a conversation rather than an outage.

## 8. No SLA, no support, no DR

Free Edition carries no service level agreement and no support policy. There is no backup strategy
here, no cross-region replication, and no tested restore. Production would need all three, plus a
documented RPO/RTO — and a restore that has actually been rehearsed, since an untested backup is a
belief rather than a control.

## 9. Security posture not addressed at all

Not implemented and not simulated: private networking, IP access lists, customer-managed keys,
compliance profiles, SCIM provisioning, SSO. Authentication here is limited to email OTP and social
sign-in.

Row-level security and column masking *are* available through Unity Catalog and would be the
correct place to enforce PII handling — the dunnhumby data is already anonymised, so there is no
honest scenario to apply them to. Building a fake one would demonstrate the syntax and none of the
judgement.

## 10. Features that could not be evaluated

| Feature | Status | Note |
|---|---|---|
| Genie Ontology | Not assessed | Public Preview since June 2026; availability on Free Edition unverified |
| Knowledge Assistant | Unavailable | Explicitly excluded from Free Edition |
| Online tables | Unavailable | Excluded |
| Clean rooms | Unavailable | Excluded |
| Provisioned throughput serving | Unavailable | Excluded |
| Predictive Query Execution | Uncertain | Documented for DBSQL Serverless; no official statement found for serverless notebooks |

---

## The honest summary

What this project demonstrates well: architecture, modelling, data quality, incremental ingestion,
measurement discipline, and the reasoning behind each decision including the ones that were
reversed.

What it cannot demonstrate: operating under real production constraints — identity separation,
capacity management, disaster recovery, security controls, and the organisational realities that
make those hard.

Those are different skills. Claiming the second on the evidence of the first is the failure mode
this document exists to prevent.
