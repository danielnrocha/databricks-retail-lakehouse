# Performance evidence

> Requirement **PRF-005**: every performance claim in this repository cites a measurement, states
> its input volume, and names what was held constant. A claim without those three is an opinion.
>
> Entries are append-only and dated. A superseded measurement is struck through, never deleted —
> knowing that a number *used* to be true is how you notice when a platform regresses.

---

## PE-001 — Bulk upload to a Unity Catalog volume: SDK vs CLI

**Date:** 2026-08-06 · **Environment:** Free Edition, serverless, `dng_dev` · **Client:** macOS, same network session throughout

### What was measured

Uploading the dunnhumby seed to `/Volumes/dng_dev/bronze/seed`.

The first attempt used `databricks fs cp` on the raw CSVs. It moved five small files, then spent
over ten minutes on `product.csv` (6.4 MB) before the command was killed. At that rate the 847 MB
seed would take somewhere past a day.

The second attempt changed **two** variables at once — CSV → Parquet, and CLI → SDK. That makes
the combined result uninterpretable, so a third run isolated the transport.

### Isolation run

Same file, same session, same network. Only the transport differs.

| Transport | File | Size | Time | Throughput |
|---|---|---:|---:|---:|
| `databricks fs cp` (CLI v1.11.0) | `product.csv` | 6.4 MB | **> 600 s** (killed) | < 0.011 MB/s |
| `WorkspaceClient.files.upload` (SDK 0.125.0) | `product.csv` | 6.4 MB | **3.0 s** | 2.2 MB/s |

**Speedup: > 200×, and that is a lower bound** — `fs cp` was terminated at the timeout, not
observed to complete, so the true ratio is unknown and larger.

### Format conversion, measured separately

| Table | Rows | CSV | Parquet (zstd-9, dict) | Ratio |
|---|---:|---:|---:|---:|
| `causal_data` | 36,786,524 | 695.9 MB | 36.6 MB | **19.0×** |
| `transaction_data` | 2,595,732 | 141.7 MB | 16.3 MB | 8.7× |
| `product` | 92,353 | 6.4 MB | 0.9 MB | 7.5× |
| `coupon` | 124,548 | 2.8 MB | 0.4 MB | 6.6× |
| `campaign_table` | 7,208 | 0.1 MB | 0.0 MB | 7.3× |
| `hh_demographic` | 801 | 0.0 MB | 0.0 MB | 6.6× |
| `coupon_redempt` | 2,318 | 0.1 MB | 0.0 MB | 4.2× |
| `campaign_desc` | 30 | 0.0 MB | 0.0 MB | **0.3×** |
| **Total** | | **847.0 MB** | **54.2 MB** | **15.6×** |

Full seed upload via SDK after conversion: **~15 s**.

### Conclusions, separated by what actually caused them

1. **The upload speedup is transport, not format.** `files.upload` is >200× faster than `fs cp`
   for the same bytes. Attributing the improvement to Parquet — the intuitive read, since the
   headline went from "ten minutes for 6 MB" to "fifteen seconds for 54 MB" — would have been
   wrong, and would have sent anyone repeating the exercise down the wrong path.

2. **Parquet's win is storage and downstream reads**, worth 15.6× on this seed and 19× on the
   34M-row promotion table. That is a real and separate benefit; it just is not the one that fixed
   the upload.

3. **`campaign_desc` got *larger* as Parquet (0.3×).** Thirty rows cannot amortise Parquet's
   footer, schema, and per-column metadata. A blanket "convert everything to Parquet" rule is
   wrong at small scale, and the fact that it is wrong by only a few kilobytes here is exactly why
   this kind of rule survives unexamined into places where it costs real money.

### What changed as a result

- `scripts/upload_seed.py` uses the SDK, never `fs cp`.
- The seed is stored as Parquet; the *event stream* stays newline-delimited JSON in the landing
  volume, because that is what Auto Loader consumes and what a real feed looks like. Bulk history
  and streaming ingest are different problems and get different formats.

### Why this belongs in a portfolio

A migration plan sized around `fs cp` throughput would be off by more than two orders of
magnitude. That is not a micro-optimisation — it is the difference between a weekend cutover and
a quarter-long programme, and it is invisible until someone measures instead of assuming the CLI
and the SDK are two front doors to the same thing.
