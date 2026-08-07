# ADR-0001 — Spec-driven development with an adversarial agent loop

**Status:** Accepted · **Date:** 2026-08-06 · **Reversal cost:** Low

> Written last among the accepted records, and deliberately so. An ADR about *how to work* that is
> written before the work has happened is a manifesto. This one is written after roughly a day of
> building, and every claim in it cites something that actually went wrong in this repository.

---

## Context

The question is not "should AI write the code". That is settled by economics. The question is
**what the human is still responsible for**, and the honest answer is narrower and more demanding
than most people admit.

Two failure modes bracket the problem.

**Prompt-driven development.** Human writes a prompt, reads the output, writes the next prompt. The
human is the only reviewer and therefore the bottleneck *and* the single point of failure.
Correctness is whatever the human happened to notice. Throughput is capped by reading speed, and
attention degrades exactly when the work gets long.

**Unsupervised delegation.** Hand over the goal, come back later. Fails differently and worse: the
agent optimises for the stated objective, declares success, and the failure is discovered by
whoever consumes the output. The output *looks* finished, which is the problem.

The interesting design space is between them, and the load-bearing question is: **what makes the
agent's self-assessment trustworthy?** Not "how do I check everything" — that is prompt-driven
again — but "what structure makes it hard for wrong work to look right?"

## Options considered

### A. Human-in-the-loop review of every change
The default, and it does not scale. Worse, it produces *false confidence*: a human skimming a
200-line diff catches style and misses semantics. In this project, four of the six real defects
were invisible in the diff — they were only visible in the output of running the thing. Reading
the code would never have found them.

### B. Test-driven development alone
Necessary and demonstrably insufficient. This repository has the receipt: `GEN-005` went green
while the emitter produced **73% duplicates against a configured 0.5%**, because the test asserted
only that *some* duplicates appeared. Requirement mapped, test written, row green, generator badly
wrong.

TDD guarantees the code does what the test says. It says nothing about whether the test says
anything.

### C. Spec-driven development with an adversarial loop ✅

**Chosen.** Four mechanisms, each addressing a failure the others cannot.

## Decision

### 1. The spec is the contract, and a machine enforces the link

`specs/REQUIREMENTS.md` holds numbered requirements with testable acceptance criteria.
`specs/traceability.md` maps each to its test. `scripts/check_traceability.py` fails the build on
any requirement without a row, or any `PASSING` row whose test file does not exist.

The gate is deliberately *lenient about progress and strict about honesty*: a `PLANNED` row is
allowed to have no test yet, because the alternative — requiring every test to exist immediately —
forces either 48 empty placeholder files or a big-bang merge, and both are worse. **A quality gate
that cannot be satisfied incrementally will be disabled, and a disabled gate is worse than a
lenient one.**

Writing the matrix *before* the tests is where most of the value is. It forces the acceptance
criterion into terms a test can check, which is the moment vague requirements collapse — you
discover you cannot say what "good data quality" would assert.

### 2. Thresholds are derived, never chosen

This one was learned the hard way, twice, in the same afternoon.

A sampler test asserted total variation distance `< 0.02` — a number picked because it looked
small. It failed at 0.026. **The test was wrong, not the code:** with 582 stores and 60,000 draws,
multinomial noise alone produces ~0.024, so the threshold sat *below the achievable floor* and no
correct sampler could ever have passed.

The obvious fix — loosen the constant until it goes green — converts a real assertion into a
decorative one. The correct fix measures the baseline: draw from the true distribution, compute
the noise floor per run, assert the sampler is within 1.5× of it.

Then the identical mistake recurred in a store-coverage assertion of 90% that failed at 73%, where
73% was correct — coupon-collector statistics over a 582-store long tail put the floor below the
threshold. Fixed the same way: a control run at identical volume.

**Rule: if you cannot derive a threshold, you do not understand what you are asserting.**

### 3. Run the thing and look at the output

The single highest-yield activity in this project, by a wide margin. Every defect below was
invisible to code review and invisible to the test suite:

| Defect | How it was found |
|---|---|
| Duplicates at 73% against 0.5% configured | Printed the manifest and divided |
| Beyond-watermark rate 0.003% against 0.200% | Compared configured to observed in a table |
| Drift never fired (threshold past run length) | Manifest field read `None` |
| `_rescued_data` empty across 200,000 rows | Queried the table |
| `ops.dq_metrics` reporting 990,065 rows | Noticed it was exactly 5× the row count |
| Cache-busting by comment not working | Runs returned `read_bytes = 0` |

That last one deserves emphasis: Databricks strips comments from the query cache key, so a
performance lab that busted the cache with a varying comment **would have been a cache benchmark
end to end** — 84 measured runs, all meaningless, all internally consistent. It was caught by one
zero in a column nobody was looking at.

The pattern across all six: **the wrong numbers were plausible.** Large, monotonic, well-formatted.
None looked like a bug. They looked like results.

### 4. Adversarial second pass, with real independence

Parallel agents on disjoint file boundaries, each verifying rather than trusting. Independence is
the operative word — an agent asked to check its own work checks whatever it was already thinking.

The concrete return: a second agent measured the wide join instead of reasoning about it and found
that `dataset-findings.md` **F2 was wrong by a factor of fifty**. The document claimed the inner
join "loses 1.4% of revenue". That figure describes a join on `STORE_ID`; the join the architecture
actually performs is on `(PRODUCT_ID, STORE_ID, WEEK_NO)` and drops **78.28%** of the fact table.

The error was measuring one thing and describing another, and the sentence still read as true.
That class of error is essentially undetectable by self-review, because the author re-reads it with
the measurement they took in mind.

A third instance: an ADR specified clustering `dim_product_scd2` on `(product_id, is_current)`.
There is no `is_current` column — `AUTO CDC` emits `__START_AT` / `__END_AT`. The rule was right;
the worked example was invented, because it was written before the code existed.

### 5. Reversals are kept, never rewritten

Three so far, all left visible with the original text intact:

- The amplifier was going to inject store skew. Profiling showed the skew is native and severe
  (2,519× max/median), so injection would have produced a mitigation validated only against
  manufactured distributions — a strawman.
- F2's fifty-fold error, above.
- ADR-0007's invented column.

Rewriting a reversal to look prescient destroys the most useful information in the record: that
evidence changed the plan. The first reversal happened about two hours in and cost one ADR edit;
three weeks later it would have cost a generator rewrite and a re-run of the performance lab.

## What the human is actually responsible for

Everything above is delegable. These are not, and this is the part worth teaching:

1. **Defining what "good" means, in advance, in falsifiable terms.** An agent will optimise
   whatever you state. State the wrong thing and you get exactly it.
2. **Deciding what is worth measuring.** The agent measured everything asked for. Nobody asked it
   whether `causal_data` duplicate keys would fan out a LEFT JOIN until a human wondered.
3. **Noticing when a number is suspicious.** 990,065 is exactly 5× 198,013. A machine checking
   "is this a positive integer" passes it.
4. **Judging trade-offs with no correct answer.** Whether logical isolation is acceptable, whether
   a 14-day trial beats a permanent free tier, whether a fragile dependency is worth an unused
   feature.
5. **Deciding when to stop.** The most under-discussed one. A loop with no exit condition burns
   budget rediscovering the same wall.

## Consequences

**Positive**
- Wrong work is expensive to make look right: it has to pass a derived-threshold test, survive an
  independent measurement, and produce output a human looked at.
- The record is honest, so the project is legible to someone who was not there.
- Parallelism is real. Two agents ran a performance lab and a silver layer simultaneously with no
  collision, because file ownership was declared up front rather than negotiated.

**Negative — and these are real costs**
- **Setup is front-loaded and feels unproductive.** Requirements, traceability and the gate all
  landed before a single pipeline existed. On a shorter project that overhead does not amortise.
- **Independent verification costs real tokens.** The adversarial pass that caught F2 was a
  full second agent. It found one error. That is worth it here and would not be worth it for
  everything.
- **Disjoint file ownership needs a coordinator**, and that coordinator is the context bottleneck.
  This does not scale past a handful of parallel agents without a different structure.
- **The loop cannot catch what nobody thought to measure.** Every defect above was found because
  some check existed or someone looked. The unknown unknowns are still unknown, and no amount of
  process changes that.

## Reversal cost: Low

The mechanisms are additive and independently removable. Deleting the traceability gate leaves the
tests. Dropping the adversarial pass leaves the spec. Nothing else depends on them structurally —
which is precisely why they must be *enforced by machine*, because anything optional at 23:00 on a
deadline is already gone.
