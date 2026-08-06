# ADR-0002 — Catalog-per-environment on a single metastore

**Status:** Accepted · **Date:** 2026-08-06 · **Reversal cost:** High

---

## Context

The platform needs three environments — `dev`, `test`, `prod` — with genuine isolation: a broken
dev pipeline must not be able to corrupt prod data, and a prod deploy must be promotable from an
artifact that was actually tested.

Databricks Free Edition imposes a hard constraint: **one workspace and one metastore per account**,
with no account console and no service principals. This is not a preference; it is the boundary
condition.

The temptation is to treat this as a blocker and reach for a paid trial. That would trade a
permanent free environment for 14 days of a fuller one — a bad trade for a project meant to be
iterated on after the first presentation, and after the trial expires the repository becomes an
artifact nobody can run.

## Options considered

### A. Workspace-per-environment
The Databricks reference pattern for enterprises: three workspaces, one metastore per region,
catalogs bound to workspaces. Strongest isolation — a dev principal cannot even authenticate
against prod.

**Rejected:** impossible on Free Edition. Also worth noting that it is *not* free of downsides even
where available — it multiplies the surface that must be kept in sync, and workspace drift
(different runtime defaults, different cluster policies) is a common source of "works in dev, fails
in prod".

### B. Schema-per-environment inside one catalog
`retail.bronze_dev`, `retail.bronze_prod`, and so on.

**Rejected.** Two fatal problems. First, Unity Catalog's natural permission boundary is the
catalog; putting environments in schemas means every grant has to encode the environment in a
naming convention, and naming conventions are not access control. Second, it makes the three-part
namespace carry two orthogonal axes (environment × layer) in one segment, so every query has to
string-build its own table names. That is how you get a dev notebook that writes to prod because
someone edited a variable.

### C. Catalog-per-environment on a shared metastore ✅
`dng_dev`, `dng_test`, `dng_prod`, each containing `bronze` / `silver` / `gold` / `ops` schemas.
Isolation is enforced at the catalog grant level. Code never hard-codes a catalog; the catalog is
injected by the Declarative Automation Bundle target.

**Chosen.**

## Decision

Three catalogs, environment injected at deploy time, never at read time.

```
dng_dev          dng_test          dng_prod
├── bronze       ├── bronze        ├── bronze
├── silver       ├── silver        ├── silver
├── gold         ├── gold          ├── gold
└── ops          └── ops           └── ops     ← quality audit, pipeline events, model metrics
```

The catalog name reaches code by exactly one route:

```
bundle target (databricks.yml) → var.catalog → job/pipeline parameter → Spark conf → code
```

No module in `src/` may read an environment variable to discover its catalog, and no module may
contain a literal catalog name. This is enforced by a lint test (`tests/unit/test_no_hardcoded_catalog.py`),
not by review — because this is precisely the rule that erodes under deadline pressure.

The `ops` schema is deliberately per-environment rather than shared. A shared ops schema would let
a dev pipeline write quality metrics that pollute prod dashboards, which defeats the purpose.

## Consequences

**Positive**
- Grants are simple and auditable: `GRANT USAGE ON CATALOG dng_prod TO <group>`.
- Promotion is a bundle target switch, not a code change — so what is tested is what deploys.
- The pattern is the one Databricks documents for single-metastore organisations, so it is not a
  Free Edition workaround being passed off as a design. It is a legitimate design that Free Edition
  happens to force.

**Negative — stated plainly**
- Isolation is *logical*, not *physical*. A user with `ALL PRIVILEGES` on the metastore can reach
  every environment. In production this would be mitigated with catalog bindings and separate
  service principals per environment; on Free Edition neither exists. This gap is real and is not
  papered over. See `docs/architecture/production-delta.md` for what would change on a paid tier.
- Compute is shared. A runaway dev job consumes the same quota as prod, and Free Edition's quota
  exhaustion shuts down *all* compute for the rest of the day. Mitigation: dev runs against a
  1%-sampled input by default (`var.sample_pct`), so the expensive path is opt-in.
- One active Lakeflow pipeline per pipeline type per account means dev and prod pipelines cannot run
  concurrently. Orchestration must serialise them. This is a genuine limitation with no elegant
  workaround; it is scheduled around rather than engineered around.

## Reversal cost: High

Changing the isolation model after tables exist means rewriting every grant, moving data across
catalogs (a full copy — Unity Catalog has no free catalog rename that preserves lineage), and
invalidating every dashboard, metric definition, and model that references three-part names.

This is why it is ADR-0002 rather than an incidental choice: it is the decision most expensive to
get wrong, so it is made first and made explicitly.

## Revisit trigger

Move to workspace-per-environment if any of these become true:
- The project moves to a paid tier **and** more than one person can deploy.
- A compliance requirement demands physical separation.
- Quota contention between dev and prod causes a missed SLA more than once.
