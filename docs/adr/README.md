# Architecture Decision Records

Every non-obvious choice in this repository has a record. A record exists when the decision was
*contested* — that is, when a competent engineer could reasonably have chosen differently. Choices
with no real alternative (use Delta on Databricks) do not get an ADR; they get a line of code.

## Format

Each ADR states: **Context → Options considered → Decision → Consequences → Reversal cost**.

The last field is the one usually missing from ADR templates and the one that matters most in
practice. "How expensive is it to undo this?" determines how much analysis a decision deserves.
A cheap-to-reverse decision made slowly is waste; an expensive-to-reverse decision made quickly is
how platforms end up rewritten.

## Index

An ADR is written **when the decision is made**, not retroactively at the end of the project.
Retroactive ADRs are reconstructions: they describe the choice you can still justify, not the one
you actually faced. Records below marked *Pending* correspond to decisions whose phase has not
started — writing them now would be fiction.

| ADR | Title | Status | Reversal cost |
|---|---|---|---|
| [0002](ADR-0002-environment-isolation.md) | Catalog-per-environment on a single metastore | Accepted | **High** |
| [0003](ADR-0003-dataset-selection.md) | dunnhumby Complete Journey + synthetic amplifier | Accepted | Medium |
| [0005](ADR-0005-streaming-batch-boundary.md) | Where streaming stops and batch begins | Accepted | Low |
| [0007](ADR-0007-table-layout.md) | Liquid clustering + Predictive Optimization; no partitioning, no Z-ORDER | Accepted | Medium |
| [0004](ADR-0004-ingestion-framework.md) | Lakeflow Declarative Pipelines over hand-rolled Structured Streaming | Accepted | Medium |
| [0001](ADR-0001-spec-driven-agent-loop.md) | Spec-driven development with an adversarial agent loop | Accepted | Low |
| 0006 | Two-layer data quality: DQX + UC-governed expectations | Pending (Phase 5) | Low |
| 0008 | SCD Type 2 via AUTO CDC, not hand-written MERGE | Pending (Phase 5) | Medium |
| [0009](ADR-0009-cicd.md) | Bundles deployed by GitHub Actions, dispatched by hand | Accepted | Low |
| 0010 | MLflow 3 + Unity Catalog model registry + inference monitoring | Pending (Phase 8) | Low |
| 0011 | Managed MCP for grounding; MLflow judges for evaluation | Pending (Phase 9) | Low |
| 0012 | English as the primary documentation language | Accepted (below) | Low |

### ADR-0012 — English as the primary documentation language

Short enough to live inline. **Context:** the author and the primary reviewer are both Brazilian;
the repository is also a public portfolio aimed at international roles. **Decision:** English for
code, ADRs, requirements, and the main README; a Portuguese `README.pt-BR.md` for the executive
summary. **Rejected:** Portuguese-primary (halves the audience for a public portfolio) and
bilingual-everything (documentation that must be updated twice is documentation that goes stale in
one language, and you cannot tell which). **Consequence:** a reviewer reading only Portuguese gets
the summary and must use the English documents for depth. Accepted as the lesser cost.

## Reversal log

Decisions that were reversed are **kept**, marked `Superseded`, with the reversal reasoning
appended. Deleting a wrong decision destroys the most useful information in the record: the shape
of the mistake. A reviewer learns more from one honest reversal than from twelve confident
accepts.
