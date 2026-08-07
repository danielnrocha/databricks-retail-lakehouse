# ADR-0009 — Declarative Automation Bundles deployed by GitHub Actions, dispatched by hand

**Status:** Accepted · **Date:** 2026-08-07 · **Reversal cost:** Low

---

## Context

ADR-0002 chose three catalogs on one metastore and made the bundle target the only thing that
selects an environment. That decision is worth nothing without a delivery mechanism that can prove
it held: something has to establish that what runs in `dng_prod` is the artifact that passed
against `dng_test`, and that a bad deploy can be undone.

Four requirements state that in testable form. ENV-002: deploying to `test` writes exclusively to
`dng_test`. ENV-003: the prod deploy references the SHA that passed the test target. ENV-004: a
clean deploy is reproducible. ENV-005: a failed deploy is recoverable by redeploying the prior SHA.

The constraints are not incidental to the choice; they determine it:

- **No service principals and no account console** (Free Edition). There is no workload identity
  federation and no OIDC trust to establish with GitHub. The only credential that exists is a
  long-lived personal access token belonging to a human who is also a metastore admin.
- **The repository is public.** A credential with write access to every catalog, no expiry, and no
  rotation story is a bad thing to keep next to a public repository, even in an encrypted secret.
- **Compute quota is shared account-wide** and exhausting it takes all compute down for the rest of
  the day — dev, test and prod alike (ADR-0002, `production-delta.md` §7).
- **One person deploys.** Any control that assumes segregation of duties is theatre here.

## Options considered

### A. GitHub Actions, PAT in repository secrets, deploy on every push to `main`

The default modern shape, and the one most reference architectures show.

**Rejected as a default, not as a capability.** The workflows are written to work this way the
moment `vars.DATABRICKS_HOST` is set — that is deliberate, so the repository demonstrates the real
pattern rather than a Free-Edition-shaped imitation of it. What is rejected is *turning it on
here*: the token would be a non-expiring metastore-admin credential stored beside a public
repository, and continuous deploy on push means a merge can start a pipeline that consumes the
day's quota while the author is asleep. Continuous deploy is the right default for a service with
a rollback button and an on-call rota. This is neither.

### B. CI inside Databricks — Lakeflow jobs running the test suite in-workspace

Attractive because it needs no external credential at all: the workspace already trusts itself.

**Rejected on two counts.** First, it inverts a dependency that ENV-006 exists to keep the right
way round — the unit suite's whole point is that it runs *without* a workspace, and making the
workspace the thing that runs the tests means a broken workspace can no longer tell you it is
broken. Second, it spends the same quota the pipeline needs, so the test suite and the system under
test compete for the resource whose exhaustion is the worst failure available on this tier.

### C. Databricks git folders, deployed from the UI

**Rejected.** No gate, and — worse — no record. "Which commit is running in prod?" becomes a
question answered by clicking through a UI and trusting what someone remembers. ENV-003 is
unimplementable in this shape, not merely untested.

### D. Bundle deploys driven by CLI from a developer machine, no CI at all

**Rejected**, but it is closer to defensible than it looks and is worth stating honestly: on Free
Edition this is how deploys *actually* happen today, because option A's credential is not
configured. What it cannot do is refuse. A gate that lives in a person's habits is not a gate, and
the failure mode it permits — deploying a commit that never passed the suite — is exactly ENV-003.

### E. GitHub Actions for the gates, `workflow_dispatch` for the deploys, provenance from the bundle's own state ✅

**Chosen.**

## Decision

**`ci.yml`** runs on every pull request and every push to `main`, and is declared `workflow_call`
so `deploy.yml` reuses it rather than restating it. Cheapest failure first: the traceability gate
is pure stdlib and runs before anything is installed; then format, lint, types; then the unit suite
in a job with no `DATABRICKS_*` variable in scope, so ENV-006 is enforced by the absence of
credentials rather than by intention; then `bundle validate` across all three targets.

**`deploy.yml`** is `workflow_dispatch` only. `verify` reuses `ci.yml`. `deploy-test` records
`git rev-parse HEAD` after checkout and emits it as a job output. `deploy-prod` `needs` that job
and checks out **its emitted output**, not a second read of `github.sha` — those resolve to the
same value only until a re-run or a queued dispatch makes them differ, which is the entire defect
class ENV-003 names. A gate step re-derives the commit after checkout and fails loudly on a
mismatch. Both jobs then read `state/metadata.json` back out of the workspace and compare
`config.bundle.git.commit`, which is what makes the provenance a server-side record rather than a
claim the workflow makes about itself.

**No `${{ }}` appears inside any `run:` block in this repository.** Expressions are substituted
before the shell parses the script, so an expression carrying attacker-controlled text is shell
source. Every value arrives through `env:`. The values used today are trusted; the rule is
unconditional because the moment one becomes event-derived, the shape is already right.
`test_no_run_block_interpolates_a_github_expression` enforces it.

**The `test` target is `mode: production`.** Only `dev` is a development target. Under
`mode: development` the test target resolved to a differently-shaped resource than prod — prefixed
name, forced development semantics, deployment lock disabled — so "the tested artifact" and "the
deployed artifact" were not the same object. See the decision log entry of 2026-08-07 for the
measured diff.

## Consequences

**Positive**

- Promotion is a target switch, not a code change, so what is tested is what deploys.
- `uses: ./.github/workflows/ci.yml` rather than a copy: the pull-request gate and the deploy gate
  cannot drift apart, and a reusable workflow runs at the caller's commit.
- Provenance is a fact about the workspace. `state/metadata.json` under the test target currently
  records `65053aa…`, which is `git rev-parse HEAD` — verified by hand, not asserted.
- Rollback needs no special machinery: redeploying the prior commit restores the prior definitions,
  and a renamed resource is updated in place rather than destroyed and recreated. Measured, because
  the alternative would have made the rollback instruction in `deploy.yml` quietly destructive.

**Negative — stated plainly**

- **The deploy jobs skip, and a skipped job renders green.** Both are gated on
  `vars.DATABRICKS_HOST != ''`, which is unset. This is not a hypothetical weakness: it is why
  `bundle validate -t prod` failed for the entire life of `resources/bronze_pipeline.yml` without
  anyone noticing. The mitigation is that everything checkable offline moved into
  `tests/unit/test_deploy_provenance.py`, which runs unconditionally on every push — but the
  residual gap is real, and it is that CI cannot tell you the deploy path works.
- **The prod deploy has never executed.** ENV-003's control flow is proven; its prod half has been
  exercised only against `test`. Deploying prod would create a third pipeline object and was judged
  not worth the workspace clutter for a mechanism already demonstrated end to end on test.
- **The human gate is a GitHub environment reviewer**, because Free Edition cannot express "only
  this service principal may deploy prod". A reviewer who is also the author is a checkbox.
- **Manual dispatch means the gate is a decision.** Nothing forces a deploy through this path; the
  CLI is right there. What the workflow guarantees is that *deploys made through it* are gated.
- ENV-004 is waived (`production-delta.md` §11). Reproducibility needs a full pipeline run into a
  clean catalog, and that is the one cost this tier cannot absorb.

## Reversal cost: Low

Two YAML files and no data. Moving to continuous deploy is setting a repository variable and
changing a trigger. Moving to OIDC is replacing two `env:` blocks. Nothing downstream encodes the
choice — which is the argument for spending the analysis on ADR-0002 instead, where the reversal
cost is High.

## Revisit trigger

- The account moves to a paid tier and a service principal exists → replace the PAT with OIDC
  federation immediately; the token is the largest single gap in the project
  (`production-delta.md` §2).
- More than one person can deploy → the environment reviewer stops being a checkbox and continuous
  deploy to `test` becomes worth its quota.
- A prod deploy is genuinely needed → run it once and record the transcript, so ENV-003's prod half
  stops being an untested branch of a tested mechanism.
