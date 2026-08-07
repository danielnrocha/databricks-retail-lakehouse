# Governance findings — UC Domains and UC Metric Views on Free Edition

Two requirements were blocked on the same unanswered question: does the feature exist on this tier
at all? GOV-003 ("every gold table belongs to exactly one UC Domain") and MOD-005 ("each KPI
resolves to exactly one UC Metrics definition") had both been left `PLANNED` with availability
unverified, which is a polite way of saying nobody had run the command.

Both were probed on 2026-08-07 against `dbc-b1debb7b-807a.cloud.databricks.com`. **Both features
exist, and both are enforced rather than decorative.** No fallback was needed, so none was built.

---

## G1 — UC Domains exist, on an undocumented endpoint, backed by a documented one

The obvious probes all fail, and each failure is worth recording because each is the shape someone
would take as an answer:

```console
$ databricks --help | grep -i domain
                                                     # no CLI command at all

$ databricks api get /api/2.1/unity-catalog/domains
Error: No API found for 'GET /unity-catalog/domains'

$ databricks api get /api/2.0/unity-catalog/domains
Error: No API found for 'GET /unity-catalog/domains'

$ databricks api get /api/2.1/unity-catalog/data-domains
Error: No API found for 'GET /unity-catalog/data-domains'
```

The SDK offers nothing either — `WorkspaceClient` at 0.125.0 exposes no `domains` service; the
closest names are `entity_tag_assignments`, `tag_policies` and `workspace_entity_tag_assignments`.
That combination — no CLI verb, no SDK service, four dead REST paths — reads exactly like "not
available on this tier", and it is wrong:

```console
$ databricks api get /api/2.0/domains
{"domains": [{"domain_id": "a363307b-…", "tag_key": "dng_domain",
              "name": "domains/a363307b-…", "effective_draft": false, …}]}
```

**The lesson is not about domains.** Four negative probes agreeing with each other is not evidence;
they were four spellings of one guess. The habit that would have found this on the first try is
enumerating the surface rather than guessing paths against it.

### How a domain is actually enforced

A domain is not a standalone object. It is a **governed tag policy** plus a domain record binding
it, and the enforcement lives in the tag policy:

```console
$ databricks api get /api/2.1/tag-policies | jq '.tag_policies[] | select(.tag_key=="dng_domain")'
{
  "tag_key": "dng_domain",
  "description": "Business domain assignment for gold assets (GOV-003)",
  "values": [{"name": "customer_marketing"}, {"name": "trade_promotions"},
             {"name": "store_operations"}]
}
```

Assignment is plain SQL, and the distinction that matters shows up immediately:

| statement | result |
|---|---|
| `ALTER TABLE … SET TAGS ('dng_domain' = 'store_operations')` | `SUCCEEDED` |
| `ALTER TABLE … SET TAGS ('dng_domain' = 'not_a_real_domain')` | **rejected** |
| `ALTER TABLE … SET TAGS ('made_up_key' = 'anything')` | `SUCCEEDED` |

```
[ErrorClass=INVALID_PARAMETER_VALUE] Tag value not_a_real_domain is not an allowed value
for tag policy key dng_domain. Allowed values: [customer_marketing, trade_promotions,
store_operations]
```

The third row is the one that decides whether GOV-003 is worth anything. **An ungoverned tag is
accepted with no policy behind it at all**, so a `domain` tag applied without a tag policy is a
free-text label that happens to be spelled like governance. Only the policy-backed key rejects a
value that is not in the register. GOV-003 must be implemented against the governed key or it
asserts nothing — which is the same failure mode as an existence check passing while the rate is
wrong.

Assignments read back through `information_schema.table_tags`, so the requirement's "exactly one"
is a `GROUP BY … HAVING count(*) = 1` and not an API walk.

### The part that is a finding about this repository, not about Databricks

The `dng_domain` tag policy and the domain record already existed when this probe ran — created
2026-08-07T03:46Z, with a description naming GOV-003. **Nothing in the repository creates them.**
No script, no bundle resource, no migration. They exist because a session created them by hand and
did not write it down.

That is exactly the state ADR-0002 and the traceability gate exist to prevent: workspace
configuration that no committed artifact reproduces. A fresh Free Edition account following this
repository's `make bootstrap` would not have them, and GOV-003 would fail there while passing here
— the worst kind of green, since it is green on the author's machine only. Recorded here so the
gap is visible until a bootstrap script owns it.

## M1 — UC Metric Views exist, and the first probe's failure was the useful one

```sql
CREATE OR REPLACE VIEW dng_dev.gold.mv_probe
WITH METRICS
LANGUAGE YAML
AS $$
version: 0.1
source: dng_dev.gold.agg_store_daily
dimensions:
  - name: store
    expr: store_id
measures:
  - name: total_sales
    expr: SUM(net_sales_amt)
$$
```

```
[UNRESOLVED_COLUMN.WITH_SUGGESTION] A column … with name `net_sales_amt` cannot be resolved.
Did you mean one of the following? [`source`.`sales_amt`, `store`, `source`.`baskets`, …]
```

A column error, not a syntax error — which answers the availability question more convincingly than
a success would have. The parser accepted `WITH METRICS LANGUAGE YAML`, resolved `source:` to a
real materialized view, and bound the dimension. Only the measure expression was wrong, and it was
wrong because the probe guessed a column name.

With `sales_amt` it creates, and the object is first-class in the catalog:

| check | result |
|---|---|
| `information_schema.tables.table_type` | `METRIC_VIEW` |
| `SELECT store, total_sales FROM …` | **rejected** — `[METRIC_VIEW_MISSING_MEASURE_FUNCTION]` |
| `SELECT store, MEASURE(total_sales) … GROUP BY store` | `[['367','21425.59'], ['406','17969.34'], ['361','12521.98']]` |

The middle row is the property that makes MOD-005 meaningful. A measure cannot be selected as if it
were a column; the engine refuses and names the reason. A metric view is therefore not a view with
a naming convention — it is a definition the query engine enforces an aggregation contract against,
which is precisely the difference between "the KPI is defined once" and "the KPI's SQL was pasted
into one place this time".

`mv_probe`, the stray tags and the free-form key were all dropped after measurement. Nothing in
this section left state behind.

---

## Both requirements are now implemented

No fallback was built, because none was needed. `scripts/governance_register.py` is the committed
source of truth and `scripts/publish_governance.py` applies it — idempotently, with `--check` as a
gate that mutates nothing and exits non-zero on drift.

- **GOV-003** — five domains, seven gold assets, exactly one domain each. The script now *owns* the
  `dng_domain` tag policy, which closes the gap in the previous section: the policy is created and
  kept in sync from `DOMAINS` rather than existing because someone made it by hand.
- **MOD-005** — two metric views, twelve KPIs. `mv_commercial` (8 measures over `fct_basket_line`)
  and `mv_household_lifecycle` (4 over `agg_household_rfm`).

### G2 — the shared fact has no business owner, and saying so is the design

GOV-003 wants exactly one domain per asset. `fct_basket_line` genuinely serves all five North Star
decisions, so any business domain named as its owner would be a fiction that survives until the
first cross-domain change request. It is assigned to `commercial_core`, a shared/stewarded domain,
and `gold_reconciliation` to `platform_operations` — an asset about the platform rather than about
the business, kept separate so a reviewer filtering for business assets excludes it rather than
finding a revenue-shaped table with no decision behind it.

Worth admitting at this size: seven assets across five domains is close to one each, and a taxonomy
only earns its keep once several assets share an owner. Here it documents intended ownership rather
than organising a crowded catalog.

### G3 — metric views are gold assets too

`information_schema.tables` lists a metric view with `table_type = METRIC_VIEW`, so any GOV-003 scan
over the gold schema picks them up. Publishing MOD-005 without registering its own output would
have broken GOV-003 — the two requirements are coupled in a direction neither one mentions. The
publish order in the script is metric-views-then-domains for the same reason: a view that does not
exist yet cannot be tagged.

## M2 — `units_sold` was measuring coupons, not units

The first definition of `units_sold` was `SUM(quantity_units)`. It published, resolved and returned
20,319,550 units across 21,479 baskets — **946 units per basket**, for a grocery chain.

| | units | share |
|---|---|---|
| `COUPON/MISC ITEMS` (1,776 lines) | 20,064,652 | **98.7%** |
| merchandise | 254,898 | 1.3% |

Line-level median quantity is 1 and p99 is 10; the maximum is 48,073, all on `COUPON/MISC ITEMS`.
On those rows the column carries a coupon face value, not a count of items. Excluding that
commodity gives 254,898 units, **11.87 per basket**, which is a grocery basket.

Stated explicitly because the two edits are indistinguishable from outside: **this corrects a
definition that measured the wrong quantity — it does not move a threshold to obtain a nicer
number.** The measurement was taken first, it is reproduced in the KPI's own definition text, and a
reader can disagree with it.

Nothing structural would have caught this. The measure was registered, published, unique, and
returned a number. Only dividing it by baskets and knowing what a grocery basket looks like did.

## M3 — non-additive measures are the argument for metric views

`baskets` is `COUNT(DISTINCT basket_id)`. Sliced by `promo_exposure`:

| exposure | baskets |
|---|---|
| not_promoted | 20,190 |
| mailer | 8,665 |
| display | 6,082 |
| both | 4,995 |
| unknown | 561 |
| **sum of slices** | **40,493** |
| **true total** | **21,479** |

One basket contains lines in several exposure buckets, so the slices overlap. A pre-aggregated
basket count would be correct at its own grain and silently wrong at every other — the metric view
re-derives it from the fact per grouping instead. This is the concrete reason MOD-005 asks for a
governed metric definition rather than a well-named summary table.
