"""PRF-005: every performance claim carries measurements and a stated input volume.

The rule this enforces is the one the whole repository is built on — a claim without a measurement
is an opinion — and it is the rule most likely to erode, because prose is easy to add and nobody
lints prose.

What is machine-checkable here, and what is not
------------------------------------------------
A test cannot decide whether a paragraph is *honest*. It can decide whether the document contains
the structural markers of measurement: numbers with units, a stated input volume, and — for the
lab, whose numbers come from the platform — a traceable identifier per run.

So this test checks the scaffolding, not the truth. That is a real limit and it is stated rather
than implied: a determined author can satisfy every assertion below and still write something
misleading. What the test buys is that the *shape* of unmeasured prose — comparative adjectives
with no figures attached — cannot pass silently.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs" / "architecture" / "perf-evidence.md"
LAB = ROOT / "docs" / "architecture" / "perf-lab.md"
RUNS = ROOT / "data" / "perf"

pytestmark = pytest.mark.unit

# Words that assert a performance difference. Each must appear near a figure, or it is an adjective
# doing a measurement's job.
COMPARATIVES = re.compile(
    r"\b(faster|slower|reduced|improved|degraded|speedup|overhead|cheaper)\b", re.IGNORECASE
)
# A figure: a number with a unit or a multiplier. Deliberately broad — the point is to catch prose
# with NO numbers at all, not to police formatting.
FIGURE = re.compile(r"\d[\d,.]*\s*(x|×|%|ms|s\b|MiB|MB|GB|KB|KiB|rows|bytes)", re.IGNORECASE)


# Paragraphs that use a comparative word to DEFINE a metric rather than to claim a result. Each
# entry is a substring and a reason, so an exception is reviewable rather than silent — the same
# ledger pattern the hardcoded-catalog test uses. Weakening the regex instead would have been the
# easy fix and would have let real unmeasured claims through with it.
DEFINITIONAL: dict[str, str] = {
    "task-seconds the warehouse retired per wall-clock second": (
        "Defines the parallelism-efficiency proxy. Explains what the ratio means; the measured "
        "values for it appear in the tables immediately below."
    ),
}


@pytest.mark.parametrize("path", [EVIDENCE, LAB], ids=lambda p: p.name)
def test_every_claim_has_measurements(path: Path) -> None:
    """No comparative claim appears in a paragraph with no figures in it."""
    assert path.exists(), f"{path.name} is missing; PRF-005 has nothing to check."

    unsupported: list[str] = []
    for block in path.read_text(encoding="utf-8").split("\n\n"):
        if block.lstrip().startswith(("|", "```", ">")):
            continue  # tables and code carry their own numbers
        if any(marker in block for marker in DEFINITIONAL):
            continue
        if COMPARATIVES.search(block) and not FIGURE.search(block):
            unsupported.append(" ".join(block.split())[:110])

    assert not unsupported, (
        f"{path.name}: performance claims with no figures in the same paragraph:\n  "
        + "\n  ".join(unsupported)
        + "\nState the number or drop the adjective."
    )


@pytest.mark.parametrize("path", [EVIDENCE, LAB], ids=lambda p: p.name)
def test_input_volume_is_stated(path: Path) -> None:
    """A measurement without its input volume is not reproducible and not comparable."""
    text = path.read_text(encoding="utf-8")
    markers = ("rows", "MB", "MiB", "lines")
    assert any(m in text for m in markers), (
        f"{path.name} states no input volume. A timing with no volume cannot be compared to "
        "anything, including a later run of itself."
    )


def test_lab_runs_are_traceable() -> None:
    """Every recorded lab run carries the platform identifier that produced it.

    This is what separates the lab's numbers from numbers in a document. `statement_id` resolves in
    `system.query.history`, so a reader can go and check rather than take the table's word for it.
    """
    files = sorted(RUNS.glob("*_runs.json"))
    assert files, "No measured runs recorded under data/perf/."

    # The files are variant-grouped: [{variant, runs: [{statement_id, ...}], discarded}]. Walking
    # nested `runs` rather than assuming a flat list -- the first version of this test asserted the
    # flat shape and failed on every row, which was the test being wrong about the data rather than
    # the data being wrong.
    def measurements(node: object):
        if isinstance(node, list):
            for item in node:
                yield from measurements(item)
        elif isinstance(node, dict):
            if "runs" in node and isinstance(node["runs"], list):
                yield from measurements(node["runs"])
            elif "execution_duration_ms" in node or "statement_id" in node:
                yield node

    missing: list[str] = []
    total = 0
    for path in files:
        for index, row in enumerate(measurements(json.loads(path.read_text(encoding="utf-8")))):
            total += 1
            if not row.get("statement_id"):
                missing.append(f"{path.name}[{index}]")

    assert total > 0, f"No measurements found in {[f.name for f in files]}."

    assert not missing, (
        "Runs recorded without a statement_id, so they cannot be traced back to "
        f"system.query.history: {missing[:8]}"
    )
