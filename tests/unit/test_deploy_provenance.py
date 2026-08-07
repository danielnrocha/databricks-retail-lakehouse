"""ENV-003 — the deployed artifact is the tested artifact.

The requirement: *prod deploy references the git SHA that passed the test target.* The mechanism
lives in `.github/workflows/deploy.yml` and has two halves, only one of which this module can
reach.

## The half this module proves

That the *workflow* cannot express a prod deploy of an untested commit. Three properties make that
true, and each is asserted below rather than trusted:

1. `deploy-prod` declares `needs: deploy-test`, so it cannot start unless the test deploy finished
   green.
2. `deploy-prod`'s checkout `ref` is `needs.deploy-test.outputs.tested_sha` — the value the test
   job *emitted* — and not a second read of `github.sha`. Re-reading the same expression would
   work today and would silently stop working the first time a re-run, a queued dispatch, or a
   force-push made the two resolve differently. That is the whole defect class this requirement
   exists to prevent, and it is invisible in a workflow that "looks right".
3. A gate step re-derives `git rev-parse HEAD` after checkout and fails if it differs from the
   tested SHA, so a checkout action that silently resolved something else is caught rather than
   deployed.

## The half this module CANNOT prove, and what Free Edition has to do with it

The runtime half is the workspace-side record: `bundle deploy` writes `state/metadata.json` into
the workspace carrying `config.bundle.git.commit`, and both deploy jobs read it back and compare.
That turns "we deployed this SHA" from a claim by the workflow into a record on the server.

**None of that executes in CI, and the reason is Free Edition specifically.** Free Edition has no
account console and no service principals, so there is no workload identity federation and no
OIDC trust to establish — the only credential that exists is a long-lived personal access token
tied to a human. Both deploy jobs are therefore gated on `vars.DATABRICKS_HOST != ''`, which is
unset, and they skip. Precisely one part of ENV-003 is blocked: **automated execution of the
provenance readback.** The readback itself works and has been run by hand; see
`docs/architecture/cicd.md` for the transcript, and `docs/architecture/production-delta.md` §2/§3
for what a paid tier would replace it with.

What this module can honestly claim is therefore: *the workflow's control flow admits no path from
an untested commit to a prod deploy.* It cannot claim that GitHub honours `needs`, or that the
workspace stored what the CLI said it stored. Stating that boundary is the point — a structural
test that quietly implies runtime coverage is worse than no test, because the gap stops being
visible.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DEPLOY = WORKFLOWS / "deploy.yml"
CI = WORKFLOWS / "ci.yml"
BUNDLE = REPO_ROOT / "databricks.yml"

# The value prod must check out. Written as a pattern rather than an exact string because
# whitespace inside `${{ }}` is not significant to GitHub and should not be to this test either.
TESTED_SHA_REF = re.compile(r"\$\{\{\s*needs\.deploy-test\.outputs\.tested_sha\s*\}\}")

# `${{ ... }}` anywhere inside a `run:` body. GitHub substitutes these before the shell sees the
# script, so an expression carrying attacker-controlled text (a branch name, a PR title) becomes
# shell source. Every value in this repository's `run:` blocks arrives through `env:` instead.
RUN_INTERPOLATION = re.compile(r"\$\{\{")

EXPECTED_CATALOG = {"dev": "dng_dev", "test": "dng_test", "prod": "dng_prod"}


def load(path: Path) -> dict[str, Any]:
    """Parse a workflow.

    YAML 1.1 reads a bare `on:` key as the boolean `True`, so `workflow["on"]` is a `KeyError` and
    the trigger block silently looks absent. Normalising it here means a test about triggers
    asserts about triggers rather than about a parser quirk.
    """
    document: dict[Any, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in document:
        document["on"] = document.pop(True)
    return document


def steps_of(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    return list(workflow["jobs"][job].get("steps") or [])


def needs_of(job: dict[str, Any]) -> set[str]:
    declared = job.get("needs") or []
    return {declared} if isinstance(declared, str) else set(declared)


# ---------------------------------------------------------------------------------------------
# ENV-003
# ---------------------------------------------------------------------------------------------
def test_prod_deploy_references_tested_sha() -> None:
    """Prod checks out the SHA the test job emitted, and re-derives it before deploying.

    The three assertions correspond to the three properties in the module docstring. They are kept
    in one test because they are one claim: any of them alone permits an untested prod deploy.
    """
    workflow = load(DEPLOY)
    prod = workflow["jobs"]["deploy-prod"]

    # 1. Ordering. Without this, prod is merely *usually* second.
    assert "deploy-test" in needs_of(prod), (
        "deploy-prod does not declare `needs: deploy-test`, so a prod deploy can start without a "
        f"test deploy having succeeded. Declared needs: {sorted(needs_of(prod))}"
    )

    # 2. Provenance of the ref. The defect being excluded is `ref: ${{ github.sha }}` here, which
    #    reads correct and is a different value from the one test proved the moment anything
    #    re-resolves it.
    checkouts = [
        s for s in steps_of(workflow, "deploy-prod") if "actions/checkout" in str(s.get("uses", ""))
    ]
    assert len(checkouts) == 1, (
        f"expected exactly one checkout in deploy-prod, found {len(checkouts)}"
    )
    ref = str((checkouts[0].get("with") or {}).get("ref", ""))
    assert TESTED_SHA_REF.search(ref), (
        f"deploy-prod checks out {ref!r}. ENV-003 requires the SHA the test deploy emitted "
        "(`needs.deploy-test.outputs.tested_sha`), not a second read of `github.sha` — those are "
        "the same value only until a re-run or a queued dispatch makes them differ."
    )

    # 3. Re-derivation after checkout. Guards against the ref being right and the checkout not.
    gate = [s for s in steps_of(workflow, "deploy-prod") if "ENV-003" in str(s.get("name", ""))]
    assert len(gate) == 1, (
        "deploy-prod has no single step named for ENV-003; the gate is unfindable"
    )
    body = str(gate[0].get("run", ""))
    assert "git rev-parse HEAD" in body, (
        "the ENV-003 gate does not re-derive the checked-out commit, so it compares the workflow's "
        "belief against itself"
    )
    assert "exit 1" in body, "the ENV-003 gate detects a mismatch without failing the job"
    assert "TESTED_SHA" in (gate[0].get("env") or {}), (
        "the ENV-003 gate does not receive the tested SHA through `env:`"
    )


def test_the_tested_sha_is_an_output_of_the_test_deploy_not_a_constant() -> None:
    """`tested_sha` must be produced by the job that did the testing.

    A hardcoded output, or one lifted from `github.sha` inside the prod job, would satisfy every
    structural check above while proving nothing. The value has to originate in `deploy-test`.

    The assertion follows the data rather than the vocabulary. An earlier version of this test
    asked only whether the emitting step's body *contained* `git rev-parse HEAD`, and it passed
    unchanged when the emitted line was rewritten to `echo "sha=${EXPECTED_SHA}"` — the derivation
    was still in the script, and the output no longer used it. "The step derives the SHA" and "the
    step emits the SHA it derived" are different claims, and only the second one is ENV-003.
    """
    workflow = load(DEPLOY)
    test_job = workflow["jobs"]["deploy-test"]

    outputs = test_job.get("outputs") or {}
    assert "tested_sha" in outputs, "deploy-test declares no `tested_sha` output"
    assert "steps." in str(outputs["tested_sha"]), (
        f"tested_sha is {outputs['tested_sha']!r}, which does not come from a step in the job that "
        "ran the test deploy"
    )

    producers = [
        s for s in steps_of(workflow, "deploy-test") if "GITHUB_OUTPUT" in str(s.get("run", ""))
    ]
    assert producers, "no step in deploy-test writes to $GITHUB_OUTPUT"

    for step in producers:
        body = str(step["run"])
        derived = re.search(r"(\w+)=\"?\$\(git rev-parse HEAD\)\"?", body)
        assert derived, (
            "the step emitting tested_sha does not assign `$(git rev-parse HEAD)` to a variable, "
            "so there is nothing for the output to have come from"
        )
        variable = derived.group(1)
        emitted = re.search(r'sha=\$\{?(\w+)\}?"?\s*>>\s*"?\$GITHUB_OUTPUT', body)
        assert emitted, "no `sha=$<var> >> $GITHUB_OUTPUT` line found in the emitting step"
        assert emitted.group(1) == variable, (
            f"tested_sha is emitted from ${emitted.group(1)}, but the checked-out commit was "
            f"assigned to ${variable}. The output records what the workflow intended to test "
            "rather than what it actually checked out."
        )


def test_both_deploy_jobs_gate_on_the_same_ci_workflow() -> None:
    """Nothing deploys that has not passed the gates a pull request passes.

    `uses:` rather than a copy of the steps, so the two cannot drift — a reusable workflow runs at
    the caller's commit, which is what makes "tested" and "deployed" the same object.
    """
    workflow = load(DEPLOY)
    verify = workflow["jobs"]["verify"]
    assert str(verify.get("uses", "")).endswith("/ci.yml"), (
        f"the verify job is {verify.get('uses')!r}; it must reuse ci.yml rather than restate it"
    )
    for job in ("deploy-test", "deploy-prod"):
        assert "verify" in needs_of(workflow["jobs"][job]), f"{job} does not need the verify job"


def test_provenance_is_read_back_from_the_workspace_not_asserted_locally() -> None:
    """Both deploys compare the workspace's own record against the expected SHA.

    Without the readback, "prod deployed the tested SHA" is a statement the workflow makes about
    itself. `state/metadata.json` is written by the CLI into the workspace, so reading it back is
    the only server-side evidence available on this tier.
    """
    workflow = load(DEPLOY)
    for job in ("deploy-test", "deploy-prod"):
        readbacks = [
            s
            for s in steps_of(workflow, job)
            if "metadata.json" in str(s.get("run", ""))
            and "config.bundle.git.commit" in str(s.get("run", ""))
        ]
        assert readbacks, f"{job} never reads the deployed metadata.json back"
        assert "exit 1" in str(readbacks[0]["run"]), (
            f"{job} reads the recorded commit without failing when it disagrees"
        )


def test_no_run_block_interpolates_a_github_expression() -> None:
    """Untrusted text must reach a shell as data, never as source.

    `${{ }}` is substituted into the script *before* the shell parses it, so a value containing a
    quote or a `$(` is shell source. Every `run:` in this repository takes its inputs through
    `env:` instead. The matrix values and SHAs used here happen to be trusted; the rule is
    unconditional because the moment one becomes event-derived, the shape is already correct.
    """
    offenders: list[str] = []
    for path in (DEPLOY, CI):
        workflow = load(path)
        for job_name, job in workflow["jobs"].items():
            for step in job.get("steps") or []:
                body = str(step.get("run", ""))
                if RUN_INTERPOLATION.search(body):
                    offenders.append(f"{path.name}:{job_name}:{step.get('name', '<unnamed>')}")
    assert not offenders, (
        "GitHub expressions interpolated directly into a shell script:\n"
        + "\n".join(f"  {o}" for o in offenders)
        + "\nPass the value through `env:` and reference it as a shell variable."
    )


# ---------------------------------------------------------------------------------------------
# The offline half of the bundle check. `databricks bundle validate` needs credentials, so the
# properties that can be read straight off databricks.yml are read here and run on every push —
# see the comment on ci.yml's `bundle` job.
# ---------------------------------------------------------------------------------------------
def test_every_target_declares_a_catalog_matching_its_environment() -> None:
    bundle = yaml.safe_load(BUNDLE.read_text(encoding="utf-8"))
    targets = bundle["targets"]

    assert set(targets) == set(EXPECTED_CATALOG), (
        f"databricks.yml declares targets {sorted(targets)}, expected {sorted(EXPECTED_CATALOG)}"
    )
    for name, target in targets.items():
        variables = target.get("variables") or {}
        assert variables.get("catalog") == EXPECTED_CATALOG[name], (
            f"target {name} declares catalog {variables.get('catalog')!r}, expected "
            f"{EXPECTED_CATALOG[name]!r}"
        )
        assert variables.get("environment") == name, (
            f"target {name} declares environment {variables.get('environment')!r}. The two names "
            "must agree or the ops schema and the tags describe a different environment from the "
            "one being written to."
        )


def test_promotion_targets_are_production_mode() -> None:
    """`test` and `prod` are both `mode: production`; only `dev` is a development target.

    `test` being production mode is what makes ENV-002's parity assertion possible — under
    `mode: development` the target resolved to a prefixed, unlocked, development-mode pipeline, so
    what passed on test was not the deployment prod would receive. See ADR-0009.

    `dev` stays development mode deliberately: it is the interactive loop, and production mode's
    refusal to deploy a dirty tree is exactly wrong there.
    """
    targets = yaml.safe_load(BUNDLE.read_text(encoding="utf-8"))["targets"]
    assert targets["dev"]["mode"] == "development"
    for name in ("test", "prod"):
        assert targets[name]["mode"] == "production", (
            f"target {name} is {targets[name]['mode']!r}. A promotion target in development mode "
            "gets its resources renamed, its pipelines forced into development semantics and its "
            "deployment lock disabled, none of which prod receives."
        )


def test_no_pipeline_resource_pins_development_mode() -> None:
    """The target decides `development`, not the resource.

    A hardcoded `development: true` in the pipeline made `bundle validate -t prod` fail outright —
    "target with 'mode: production' cannot include a pipeline with 'development: true'" — for the
    entire life of the file, unnoticed because the CI bundle job is gated on a variable that is
    unset. Restating a value the mode already sets only creates a way for the two to disagree.
    """
    for resource_file in sorted((REPO_ROOT / "resources").glob("*.yml")):
        document = yaml.safe_load(resource_file.read_text(encoding="utf-8")) or {}
        pipelines = (document.get("resources") or {}).get("pipelines") or {}
        for name, pipeline in pipelines.items():
            assert "development" not in pipeline, (
                f"{resource_file.name}:{name} pins `development: {pipeline['development']}`. Let "
                "the bundle mode set it, or prod stops validating."
            )
