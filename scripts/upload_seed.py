#!/usr/bin/env python3
"""Convert the dunnhumby seed to Parquet and upload it to a Unity Catalog volume.

Why Parquet and not the raw CSV
--------------------------------
The first attempt uploaded CSVs directly. `databricks fs cp` moved 6.4 MB in over ten minutes,
which puts the 838 MB seed somewhere north of a day. That is a tooling limit, not a network one,
and it is worth knowing before you plan a migration around bulk file copies.

Converting first is also the better design, independent of speed. The seed is a **one-time bulk
historical load**, and Parquet is the right format for that: columnar, typed, dictionary-encoded.
`causal_data` is 36.8M rows of low-cardinality integers, so it compresses hard.

CSV does not disappear from the architecture — it is simply moved to where it belongs. The
*event stream* written by the amplifier stays as newline-delimited JSON in the landing volume,
because that is what Auto Loader consumes and what a real event feed looks like. Bulk history and
streaming ingest are different problems and deserve different formats; collapsing them into one
because "the source was CSV" is how a landing zone becomes a swamp.

Usage:
    python3 scripts/upload_seed.py --env dev
    python3 scripts/upload_seed.py --env dev --convert-only
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
from databricks.sdk import WorkspaceClient

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW = REPO_ROOT / "data" / "raw" / "complete-journey"
INTERIM = REPO_ROOT / "data" / "interim" / "seed-parquet"

# Explicit schemas. Inferring types from CSV is how STORE_ID becomes a float in one table and a
# string in another, and the join then silently matches nothing. Typing at the boundary is
# cheaper than debugging a zero-row result three layers downstream.
SCHEMAS: dict[str, pa.Schema] = {
    "transaction_data": pa.schema(
        [
            ("household_key", pa.int32()),
            ("BASKET_ID", pa.int64()),
            ("DAY", pa.int16()),
            ("PRODUCT_ID", pa.int64()),
            ("QUANTITY", pa.int32()),
            ("SALES_VALUE", pa.float64()),
            ("STORE_ID", pa.int32()),
            ("RETAIL_DISC", pa.float64()),
            ("TRANS_TIME", pa.int16()),
            ("WEEK_NO", pa.int16()),
            ("COUPON_DISC", pa.float64()),
            ("COUPON_MATCH_DISC", pa.float64()),
        ]
    ),
    "causal_data": pa.schema(
        [
            ("PRODUCT_ID", pa.int64()),
            ("STORE_ID", pa.int32()),
            ("WEEK_NO", pa.int16()),
            ("display", pa.string()),
            ("mailer", pa.string()),
        ]
    ),
    "product": pa.schema(
        [
            ("PRODUCT_ID", pa.int64()),
            ("MANUFACTURER", pa.int32()),
            ("DEPARTMENT", pa.string()),
            ("BRAND", pa.string()),
            ("COMMODITY_DESC", pa.string()),
            ("SUB_COMMODITY_DESC", pa.string()),
            ("CURR_SIZE_OF_PRODUCT", pa.string()),
        ]
    ),
}


def convert(csv_path: Path, out_path: Path) -> tuple[int, int, int]:
    """CSV -> Parquet. Returns (rows, csv_bytes, parquet_bytes)."""
    schema = SCHEMAS.get(csv_path.stem)
    convert_options = (
        pacsv.ConvertOptions(column_types=schema) if schema else pacsv.ConvertOptions()
    )
    table = pacsv.read_csv(csv_path, convert_options=convert_options)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # ZSTD over SNAPPY: roughly 30% smaller for a one-time load where decompression happens once.
    # For a hot table read thousands of times the trade would go the other way.
    pq.write_table(table, out_path, compression="zstd", compression_level=9, use_dictionary=True)
    return table.num_rows, csv_path.stat().st_size, out_path.stat().st_size


def upload(client: WorkspaceClient, local: Path, volume_path: str) -> float:
    start = time.monotonic()
    with local.open("rb") as handle:
        client.files.upload(volume_path, handle, overwrite=True)
    return time.monotonic() - start


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="dev", choices=("dev", "test", "prod"))
    parser.add_argument("--convert-only", action="store_true")
    args = parser.parse_args()

    csvs = sorted(RAW.rglob("*.csv"))
    if not csvs:
        print(f"No CSVs under {RAW}. Run: python3 scripts/fetch_data.py", file=sys.stderr)
        return 1

    client = None if args.convert_only else WorkspaceClient()
    volume = f"/Volumes/dng_{args.env}/bronze/seed"

    total_csv = total_pq = 0
    print(f"{'table':<22}{'rows':>12}{'csv':>10}{'parquet':>10}{'ratio':>8}{'upload':>10}")
    print("-" * 72)

    for csv_path in csvs:
        out = INTERIM / f"{csv_path.stem}.parquet"
        rows, csv_bytes, pq_bytes = convert(csv_path, out)
        total_csv += csv_bytes
        total_pq += pq_bytes

        elapsed = ""
        if client is not None:
            seconds = upload(client, out, f"{volume}/{out.name}")
            elapsed = f"{seconds:.1f}s"

        print(
            f"{csv_path.stem:<22}{rows:>12,}{csv_bytes / 1e6:>9.1f}M{pq_bytes / 1e6:>9.1f}M"
            f"{csv_bytes / max(pq_bytes, 1):>7.1f}x{elapsed:>10}"
        )

    print("-" * 72)
    print(
        f"{'TOTAL':<22}{'':>12}{total_csv / 1e6:>9.1f}M{total_pq / 1e6:>9.1f}M"
        f"{total_csv / max(total_pq, 1):>7.1f}x"
    )
    if client is not None:
        print(f"\nuploaded to {volume}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
