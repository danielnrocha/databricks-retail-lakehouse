"""LLM-as-judge evaluation, gating deployment on scores rather than on impressions.

Why a judge and not a human eyeball
------------------------------------
Reading ten agent answers and concluding "seems good" is the standard practice and it does not
survive contact with a change. Swap the model, edit the prompt, add a tool — now you need to read
ten more, and you have no way to say whether it got better or worse, because your previous
judgement was a feeling.

The judge is a different model from the agent (AGENT_MODEL vs JUDGE_MODEL). A model grading its
own output grades its own assumptions: if it misread the tool description, it will misread it
identically when scoring, and agree with itself.

The eval set is where the real work is
---------------------------------------
Judges are cheap; a good eval set is not. Ours is deliberately weighted toward the cases that
break agents rather than the cases that flatter them:

* **answerable** — the happy path, and the smallest share.
* **unanswerable** — the data does not exist. The correct answer is a refusal (AGT-006), and
  scoring these is the only way to detect an agent that has learned to always produce something.
* **empty** — the tool works and returns zero rows. This is the sharpest test in the set: the
  agent must distinguish "no rows matched" from "the value is zero", and the wrong answer here
  looks *exactly* like a right one.
* **trap** — the data exists but the naive reading of it is wrong. Promotion `unknown` reported as
  `not_promoted` is the case that matters most, because that single substitution silently converts
  a measurement gap into a business fact.

An eval set of only answerable questions measures nothing except whether the model can read.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from databricks.sdk import WorkspaceClient

from retail_lakehouse.agents.agent import AGENT_MODEL, Answer, MerchandisingAgent
from retail_lakehouse.common.workspace import openai_client

REPO_ROOT = Path(__file__).resolve().parents[3]
EVAL_SET = REPO_ROOT / "data" / "agents" / "eval_set.json"
RESULTS = REPO_ROOT / "data" / "agents" / "eval_results.json"

# A different family from the agent, deliberately. Same-family judges share failure modes.
JUDGE_MODEL = "databricks-meta-llama-3-3-70b-instruct"

# Declared before any answer is generated, so the bar cannot be lowered to fit the result — the
# same discipline the ML gate uses. Groundedness is strictest because a fabricated number is the
# failure with the highest cost.
THRESHOLDS = {"grounded": 0.90, "correct": 0.80, "appropriate": 0.90}

JUDGE_PROMPT = """You are grading a data analyst's answer. Be strict and literal.

QUESTION: {question}

EXPECTED BEHAVIOUR: {expectation}

TOOL RESULTS THE ANALYST ACTUALLY RECEIVED:
{evidence}

THE ANALYST'S ANSWER:
{answer}

The expectation states the MINIMUM the answer must contain, not the maximum. Extra facts that
came from the tool results are correct and must not be penalised. Verbosity is not a failure.
Score each dimension independently -- a complaint about one must never lower another.

Score three dimensions, each 0 or 1:

grounded: 1 if every number in the answer appears in the tool results above, or the answer
contains no numbers because it correctly declined. 0 if any number was invented, estimated, or
carried over from general knowledge.

correct: 1 if the answer is factually right given the tool results. 0 if it misreads them.

appropriate: 1 if the answer matches the expected behaviour above — in particular, if the expected
behaviour is a refusal, the analyst must actually decline rather than answer. 0 otherwise.

Reply with ONLY a JSON object, no other text:
{{"grounded": 0 or 1, "correct": 0 or 1, "appropriate": 0 or 1, "reason": "one sentence"}}"""


@dataclass
class Score:
    case_id: str
    category: str
    grounded: int
    correct: int
    appropriate: int
    reason: str
    agent_declined: bool
    tools_used: list[str]


@dataclass
class GateResult:
    n_cases: int
    grounded: float
    correct: float
    appropriate: float
    by_category: dict[str, float]
    passes: bool
    failures: list[str]


def _evidence(answer: Answer) -> str:
    if not answer.tool_calls:
        return "(no tools were called)"
    lines = []
    for call in answer.tool_calls:
        if call.error:
            lines.append(f"{call.name}{call.arguments} -> ERROR: {call.error}")
        elif not call.rows:
            lines.append(f"{call.name}{call.arguments} -> NO ROWS MATCHED (not a zero value)")
        else:
            lines.append(f"{call.name}{call.arguments} -> {json.dumps(call.rows)}")
    return "\n".join(lines)


def judge(client: WorkspaceClient, case: dict[str, Any], answer: Answer) -> Score:
    openai = openai_client(client)
    response = openai.chat.completions.create(
        model=JUDGE_MODEL,
        messages=[
            {
                "role": "user",
                "content": JUDGE_PROMPT.format(
                    question=case["question"],
                    expectation=case["expectation"],
                    evidence=_evidence(answer),
                    answer=answer.text or "(empty)",
                ),
            }
        ],
        temperature=0.0,
    )
    raw = (response.choices[0].message.content or "").strip()
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        verdict = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        # An unparseable verdict scores zero rather than being skipped. Skipping would let a judge
        # failure silently shrink the denominator and inflate the pass rate.
        verdict = {
            "grounded": 0,
            "correct": 0,
            "appropriate": 0,
            "reason": f"unparseable: {raw[:80]}",
        }

    declined = not answer.is_grounded and bool(answer.text)
    return Score(
        case_id=case["id"],
        category=case["category"],
        grounded=int(verdict.get("grounded", 0)),
        correct=int(verdict.get("correct", 0)),
        appropriate=int(verdict.get("appropriate", 0)),
        reason=str(verdict.get("reason", ""))[:200],
        agent_declined=declined,
        tools_used=[c.name for c in answer.tool_calls],
    )


def gate(scores: list[Score]) -> GateResult:
    def mean(attr: str, subset: list[Score] | None = None) -> float:
        rows = subset if subset is not None else scores
        return statistics.mean(getattr(s, attr) for s in rows) if rows else 0.0

    categories = sorted({s.category for s in scores})
    by_category = {
        c: mean("appropriate", [s for s in scores if s.category == c]) for c in categories
    }

    measured = {k: mean(k) for k in THRESHOLDS}
    failures = [
        f"{k}: {v:.0%} below the {THRESHOLDS[k]:.0%} threshold"
        for k, v in measured.items()
        if v < THRESHOLDS[k]
    ]
    return GateResult(
        n_cases=len(scores),
        grounded=measured["grounded"],
        correct=measured["correct"],
        appropriate=measured["appropriate"],
        by_category=by_category,
        passes=not failures,
        failures=failures,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, help="environment catalog holding the tools")
    args = parser.parse_args()

    cases = json.loads(EVAL_SET.read_text(encoding="utf-8"))
    client = WorkspaceClient()
    agent = MerchandisingAgent(args.catalog, client=client)

    print(f"agent: {AGENT_MODEL}   judge: {JUDGE_MODEL}   cases: {len(cases)}\n")
    print(f"{'id':<10}{'category':<14}{'gr':>4}{'co':>4}{'ap':>4}  tools")
    print("-" * 74)

    scores: list[Score] = []
    for case in cases:
        answer = agent.ask(case["question"])
        score = judge(client, case, answer)
        scores.append(score)
        used = ",".join(sorted(set(score.tools_used))) or "(none)"
        print(
            f"{score.case_id:<10}{score.category:<14}"
            f"{score.grounded:>4}{score.correct:>4}{score.appropriate:>4}  {used[:36]}"
        )

    result = gate(scores)
    print(f"\n{'dimension':<16}{'score':>8}{'threshold':>12}")
    print("-" * 36)
    for k in THRESHOLDS:
        print(f"{k:<16}{getattr(result, k):>7.0%}{THRESHOLDS[k]:>12.0%}")

    print("\nappropriate-behaviour rate by category:")
    for category, rate in sorted(result.by_category.items()):
        print(f"  {category:<16}{rate:>6.0%}")

    print(f"\ngate: {'PASS' if result.passes else 'FAIL'}")
    for failure in result.failures:
        print(f"  {failure}")

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(
        json.dumps(
            {
                "agent_model": AGENT_MODEL,
                "judge_model": JUDGE_MODEL,
                "thresholds": THRESHOLDS,
                "gate": asdict(result),
                "scores": [asdict(s) for s in scores],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {RESULTS.relative_to(REPO_ROOT)}")
    return 0 if result.passes else 2


if __name__ == "__main__":
    sys.exit(main())
