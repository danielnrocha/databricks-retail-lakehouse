"""ENV-002 — a deploy to `test` writes exclusively to `dng_test`.

ADR-0002 permits exactly one route for a catalog name to reach code:

    bundle target -> var.catalog -> pipeline configuration -> spark.conf -> code

`tests/unit/test_no_hardcoded_catalog.py` guards the last hop: no literal catalog name in shipped
source. This module guards the first one. Between them the chain has no unwatched link — a literal
in code fails the unit suite, and a target that resolves to the wrong catalog fails here.

## Why `bundle validate -o json` is the instrument

The requirement says "deploying to `test` writes exclusively to `dng_test`". The honest way to
prove that is to deploy and observe the writes. That is also the expensive way: a deploy on Free
Edition starts a pipeline, and quota exhaustion takes *all* compute in the account down for the
rest of the day (ADR-0002, production-delta §7).

`databricks bundle validate -t <target> -o json` emits the fully-resolved configuration — every
variable substituted, every preset applied, every workspace path expanded — which is the exact
object `bundle deploy` would send. Asserting against it proves the resolution, which is where the
defect this requirement fears actually lives. It does not prove the platform then honours it.

That is a real gap and it is stated rather than implied: this module verifies **what would be
deployed**, not what was written. The gap is narrow because the resolved config names the catalog
in every position that determines a write — pipeline `catalog`, `event_log.catalog`, and every
`dng.*` configuration value the code reads — and `test_the_resolved_config_names_a_catalog_where_it_matters`
fails if any of those positions stops existing, so the scan cannot go quietly vacuous.

## The instrument can fail like a result

A scan for "no `dng_dev` or `dng_prod` in the test target's JSON" passes trivially if the JSON is
empty, if the key names change, or if the CLI starts emitting a summary instead of a config. Every
assertion below is therefore paired with a check that the scan found something:
`test_every_target_resolves_to_its_own_catalog` runs the same scan across all three targets and
demands that each one differ, which no broken scanner can satisfy.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

TARGETS = ("dev", "test", "prod")
EXPECTED_CATALOG = {"dev": "dng_dev", "test": "dng_test", "prod": "dng_prod"}

# Same pattern as ENV-001, and for the same reason: matching the `dng_` prefix rather than the
# three known names means a fourth catalog is caught on the day it appears, not on the day someone
# remembers to extend a list.
CATALOG_TOKEN = re.compile(r"\bdng_[a-z][a-z0-9_]*")

# Leaf paths in the resolved config that determine where the pipeline writes. If the CLI stops
# emitting one of these, the scan below has lost a place it was watching, and a pass would mean
# less than it did yesterday.
WRITE_SITES = (
    ".resources.pipelines.dng_medallion.catalog",
    ".resources.pipelines.dng_medallion.event_log.catalog",
    ".resources.pipelines.dng_medallion.configuration.dng.catalog",
    ".resources.pipelines.dng_medallion.configuration.dng.events_landing",
    ".resources.pipelines.dng_medallion.configuration.dng.schema_location",
    ".resources.pipelines.dng_medallion.configuration.dng.seed",
)


@lru_cache(maxsize=len(TARGETS))
def resolve(target: str) -> dict[str, Any]:
    """The configuration `bundle deploy -t <target>` would send, as JSON.

    Needs an authenticated workspace: the CLI resolves `${workspace.current_user}` server-side and
    there is no offline mode. Running it unauthenticated prints an error and exits non-zero, which
    is why the return code is checked rather than the output being parsed optimistically.
    """
    completed = subprocess.run(
        ["databricks", "bundle", "validate", "-t", target, "-o", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "DATABRICKS_CONFIG_PROFILE": os.environ.get("DATABRICKS_CONFIG_PROFILE", "dng"),
        },
    )
    assert completed.returncode == 0, (
        f"`databricks bundle validate -t {target}` exited {completed.returncode}. The target does "
        f"not validate, so nothing below is measuring isolation:\n{completed.stderr}"
    )
    return json.loads(completed.stdout)  # type: ignore[no-any-return]


def leaves(node: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Every scalar in the resolved config, with a dotted path to it."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from leaves(value, f"{prefix}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from leaves(value, f"{prefix}[{index}]")
    else:
        yield prefix, node


def catalogs_named_in(config: dict[str, Any]) -> dict[str, set[str]]:
    """Map each catalog token found to the set of leaf paths naming it."""
    found: dict[str, set[str]] = {}
    for path, value in leaves(config):
        if not isinstance(value, str):
            continue
        for match in CATALOG_TOKEN.finditer(value):
            found.setdefault(match.group(0), set()).add(path)
    return found


# ---------------------------------------------------------------------------------------------
# ENV-002
# ---------------------------------------------------------------------------------------------
def test_test_target_writes_only_to_test_catalog() -> None:
    """No catalog other than `dng_test` survives resolution of the `test` target.

    Stated as "the set of catalogs named is exactly {dng_test}" rather than as "dng_dev and
    dng_prod are absent". The two differ on the case that matters: a fourth catalog appearing in
    the resolved config would satisfy the second phrasing and violate the requirement.
    """
    found = catalogs_named_in(resolve("test"))

    assert found, (
        "The resolved `test` configuration names no catalog at all. Either the bundle stopped "
        "declaring one — in which case the pipeline has no target and this is a worse failure "
        "than the one being tested for — or the CLI's JSON shape changed and this scan is now "
        "looking at nothing."
    )
    assert set(found) == {"dng_test"}, (
        "A deploy to `test` would reference a catalog other than dng_test. ADR-0002 makes the "
        "target the only thing that chooses an environment; something else is choosing one:\n\n"
        + "\n".join(
            f"  {catalog}\n" + "\n".join(f"    at {p}" for p in sorted(paths))
            for catalog, paths in sorted(found.items())
            if catalog != "dng_test"
        )
    )


def test_the_resolved_config_names_a_catalog_where_it_matters() -> None:
    """Guards the scan against going vacuous.

    Every path listed in `WRITE_SITES` is a position that decides where data lands. If the CLI
    stops emitting one, the test above still passes while watching one fewer place. This is the
    check that turns that silent loss into a failure.
    """
    config = resolve("test")
    present = dict(leaves(config))

    missing = [path for path in WRITE_SITES if path not in present]
    assert not missing, (
        "The resolved configuration no longer emits these write sites, so ENV-002's scan covers "
        "less than it claims:\n" + "\n".join(f"  {p}" for p in missing)
    )
    for path in WRITE_SITES:
        assert "dng_test" in str(present[path]), (
            f"{path} resolved to {present[path]!r}, which does not name dng_test"
        )


def test_every_target_resolves_to_its_own_catalog() -> None:
    """The instrument check: the same scan must distinguish the three targets.

    A scanner that returned the empty set, or the same set for every target, would pass ENV-002
    while measuring nothing. Requiring three different answers from one code path is the cheapest
    way to rule that out.
    """
    resolved = {target: catalogs_named_in(resolve(target)) for target in TARGETS}

    for target, found in resolved.items():
        assert set(found) == {EXPECTED_CATALOG[target]}, (
            f"target {target} resolved to catalogs {sorted(found)}, expected "
            f"[{EXPECTED_CATALOG[target]}]"
        )

    distinct = {next(iter(found)) for found in resolved.values()}
    assert len(distinct) == len(TARGETS), (
        f"the three targets resolved to {len(distinct)} distinct catalog(s), so they are not "
        "isolated from each other regardless of what any single target's scan says"
    )


# ---------------------------------------------------------------------------------------------
# ENV-002, second half — isolation is not only about the catalog name
# ---------------------------------------------------------------------------------------------
def test_test_and_prod_differ_only_in_the_environment_axis() -> None:
    """`test` must be prod with the environment swapped, and nothing else.

    This is the assertion that gives ENV-003 ("the deployed artifact is the tested artifact") its
    meaning. Two deploys of the same commit prove nothing if the target resolves them into
    differently-shaped resources — passing on test then says the commit ran *somewhere*, not that
    the deployment prod receives was exercised.

    It is not hypothetical. Before ADR-0009 the `test` target was `mode: development`, and this
    test would have failed on eight keys that existed only on test — `presets.name_prefix`,
    `presets.pipelines_development`, `presets.trigger_pause_status`, `bundle.deployment.lock.enabled`
    and the pipeline's own `development` and `tags.dev` — plus a pipeline named
    `[dev daniel_rocha] dng-medallion-test`. Two of those change behaviour rather than labelling:
    a development-mode pipeline reuses compute and does not retry, and the deployment lock, which
    serialises concurrent deploys, was off.
    """
    test_leaves = dict(leaves(resolve("test")))
    prod_leaves = dict(leaves(resolve("prod")))

    only_test = sorted(set(test_leaves) - set(prod_leaves))
    only_prod = sorted(set(prod_leaves) - set(test_leaves))
    assert not only_test and not only_prod, (
        "test and prod resolve to differently-shaped configurations, so a test deploy does not "
        "exercise the deployment prod would receive.\n"
        f"  only in test: {only_test}\n"
        f"  only in prod: {only_prod}"
    )

    unexplained = {
        path: (test_leaves[path], prod_leaves[path])
        for path in sorted(test_leaves)
        if test_leaves[path] != prod_leaves[path]
        and not (
            isinstance(test_leaves[path], str)
            and test_leaves[path].replace("test", "prod") == prod_leaves[path]
        )
    }
    assert not unexplained, (
        "test and prod differ in a way that is not a test->prod substitution, so the difference "
        "is something other than the environment:\n"
        + "\n".join(f"  {p}: {t!r} vs {v!r}" for p, (t, v) in unexplained.items())
    )

    substituted = sum(1 for p in test_leaves if test_leaves[p] != prod_leaves[p])
    assert substituted >= len(WRITE_SITES), (
        f"only {substituted} leaves differ between test and prod. The two targets are supposed to "
        "differ in at least every write site; this few suggests one target failed to resolve its "
        "variables and both are reading the same defaults."
    )


# ---------------------------------------------------------------------------------------------
# The scanner itself, on a planted configuration. Needs no workspace.
# ---------------------------------------------------------------------------------------------
PLANTED = {
    "resources": {
        "pipelines": {
            "p": {
                "catalog": "dng_test",
                "configuration": {"dng.seed": "/Volumes/dng_test/bronze/seed"},
                # The failure this whole module exists to catch: one leaf, deep in the tree,
                # left pointing at another environment.
                "event_log": {"catalog": "dng_prod"},
                "libraries": [{"glob": {"include": "/x/dng_staging/**"}}],
                "development": True,
            }
        }
    }
}


def test_scanner_finds_a_stray_catalog_anywhere_in_the_tree() -> None:
    found = catalogs_named_in(PLANTED)
    assert set(found) == {"dng_test", "dng_prod", "dng_staging"}
    assert found["dng_prod"] == {".resources.pipelines.p.event_log.catalog"}
    assert found["dng_staging"] == {".resources.pipelines.p.libraries[0].glob.include"}


def test_scanner_ignores_non_string_leaves() -> None:
    """`development: True` must not be stringified into the scan.

    Booleans and numbers cannot contain a catalog name, and coercing them to strings is how a
    scanner starts matching things that are not there.
    """
    paths = {path for _, path in ((v, p) for p, v in leaves(PLANTED))}
    assert ".resources.pipelines.p.development" in paths
    assert not any(
        ".development" in path for paths_ in catalogs_named_in(PLANTED).values() for path in paths_
    )
