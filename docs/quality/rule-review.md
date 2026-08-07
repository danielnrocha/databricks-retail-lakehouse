# Rule review — DQX candidates, and what happened to each

`dqx-candidate-rules.yml` is machine-written and enforces nothing. This file is the human decision
for every candidate in it. `src/retail_lakehouse/quality/rules.py` holds the rules that are
actually published to Unity Catalog and enforced.

`tests/unit/test_quality_rules.py::test_generated_rules_require_review` parses the table below and
fails if a candidate has no decision, if a decision names a rule that does not exist, or if a
profiler-derived rule reaches `rules.py` without appearing here. QLT-005 is that gate.

---

## What the candidates would have cost

The profiler was run against a 200,000-row extract of `dng_dev.bronze.basket_line_events_raw`.
It generated 23 rules, all at `criticality: error` — that is, all of them quarantining.

Applying them unreviewed:

| | rows | share |
|---|---:|---:|
| input | 200,000 | |
| **quarantined** | **17,927** | **9.0%** |
| revenue quarantined | £146,099.63 of £619,942.65 | **23.6%** |
| stores affected | 211 of 260 | 81% |
| households affected | 1,886 of 2,337 | 81% |

Nearly a quarter of revenue, routed to quarantine, by rules that a reviewer would have waved
through because each one individually looks reasonable.

**The mechanism is worth naming, because it is not "the profiler is bad".** DQX profiles a
**sample** — 1,000 rows here, the default — and derives each range from that sample's spread.
Every bound is therefore a statement about 0.5% of the data presented as a statement about all of
it. Six of the eleven range rules have a bound that the full table already violates on day one:

| column | profiled bound | actual extreme | first-run effect |
|---|---|---|---|
| `quantity_units` | ≤ 3,893.82 | 48,073 | quarantines weight-priced items (F6) |
| `sales_amt` | ≤ 13.66 | 210.00 | quarantines every large basket line |
| `retail_disc_amt` | ≥ −4.23 | −70.00 | quarantines every large discount |
| `transaction_ts` | ≥ 2024-01-30 | 2024-01-01 | quarantines the first month of history |
| `week_no` | ≥ 5 | 1 | quarantines the first four weeks |
| `transaction_time_hhmm` | 495–2355 | 0–2359 | quarantines early-morning and late-night trade |

The upper bound on `quantity_units` is `3893.8152094863253`. A count of items with thirteen
decimal places is the tell: that number was fitted, not reasoned. Nobody would write it by hand,
and nobody reading a generated file scrutinises it either — which is precisely why generation and
enforcement have to be separate steps with a person in between.

The `sales_amt` bound deserves its own note. It is the most dangerous rule in the file, because
it is the only one whose failure mode is *silent revenue loss that grows with the business*. A
range fitted to a sample's 75th percentile quarantines high-value baskets first, so the metric it
distorts most is the one every commercial conversation starts with.

---

## Decisions

`ACCEPT` — enforced as proposed. `AMEND` — enforced in modified form. `REJECT` — not enforced.

| Candidate | Decision | Enforced as | Why |
|---|---|---|---|
| `event_id_is_null_or_empty` | ACCEPT | `event_id_present` | The dedupe key. Without it a replay cannot be made idempotent. |
| `store_id_is_null` | ACCEPT | `store_id_present` | Foreign key to `dim_store`. |
| `store_id_isnt_in_range` | REJECT | — | Store ids are identifiers, not quantities. A numeric range over an identifier is meaningless: it would reject store 40000 for being large rather than for not existing. Referential integrity against `dim_store` is the correct check, and it is enforced in `ops.join_coverage`. |
| `product_id_is_null` | ACCEPT | `product_id_present` | Foreign key to `dim_product_scd2`; required by QLT-004. |
| `product_id_isnt_in_range` | REJECT | — | Same as `store_id_isnt_in_range`. Identifier, not measure. |
| `household_key_is_null` | ACCEPT | `household_key_present` | Foreign key to `dim_household_scd2`; required by QLT-004. |
| `household_key_isnt_in_range` | REJECT | — | Same. The proposed upper bound was 2,490 against an actual maximum of 2,500, so it would also have rejected ten real households. |
| `week_no_is_null` | AMEND | `week_no_within_seed_window` | Folded into the range rule and downgraded to `warn`. |
| `week_no_isnt_in_range` | AMEND | `week_no_within_seed_window` | Lower bound corrected from 5 to 1, and severity downgraded to `warn`: the window is a property of this snapshot, not of the business. |
| `quantity_units_is_null` | ACCEPT | `quantity_parsed` | An unparseable quantity is a shape nobody has seen; it must not be summed. |
| `quantity_units_isnt_in_range` | AMEND | `quantity_non_negative` | **The headline case, and the reason QLT-005 exists.** `QUANTITY` legitimately reaches 89,638 in the seed because weight-priced items are expressed in grams (F6). Only the sign is asserted, because only the sign is actually impossible. |
| `transaction_time_hhmm_is_null` | ACCEPT | `transaction_time_reconciled` | Kept, with a different meaning than the profiler could have known: it is the tripwire for the `trans_time` → `transaction_time` rename drift. |
| `transaction_time_hhmm_isnt_in_range` | AMEND | `transaction_time_is_valid_hhmm` | Widened to 0–2359 and strengthened: HHMM is a packed integer, so 1373 sits inside any observed range and is still not a time. The profiler cannot know the encoding. |
| `sales_amt_is_null` | ACCEPT | `sales_amt_present` | A null revenue measure is summed as zero and never shows up as a missing row. |
| `sales_amt_isnt_in_range` | AMEND | `sales_amt_non_negative` | Upper bound dropped. Zero is explicitly kept legal — a fully coupon-offset line is legitimate (F6). |
| `retail_disc_amt_is_null` | AMEND | `discount_amounts_present` | Merged with the other two discount null checks: the three columns are one ledger and one defect. |
| `retail_disc_amt_isnt_in_range` | AMEND | `retail_discount_is_not_a_surcharge` | Only the sign survives. Two rows in 200,000 carry a positive retail discount, which is a sign error worth catching; the fitted lower bound of −4.23 would have caught 2,802 legitimate discounts as well. |
| `coupon_disc_amt_is_null` | AMEND | `discount_amounts_present` | Merged, as above. |
| `coupon_disc_amt_isnt_in_range` | AMEND | `coupon_discounts_are_not_surcharges` | Sign only, as above. |
| `coupon_match_disc_amt_is_null` | AMEND | `discount_amounts_present` | Merged, as above. |
| `coupon_match_disc_amt_isnt_in_range` | AMEND | `coupon_discounts_are_not_surcharges` | Sign only, as above. |
| `transaction_ts_is_null` | ACCEPT | `transaction_ts_present` | The point-in-time join key. |
| `transaction_ts_isnt_in_range` | AMEND | `transaction_ts_not_in_future` | Lower bound dropped — it was a month later than the actual start of history. The upper bound is kept as `current_timestamp()`, because a future event time genuinely cannot resolve to any dimension version. |

---

## Rules that no profiler could have proposed

Three enforced rules have no candidate behind them, and the reason is the same in each case: they
assert a relationship between columns, and a column profiler only ever sees one column at a time.

| Rule | Severity | What it asserts |
|---|---|---|
| `revenue_requires_quantity` | error | A line cannot charge money for nothing. Fires on 5 rows. |
| `zero_sales_has_offsetting_discount` | warn | A zero-value line with a positive quantity should have a discount that explains it. Fires on 62 rows. |
| `demographics_flag_matches_payload` | error | `has_demographics` and the demographic columns must agree, so that "unknown" and "not applicable" stay distinguishable (F4). |

`zero_sales_has_offsetting_discount` is `warn` on purpose and the reasoning generalises. Those 62
rows cannot be reconciled — but quarantining them protects zero revenue, by construction, while
removing 62 real lines from basket-size and items-per-visit metrics. Recording a defect you cannot
explain is strictly better than deleting it. Quarantine is for rows that are *unusable*, not for
rows that are *unexplained*.

---

## Net effect

| | rules on `fact_basket_line` | rows quarantined on the 200,000-row extract |
|---|---:|---:|
| candidates, applied unreviewed | 23 | 17,927 |
| reviewed ruleset | 18 (16 `error`, 2 `warn`) | **6** |

The published ruleset is 25 rules in total: 18 on `fact_basket_line` and 7 invariants across the
three dimensions.

Six rows: five that charge money for zero units, and two with a positive retail discount, with one
row failing both. Every one of them is genuinely defective, and none of them is merely unusual.
