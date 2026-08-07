"""Silver — the conformed basket-line fact, its quarantine, and the quality gate between them.

Three things happen here, in this order, and the order is the design:

1. **Conform.** Bronze types are source-faithful, which means `quantity` is a string and the
   transaction time lives in two differently-named columns. Silver fixes both, and records *that*
   it had to — see `transaction_time_source` and `quantity_format` below.
2. **Gate.** Every row is evaluated against the rules published in `<catalog>.ops.dq_rules`.
   Rows failing an `error` rule are routed to `fact_basket_line_quarantine` with the rule name and
   the rule text on the row. Nothing is dropped (NFR-3).
3. **Deduplicate, idempotently.** The CDC source replays roughly 1% of events. Both the fact and
   the quarantine are populated by `AUTO CDC ... STORED AS SCD TYPE 1` keyed on `event_id`, which
   is an upsert: a replay rewrites the same row instead of appending a second one.

Why AUTO CDC rather than `dropDuplicates`
-----------------------------------------
`dropDuplicates` on a stream is stateful and bounded by whatever state the checkpoint happens to
hold. It deduplicates *within* the watermark and silently stops doing so outside it, so a replay
arriving a day late duplicates the row and nothing reports it. An upsert keyed on the business key
has no such horizon: the target converges to one row per key regardless of when the replay lands.
That is exactly-once *effect*, which is achievable; it is not exactly-once *delivery*, which is
not, and conflating the two is how "we handle duplicates" becomes untrue at 3am.

Row conservation (QLT-003)
--------------------------
The identity this pipeline asserts is stronger than `input = passed + quarantined`::

    bronze rows = duplicates collapsed + passed + quarantined

Deduplication removes rows, and folding it into "input" would hide the one number that proves the
replay scenario worked. `ops.row_conservation` asserts the full identity with a fail-the-update
expectation and no tolerance.
"""

from __future__ import annotations

from pyspark import pipelines as dp
from pyspark.sql import Column, DataFrame, SparkSession
from pyspark.sql import functions as F

spark = SparkSession.getActiveSession()
assert spark is not None, "This module runs inside a Lakeflow pipeline."

CATALOG = spark.conf.get("dng.catalog")
DATASET = "silver.fact_basket_line"

# ---------------------------------------------------------------------------------------------
# Layer B — read the governed ruleset at graph-construction time.
#
# This is the documented reusable-expectations pattern: rules live in a Unity Catalog table, are
# collected while the graph is built, and become both physical routing (quarantine) and Lakeflow
# expectations (event-log telemetry). Two properties follow that a hard-coded rule list does not
# have: the rules are grantable and auditable like any other table, and several pipelines can
# share one definition instead of drifting apart.
#
# No try/except. If the table is missing the pipeline must fail — a silent fallback to "no rules"
# would produce a green run with an ungated silver layer, which is the single worst outcome
# available here.
# ---------------------------------------------------------------------------------------------
_rules = (
    spark.read.table(f"{CATALOG}.ops.dq_rules")
    .filter(F.col("dataset") == DATASET)
    .select("rule_name", "expression", "severity", "ruleset_version")
    .collect()
)
if not _rules:
    raise ValueError(
        f"No quality rules published for {DATASET} in {CATALOG}.ops.dq_rules. "
        "Run `python -m retail_lakehouse.quality.publish` before deploying (QLT-001)."
    )

ERROR_RULES = {r["rule_name"]: r["expression"] for r in _rules if r["severity"] == "error"}
WARN_RULES = {r["rule_name"]: r["expression"] for r in _rules if r["severity"] == "warn"}
RULESET_VERSION = _rules[0]["ruleset_version"]


# ---------------------------------------------------------------------------------------------
# Column specification.
#
# One tuple drives both the projection and the DDL, so the declared type and the expression that
# produces it cannot drift. Declaring the schema explicitly is not ceremony: an implicit schema
# means an upstream type change silently rewrites the table, and the first symptom is a dashboard
# that stops matching. MOD-006 also requires every monetary column to name its currency in a
# Unity Catalog comment, and a comment has to be attached to a declared column.
#
# Currency note: the dunnhumby seed states no currency. USD is asserted here as a modelling
# decision, recorded in the comment rather than assumed silently — an unlabelled money column is
# the reason two systems eventually add dollars to pounds.
# ---------------------------------------------------------------------------------------------
_TIME_SOURCE = (
    F.when(F.col("trans_time").isNotNull(), F.lit("trans_time"))
    .when(F.col("transaction_time").isNotNull(), F.lit("transaction_time"))
    .otherwise(F.lit("missing"))
)

# The retype drift turns `12` into `"12 units"` partway through the stream. Parse the leading
# numeral and keep the raw string alongside, so the parse is reversible and auditable.
_QUANTITY_DIGITS = F.regexp_extract(F.col("quantity"), r"^\s*([0-9]+(?:\.[0-9]+)?)", 1)
_QUANTITY_FORMAT = (
    F.when(F.col("quantity").rlike(r"^\s*[0-9]+(\.[0-9]+)?\s*$"), F.lit("numeric"))
    .when(F.col("quantity").rlike(r"^\s*[0-9]+(\.[0-9]+)?\s+\S+\s*$"), F.lit("unit_suffixed"))
    .otherwise(F.lit("unparsed"))
)

COLUMNS: tuple[tuple[str, str, Column, str], ...] = (
    (
        "event_id",
        "STRING",
        F.col("event_id"),
        "Business key. Stable and content-derived at source, so a replayed event carries the same id and the SCD1 upsert collapses it.",
    ),
    (
        "basket_id",
        "BIGINT",
        F.col("basket_id").cast("bigint"),
        "Synthetic basket identifier assigned by the amplifier.",
    ),
    ("line_no", "INT", F.col("line_no").cast("int"), "Line position within the basket."),
    (
        "household_key",
        "BIGINT",
        F.col("household_key").cast("bigint"),
        "FK to silver.dim_household_scd2. Join point-in-time on transaction_ts.",
    ),
    ("store_id", "BIGINT", F.col("store_id").cast("bigint"), "FK to silver.dim_store."),
    (
        "product_id",
        "BIGINT",
        F.col("product_id").cast("bigint"),
        "FK to silver.dim_product_scd2. Join point-in-time on transaction_ts.",
    ),
    (
        "transaction_ts",
        "TIMESTAMP",
        F.col("event_ts").cast("timestamp"),
        "Event time at the till. The point-in-time join key for every Type 2 dimension.",
    ),
    (
        "transaction_date",
        "DATE",
        F.to_date(F.col("event_ts")),
        "Date part of transaction_ts. A liquid clustering key (ADR-0007), not a partition column.",
    ),
    (
        "transaction_time_hhmm",
        "INT",
        F.coalesce(F.col("trans_time"), F.col("transaction_time")).cast("int"),
        "Time of day as a packed HHMM integer, reconciled across the upstream rename.",
    ),
    (
        "transaction_time_source",
        "STRING",
        _TIME_SOURCE,
        "Which upstream column supplied transaction_time_hhmm: trans_time before the rename, transaction_time after. Kept so the drift stays visible instead of being coalesced into invisibility.",
    ),
    (
        "quantity_units",
        "BIGINT",
        _QUANTITY_DIGITS.cast("double").cast("bigint"),
        "Units sold. For weight-priced items this is grams and legitimately reaches five figures (finding F6) — do not bound it.",
    ),
    (
        "quantity_raw",
        "STRING",
        F.col("quantity"),
        "The source value verbatim, including the '12 units' form the retype drift introduced. Kept so the parse is reversible.",
    ),
    (
        "quantity_format",
        "STRING",
        _QUANTITY_FORMAT,
        "How quantity_raw was shaped: numeric, unit_suffixed, or unparsed. A shift in this distribution is the drift signal.",
    ),
    (
        "sales_amt",
        "DOUBLE",
        F.col("sales_value").cast("double"),
        "Amount charged for the line, USD. Zero is legitimate: a fully coupon-offset line (F6).",
    ),
    (
        "retail_disc_amt",
        "DOUBLE",
        F.col("retail_disc").cast("double"),
        "Retailer-funded discount, USD. Negative or zero; a positive value is a sign error and is quarantined.",
    ),
    (
        "coupon_disc_amt",
        "DOUBLE",
        F.col("coupon_disc").cast("double"),
        "Manufacturer coupon discount, USD. Negative or zero.",
    ),
    (
        "coupon_match_disc_amt",
        "DOUBLE",
        F.col("coupon_match_disc").cast("double"),
        "Retailer coupon-match discount, USD. Negative or zero.",
    ),
    (
        "week_no",
        "INT",
        F.col("week_no").cast("int"),
        "Week index relative to the seed window, 1-102. Not a calendar week — the seed is day-relative (ADR-0003).",
    ),
    (
        "loyalty_tier",
        "STRING",
        F.col("loyalty_tier"),
        "Loyalty tier at time of sale. NULL before the column was added upstream, which is drift, not absence of a tier.",
    ),
    (
        "is_synthetic",
        "BOOLEAN",
        F.col("is_synthetic").cast("boolean"),
        "TRUE for amplifier-generated rows. MLR-003 requires evaluation sets to exclude these.",
    ),
    (
        "source_basket_id",
        "BIGINT",
        F.col("source_basket_id").cast("bigint"),
        "The seed basket this row was resampled from. The audit trail back to real data.",
    ),
    (
        "_source_file",
        "STRING",
        F.col("_source_file"),
        "Landing-zone file this row arrived in (ING-002).",
    ),
    (
        "_source_file_ts",
        "TIMESTAMP",
        F.col("_source_file_ts"),
        "Arrival time of that file. Differs from transaction_ts for late-arriving events, which is the point.",
    ),
    (
        "_ingest_ts",
        "TIMESTAMP",
        F.col("_ingest_ts"),
        "Bronze ingestion time. Part of the SCD1 sequence.",
    ),
    ("_pipeline_id", "STRING", F.col("_pipeline_id"), "Pipeline that ingested the row (ING-002)."),
)


def _quote(comment: str) -> str:
    """SQL string literal. The doubling is not decorative.

    Two of the comments below quote a value verbatim — `'12 units'`, and a question ending in a
    quoted phrase — and an unescaped apostrophe terminates the literal, producing a parse error
    a thousand characters into a generated DDL statement. Building SQL by concatenation without
    escaping is the same class of mistake as string-building a WHERE clause; it fails here as a
    syntax error rather than as an injection only because the inputs are ours.
    """
    return "'" + comment.replace("'", "''") + "'"


def _ddl(extra: tuple[tuple[str, str, str], ...] = ()) -> str:
    parts = [
        f"{name} {sql_type} COMMENT {_quote(comment)}" for name, sql_type, _, comment in COLUMNS
    ]
    parts += [f"{name} {sql_type} COMMENT {_quote(comment)}" for name, sql_type, comment in extra]
    return ", ".join(parts)


def _violations() -> Column:
    """`array<struct<rule_name, rule_expression>>` of every `error` rule this row fails.

    A rule that evaluates to NULL is not a violation. That matches Lakeflow expectation semantics
    and is the right default: a rule about a column says nothing about rows where the column is
    absent. Presence is a separate rule, and merging the two produces a quarantine reason that
    means two different things.

    Rules are evaluated in **name order**, not dict order. The quarantine table's `rule_name`
    column is element zero of this array, so an unordered evaluation would make the headline
    reason for a multi-rule failure depend on how the rules table happened to be read — a column
    that changes between runs without the data changing is worse than no column.
    """
    candidates = [
        F.when(
            F.expr(f"({expression}) IS FALSE"),
            F.struct(
                F.lit(name).alias("rule_name"),
                F.lit(expression).alias("rule_expression"),
            ),
        )
        for name, expression in sorted(ERROR_RULES.items())
    ]
    return F.array_compact(F.array(*candidates))


def _conformed() -> DataFrame:
    """Bronze, typed and reconciled, with the quality verdict attached.

    Called by both the valid and the rejected view rather than being materialised once. That reads
    like waste and is not: each view becomes its own flow with its own checkpoint, and the
    alternative — a shared intermediate streaming table — buys one table scan at the cost of an
    extra materialisation and a second place for the two halves to fall out of step.
    """
    events = spark.readStream.table(f"{CATALOG}.bronze.basket_line_events_raw")
    projected = events.select(*[expression.alias(name) for name, _, expression, _ in COLUMNS])
    return projected.withColumn("_dq_violations", _violations())


@dp.temporary_view(name="basket_line_valid")
@dp.expect_all(WARN_RULES)
def basket_line_valid() -> DataFrame:
    """Rows that pass every `error` rule.

    `warn` rules are attached here rather than upstream so their counts describe the population
    that actually reaches silver. A warn count computed over rows that were about to be
    quarantined anyway would double-count the same defect under two names.
    """
    return _conformed().filter(F.size("_dq_violations") == 0).drop("_dq_violations")


@dp.temporary_view(name="basket_line_rejected")
def basket_line_rejected() -> DataFrame:
    """Rows failing at least one `error` rule, shaped for the quarantine table.

    One row per rejected input row, not one row per violation. Exploding to per-violation grain
    would give a prettier reason table and would break row conservation, which is the invariant
    that actually matters: `input = passed + quarantined` has to be checkable by counting. The
    full violation list is kept in `failed_rules` for rows that break more than one rule.
    """
    return (
        _conformed()
        .filter(F.size("_dq_violations") > 0)
        .withColumn("rule_name", F.col("_dq_violations")[0]["rule_name"])
        .withColumn("rule_expression", F.col("_dq_violations")[0]["rule_expression"])
        .withColumn("failed_rules", F.col("_dq_violations").getField("rule_name"))
        .withColumn("failed_at", F.current_timestamp())
        .withColumn("ruleset_version", F.lit(RULESET_VERSION))
        .drop("_dq_violations")
    )


dp.create_streaming_table(
    name=DATASET,
    comment=(
        "Conformed basket-line fact. One row per event_id; duplicate CDC delivery is collapsed by "
        "an SCD Type 1 upsert rather than by a stateful dropDuplicates, so a replay arriving "
        "outside any watermark still cannot duplicate a row. Rows failing an error-severity rule "
        "in ops.dq_rules are in fact_basket_line_quarantine, never dropped."
    ),
    table_properties={"quality": "silver", "delta.enableChangeDataFeed": "true"},
    # ADR-0007: liquid clustering on the two predicates present in essentially every downstream
    # query. No partitioning, no Z-ORDER, and the keys are revisable without a rewrite.
    cluster_by=["transaction_date", "store_id"],
    schema=_ddl(),
    # These must hold by construction, because the error gate upstream already rejected anything
    # that would violate them. Asserting them anyway is how you find out the gate stopped working
    # — an expectation that can never fail is worthless, and one that should never fail is a
    # tripwire.
    expect_all_or_fail={
        "gate_held_event_id": "event_id IS NOT NULL",
        "gate_held_transaction_time": "transaction_time_hhmm IS NOT NULL",
        "gate_held_quantity": "quantity_units IS NOT NULL AND quantity_units >= 0",
    },
)

dp.create_auto_cdc_flow(
    target=DATASET,
    source="basket_line_valid",
    keys=["event_id"],
    # A struct, not a single column. Replayed duplicates share an ingest timestamp, so a
    # single-column sequence leaves the winner undefined — the row count stays right and the
    # checksum does not, which breaks MOD-004 in a way that only shows up on a re-run comparison.
    sequence_by=F.struct(F.col("_ingest_ts"), F.col("_source_file")),
    stored_as_scd_type="1",
)

dp.create_streaming_table(
    name=f"{DATASET}_quarantine",
    comment=(
        "Rows rejected by an error-severity rule in ops.dq_rules, with the rule that rejected "
        "them (QLT-002). One row per rejected input row, so input = passed + quarantined holds by "
        "counting. Keyed on event_id and upserted, so a replayed bad row does not inflate the "
        "quarantine either."
    ),
    table_properties={"quality": "silver_quarantine"},
    cluster_by=["rule_name", "transaction_date"],
    schema=_ddl(
        (
            (
                "rule_name",
                "STRING",
                "The first error-severity rule this row failed. Machine-readable, matches ops.dq_rules.rule_name.",
            ),
            (
                "rule_expression",
                "STRING",
                "The rule text as published, so the row can be re-evaluated without looking anything up.",
            ),
            (
                "failed_rules",
                "ARRAY<STRING>",
                "Every error rule this row failed, for rows that break more than one.",
            ),
            ("failed_at", "TIMESTAMP", "When the rule was evaluated."),
            (
                "ruleset_version",
                "STRING",
                "Ruleset version in force at rejection. Answers 'would this row still be rejected today?'",
            ),
        )
    ),
)

dp.create_auto_cdc_flow(
    target=f"{DATASET}_quarantine",
    source="basket_line_rejected",
    keys=["event_id"],
    sequence_by=F.struct(F.col("_ingest_ts"), F.col("_source_file")),
    stored_as_scd_type="1",
)
