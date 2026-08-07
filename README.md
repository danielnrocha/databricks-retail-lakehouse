# Retail Lakehouse — a spec-driven, agentic data platform on Databricks

A grocery-retail lakehouse built the way a platform actually gets built: requirements first, tests
that enforce them, decisions recorded with what was rejected, and failure modes induced on purpose
so the fixes can be measured rather than claimed.

> **The claim this repository makes:** anyone can show a pipeline that works. This one shows a
> pipeline that **fails correctly** — loudly, in quarantine, with enough context to diagnose — and
> proves every performance claim with a query profile instead of an adjective.

---

## What is actually built, and what is not

This table is split on purpose. A portfolio README that lists intentions alongside results is
making the reader do the verification, and this project's whole argument is that verification is
the author's job.

**Built and measured:**

| Area | What it demonstrates | Evidence |
|---|---|---|
| **Ingestion** | Auto Loader with schema evolution and `_rescued_data`; Lakeflow Spark Declarative Pipelines; 200,000 events, zero loss | [bronze-findings](docs/architecture/bronze-findings.md) |
| **Modelling** | SCD Type 2 via `AUTO CDC`, and the point-in-time join that stops it inflating revenue by 1.706% | [silver](docs/architecture/silver-findings.md) · [gold](docs/architecture/gold-findings.md) |
| **Data quality** | DQX profiling as *candidates* + reviewed rules; quarantine with reason codes; row conservation asserted per run | [rule-review](docs/quality/rule-review.md) |
| **Performance** | Skew and spill measured on real distributions across 84 runs, each traceable to a `statement_id` | [perf-lab](docs/architecture/perf-lab.md) |
| **CI/CD** | Bundles with three catalog environments, GitHub Actions, no literal catalog in `src/` | [ci.yml](.github/workflows/ci.yml) |
| **ML** | Lapse model with MLflow and a promotion gate — which **refused** the model, correctly | [ml-findings](docs/architecture/ml-findings.md) |
| **Agents** | Agent over governed UC functions, LLM-as-judge gating, declines rather than guesses | [agent-findings](docs/architecture/agent-findings.md) |

**Not built.** Named rather than implied, because an unstated gap reads as an oversight:

| Area | Status |
|---|---|
| Managed MCP | Agent tools are UC functions called directly; same governance, different transport |
| Adversarial robustness | No prompt-injection cases in the agent eval set |
| Lakebase CDC | Designed in ADR-0005, not wired |
| Unity Catalog Metrics · Domains | Availability on Free Edition unverified |

`specs/traceability.md` is the authoritative count: **19 of 53 requirements proven.** Everything
else reads `PLANNED`, and that file is the single source of truth for the difference between what
is written and what is shown.

---

## Leitura visual e em português

📊 **[Os seis achados, em uma página](https://claude.ai/code/artifact/23926add-e082-4dc4-8450-1cbb35e353fa)** — the six measured defects, one page.

🇧🇷 **[README.pt-BR.md](README.pt-BR.md)** — resumo executivo em português.

🎓 **[docs/mentoring/](docs/mentoring/)** — a 90-minute session plan and six break-and-fix exercises.

## Start here

Read in this order. The code will not make sense without the first two.

1. **[docs/00-north-star.md](docs/00-north-star.md)** — the business decisions the platform exists
   to serve, and the engineering problems staged on purpose.
2. **[docs/adr/](docs/adr/)** — every contested decision, with the alternatives that were rejected
   and why, plus a reversal-cost field on each.
3. **[specs/REQUIREMENTS.md](specs/REQUIREMENTS.md)** — 53 numbered, testable requirements.
4. **[specs/traceability.md](specs/traceability.md)** — requirement → test mapping, enforced in CI
   by [`scripts/check_traceability.py`](scripts/check_traceability.py). A requirement with no test
   row fails the build.

---

## The dataset

**dunnhumby — *The Complete Journey*** (CC BY 4.0, [DOI 10.17632/7myy93ym6k.1](https://data.mendeley.com/datasets/7myy93ym6k/1)):
two years of basket-level grocery transactions across 2,500 households, with product hierarchy,
household demographics, campaigns, coupons, redemptions, and in-store promotion exposure.

It is used as a **seed**, not as the whole story. `generator/` resamples its empirical
distributions to produce a continuous event stream at arbitrary volume, and injects the arrival
pathologies the seed cannot exhibit: small files, late arrival, schema drift, duplicate delivery.

It deliberately does **not** inject skew. Profiling found the seed's own skew is severe — the top
10% of stores carry 69.3% of transaction lines, max/median 2,519x — so manufacturing more would
have produced a mitigation validated only against invented distributions. See the amendment on
[ADR-0003](docs/adr/ADR-0003-dataset-selection.md).

The honesty constraint that makes this defensible: **models are trained on amplified data and
evaluated exclusively on held-out real seed data.** A model trained on data generated by a rule you
wrote will recover the rule you wrote. See [ADR-0003](docs/adr/ADR-0003-dataset-selection.md).

---

## Environments

Databricks Free Edition allows one workspace and one metastore. Environments are therefore
**catalogs**, not workspaces — which is the pattern Databricks documents for single-metastore
organisations, not a workaround dressed up as a design:

```
dng_dev · dng_test · dng_prod
   └── bronze · silver · gold · ops
```

The catalog reaches code by exactly one route — bundle target → variable → job parameter — and a
lint test fails the build if any literal catalog name appears in `src/`. That rule is enforced by
machine because it is precisely the rule that erodes under deadline pressure.

Where Free Edition genuinely cannot match a production posture (no service principals, no private
networking, logical rather than physical isolation), the gap is written down in
`docs/architecture/production-delta.md` rather than hidden. Naming the gap is stronger than
pretending it isn't there.

---

## Quickstart

```bash
make setup                 # local env (pyspark) — no Databricks account needed
make check                 # lint + types + traceability gate + unit tests

# Against a workspace:
databricks auth login --host https://<your-workspace-host>
make setup-dbconnect
make validate TARGET=dev
make deploy   TARGET=dev
```

`make check` is what CI runs on every pull request. It requires no Databricks connection — a unit
suite that needs a workspace is a unit suite that gets skipped.

---

## Repository layout

```
docs/
  00-north-star.md          business decisions, staged failure modes, NFRs
  decision-log.md           chronological, including every reversal
  adr/                      architecture decision records
  architecture/             findings, perf evidence, production delta
  mentoring/                session plan and break-and-fix exercises
  quality/                  DQX candidates and the human review of each
specs/
  REQUIREMENTS.md           numbered, testable requirements
  traceability.md           requirement -> test, CI-enforced
src/retail_lakehouse/       bronze · silver · gold · quality · perf
generator/                  distribution-preserving synthetic amplifier
resources/                  bundle resource definitions
tests/unit/                 no workspace required (63 tests)
tests/integration/          requires an authenticated workspace
data/perf/                  84 measured runs, each with its statement_id
scripts/                    gates and operational tooling
```

---

## Status

Built in phases; each phase closes only when its requirements move from `PLANNED` to `PASSING` in
the traceability matrix. Current state is visible in that file — it is the single source of truth
for what is proven versus what is merely written.

## Licence

Code: Apache-2.0. Data: dunnhumby *The Complete Journey*, CC BY 4.0 — attribution in
`data/README.md`.
