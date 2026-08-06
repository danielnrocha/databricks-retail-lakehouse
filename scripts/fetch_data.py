#!/usr/bin/env python3
"""Fetch the dunnhumby seed datasets from the publisher's CDN.

Reproducibility matters more than convenience here. A dataset acquired by "I downloaded it from
a page once" is not reproducible; six months later the link is dead and nobody knows which
revision the numbers in the README came from. So: pinned URLs, recorded sizes, checksum written
on first fetch and verified on every subsequent one.

Source: https://www.dunnhumby.com/source-files/  (Licence: CC BY 4.0 — see data/README.md)

Usage:
    python3 scripts/fetch_data.py                 # the Complete Journey seed
    python3 scripts/fetch_data.py --dataset real-50k
    python3 scripts/fetch_data.py --list
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW = REPO_ROOT / "data" / "raw"
MANIFEST = RAW / "manifest.json"

CDN = "https://assets.ctfassets.net/psj0p18eh7z1"


@dataclass(frozen=True)
class Dataset:
    key: str
    name: str
    path: str
    approx_bytes: int
    note: str


DATASETS: dict[str, Dataset] = {
    "complete-journey": Dataset(
        key="complete-journey",
        name="dunnhumby — The Complete Journey",
        path="3e9OAF7F9ONT4pwJc1luEw/a8e5c0036e64fc6267dc12e1b625ab13/dunnhumby_The-Complete-Journey.zip",
        approx_bytes=134_716_463,
        note=(
            "Primary seed. 8 relational tables, 2 years, 2,500 households. "
            "Carries the business semantics: baskets, campaigns, coupons, promotion exposure."
        ),
    ),
    "real-50k": Dataset(
        key="real-50k",
        name="dunnhumby — Let's Get Sort-of-Real (50K customer sample)",
        path="12PzBBsw5BGLhQlhu5qWlT/a645c6913b42d6987ec97c28a2dfce80/dunnhumby_Let-s-Get-Sort-of-Real-_Sample-50K-customers_.zip",
        approx_bytes=416_842_563,
        note=(
            "Optional. Larger transaction volume for the performance lab when the Complete "
            "Journey's 2.6M lines are not enough to make a query plan hurt. Lacks the campaign "
            "and coupon tables, so it cannot replace the primary seed."
        ),
    ),
}


def _download(url: str, dest: Path) -> None:
    print(f"  fetching {url}")
    with urllib.request.urlopen(url) as response, dest.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(response, out)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest() -> dict[str, dict[str, object]]:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {}


def fetch(dataset: Dataset, *, force: bool) -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    archive = RAW / f"{dataset.key}.zip"
    extracted = RAW / dataset.key
    manifest = _load_manifest()

    if archive.exists() and not force:
        print(f"{dataset.key}: archive present, skipping download (use --force to refetch)")
    else:
        print(f"{dataset.key}: ~{dataset.approx_bytes / 1e6:.0f} MB")
        _download(f"{CDN}/{dataset.path}", archive)

    checksum = _sha256(archive)
    recorded = manifest.get(dataset.key, {}).get("sha256")

    if recorded and recorded != checksum:
        # The publisher replacing a file in place is exactly the scenario that silently
        # invalidates every number downstream. Fail loudly rather than proceed.
        print(
            f"CHECKSUM MISMATCH for {dataset.key}\n"
            f"  recorded: {recorded}\n"
            f"  actual:   {checksum}\n"
            "The upstream file changed. Do not proceed until you know what changed — every "
            "profiled distribution and every committed fixture assumes the recorded version.",
            file=sys.stderr,
        )
        return 1

    if extracted.exists() and not force:
        print(f"{dataset.key}: already extracted at {extracted.relative_to(REPO_ROOT)}")
    else:
        if extracted.exists():
            shutil.rmtree(extracted)
        print(f"  extracting -> {extracted.relative_to(REPO_ROOT)}")
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extracted)

    manifest[dataset.key] = {
        "name": dataset.name,
        "url": f"{CDN}/{dataset.path}",
        "bytes": archive.stat().st_size,
        "sha256": checksum,
        "licence": "CC BY 4.0",
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    csvs = sorted(p for p in extracted.rglob("*.csv"))
    print(f"  {len(csvs)} CSV file(s):")
    for csv in csvs:
        print(f"    {csv.name:<24} {csv.stat().st_size / 1e6:>8.1f} MB")
    print(f"  manifest updated: sha256={checksum[:16]}...")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="complete-journey", choices=sorted(DATASETS))
    parser.add_argument("--force", action="store_true", help="refetch and re-extract")
    parser.add_argument("--list", action="store_true", help="list available datasets and exit")
    args = parser.parse_args()

    if args.list:
        for ds in DATASETS.values():
            print(f"{ds.key:<18} {ds.approx_bytes / 1e6:>7.0f} MB  {ds.name}")
            print(f"{'':<18} {'':>7}     {ds.note}\n")
        return 0

    return fetch(DATASETS[args.dataset], force=args.force)


if __name__ == "__main__":
    sys.exit(main())
