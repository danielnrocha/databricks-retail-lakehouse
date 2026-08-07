"""ENV-001 — no literal catalog name reaches runtime code.

ADR-0002 makes exactly one route legal for a catalog name to reach code:

    bundle target -> var.catalog -> pipeline configuration -> spark.conf -> code

This module is the machine that enforces it. ADR-0002 calls the rule load-bearing "precisely
because it erodes under deadline pressure", and a rule enforced by review is a rule enforced until
the first Friday afternoon.

---

## What counts as a violation, and why comments and docstrings do not

The scan looks at **string literals the interpreter will actually evaluate**, and nothing else.
Comments and docstrings are excluded — deliberately, and structurally rather than by heuristic:

* Comments never enter the AST at all, so parsing with `ast` excludes them for free.
* Docstrings and free-standing string statements (`Expr(Constant(str))`) are identified by their
  position in the tree, not by a regex over the source.

The justification is not convenience. ENV-001 exists to stop a *runtime* value being wrong; a
sentence in `src/retail_lakehouse/silver/__init__.py` that explains the catalog-injection contract
is the documentation of that very rule. A lint that fires on its own rationale is a lint that gets
`# noqa`-ed and then deleted, and the repository is then worse off than if it had never existed.
The cost of the exclusion is real and is named here so it is not a surprise: an f-string built
inside a docstring-looking triple-quoted *expression* would be missed. Nothing in the codebase does
that, and the deployed-file gate below is zero-tolerance, so the residual risk is small.

## Deployed code vs. operator tooling

`src/` holds two categories that ADR-0002 does not distinguish but the threat model does:

1. **Deployed code** — the modules a bundle target ships into the pipeline graph. These are the
   only files where a literal catalog can cause a dev run to write to prod, which is the failure
   ADR-0002 exists to prevent. Zero tolerance, no ledger, no exceptions.
2. **Operator tooling** — modules run by hand from a laptop against a named environment
   (`retail_lakehouse.perf.*`). A literal here cannot be promoted anywhere, because nothing
   promotes it.

Category 1 is derived from `resources/*.yml` rather than declared here, so it cannot drift: the
membership test is "does a bundle `libraries` glob match this file". Category 2 offenders must
appear in `QUARANTINE` with a reason, and `test_quarantine_ledger_has_no_stale_entries` fails if a
ledger entry stops being true — so the ledger can only shrink, and a file that is later added to a
bundle glob immediately becomes a hard failure.

The ledger is not an approval. It is a debt register that a reviewer can read in ten seconds.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
RESOURCES = REPO_ROOT / "resources"

# `dng_` is the account-wide catalog prefix (ADR-0002). Matching the prefix rather than the three
# known names is deliberate: a fourth catalog created next month must be caught on the day it is
# hardcoded, not on the day someone remembers to update this list.
CATALOG_LITERAL = re.compile(r"\bdng_[a-z][a-z0-9_]*")

# Files that are NOT shipped by any bundle target and that still contain a literal catalog name.
# Each entry needs a reason a reviewer can disagree with. Entries are removed, never added, unless
# the addition comes with the same justification and a reviewer who accepted it.
QUARANTINE: dict[str, str] = {
    "retail_lakehouse/perf/tables.py": (
        "Performance-lab DDL. Runs from a developer machine via the Statement Execution API "
        "against a disposable `<catalog>.perf` schema; no bundle target ships it, so there is no "
        "promotion path along which the literal could reach prod. Should still be parameterised "
        "so the lab is runnable against test — tracked as debt, not blessed."
    ),
    "retail_lakehouse/perf/platform_probe.py": (
        "Capability probes issuing `CACHE TABLE <lab table>` against the same disposable lab "
        "schema. Same reasoning, same debt."
    ),
}


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    column: int
    literal: str
    snippet: str

    @property
    def relative(self) -> str:
        return str(self.path.relative_to(SRC))

    def __str__(self) -> str:
        location = f"{self.path.relative_to(REPO_ROOT)}:{self.line}:{self.column + 1}"
        return f"{location}  {self.literal!r} in {self.snippet!r}"


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Ids of string constants that are documentation rather than data.

    Covers module/class/function docstrings and any bare string statement — the two shapes prose
    takes inside a Python file. Everything else is a value the interpreter will hand to something.
    """
    documentation: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            documentation.add(id(node.value))
    return documentation


def scan_source(source: str, path: Path) -> list[Violation]:
    """Return every evaluated string literal in `source` that names a catalog."""
    tree = ast.parse(source, filename=str(path))
    documentation = _docstring_nodes(tree)

    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in documentation:
            continue
        for match in CATALOG_LITERAL.finditer(node.value):
            snippet = node.value.strip()
            violations.append(
                Violation(
                    path=path,
                    line=node.lineno,
                    column=node.col_offset,
                    literal=match.group(0),
                    snippet=snippet if len(snippet) <= 80 else snippet[:77] + "...",
                )
            )
    return violations


def _bundle_globs() -> list[str]:
    """Every `libraries: - glob: include:` pattern across the bundle resource files.

    Read from `resources/*.yml` rather than restated here so the set of deployed files is the one
    the bundle actually ships. A restated list is a list that is wrong the first time someone adds
    a module to the graph.
    """
    patterns: list[str] = []
    for resource_file in sorted(RESOURCES.glob("*.yml")):
        document = yaml.safe_load(resource_file.read_text(encoding="utf-8")) or {}
        for pipeline in (document.get("resources", {}).get("pipelines") or {}).values():
            for library in pipeline.get("libraries") or []:
                include = (library.get("glob") or {}).get("include")
                if include:
                    # Patterns are relative to the resource file's directory.
                    patterns.append(str((resource_file.parent / include).resolve()))
    return patterns


def deployed_files() -> set[Path]:
    """Python files that at least one bundle target ships into a pipeline graph."""
    shipped: set[Path] = set()
    for pattern in _bundle_globs():
        # A DAB `**` include means "this directory and everything under it".
        root = Path(pattern.removesuffix("/**"))
        if root.is_dir():
            shipped.update(p.resolve() for p in root.rglob("*.py"))
    return shipped


def all_violations() -> list[Violation]:
    found: list[Violation] = []
    for path in sorted(SRC.rglob("*.py")):
        found.extend(scan_source(path.read_text(encoding="utf-8"), path))
    return found


# ---------------------------------------------------------------------------------------------
# ENV-001
# ---------------------------------------------------------------------------------------------
def test_no_literal_catalog_in_src() -> None:
    """No module shipped by a bundle target contains a literal catalog name.

    Failure names the file, the line, the column and the offending literal, because a lint whose
    message is "violations found" costs the reader a grep and gets ignored twice before it gets
    fixed once.
    """
    shipped = deployed_files()
    assert shipped, (
        "No deployed files resolved from resources/*.yml — the bundle library globs changed shape "
        "and this gate is now testing nothing. Fix the glob resolution before trusting a pass."
    )

    hard_failures = [v for v in all_violations() if v.path.resolve() in shipped]
    assert not hard_failures, (
        "Literal catalog name in code shipped by a bundle target. ADR-0002 allows exactly one "
        "route — target -> var.catalog -> pipeline configuration -> spark.conf -> code — and this "
        "bypasses it:\n\n"
        + "\n".join(f"  {v}" for v in hard_failures)
        + "\n\nRead the catalog from `spark.conf.get('dng.catalog')` instead."
    )

    unlisted = sorted(
        {v.relative for v in all_violations() if v.path.resolve() not in shipped} - set(QUARANTINE)
    )
    assert not unlisted, (
        "Literal catalog name in operator tooling that is not in the QUARANTINE ledger:\n\n"
        + "\n".join(f"  {v}" for v in all_violations() if v.relative in unlisted)
        + "\n\nEither parameterise it, or add it to QUARANTINE with a reason a reviewer can "
        "disagree with. Adding it silently is the erosion ADR-0002 names."
    )


def test_quarantine_ledger_has_no_stale_entries() -> None:
    """The ledger may only shrink.

    Three ways an entry goes stale, all of them failures: the file was deleted, the literal was
    removed (so the waiver now excuses nothing and hides the next one), or the file was added to a
    bundle glob (so it is deployed code and the waiver is now dangerous).
    """
    shipped = deployed_files()
    offenders = {v.relative for v in all_violations()}

    for entry, reason in sorted(QUARANTINE.items()):
        path = SRC / entry
        assert path.exists(), f"QUARANTINE names {entry}, which no longer exists. Delete the row."
        assert path.resolve() not in shipped, (
            f"{entry} is now shipped by a bundle target, so its QUARANTINE row is no longer a "
            "documented gap — it is a live path from a literal catalog to a deployed pipeline. "
            "Parameterise it."
        )
        assert entry in offenders, (
            f"{entry} no longer contains a literal catalog name. Delete its QUARANTINE row so the "
            "ledger keeps meaning what it says."
        )
        assert len(reason) > 60, f"{entry}'s QUARANTINE reason is too thin to review."


# ---------------------------------------------------------------------------------------------
# The scanner itself. A lint nobody has watched fail is a lint nobody should trust.
# ---------------------------------------------------------------------------------------------
PLANTED = '''
"""Module docstring mentioning dng_prod, which is documentation, not a value."""

# A comment naming dng_test must not trip the scan either.

CATALOG = spark.conf.get("dng.catalog")
TABLE = f"{CATALOG}.silver.fact_basket_line"

"A bare string statement about dng_dev is prose too."


def f() -> str:
    """Docstring referencing dng_dev."""
    return "SELECT * FROM dng_dev.silver.fact_basket_line"
'''


def test_scanner_flags_an_evaluated_literal() -> None:
    found = scan_source(PLANTED, Path("planted.py"))
    assert [v.literal for v in found] == ["dng_dev"], (
        f"expected exactly the executable literal, got {[str(v) for v in found]}"
    )
    assert found[0].line == 14, "line number is wrong, so the failure message would misdirect"


def test_scanner_ignores_prose() -> None:
    """The false-positive half, asserted separately.

    Several modules legitimately discuss the catalogs — `silver/__init__.py` documents the
    injection contract by name. If those tripped the scan, the rule would be turned off, and a
    disabled rule protects nothing.
    """
    prose_only = "\n".join(line for line in PLANTED.splitlines() if "SELECT * FROM" not in line)
    assert scan_source(prose_only, Path("prose.py")) == []


def test_scanner_catches_an_unknown_catalog_name() -> None:
    """A fourth catalog must fail on the day it is hardcoded, not on the day the list is updated."""
    found = scan_source('X = "dng_staging.bronze.events"', Path("planted.py"))
    assert [v.literal for v in found] == ["dng_staging"]
