"""Bronze — event ingestion via Auto Loader into a Lakeflow streaming table.

API note, because it dates the code: this uses `from pyspark import pipelines as dp`, the Spark
Declarative Pipelines module, not `import dlt`. Delta Live Tables became Lakeflow Spark
Declarative Pipelines; `dlt` still works indefinitely for backward compatibility, so migrating is
optional. It is done here because the open-source SDP API is what Spark 4.1 ships, and code that
targets the durable interface ages better than code that targets the compatibility shim.

Bronze rules, enforced rather than encouraged:

* **Append-only, source-faithful.** No business logic, no filtering, no type coercion beyond what
  Auto Loader does. The moment bronze starts "cleaning" data you lose the ability to answer
  "what did the source actually send?", which is the only question bronze exists to answer.
* **Nothing is dropped.** Fields that do not fit the schema land in `_rescued_data`. A pipeline
  that drops a malformed field and carries on is losing data silently, which is worse than
  failing, because nobody investigates a green run.
* **Every row carries its provenance** (ING-002). Without `_source_file` you cannot answer
  "where did this bad row come from?", and that question is asked at the worst possible moment.
"""

from __future__ import annotations

from pyspark import pipelines as dp
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

spark = SparkSession.getActiveSession()
assert spark is not None, "This module runs inside a Lakeflow pipeline."

# Injected by the bundle target -> pipeline configuration -> Spark conf. Never a literal, never an
# environment variable read from inside the code (ENV-001).
CATALOG = spark.conf.get("dng.catalog")
LANDING = spark.conf.get("dng.events_landing")
SCHEMA_LOCATION = spark.conf.get("dng.schema_location", f"{LANDING}/_schema")


@dp.table(
    name="basket_line_events_raw",
    comment=(
        "Raw basket-line events from the landing volume. Append-only, source-faithful. "
        "Fields that do not fit the inferred schema are preserved in _rescued_data, never "
        "dropped. Do not query this for analysis — use silver."
    ),
    table_properties={
        # Streaming sink with a short trigger produces many small files. Auto-compaction runs
        # synchronously after each write on the writing cluster: the right trade here, pure
        # overhead on a daily batch table. See ADR-0007.
        "delta.autoOptimize.autoCompact": "true",
        "delta.autoOptimize.optimizeWrite": "true",
        "quality": "bronze",
    },
    # AUTO lets Databricks infer clustering from observed query patterns rather than making us
    # guess before a single query exists. Bronze is ingest-only, so there is nothing to guess from.
    cluster_by_auto=True,
)
def basket_line_events_raw() -> DataFrame:
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "json")
        # Auto Loader must track the schema across restarts. Without schemaLocation every restart
        # re-infers from whatever files happen to be present, so the schema silently depends on
        # retention rather than on the data contract.
        .option("cloudFiles.schemaLocation", SCHEMA_LOCATION)
        # addNewColumns: the pipeline stops on a genuinely new column, records it, and picks it
        # up on restart. The alternative worth understanding is `rescue`, which never stops but
        # buries new columns in _rescued_data forever — quieter, and quietly wrong, because a new
        # upstream field is a business event that someone should notice.
        .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
        .option("cloudFiles.rescuedDataColumn", "_rescued_data")
        .option("cloudFiles.inferColumnTypes", "true")
        # Hint only what is genuinely ambiguous. The first version of this file also hinted
        # `quantity STRING`, reasoning that a defensive widening was harmless. It was not: the
        # generator's retype drift turns quantity from an int into "12 units" partway through the
        # stream, and pre-widening the column meant the incompatible values were accepted as
        # ordinary strings. `_rescued_data` stayed at zero rows across 200,000 events, and the
        # scenario built to demonstrate rescue never exercised it.
        #
        # The general trap: a schema hint is an assertion that you already know the type. Every
        # hint you add is a class of drift you have chosen not to be told about. Hint the
        # ambiguous, never the merely inconvenient.
        .option("cloudFiles.schemaHints", "event_ts TIMESTAMP")
        .load(LANDING)
        # ING-002. `_metadata` is a hidden column Auto Loader exposes; capturing it here is the
        # only chance — it is not recoverable downstream.
        .select(
            "*",
            F.col("_metadata.file_path").alias("_source_file"),
            F.col("_metadata.file_modification_time").alias("_source_file_ts"),
            F.current_timestamp().alias("_ingest_ts"),
            F.lit(spark.conf.get("pipelines.id", "local")).alias("_pipeline_id"),
        )
    )


@dp.materialized_view(
    name="ingest_health",
    comment=(
        "Per-source-file ingest summary. Exists so that 'the pipeline ran' and 'the pipeline "
        "ingested what it should have' are separately answerable questions."
    ),
)
def ingest_health() -> DataFrame:
    events = spark.read.table(f"{CATALOG}.bronze.basket_line_events_raw")
    return events.groupBy("_source_file").agg(
        F.count("*").alias("row_count"),
        F.min("_ingest_ts").alias("first_ingest_ts"),
        F.max("_ingest_ts").alias("last_ingest_ts"),
        F.min("event_ts").alias("min_event_ts"),
        F.max("event_ts").alias("max_event_ts"),
        # A non-zero rescued count is the signal that upstream changed shape. Counting it per
        # file localises the change to a point in time instead of "sometime last week".
        F.sum(F.when(F.col("_rescued_data").isNotNull(), 1).otherwise(0)).alias("rescued_rows"),
        F.approx_count_distinct("store_id").alias("distinct_stores"),
    )
