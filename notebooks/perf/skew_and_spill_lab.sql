-- Databricks notebook source
-- MAGIC %md
-- MAGIC # Skew and spill lab — interactive reproduction
-- MAGIC
-- MAGIC Every measurement in `docs/architecture/perf-lab.md` is produced by
-- MAGIC `python -m retail_lakehouse.perf.cli <stage>`, which records raw per-run metrics to
-- MAGIC `data/perf/*.json`. This notebook is the *inspection* surface: it re-derives the
-- MAGIC data-shape measurements — the ones that are cheap, deterministic and do not need a
-- MAGIC repeated-run harness — and shows the Query Profile for each variant so a reader can
-- MAGIC check the physical plan themselves.
-- MAGIC
-- MAGIC **Attach to the 2X-Small serverless SQL warehouse.** Every number in the write-up is
-- MAGIC stated against that warehouse and nothing else.
-- MAGIC
-- MAGIC What this notebook cannot show you, and why:
-- MAGIC
-- MAGIC | Missing | Reason |
-- MAGIC |---|---|
-- MAGIC | Spark UI stage/task detail | Does not exist on serverless. |
-- MAGIC | Per-task durations | No surface exposes them, so Databricks' own skew definition (max task > 1.5x p75) is unmeasurable here. |
-- MAGIC | `shuffle_read_bytes` | The column exists in `system.query.history` and reads 0 for every statement on this workspace. |
-- MAGIC | Any `spark.sql.*` toggle | None are settable on a serverless SQL warehouse — run the cell below and read the error. |

-- COMMAND ----------

-- MAGIC %md ## 0. What the platform refuses
-- MAGIC Each of these fails with `CONFIG_NOT_AVAILABLE`. That is the reason every intervention
-- MAGIC in this lab is code-level: there is no config A/B to run.

-- COMMAND ----------

SET spark.sql.adaptive.skewJoin.enabled = true;

-- COMMAND ----------

SET spark.sql.shuffle.partitions = 200;

-- COMMAND ----------

-- MAGIC %md ## 1. Lab tables
-- MAGIC Loaded unclustered from the Parquet seed. `numFiles = 1` on both is worth noticing: read
-- MAGIC parallelism comes from row groups, not files.

-- COMMAND ----------

DESCRIBE DETAIL dng_dev.perf.transactions;

-- COMMAND ----------

DESCRIBE DETAIL dng_dev.perf.causal;

-- COMMAND ----------

-- MAGIC %md ## 2. Key skew
-- MAGIC The dataset profile reports 2,519x on `STORE_ID` and 9,926x on `PRODUCT_ID`. The question
-- MAGIC this lab exists to answer is whether that survives into the *composite* key the join
-- MAGIC actually uses.

-- COMMAND ----------

WITH per_key AS (
  SELECT STORE_ID AS k, count(*) AS n FROM dng_dev.perf.transactions GROUP BY STORE_ID
)
SELECT 'STORE_ID' AS key_expr, count(*) AS distinct_keys, max(n) AS max_rows,
       percentile(n, 0.5) AS median_rows, max(n) / percentile(n, 0.5) AS max_over_median
FROM per_key
UNION ALL
SELECT 'PRODUCT_ID, STORE_ID, WEEK_NO', count(*), max(n), percentile(n, 0.5),
       max(n) / percentile(n, 0.5)
FROM (
  SELECT count(*) AS n FROM dng_dev.perf.transactions
  GROUP BY PRODUCT_ID, STORE_ID, WEEK_NO
);

-- COMMAND ----------

-- MAGIC %md ## 3. Post-shuffle partition sizes
-- MAGIC `hash()` in Spark SQL is Murmur3, the function `HashPartitioner` uses, so `pmod(hash(k), n)`
-- MAGIC reproduces the real bucketing rather than modelling it. Compare `max_partition_bytes`
-- MAGIC against AQE's 256 MB condition — and note that the factor condition (>5x median) and the
-- MAGIC byte condition must **both** hold before AQE splits anything.

-- COMMAND ----------

WITH per_partition AS (
  SELECT pmod(hash(STORE_ID), 1024) AS p, count(*) AS n
  FROM dng_dev.perf.transactions
  GROUP BY pmod(hash(STORE_ID), 1024)
)
SELECT max(n) AS max_rows,
       percentile(n, 0.5) AS median_rows,
       max(n) / percentile(n, 0.5) AS max_over_median,
       -- 40 bytes = UnsafeRow header + 4 fields, the projection the join carries.
       max(n) * 40 / 1024 / 1024 AS est_max_partition_mib,
       268435456 / max(n) AS break_even_bytes_per_row
FROM per_partition;

-- COMMAND ----------

-- MAGIC %md ## 4. The join under test
-- MAGIC Open the Query Profile for the next cell. The plan is a `PhotonBroadcastHashJoin`: the
-- MAGIC planner broadcasts the 14.4 MiB fact side unprompted, so `causal` is never shuffled by the
-- MAGIC join key and AQE skew handling — which only applies to shuffle joins — cannot engage at all.

-- COMMAND ----------

SELECT t.STORE_ID, sum(t.SALES_VALUE) AS sales, count(*) AS matched_lines
FROM dng_dev.perf.transactions t
JOIN dng_dev.perf.causal c
  ON t.PRODUCT_ID = c.PRODUCT_ID AND t.STORE_ID = c.STORE_ID AND t.WEEK_NO = c.WEEK_NO
GROUP BY t.STORE_ID;

-- COMMAND ----------

-- MAGIC %md Forcing a sort-merge join is the only way to get a keyed shuffle in this plan.
-- MAGIC `EXPLAIN FORMATTED` confirms `SortMergeJoin` with an exchange on both sides.

-- COMMAND ----------

EXPLAIN FORMATTED
SELECT /*+ SHUFFLE_MERGE(t, c) */
       t.STORE_ID, sum(t.SALES_VALUE) AS sales, count(*) AS matched_lines
FROM dng_dev.perf.transactions t
JOIN dng_dev.perf.causal c
  ON t.PRODUCT_ID = c.PRODUCT_ID AND t.STORE_ID = c.STORE_ID AND t.WEEK_NO = c.WEEK_NO
GROUP BY t.STORE_ID;

-- COMMAND ----------

-- MAGIC %md ## 5. Join coverage — the 1.4% that hides an 80% loss
-- MAGIC An inner join here drops 1.4% of transaction lines and 80% of stores. Both variants are
-- MAGIC measured in the write-up; this cell is the reason the LEFT JOIN variant exists at all.

-- COMMAND ----------

SELECT count(*) AS all_lines,
       count(c.PRODUCT_ID) AS matched_lines,
       round(100.0 * count(c.PRODUCT_ID) / count(*), 2) AS pct_lines_matched,
       count(DISTINCT t.STORE_ID) AS all_stores,
       count(DISTINCT c.STORE_ID) AS matched_stores
FROM dng_dev.perf.transactions t
LEFT JOIN dng_dev.perf.causal c
  ON t.PRODUCT_ID = c.PRODUCT_ID AND t.STORE_ID = c.STORE_ID AND t.WEEK_NO = c.WEEK_NO;

-- COMMAND ----------

-- MAGIC %md ## 6. Spill
-- MAGIC Nothing on the native seed spills. The next two cells are the same query at two sort-key
-- MAGIC widths over the identical 36,786,524 rows; only the second one spills. Check
-- MAGIC `spilled_local_bytes` in `system.query.history` afterwards, not the Query Profile's
-- MAGIC wall-clock.

-- COMMAND ----------

SELECT max(rn) AS max_rank
FROM (
  SELECT row_number() OVER (
    ORDER BY lpad(cast(PRODUCT_ID AS string), 10, '0'), STORE_ID, WEEK_NO
  ) AS rn
  FROM dng_dev.perf.causal
);

-- COMMAND ----------

SELECT max(rn) AS max_rank
FROM (
  SELECT row_number() OVER (
    ORDER BY concat(repeat('x', 512), lpad(cast(PRODUCT_ID AS string), 10, '0')), STORE_ID, WEEK_NO
  ) AS rn
  FROM dng_dev.perf.causal
);

-- COMMAND ----------

-- MAGIC %md ## 7. Read the evidence back
-- MAGIC `system.query.history` lags by roughly six minutes on this workspace, so run this a few
-- MAGIC minutes after the cells above. `query_tags` is populated only for statements submitted
-- MAGIC through the harness; ad-hoc notebook cells arrive untagged.

-- COMMAND ----------

SELECT statement_id,
       left(replace(statement_text, '\n', ' '), 70) AS statement,
       execution_duration_ms,
       total_task_duration_ms,
       round(total_task_duration_ms / nullif(execution_duration_ms, 0), 2) AS parallelism_proxy,
       spilled_local_bytes,
       shuffle_read_bytes,        -- always 0 on this platform; kept visible on purpose
       read_rows,
       read_io_cache_percent,
       query_tags
FROM system.query.history
WHERE start_time > current_timestamp() - INTERVAL 2 HOURS
  AND statement_type = 'SELECT'
ORDER BY start_time DESC
LIMIT 50;
