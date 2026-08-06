# ADR-0005 — Where streaming stops and batch begins

**Status:** Accepted · **Date:** 2026-08-06 · **Reversal cost:** Low

---

## Context

Streaming is the most over-applied pattern in modern data engineering. It is also the most
over-*claimed*: a great many "real-time platforms" are a `trigger(availableNow=True)` job on a
15-minute cron, which is batch with extra operational surface.

This ADR exists because the honest answer to "is this platform real-time?" is **"partly, and here
is exactly which part and why"**. A reviewer who asks that question is testing whether the
architecture was reasoned about or copied.

## The test applied

A path is streaming **only if** a decision in the North Star §2 table degrades measurably when
latency exceeds minutes. Not "would be nice to see sooner" — *degrades measurably*.

| Decision | Latency tolerance | Streaming? | Reasoning |
|---|---|---|---|
| D1 coupon targeting | Daily | **No** | The campaign wave ships once a day. A score computed at 06:00 and a score computed at 06:00:03 produce the same coupon. Streaming here buys nothing and costs continuous compute. |
| D2 lapse risk | Weekly | **No** | Lapse is defined over a 30-day window. Sub-daily updates are noise on a monthly signal. |
| D3 promo underperformance | Minutes | **Yes** | A 14-day promo that is failing burns ~0.3% of its budget per hour. Detecting at T+1 wastes a day of spend; detecting in 10 minutes does not. The value of latency is directly computable. |
| D4 store anomaly | Minutes | **Yes** | A mispriced SKU or a POS outage leaks revenue continuously. The loss is linear in detection time. |
| D5 NL query | Interactive | **No** | Interactive ≠ streaming. This is a query-latency requirement served by gold materialized views and a SQL warehouse. |

Two of five decisions justify streaming. The architecture reflects that ratio rather than
streaming everything because streaming is impressive.

## Decision

### Streaming path (continuous)
```
event landing volume ──Auto Loader──▶ bronze.basket_line_events  (streaming table)
                                            │
                                            ▼  watermark 6h, dedupe on event_id
                                      silver.fact_basket_line_rt
                                            │
                                            ▼  windowed aggregation
                                      gold.promo_performance_rt   (D3)
                                      gold.store_health_rt        (D4)
```
Trigger: processing time, 30s. Not continuous-mode — 30s is well inside the "minutes" tolerance and
costs materially less. Choosing the loosest trigger that satisfies the SLO is the actual
engineering decision; choosing the tightest is a reflex.

### Batch path (scheduled)
```
Lakebase CDC ──▶ bronze.* ──AUTO CDC──▶ silver dims (SCD1/SCD2) ──▶ gold star schema
                                                                        │
                                                                        ▼
                                                            feature tables → ML (D1, D2)
```
Trigger: daily, 04:00 local, sized to satisfy NFR-2 (gold ready by 06:00).

### The reconciliation problem, and why it is not hidden

Two paths compute overlapping quantities: `gold.promo_performance_rt` (streaming, watermarked,
late-data-lossy) and `gold.fct_promo_performance` (batch, complete). **They will disagree.** That is
not a bug; it is the definition of the trade.

Handling this badly is the classic Lambda-architecture failure: two codebases, two answers,
nobody knows which to trust, and eventually both are distrusted.

Handling here:
1. The streaming table is explicitly typed as **provisional**. Its Unity Catalog comment says so,
   and its column set includes `is_provisional = true`.
2. A daily reconciliation job writes `ops.rt_batch_variance` — the delta between the two, per
   promo, per day. This is a *monitored metric*, not a report nobody reads: variance beyond a
   threshold raises an alert, because a growing gap means the watermark is wrong.
3. Batch is always authoritative. No dashboard mixes the two on one axis.

The reconciliation table is the part most implementations skip, and it is the part that makes the
dual path defensible rather than merely present. Without it, "the streaming number is different"
is a rumour instead of a measurement.

### Why not Kappa (streaming-only, batch as a replay)

Genuinely considered, and it is the more elegant model on paper.

**Rejected on Free Edition:** one active pipeline per pipeline type per account. A streaming-only
architecture would need the continuous pipeline running permanently, leaving nothing for
backfill/replay. On a paid tier this constraint disappears and Kappa becomes the better answer —
recorded here so that the reason is a platform constraint, not a belief about Kappa.

Secondary reason, independent of tier: the SCD Type 2 dimension build is genuinely batch-shaped.
Expressing it as a stream would be contortion for uniformity's sake.

## Consequences

**Positive**
- Streaming cost is bounded to the two paths that pay for it.
- The provisional/authoritative split is explicit in the catalog, so a consumer cannot pick the
  wrong table by accident.
- `ops.rt_batch_variance` turns the dual-path risk into an observable, which is the only version of
  that risk anyone can manage.

**Negative**
- Two code paths compute related logic. Mitigated by factoring shared transformation into
  `src/retail_lakehouse/common/transforms.py`, imported by both, unit-tested once — but the
  duplication is not zero and pretending otherwise would be dishonest.
- Watermark tuning is a real ongoing cost. 6h is derived from the generator's configured max lag;
  in a real deployment it would be derived from a measured arrival-lag distribution, and that
  measurement job would itself be a deliverable.

## Reversal cost: Low

Both paths are declarative pipelines. Collapsing to batch-only means deleting two pipeline
definitions and two gold tables. Nothing else depends on the split. The low reversal cost is
precisely why this decision did not warrant more analysis than it got.
