# The agentic layer — what the judges actually caught

A merchandising agent over three Unity Catalog functions, evaluated by a different model against a
curated eval set, gated on scores. Agent: `databricks-gpt-oss-120b`. Judge:
`databricks-meta-llama-3-3-70b-instruct`.

Final: **grounded 100%, correct 100%, appropriate 100%** against thresholds of 90/80/90. Gate PASS.

That headline is the least interesting part of this document.

---

## A1 — The first gate failure was in the evaluation, not the agent

The first run failed, and it failed in the direction nobody predicts:

| category | appropriate-behaviour rate |
|---|---:|
| unanswerable | 100% |
| trap | 100% |
| empty | 100% |
| **answerable** | **33%** |

The agent handled every hard case — refusing on missing demographics, refusing to forecast,
reporting `unknown` promotion exposure as unknown, distinguishing "no rows matched" from "zero
sales" — and failed the easy ones.

The judge's reasons said why:

> *"The analyst's answer included unnecessary information beyond what was expected, such as
> households and lines."*

The expectations had been written as exhaustive lists — *"Report revenue 515.93 and 1 basket"* —
so the judge read extra correct columns from the same tool call as non-compliance. And on `ans-03`
it marked **`grounded = 0`** for what was purely a verbosity complaint, conflating two dimensions
that were supposed to be scored independently.

**The system under test was fine. The instrument was broken.** This is the third time in this
project that a measurement apparatus failed in a way that looked like a real result — after the
total-variation threshold set below the noise floor, and after F2 measuring store coverage while
describing a composite-key join.

### Why the fix is not tuning-until-it-passes

The distinction matters enough to state precisely, because from a distance the two look identical:

- **Lowering `THRESHOLDS` from 90% to 80%** would be tuning. The standard changes to fit the
  result.
- **Rewriting an expectation from "report X and Y" to "MUST INCLUDE X and Y; extra facts from the
  same tool are welcome"** is correcting a specification that said something other than what it
  meant. The standard is unchanged; its statement is fixed.

The thresholds were not touched. They are declared as a module constant above the evaluation code,
before any answer exists, for exactly this reason. The judge prompt also gained one line — *"the
expectation states the MINIMUM, not the maximum"* and *"score each dimension independently"* —
because a judge that silently couples dimensions produces numbers you cannot reason about.

The honest caveat: this correction was made *after* seeing a failure. That is the shape of
p-hacking, and the only defence is that the change is inspectable, the reasoning is written down,
and the bar itself did not move. A reader who disagrees can revert the expectations and re-run.

## A2 — The eval set is the artifact, the judge is a commodity

Ten cases across four categories, and only three are the happy path:

| category | cases | what it detects |
|---|---:|---|
| answerable | 3 | can it read at all |
| **unanswerable** | 3 | does it decline, or does it always produce something |
| **empty** | 2 | does it say "no rows matched" or "zero sales" |
| **trap** | 2 | does the naive reading of real data fool it |

The `empty` category is the sharpest test in the set, because **the wrong answer looks exactly
like a right one.** "Store 33923 made 0 on 2027-03-15" is a well-formed, confident, entirely
fabricated sentence, and no amount of staring at the answer reveals that the tool returned nothing.

The `trap` cases are second. `trap-01` asks whether a product was on promotion in week 102 — the
tool returns `unknown`, because week 102 is outside the 9–101 collection window. Answering "no, it
was not on promotion" converts a measurement gap into a business fact, in one word, invisibly.

`trap-02` asks whether sales appearing under `not_promoted` prove the promotion had no effect. The
correct answer is a refusal: exposure is not randomly assigned, there is no counterfactual, and the
same product appears under several states. **The agent declined without calling any tool** — it
recognised a causal question that no amount of data retrieval could answer, which is a
qualitatively harder refusal than "I don't have that column".

An eval set of only answerable questions measures whether the model can read. It cannot detect the
failure mode that matters.

## A3 — Three mechanisms make declining work; the prompt alone would not

AGT-006 held on 5 of 5 cases where declining was correct (3 unanswerable + 2 empty). The
attribution is not "we asked it nicely":

1. **Narrow tools.** Three declared functions, not `run_sql`. A question outside their reach has
   no path to a fabricated answer. An agent holding `run_sql` will always produce *something*, and
   its reach equals its credential's reach — which also turns every prompt injection into
   arbitrary SQL.
2. **The instruction carries a cost, not a request.** "A number you produce here becomes a
   purchasing decision, so a wrong number is more expensive than no number" gives the refusal a
   justification. "Say you don't know if you don't know" makes it an apology.
3. **Empty results are surfaced verbatim**, with the note *"No rows matched these arguments. This
   is not a zero value."* The tool layer refuses to make the interpretation, so the model has to.

## A4 — Two implementation defects worth the record

**Reasoning models return blocks, not strings.** `gpt-oss` returns
`[{"type": "reasoning", ...}, {"type": "text", "text": "..."}]`. Assigning `content` directly
produced an "answer" that was the repr of that list, chain-of-thought included. Two consequences,
and the second is worse: a judge scoring that blob grades the model's scratchpad rather than its
answer, so every quality number would have measured the wrong artifact — and shipping internal
reasoning to a user leaks the part where the model speculates freely before deciding what it
believes.

**Generated DDL needs escaping, again.** A tool description containing the word `'unknown'` in
quotes produced `PARSE_SYNTAX_ERROR` at position 141 of a generated `CREATE FUNCTION`. This is the
identical bug the silver work hit with `create_streaming_table(schema=...)`. Tool descriptions are
prose written for a language model, so they *will* contain apostrophes. Every literal now goes
through one `_q()` helper.

## A5 — Why Unity Catalog functions rather than Python callables

A Python function passed to an agent is a tool. A UC function is a tool **the platform governs**:
owner, grant, comment, lineage to the tables it reads, audit trail per invocation. "What can this
agent see?" becomes `SHOW FUNCTIONS` instead of a code review.

That is the entire argument for putting agents inside the data platform rather than beside it, and
it is the argument that survives when someone asks who approved the agent's access to a table.

---

## Requirements

| ID | Status | Evidence |
|---|---|---|
| AGT-001 | holds | every numeric claim traces to a tool call; grounded 100% |
| AGT-003 | holds | judges score 10 curated cases on three dimensions |
| AGT-004 | holds | gate returns exit code 2 on failure; thresholds declared before results exist |
| AGT-006 | holds | 5 of 5 decline-correct cases, including a causal question with no tool call |
| AGT-002 | partial | tool calls and arguments captured per answer; MLflow trace export not wired |
| AGT-005 | not attempted | no production traffic to sample |

## What this does not demonstrate

- **Managed MCP.** The tools are UC functions called directly, not exposed through a Databricks
  managed MCP server. The governance argument is identical; the transport is not.
- **Adversarial robustness.** No prompt-injection cases in the eval set. An agent whose tools are
  three typed functions has a small injection surface, but "small" is not "measured".
- **Scale.** Ten cases. Enough to catch a regression in behaviour class, not enough to estimate a
  rate with any precision — the same underpowered-evaluation problem the ML layer hit, and worth
  naming here rather than letting three 100% scores imply more confidence than ten cases can carry.
