#!/usr/bin/env python3
"""Document every gold column, and find out whether the comments survive a refresh.

Finding G3 left a question open: Unity Catalog propagates a column comment when the column passes
through a single upstream source unchanged, and drops it when the column comes from a join or an
expression. That leaves 9 of 22 fact columns undocumented — and they are exactly the derived ones,
so documentation coverage degrades precisely where the modelling gets interesting.

`@dp.materialized_view(comment=...)` sets the *table* comment only. The two ways to set column
comments are an explicit `schema=` on the decorator — which the silver work found does not escape
its generated DDL — or `ALTER TABLE ... ALTER COLUMN ... COMMENT` afterwards.

The open question with the second approach is **durability**: a materialized view is rebuilt on
every pipeline update, and if the rebuild drops the comments then this script is a treadmill
rather than a fix. Running it, then running the pipeline, then running `--check` answers that
empirically, because "probably survives" is not a governance posture.

    python3 scripts/document_gold_columns.py --catalog dng_dev
    python3 scripts/document_gold_columns.py --catalog dng_dev --check
"""

from __future__ import annotations

import argparse
import sys

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

# Only the columns UC does not propagate: the joined and the derived. Pass-through columns inherit
# their silver comments, and re-stating them here would create two sources of truth that drift.
COMMENTS: dict[str, dict[str, str]] = {
    "fct_basket_line": {
        "department": "Product department, resolved point-in-time against dim_product_scd2.",
        "brand_tier": "National or Private label. NOT a brand name -- the source column is called BRAND and has cardinality 2 (finding F5). Manufacturer is the brand-like dimension.",
        "commodity_desc": "Product commodity, resolved point-in-time. 308 distinct values in the seed.",
        "manufacturer_id": "Manufacturer identifier, resolved point-in-time. 6,476 distinct values.",
        "income_band": "Household income band. Null for 1,699 of 2,500 households -- demographics cover 32% of the panel, so this must never be a required model input.",
        "household_composition": "Household composition group. Same 32% coverage caveat as income_band.",
        "household_has_demographics": "TRUE when the household appears in hh_demographic. Use to split populations, not to filter -- filtering silently restricts analysis to a self-selected third.",
        "store_volume_decile": "Store decile by transaction volume. Derived, not a store master attribute; the seed has no store dimension.",
        "promo_exposure": "Three-state promotion exposure: display, mailer, both, not_promoted, or unknown. 'unknown' means the week falls outside the 9-101 collection window, so exposure was never recorded. Treating unknown as not_promoted fabricates a signal (finding F3).",
    },
    "agg_household_rfm": {
        "household_key": "Household identifier. 2,500 in the seed panel.",
        "recency_days": "Days between the household's last transaction and the dataset's own maximum date. Measured against the data, never wall-clock time -- anchoring to today would make every household look lapsed by however long ago collection ended.",
        "frequency_baskets": "Distinct baskets in the observation window.",
        "monetary_amt": "Total spend, USD.",
        "avg_basket_amt": "Mean spend per basket, USD.",
        "distinct_departments": "Departments purchased from. A breadth proxy.",
        "distinct_commodities": "Commodities purchased from. A finer breadth proxy.",
        "coupon_share_of_spend": "Share of spend on lines carrying a coupon discount, 0 to 1.",
        "promo_share_of_spend": "Share of spend on lines with display or mailer exposure, 0 to 1. Excludes unknown-exposure lines from the numerator but not the denominator.",
        "has_demographics": "TRUE when demographics exist for this household. 801 of 2,500.",
        "first_seen_date": "First transaction date observed.",
        "last_seen_date": "Last transaction date observed.",
    },
    "agg_promo_performance": {
        "week_no": "Week index, 1-102, relative to the seed window. No calendar anchor exists (ADR-0003), so no seasonality may be inferred.",
        "product_id": "Product identifier.",
        "department": "Product department at the time of sale.",
        "promo_exposure": "Three-state exposure. See fct_basket_line.promo_exposure.",
        "sales_amt": "Sales for this product-week-exposure combination, USD.",
        "quantity_units": "Units sold. For weight-priced items this is grams, which is why values reach 89,638 legitimately (finding F6).",
        "baskets": "Distinct baskets.",
        "households": "Distinct households.",
        "stores": "Distinct stores.",
        "retail_disc_amt": "Retailer-funded discount, USD. Negative or zero.",
    },
    "agg_store_daily": {
        "transaction_date": "Calendar date derived from the anchored relative day. Day-of-week structure is real; month and holiday effects are not recoverable.",
        "store_id": "Store identifier.",
        "store_volume_decile": "Store decile by transaction volume.",
        "sales_amt": "Daily revenue, USD.",
        "baskets": "Distinct baskets.",
        "households": "Distinct households transacting.",
        "lines": "Transaction lines.",
        "avg_basket_amt": "Mean basket value, USD.",
    },
    "gold_reconciliation": {
        "gold_rows": "Row count of gold.fct_basket_line.",
        "silver_rows": "Row count of silver.fact_basket_line.",
        "row_variance": "gold_rows minus silver_rows. Any non-zero value means a join changed the grain.",
        "gold_revenue": "Sum of sales_amt in gold, USD.",
        "silver_revenue": "Sum of sales_amt in silver, USD.",
        "revenue_variance": "gold_revenue minus silver_revenue. The point-in-time join exists to keep this at zero; the naive key-only join moves it by 1.706%.",
        "reconciles": "TRUE when both variances are zero. Asserted every update rather than checked once.",
        "measured_at": "When this reconciliation ran.",
    },
}


def _run(client: WorkspaceClient, warehouse: str, sql: str) -> tuple[bool, str]:
    result = client.statement_execution.execute_statement(
        warehouse_id=warehouse, statement=sql, wait_timeout="50s"
    )
    if result.status and result.status.state == StatementState.SUCCEEDED:
        return True, ""
    message = result.status.error.message if result.status and result.status.error else "?"
    return False, (message or "?")[:160]


def uncommented(client: WorkspaceClient, warehouse: str, catalog: str) -> list[tuple[str, str]]:
    result = client.statement_execution.execute_statement(
        warehouse_id=warehouse,
        wait_timeout="50s",
        statement=(
            "SELECT table_name, column_name FROM system.information_schema.columns "
            f"WHERE table_catalog = '{catalog}' AND table_schema = 'gold' "
            "AND comment IS NULL AND table_name NOT LIKE '__materialization%' "
            "ORDER BY table_name, ordinal_position"
        ),
    )
    data = result.result.data_array if result.result else None
    return [(r[0] or "", r[1] or "") for r in (data or [])]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--check", action="store_true", help="report only, change nothing")
    args = parser.parse_args()

    client = WorkspaceClient()
    warehouses = list(client.warehouses.list())
    if not warehouses or warehouses[0].id is None:
        raise RuntimeError("No SQL warehouse with an id is available.")
    warehouse = warehouses[0].id

    before = uncommented(client, warehouse, args.catalog)
    print(f"uncommented gold columns before: {len(before)}")

    if args.check:
        for table, column in before:
            print(f"  {table}.{column}")
        return 1 if before else 0

    applied = failed = 0
    for table, columns in COMMENTS.items():
        for column, text in columns.items():
            escaped = text.replace("'", "''")
            ok, error = _run(
                client,
                warehouse,
                # COMMENT ON COLUMN, not ALTER TABLE ... ALTER COLUMN. A Lakeflow materialized
                # view is a VIEW, and the ALTER form rejects it outright with
                # EXPECT_TABLE_NOT_VIEW.NO_ALTERNATIVE. `ALTER VIEW ... ALTER COLUMN` is not valid
                # syntax either. COMMENT ON COLUMN is the one form that works on both, which is
                # not obvious from the docs and cost a full pass of 42 failures to discover.
                f"COMMENT ON COLUMN {args.catalog}.gold.{table}.{column} IS '{escaped}'",
            )
            if ok:
                applied += 1
            else:
                failed += 1
                print(f"  FAIL {table}.{column}: {error}")

    after = uncommented(client, warehouse, args.catalog)
    print(f"applied {applied}, failed {failed}")
    print(f"uncommented gold columns after: {len(after)}")
    for table, column in after:
        print(f"  still null: {table}.{column}")
    return 0 if not after else 1


if __name__ == "__main__":
    sys.exit(main())
