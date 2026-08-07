# ML — the gate held, and the model lost

The household-lapse model **failed its promotion gate** and was not registered. That is the
result, it is not being spun as a success, and the rest of this document explains why it is the
most useful outcome the layer could have produced.

---

## M1 — Gold cannot be used for machine learning at all

Measured before anything else was built:

```sql
SELECT is_synthetic, count(*) FROM dng_dev.gold.fct_basket_line GROUP BY 1;
-- true   198,013
```

**100% of the gold fact table is synthetic.** Every row came from the amplifier. That is correct
and intended — the amplified stream exists to exercise the *pipeline*: small files, late arrival,
schema drift, duplicate delivery. It did all four.

But MLR-003 forbids synthetic rows in evaluation, and the reason is sharper than "synthetic data
is bad". The amplifier resamples **whole observed baskets**, so it invents no generating rule and
introduces no fake correlation. What it cannot do is produce a household the seed never showed.
Every household in the stream is a resampled copy of a real one, so a train/test split over
amplified data **leaks by construction** — the same real basket can land on both sides.

So the ML layer reads `transaction_data` directly: 2,595,732 real lines, 2,500 real households.
The pipeline work and the modelling work use different inputs on purpose.

This is worth stating loudly because the alternative is so easy and so invisible: gold is right
there, it has 198,013 rows, it joins cleanly, and a model trained on it would report a beautiful
metric that means nothing.

## M2 — The baseline won, on both metrics

Temporal split: features from days 1–547, outcome observed over days 548–711.

| metric | model (LightGBM, 18 features) | baseline (recency alone) |
|---|---:|---:|
| PR-AUC | 0.1420 | **0.3846** |
| ROC-AUC | 0.8867 | **0.9274** |

Relative lift on PR-AUC: **−63.1%** against a required +10%. Gate: **FAIL**. Not registered.

The baseline is not a strawman. It is one column — days since last purchase — sorted descending,
with no fitting and no parameters. It is what a competent analyst produces with a SQL query in ten
minutes, and on lapse problems it is genuinely hard to beat because recency really is the dominant
signal. The model's own feature importance agrees: `recency_days` has the top gain by a factor of
1.7 over the next feature.

**The gate did exactly what it exists for.** A model that cannot beat a free baseline is a model
that costs money to maintain and adds nothing, and the most common way that model reaches
production is that nobody computed the baseline.

## M3 — The evaluation is underpowered, and that is the real finding

Losing to the baseline is not the whole story. The deeper problem is that this evaluation could
not have resolved the difference either way.

| split day | outcome window | households | lapsed | rate |
|---:|---:|---:|---:|---:|
| 400 | 44 weeks | 2,497 | 27 | 1.1% |
| 480 | 33 weeks | 2,498 | 47 | 1.9% |
| **547** | **23 weeks** | **2,498** | **76** | **3.0%** |
| 620 | 13 weeks | 2,499 | 171 | 6.8% |
| 660 | 7 weeks | 2,499 | 310 | 12.4% |

At the chosen boundary: **76 positives**, of which **19 land in the test split**.

PR-AUC computed over 19 positives is dominated by sampling noise. The 10% margin the gate demands
is far inside the confidence interval of a metric with that many events. So the honest reading is
not "the model is bad" — it is **"this experiment cannot tell a good model from a bad one"**, and
the loss to the baseline is consistent with a model overfitting 57 training positives across 18
features.

### Why the base rate is so low, and why that is structural

The table above shows the lapse rate is a function of **how long you wait**, not of household
behaviour. And the reason it stays low even at 44 weeks is in the dataset's own definition:
dunnhumby selected **2,500 frequent shoppers**. Households that lapse are, by the selection
criteria, largely not in the sample.

**The dataset's own inclusion criteria make its churn problem nearly unlearnable.** No amount of
feature engineering fixes a 3% base rate over 2,498 rows; that is roughly one positive per 33
households, and there is no model that extracts a reliable signal from 57 training examples spread
across 18 dimensions.

## M4 — What was deliberately not done

The obvious next move is to tune until the model wins: shorten the outcome window to day 660,
where the base rate reaches 12.4% and the problem becomes learnable, then report the win.

**That would be choosing the experiment to fit the desired result.** The 23-week window was chosen
before any model was trained, on the business reasoning that a grocery household's inter-purchase
interval is days, so 23 weeks of silence means lapsed rather than "between trips". Seven weeks of
silence does not mean lapsed; it means someone went on holiday. Moving the boundary to 660 would
produce a better metric measuring a worse question.

Similarly not done: class weighting, resampling, threshold tuning, or reducing the feature count
until the model edges past the baseline. Each is defensible in isolation and all of them, applied
after seeing the result, are the same act — searching the space of experimental setups for one
that produces the number you wanted.

The margin (`REQUIRED_RELATIVE_LIFT = 0.10`) is declared as a module constant above the training
code, before the data is loaded, precisely so it cannot be adjusted to fit.

## M5 — What would make this problem learnable

Stated so the failure is actionable rather than merely honest:

1. **More households.** dunnhumby's *Let's Get Sort-of-Real* covers ~300M transactions across a
   far larger panel and is already registered in `scripts/fetch_data.py` as `real-50k`. It lacks
   the campaign and coupon tables, so it cannot serve D1, but for D2 it would raise the positive
   count by orders of magnitude.
2. **A different target.** Predicting *spend decline* — a continuous outcome available for every
   household — instead of binary lapse turns 76 positives into 2,498 observations. It answers a
   slightly different business question, which is a decision for the CRM owner in the North Star,
   not for the modeller.
3. **A population without the selection effect.** The lapse problem is best posed on all
   households, not on those pre-selected for frequency.

---

## Requirements

| ID | Status | Note |
|---|---|---|
| MLR-002 | **fails, correctly** | model does not beat the baseline; not registered |
| MLR-003 | holds | evaluation reads the real seed only; gold is 100% synthetic and unused |
| MLR-004 | holds | registration is inside the gate, so a failing model never becomes promotable |
| MLR-006 | holds | `assert_no_leakage` rebuilds features from truncated input and asserts equality |
| MLR-001 | partial | run logged with params, metrics and feature importance; reproduction not re-run |
| MLR-005 | not attempted | no drift monitoring without a registered model to monitor |

MLR-002 is listed as *failing correctly*. A requirement that fails because the system honestly
reports a bad result is working; a requirement that passes because the experiment was reshaped
until it did is the thing this whole repository is arguing against.
