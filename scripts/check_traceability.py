#!/usr/bin/env python3
"""Enforce that every declared requirement is mapped to a test that exists.

This runs in CI. It is the gate that makes `specs/REQUIREMENTS.md` load-bearing instead of
decorative: without it, requirements drift out of sync with the suite within about two weeks and
nobody notices until a reviewer asks "where is that tested?".

Three failure modes are caught:

1. A requirement declared in REQUIREMENTS.md with no row in traceability.md.
2. A traceability row pointing at a test file that does not exist on disk.
3. A traceability row for a requirement ID that was deleted from REQUIREMENTS.md (stale row).

Deliberately NOT caught: whether the mapped test currently passes. That is pytest's job. Coupling
the two would force either placeholder assertions or a big-bang merge, and both are worse than an
honest PLANNED marker.

Exit code 0 = clean, 1 = violations found.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "specs" / "REQUIREMENTS.md"
TRACEABILITY = REPO_ROOT / "specs" / "traceability.md"

# Requirement IDs look like ING-001, MOD-003, AGT-006.
REQ_ID = re.compile(r"\b([A-Z]{3})-(\d{3})\b")

# A traceability row: | ING-001 | tests/...py::test_name | unit | PLANNED |
TRACE_ROW = re.compile(
    r"^\|\s*([A-Z]{3}-\d{3})\s*\|\s*`?([^|`]+?)`?\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|",
    re.MULTILINE,
)

VALID_STATUSES = {"PASSING", "PLANNED", "WAIVED"}


@dataclass(frozen=True)
class TraceRow:
    req_id: str
    test_ref: str
    kind: str
    status: str

    @property
    def test_path(self) -> str:
        """Strip the ::test_name selector to get the file path."""
        return self.test_ref.split("::", 1)[0].strip()


def parse_requirement_ids(text: str) -> set[str]:
    """Collect requirement IDs from the requirement tables only.

    Requirement rows start with `| XXX-000 |`. Prose mentions of an ID (there are several, since
    the document explains its own tricky requirements) must not be counted as declarations —
    otherwise editing the commentary silently changes the contract.
    """
    ids: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        first_cell = stripped.split("|")[1].strip()
        match = REQ_ID.fullmatch(first_cell)
        if match:
            ids.add(first_cell)
    return ids


def parse_trace_rows(text: str) -> list[TraceRow]:
    return [
        TraceRow(req_id=m.group(1), test_ref=m.group(2).strip(), kind=m.group(3), status=m.group(4))
        for m in TRACE_ROW.finditer(text)
    ]


def main() -> int:
    problems: list[str] = []

    for path in (REQUIREMENTS, TRACEABILITY):
        if not path.exists():
            print(f"FATAL: missing {path.relative_to(REPO_ROOT)}", file=sys.stderr)
            return 1

    declared = parse_requirement_ids(REQUIREMENTS.read_text(encoding="utf-8"))
    rows = parse_trace_rows(TRACEABILITY.read_text(encoding="utf-8"))
    mapped = {row.req_id for row in rows}

    if not declared:
        problems.append("No requirement IDs parsed from REQUIREMENTS.md — the format changed.")

    for req_id in sorted(declared - mapped):
        problems.append(f"{req_id}: declared in REQUIREMENTS.md but has no traceability row.")

    for req_id in sorted(mapped - declared):
        problems.append(f"{req_id}: stale traceability row — no such requirement.")

    seen: set[str] = set()
    for row in rows:
        if row.req_id in seen:
            problems.append(f"{row.req_id}: duplicate traceability row.")
        seen.add(row.req_id)

        if row.status not in VALID_STATUSES:
            problems.append(
                f"{row.req_id}: status {row.status!r} is not one of {sorted(VALID_STATUSES)}."
            )

        # WAIVED rows point at a doc, not a test, so neither check applies.
        if row.status == "WAIVED":
            continue

        if "::" not in row.test_ref:
            problems.append(
                f"{row.req_id}: test reference {row.test_ref!r} has no ::test_name selector. "
                "Point at the specific test, not the file — a file-level reference cannot tell "
                "you which assertion covers the requirement."
            )

        # PLANNED rows are a registered intent, so the file is allowed not to exist yet.
        # PASSING rows claim proof, so the file must be there. Enforcing existence on PLANNED
        # would force empty placeholder test files, which are worse than an honest marker:
        # they make the suite look green while asserting nothing.
        if row.status == "PASSING" and not (REPO_ROOT / row.test_path).exists():
            problems.append(
                f"{row.req_id}: marked PASSING but test file {row.test_path} does not exist."
            )

    if problems:
        print(f"Traceability gate FAILED — {len(problems)} problem(s):\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nFix by adding the missing test file, adding the traceability row, or removing "
            "the requirement with a note in docs/decision-log.md.",
            file=sys.stderr,
        )
        return 1

    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
    summary = ", ".join(f"{count} {status}" for status, count in sorted(by_status.items()))
    print(f"Traceability gate PASSED — {len(declared)} requirements ({summary}).")

    planned = by_status.get("PLANNED", 0)
    if planned:
        print(
            f"\nNote: {planned} requirement(s) still PLANNED. These are registered but unproven. "
            "A phase is not done while any of its requirements are still PLANNED."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
