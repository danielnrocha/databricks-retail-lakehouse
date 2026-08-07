# CI/CD — what runs, what is proven, what is skipped

ADR-0009 records *why* the delivery pipeline has this shape. This document records what it actually
does, with the transcripts, and — more usefully — the two defects that finding out cost.

---

## Shape

```
  pull request  ─┐
  push to main  ─┤
                 │
                 ▼
        ┌─────────────────┐
        │ ci.yml          │  workflow_call, so deploy.yml reuses it rather than restating it
        ├─────────────────┤
        │ static          │  traceability -> format -> lint -> types   (cheapest failure first)
        │   ▼             │
        │ unit            │  no DATABRICKS_* in scope at all           (ENV-006, structurally)
        │   ▼             │
        │ bundle × 3      │  validate dev / test / prod   ← SKIPPED: vars.DATABRICKS_HOST unset
        └─────────────────┘

  workflow_dispatch(target)
                 │
                 ▼
        ┌─────────────────────────────────────────────────────────────┐
        │ deploy.yml                                                  │
        ├─────────────────────────────────────────────────────────────┤
        │ verify        uses: ./.github/workflows/ci.yml              │
        │   ▼                                                          │
        │ deploy-test   record HEAD -> output tested_sha              │
        │               deploy -> read metadata.json back, compare    │
        │   ▼           needs: deploy-test                            │
        │ deploy-prod   checkout ref = needs.deploy-test.outputs.…    │
        │               ENV-003 gate: re-derive HEAD, compare, exit 1 │
        │               deploy -> read metadata.json back, compare    │
        └─────────────────────────────────────────────────────────────┘
                        ↑ both skipped: vars.DATABRICKS_HOST unset
```

The two `SKIPPED` annotations are the honest part of this diagram and are dealt with below.

## The requirements this closes

| ID | Claim | Where it is proven | Kind of proof |
|---|---|---|---|
| ENV-001 | No literal catalog in shipped source | `tests/unit/test_no_hardcoded_catalog.py` | static, offline |
| ENV-002 | A deploy to `test` writes only to `dng_test` | `tests/integration/test_bundle_targets.py` | resolved config |
| ENV-003 | Prod deploys the SHA that passed test | `tests/unit/test_deploy_provenance.py` + transcript below | structural + manual |
| ENV-004 | A clean deploy is reproducible | **WAIVED** — `production-delta.md` §11 | — |
| ENV-005 | A failed deploy is recoverable | `tests/integration/test_rollback.py` | executed, 6 deploys |
| ENV-006 | Unit tests need no workspace | `tests/unit/test_offline_capable.py` + a job with no credentials | structural |

## ENV-003 — the provenance record, run by hand

The workflow's control-flow half is asserted offline. The workspace half cannot run in CI (see
*What is skipped* below), so it was executed manually against the `test` target:

```console
$ databricks bundle deploy -t test
Uploading bundle files to /Workspace/Users/…/.bundle/dng-retail-lakehouse/test/files...
Deploying resources...
Updating deployment state...
Deployment complete!                                              # 19.2s

$ databricks workspace export "$(databricks bundle summary -t test -o json \
      | jq -r .workspace.state_path)/metadata.json" | jq -c .config.bundle
{"name":"dng-retail-lakehouse","target":"test","mode":"production",
 "git":{"branch":"main","commit":"65053aafc3a2f4570b421acf8b7dababfd444a1f",…}}

$ git rev-parse HEAD
65053aafc3a2f4570b421acf8b7dababfd444a1f
```

The recorded commit is the checked-out commit, and `mode: production` is recorded too. This is the
step that turns "we deployed this SHA" into a server-side record, and it is what both deploy jobs
compare against.

**The prod half has never executed.** The mechanism is identical and it is demonstrated end to end
on `test`; deploying prod would create a third pipeline object for no additional evidence. Named
here rather than left for a reader to discover.

## ENV-005 — the rollback round trip, measured

Three deploys, no pipeline update, both pipelines `IDLE` throughout:

| step | wall | recorded commit | mode | pipeline name | pipeline id |
|---|---|---|---|---|---|
| deploy `HEAD` | 19.2s | `65053aa` | production | `dng-medallion-test` | `57eefe21` |
| deploy `HEAD~1` | 12.6s | `f4dd57f` | development | `[dev daniel_rocha] dng-medallion-test` | `57eefe21` |
| deploy `HEAD` | 13.2s | `65053aa` | production | `dng-medallion-test` | `57eefe21` |

Two things fall out of that table, and only one of them was expected.

**Expected:** the third row equals the first. Redeploying the prior commit restores the prior
definitions with no manual step, which is what the rollback instruction in `deploy.yml` promises.

**Not expected, and load-bearing:** the pipeline id is constant across a *rename*. `HEAD~1` names
the pipeline `[dev daniel_rocha] dng-medallion-test` and `HEAD` names it `dng-medallion-test`, and
the CLI applied that as an in-place update. Had it been implemented as destroy-and-create, the
documented recovery procedure would have deleted a pipeline and its update history every time it
was used, and would have left the resource under the old name behind. That is now asserted by
`test_rollback_renames_in_place_rather_than_orphaning_the_resource` rather than relied upon.

**What this does not cover:** data. A deploy that succeeded, ran, and wrote wrong rows into gold is
not repaired by redeploying — the definitions revert and the rows stay. Data-level recovery is
Delta time travel plus a rehearsed restore procedure, which is `production-delta.md` §8.

Run it with `make test-slow`. It is excluded from `make test-integration` because it mutates the
`test` target, and it needs a clean tree because `mode: production` refuses to deploy one that
isn't.

## What is skipped, and why that is dangerous rather than merely unfortunate

`ci.yml`'s `bundle` job and both jobs in `deploy.yml` are gated on `vars.DATABRICKS_HOST != ''`.
That variable is unset, so they skip. Free Edition has no service principal and no account console,
so there is no workload identity federation; the only credential that exists is a long-lived
personal access token belonging to a metastore admin, and a public repository is the wrong place to
keep one (ADR-0009, option A).

**A skipped job renders as a green check.** This is not a theoretical concern:

> `databricks bundle validate -t prod` failed from the day `resources/bronze_pipeline.yml` was
> written — `target with 'mode: production' cannot include a pipeline with 'development: true'` —
> and CI reported green through every one of those commits, because the job that would have caught
> it was skipping.

The mitigation is not to trust the gate more. It is that everything checkable without credentials
moved into `tests/unit/test_deploy_provenance.py`, which runs unconditionally: every target
declares a catalog matching its environment, `test` and `prod` are production mode, and no pipeline
resource pins `development`. The residual gap stays real — CI still cannot tell you the deploy path
works — and that is the gap, stated.

## Deploying, by hand

```bash
export DATABRICKS_CONFIG_PROFILE=dng

make check                        # what CI runs on a pull request
databricks bundle validate -t test
databricks bundle deploy   -t test    # ~20s, starts no compute

# promote: only from a commit that passed the above
databricks bundle validate -t prod
databricks bundle deploy   -t prod
```

`bundle deploy` applies resource definitions. It does **not** start a pipeline update, which is why
ENV-005 is affordable and ENV-004 is not — see `production-delta.md` §11 for the timing that
separated them.

### Rolling back

```bash
git log --oneline                            # find the last known-good commit
git worktree add ../rollback <good-sha>      # do not move the working tree
cd ../rollback && databricks bundle deploy -t <target>
```

Or, through the workflow once credentials exist:

```bash
gh workflow run deploy.yml --ref <good-sha> -f target=prod
```

## Two defects this work surfaced

Recorded together because they share a shape, and the shape is the transferable part.

1. **A gate configured so that its absence looks like success.** The `bundle` job's skip condition
   is indistinguishable from a pass in the GitHub UI. The traceability gate hit the same shape on
   2026-08-06 from the other direction — a gate that could not be satisfied incrementally would
   have been disabled. Both are the same rule: *the failure mode of a gate is what it does when it
   is not doing its job.*

2. **An assertion that named the right property and checked a different one.** The first version of
   `test_the_tested_sha_is_an_output_of_the_test_deploy_not_a_constant` asked whether the emitting
   step *contained* `git rev-parse HEAD`. Rewriting the emitted line to use a different variable —
   the exact defect the test exists to catch — left it green. It now follows the variable from
   assignment to `$GITHUB_OUTPUT`. This is the third instance of an existence check being mistaken
   for coverage in this project; see the decision log.
