"""PRF-006 and the table-layout rules ADR-0007 declares.

These are static checks over the pipeline source rather than queries against a live workspace, so
they run offline (ENV-006) and fail on a pull request rather than after a deploy. A layout rule
enforced only by review is a layout rule that survives until the first deadline.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "retail_lakehouse"
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

pytestmark = pytest.mark.unit


def _pipeline_sources() -> list[Path]:
    return sorted(SRC.rglob("pipeline/*.py")) + sorted(SRC.glob("bronze/*.py"))


def test_po_and_manual_optimize_disjoint() -> None:
    """PRF-006: no table is subject to both Predictive Optimization and a manual OPTIMIZE.

    Running both on one table is a documented anti-pattern rather than merely redundant: they
    contend, and the manual run reverses decisions Predictive Optimization made while still being
    billed for the rewrite. The failure is invisible — the table is fine, the bill is not — so it
    has to be caught structurally.

    Every table here is Unity Catalog managed, so Predictive Optimization applies by default.
    Therefore the rule reduces to: no manual OPTIMIZE anywhere in the pipeline or operator code.
    """
    offenders: list[str] = []
    pattern = re.compile(r"\bOPTIMIZE\s+[\w.${}]+", re.IGNORECASE)

    for path in [*_pipeline_sources(), *sorted(SCRIPTS.glob("*.py"))]:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            # Prose about OPTIMIZE is the whole point of several docstrings in this repo, so only
            # executable lines count. A comment-blind check would make the rule unwritable-about.
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            if pattern.search(line) and "autoOptimize" not in line:
                offenders.append(f"{path.name}:{lineno}: {stripped[:80]}")

    assert not offenders, (
        "Manual OPTIMIZE found on Predictive-Optimization-managed tables:\n  "
        + "\n  ".join(offenders)
        + "\nPick one. Running both contends and bills you for the rewrite PO already scheduled."
    )


def test_no_partitioned_by() -> None:
    """ADR-0007: liquid clustering everywhere, no Hive-style partitioning.

    At this data's scale a daily partition holds a few megabytes, so partitioning is a small-file
    generator. It is also a structural commitment — the column is encoded in the physical path, so
    changing your mind means rewriting the table.
    """
    offenders = [
        f"{path.name}:{lineno}"
        for path in _pipeline_sources()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if "partitioned_by" in line.lower() and not line.strip().startswith("#")
    ]
    assert not offenders, f"PARTITIONED BY found: {offenders}. ADR-0007 requires liquid clustering."


def test_streaming_sinks_enable_auto_compaction() -> None:
    """Auto-compaction on streaming sinks only.

    It runs synchronously after each write on the writing cluster: the right trade for a 30-second
    trigger producing thousands of sub-megabyte files, and pure overhead on a daily batch write.
    Bronze is the streaming sink here.
    """
    bronze = (SRC / "bronze" / "events.py").read_text(encoding="utf-8")
    assert "delta.autoOptimize.autoCompact" in bronze, (
        "The streaming sink does not enable auto-compaction. A 30-second trigger produces "
        "~2,880 commits per day, and the generator's measured median file size was 172 KB — "
        "95x below the 16 MB floor PRF-003 asks for."
    )
