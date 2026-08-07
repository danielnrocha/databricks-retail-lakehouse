# Gold — what the dimensional layer proved

Five materialized views in `dng_dev.gold`, built from silver, deployed through the same
`dng_medallion` pipeline. Measured, not asserted.

---

## G1 — The point-in-time join did not fan out, and that is the whole point

```
gold_rows        198,013     silver_rows        198,013     row_variance      0
gold_revenue     613,396.36  silver_revenue     613,396.36  revenue_variance  0.00
reconciles       true
```

Silver measured what the *wrong* join costs: joining `dim_product_scd2` on `product_id` alone
returns 201,476 rows and 623,863.52 — **1.706% of revenue that does not exist**, from 3,463 phantom
rows, with only 2% of product keys carrying a second version.

Gold uses the half-open validity predicate and reconciles exactly. `gold_reconciliation` is a
table, not a one-off check, so the assertion runs every update: a future change that reintroduces
the fan-out turns `reconciles` false rather than quietly inflating a dashboard.

The half-open interval matters more than it looks. `>= __START_AT AND (__END_AT IS NULL OR
< __END_AT)` — using `<=` on the upper bound double-counts every fact landing exactly on a version
boundary. Rare enough to survive testing, frequent enough to be wrong.

## G2 — Three-state promotion exposure, and what the third state is worth

| state | lines | share | revenue |
|---|---:|---:|---:|
| `not_promoted` | 149,615 | 75.56% | 478,105.77 |
| `mailer` | 22,489 | 11.36% | 64,756.17 |
| `display` | 11,304 | 5.71% | 30,729.46 |
| `both` | 9,265 | 4.68% | 23,765.37 |
| **`unknown`** | **5,340** | **2.70%** | **16,039.59** |

The `unknown` bucket is the 2.70% of lines falling outside weeks 9–101, where promotion exposure
was never collected (finding F3). Every alternative to carrying it explicitly is a fabrication:

- **Default to `not_promoted`** — teaches every downstream model that weeks 1–8 had no promotions.
  A signal nobody observed, invented by a join default.
- **Inner join** — discards those lines entirely, so the denominator of any lift calculation
  silently shrinks and the lift looks better than it is.
- **Null** — survives one hop, then someone writes `WHERE promo_exposure != 'display'` and the
  nulls vanish from the result without anyone deciding they should.

2.70% sounds ignorable. It is 16,039.59 of revenue whose promotional status is unknowable, and a
promo-lift model that treats it as a control group is measuring partly noise.

The 78.28% inner-join drop that finding F2 warned about did not materialise here, because the join
is a LEFT against a **deduplicated** right side. `causal` holds 15,245 composite keys appearing more
than once across 30,490 rows; joining it raw adds 858 rows to the fact. "LEFT JOIN preserves the
left side" is true only when the right side is unique on the join key, which is the assumption
nobody states and everybody makes.

## G3 — Column comments propagate through lineage, but only for pass-through columns

GOV-001 requires zero uncommented gold columns. `fct_basket_line` has 22 columns and **9 are
uncommented** — but the 13 that are commented were never commented in this module. They were
inherited.

The pattern is exact:

| column origin | comment |
|---|---|
| selected unchanged from `silver.fact_basket_line` | **inherited** |
| produced by a join (`department`, `income_band`, `store_volume_decile`) | **null** |
| produced by an expression (`promo_exposure`) | **null** |

So Unity Catalog propagates a column comment when the column passes through a single upstream
source unchanged, and drops it the moment provenance becomes ambiguous. Which is defensible —
a joined column's meaning genuinely may differ from its source's — but it means **documentation
coverage silently degrades exactly where the modelling gets interesting**. The columns that most
need explaining are the derived ones, and those are precisely the ones that arrive undocumented.

**GOV-001 does not pass.** The `@dp.materialized_view(comment=...)` decorator sets the *table*
comment only. Setting column comments requires either an explicit `schema=` on the decorator —
which the silver work found does not escape its generated DDL, so a comment containing `'12 units'`
produced a parse error — or `ALTER TABLE ... ALTER COLUMN ... COMMENT` after the fact, whose
durability across a materialized-view refresh is **untested here**.

Left open deliberately rather than papered over with a fix nobody verified survives a refresh.

## G4 — Two mistakes in the first deploy, both instructive

**The pipeline's default schema silently wins.** `resources/bronze_pipeline.yml` sets
`schema: bronze`, so `@dp.materialized_view(name="fct_basket_line")` created
`dng_dev.bronze.fct_basket_line`. The error surfaced three tables later as
`TABLE_OR_VIEW_NOT_FOUND: dng_dev.gold.fct_basket_line` — the aggregate looked for the table where
it *should* be, and the fact had been created where the *pipeline default* put it.

Fix: qualify the dataset name, `name="gold.fct_basket_line"`. Worth knowing because the failure
mode is a table that exists, is populated, and is in the wrong schema — which no test asserting
"the table has rows" would catch.

**A single pipeline graph means a gold defect fails bronze.** The whole update failed, including
ingestion that had nothing to do with the broken module. That is the cost of Free Edition's
one-pipeline-per-type limit, made concrete: no independent failure domains. On a paid tier the
streaming path would not have gone down for a modelling error in an aggregate.

---

## Requirements

| ID | Status | Note |
|---|---|---|
| MOD-003 | **holds** | point-in-time join reconciles exactly, asserted every run |
| MOD-004 | **holds** | reconciliation table is the determinism assertion |
| GOV-001 | **fails** | 9 of 22 fact columns uncommented; see G3 |
| GOV-003 | not attempted | UC Domains availability on Free Edition unverified |
| MOD-005 | not attempted | Unity Catalog Metrics unverified on Free Edition |

The last two are unattempted rather than failed, and saying so is the difference between a gap and
a claim.
