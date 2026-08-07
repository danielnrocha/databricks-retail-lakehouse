"""ENV-005 — a failed deploy is recovered by redeploying the prior commit.

## Why this one is implemented and ENV-004 is waived

Both requirements were candidates for a waiver on cost grounds, and they were separated by
measuring rather than by assuming. `databricks bundle deploy` applies resource definitions; it
does not start a pipeline update. Three deploys against the `test` target were timed at 19.2s,
12.6s and 13.2s, and both pipelines in the account stayed `IDLE` throughout — **zero compute**.

Free Edition's quota is shared across the account and exhausting it shuts down all compute for
the rest of the day (ADR-0002, production-delta §7). That is the cost that makes ENV-004
unaffordable: reproducibility needs a full medallion run over 2.6M transactions and 36.8M causal
rows into a clean catalog, and there is no version of that which is cheap. ENV-005 needs no run at
all. Waiving both would have been waiving one requirement for a cost the other one has.

## What this proves, and the part it does not

Proven: the deployment layer is declarative in the way the rollback instruction in `deploy.yml`
assumes. Redeploying the prior commit restores the prior resource definitions exactly, with no
intervening manual step, and — the non-obvious part — a resource *rename* is applied in place. The
pipeline keeps its id across the round trip, so nothing is orphaned and no pipeline history is
lost. Had the CLI implemented a rename as destroy-and-create, the rollback instruction in
`deploy.yml` would have been quietly destructive.

**Not proven, and it is the failure that actually hurts: data does not roll back.** A deploy that
succeeded, ran, and wrote wrong rows into gold is not repaired by redeploying the prior commit —
the definitions revert and the rows stay. ENV-005 as written ("failed deploys are recoverable")
covers the deployment, and this module covers exactly that much. Data-level recovery would need
Delta time travel plus a restore procedure that has actually been rehearsed, which is
`production-delta.md` §8, not this test.

## Side effects, and why this is opt-in

This module deploys three times against `dng_test`. It is marked `slow` and excluded from the
default integration run; invoke it deliberately:

    make test-slow            # or: pytest tests/integration -m slow

It requires a clean tree, because the `test` target is `mode: production` and production mode
refuses to deploy uncommitted changes (ADR-0009). That refusal is the reason the test can use the
working tree's own history as its two versions instead of fabricating a broken commit.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET = "test"

# Fields of the workspace-side deployment record that a rollback must restore. Deliberately not
# "the whole file": `metadata.json` also carries paths that are a function of the target rather
# than of the commit, and asserting on those would make the test fail for reasons unrelated to
# rollback. These three are the ones that identify *which version is deployed*.
IDENTITY_FIELDS = ("commit", "mode", "pipeline_id")


def git(*args: str, cwd: Path = REPO_ROOT) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def databricks(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["databricks", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "DATABRICKS_CONFIG_PROFILE": os.environ.get("DATABRICKS_CONFIG_PROFILE", "dng"),
        },
    )


def deploy(cwd: Path) -> None:
    result = databricks("bundle", "deploy", "-t", TARGET, cwd=cwd)
    assert result.returncode == 0, f"deploy from {cwd} failed:\n{result.stdout}\n{result.stderr}"


def deployed_identity() -> dict[str, Any]:
    """The workspace's own record of what is currently deployed to `test`.

    Read from the server rather than inferred from the local tree — the whole point of ENV-003 and
    ENV-005 is that the deployment is a fact about the workspace, not about the machine that ran
    the CLI.
    """
    summary = databricks("bundle", "summary", "-t", TARGET, "-o", "json")
    assert summary.returncode == 0, f"bundle summary failed:\n{summary.stderr}"
    state_path = json.loads(summary.stdout)["workspace"]["state_path"]

    exported = databricks("workspace", "export", f"{state_path}/metadata.json")
    assert exported.returncode == 0, f"no deployment metadata at {state_path}:\n{exported.stderr}"
    metadata = json.loads(exported.stdout)

    return {
        "commit": metadata["config"]["bundle"]["git"]["commit"],
        "mode": metadata["config"]["bundle"]["mode"],
        "pipeline_id": metadata["config"]["resources"]["pipelines"]["dng_medallion"]["id"],
    }


def pipeline_names() -> dict[str, str]:
    """Live pipeline id -> name, so an orphaned resource is visible rather than inferred."""
    listed = databricks("pipelines", "list-pipelines", "-o", "json")
    assert listed.returncode == 0, listed.stderr
    return {p["pipeline_id"]: p["name"] for p in json.loads(listed.stdout)}


@pytest.fixture(scope="module")
def prior_commit_worktree() -> Iterator[Path]:
    """A checkout of HEAD~1, isolated from the working tree.

    A worktree rather than a `git checkout`: the test must not be able to leave the developer's
    tree on another commit if it dies partway, and `mode: production` would refuse to deploy from
    a dirty one anyway.
    """
    if git("status", "--porcelain"):
        pytest.skip(
            "working tree is dirty; the `test` target is mode: production and refuses to deploy "
            "uncommitted changes. Commit or stash first."
        )

    directory = Path(tempfile.mkdtemp(prefix="dng-rollback-"))
    worktree = directory / "prior"
    git("worktree", "add", "-q", "--detach", str(worktree), "HEAD~1")
    try:
        validation = databricks("bundle", "validate", "-t", TARGET, cwd=worktree)
        if validation.returncode != 0:
            pytest.skip(
                f"HEAD~1 ({git('rev-parse', '--short', 'HEAD~1')}) does not validate against the "
                f"{TARGET} target, so it cannot stand in for a prior good deploy:\n"
                f"{validation.stderr}"
            )
        yield worktree
    finally:
        git("worktree", "remove", "--force", str(worktree))
        shutil.rmtree(directory, ignore_errors=True)


def test_redeploy_prior_sha_restores_state(prior_commit_worktree: Path) -> None:
    """Deploy HEAD, roll back to HEAD~1, roll forward — the third state must equal the first.

    The round trip is what makes this a rollback test rather than a deploy test. Asserting only
    that the rollback *changed* something would pass on a deploy that broke the resource; asserting
    only that the roll-forward matched would pass if the rollback had done nothing at all. Both
    directions are checked.
    """
    head = git("rev-parse", "HEAD")
    prior = git("rev-parse", "HEAD~1")
    assert head != prior, "HEAD and HEAD~1 are the same commit; there is no rollback to perform"

    # A — the known-good deployment.
    deploy(REPO_ROOT)
    before = deployed_identity()
    assert before["commit"] == head, (
        f"the workspace records {before['commit']} after deploying {head}; ENV-003's provenance "
        "record is wrong, so nothing below can be trusted"
    )

    try:
        # B — the rollback. This must actually move the deployment, or the round trip is vacuous.
        deploy(prior_commit_worktree)
        rolled_back = deployed_identity()
        assert rolled_back["commit"] == prior, (
            f"redeploying {prior} left the workspace recording {rolled_back['commit']}. The "
            "rollback did not take effect, and the roll-forward assertion below would then pass "
            "for the wrong reason."
        )
        assert rolled_back != before, (
            "the rollback produced an identical deployment identity, so HEAD and HEAD~1 do not "
            "differ in anything this test can observe and the round trip proves nothing"
        )
    finally:
        # C — roll forward. In `finally` so a failed assertion above cannot leave `test` pinned to
        # an older commit; the recovery is the same single command the operator would run.
        deploy(REPO_ROOT)

    after = deployed_identity()
    assert after == before, (
        "redeploying the prior commit did not restore the deployment. ENV-005 claims recovery "
        "needs no manual cleanup; these fields disagree:\n"
        + "\n".join(
            f"  {field}: {before[field]!r} -> {after[field]!r}"
            for field in IDENTITY_FIELDS
            if before[field] != after[field]
        )
    )


def test_rollback_renames_in_place_rather_than_orphaning_the_resource(
    prior_commit_worktree: Path,
) -> None:
    """A resource renamed by a rollback keeps its id.

    This is the assumption `deploy.yml`'s rollback instruction rests on, and it is not obvious: if
    the CLI implemented a rename as destroy-and-create, "redeploy the prior commit" would silently
    delete a pipeline and its update history, and the resource under the old name would linger.

    HEAD~1 and HEAD happen to name the `test` pipeline differently — HEAD~1 had the target at
    `mode: development`, which prefixes resource names — so the round trip exercises a rename
    without needing one to be fabricated.
    """
    deploy(REPO_ROOT)
    original = deployed_identity()

    try:
        deploy(prior_commit_worktree)
        renamed = deployed_identity()
        assert renamed["pipeline_id"] == original["pipeline_id"], (
            f"the rollback replaced pipeline {original['pipeline_id']} with "
            f"{renamed['pipeline_id']}. A destroy-and-create loses the pipeline's update history, "
            "and `deploy.yml` tells an operator this is a safe recovery."
        )

        live = pipeline_names()
        stale = {
            pid: name
            for pid, name in live.items()
            if name.endswith("dng-medallion-test") and pid != original["pipeline_id"]
        }
        assert not stale, f"the rollback left an orphaned test pipeline behind: {stale}"
    finally:
        deploy(REPO_ROOT)

    assert deployed_identity()["pipeline_id"] == original["pipeline_id"]
