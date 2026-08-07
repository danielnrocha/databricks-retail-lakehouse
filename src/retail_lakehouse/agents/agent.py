"""A merchandising agent grounded in Unity Catalog, with declining as a first-class outcome.

The design constraint that shapes everything here
--------------------------------------------------
AGT-006: for a question with no supporting data, the agent must state the gap instead of producing
a number. This is the requirement most agent demos fail, and the failure is invisible in a demo
because a confident wrong answer and a confident right answer look identical.

Three mechanisms, because the system prompt alone is not enough:

1. **Narrow tools.** The agent has three functions, not `run_sql`. A question outside their reach
   has no path to a fabricated answer — see `tools.py`.
2. **An explicit instruction with a stated cost.** Telling a model "say you don't know" is weak.
   Telling it *why* — that a wrong number here becomes a purchasing decision — measurably shifts
   behaviour, because it gives the refusal a justification rather than making it an apology.
3. **Empty results are surfaced verbatim.** When a tool returns zero rows, the agent sees
   `[]` and a note saying so. Silently converting an empty result into prose is where
   "no rows matched" becomes "sales were low".

Tracing
-------
Every request is traced to MLflow (AGT-002). Tracing is not observability decoration: an answer
you cannot trace to a tool call is an answer you cannot audit, and the whole grounding claim
(AGT-001) rests on being able to show which call produced which number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

from retail_lakehouse.agents import tools

# gpt-oss-120b: the largest chat endpoint available on this workspace that supports tool calling.
# Named as a constant rather than inlined because the judge in evaluate.py must use a *different*
# model — a model grading its own output grades its own assumptions.
AGENT_MODEL = "databricks-gpt-oss-120b"

MAX_TOOL_ROUNDS = 4

SYSTEM_PROMPT = """You are a merchandising analyst for a grocery retailer. You answer questions \
using only the three tools available to you.

The rule that matters most: if the tools cannot answer the question, say so plainly and say what \
would be needed. Do not estimate, do not extrapolate, do not reason from general knowledge about \
retail. A number you produce here becomes a purchasing decision, so a wrong number is more \
expensive than no number.

Specifically:
- If a tool returns no rows, that means no data matched — say that. It does not mean zero sales.
- If a question needs data your tools do not expose (demographics, competitor data, dates outside \
the loaded range, forecasts), say which part you cannot answer.
- If promotion exposure comes back as 'unknown', that means the week falls outside the window \
where promotion data was collected. Report it as unknown, never as 'not promoted'.
- Always state the numbers you used and which tool they came from.

Be concise. One short paragraph unless the question needs more."""


def _extract_text(content: object) -> str:
    """Pull the answer out of a reasoning model's structured content.

    Reasoning-capable endpoints (gpt-oss here) return a list of blocks rather than a string:
    `[{"type": "reasoning", ...}, {"type": "text", "text": "..."}]`. Assigning `content` directly
    produced an "answer" that was the repr of that list, chain-of-thought and all.

    Two reasons that matters more than cosmetics. A judge scoring the raw blob is scoring the
    model's scratchpad, not its answer — so every downstream quality number would be measuring the
    wrong artifact. And shipping internal reasoning to a user is a leak: it is where the model
    speculates freely before deciding what it actually believes.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip()
    return "" if content is None else str(content)


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    rows: list[dict[str, Any]]
    error: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.rows and self.error is None


@dataclass
class Answer:
    question: str
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    rounds: int = 0

    @property
    def is_grounded(self) -> bool:
        """At least one tool call returned rows. AGT-001's mechanical half."""
        return any(c.rows for c in self.tool_calls)


class MerchandisingAgent:
    def __init__(self, catalog: str, *, client: WorkspaceClient | None = None) -> None:
        self._client = client or WorkspaceClient()
        self._catalog = catalog
        self._warehouse = next(iter(self._client.warehouses.list())).id
        self._openai = self._client.serving_endpoints.get_open_ai_client()

    # -- tool execution ------------------------------------------------------------------

    def _invoke(self, name: str, arguments: dict[str, Any]) -> ToolCall:
        tool = tools.BY_NAME.get(name)
        if tool is None:
            return ToolCall(name, arguments, [], error=f"No such tool: {name}")

        # Positional binding against the declared parameter order, with literals rendered by
        # type. The arguments come from a language model, so they are untrusted input: string
        # values are escaped and everything else must parse as a number or the call fails. This
        # is why the tools are declared functions rather than generated SQL.
        try:
            args = []
            for pname, ptype, _ in tool.params:
                value = arguments[pname]
                if ptype in ("STRING", "DATE"):
                    escaped = str(value).replace("'", "''")
                    args.append(f"'{escaped}'" if ptype == "STRING" else f"DATE '{escaped}'")
                else:
                    args.append(str(int(value)))
        except (KeyError, ValueError, TypeError) as exc:
            return ToolCall(name, arguments, [], error=f"Bad arguments: {exc}")

        sql = f"SELECT * FROM {self._catalog}.gold.{name}({', '.join(args)})"
        result = self._client.statement_execution.execute_statement(
            warehouse_id=self._warehouse, statement=sql, wait_timeout="30s"
        )
        if result.status and result.status.state != StatementState.SUCCEEDED:
            message = result.status.error.message if result.status.error else "unknown"
            return ToolCall(name, arguments, [], error=message[:200])

        columns = [c.name for c in result.manifest.schema.columns]
        rows = [dict(zip(columns, r, strict=True)) for r in (result.result.data_array or [])]
        return ToolCall(name, arguments, rows)

    @staticmethod
    def _tool_message(call: ToolCall) -> str:
        if call.error:
            return json.dumps({"error": call.error})
        if not call.rows:
            # The wording is deliberate. "No rows matched" is a fact about the query; "zero sales"
            # is a claim about the business, and the gap between them is where agents invent.
            return json.dumps(
                {"rows": [], "note": "No rows matched these arguments. This is not a zero value."}
            )
        return json.dumps({"rows": call.rows})

    # -- the loop ------------------------------------------------------------------------

    def ask(self, question: str) -> Answer:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        answer = Answer(question=question, text="")

        for round_index in range(MAX_TOOL_ROUNDS):
            response = self._openai.chat.completions.create(
                model=AGENT_MODEL,
                messages=messages,
                tools=tools.schemas(),
                temperature=0.0,
            )
            choice = response.choices[0].message
            answer.rounds = round_index + 1

            if not choice.tool_calls:
                answer.text = _extract_text(choice.content)
                return answer

            messages.append(
                {
                    "role": "assistant",
                    "content": choice.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in choice.tool_calls
                    ],
                }
            )

            for tc in choice.tool_calls:
                try:
                    arguments = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    arguments = {}
                call = self._invoke(tc.function.name, arguments)
                answer.tool_calls.append(call)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": self._tool_message(call),
                    }
                )

        # Falling out of the loop means the model kept calling tools without concluding. Returning
        # the partial state is better than looping: a bounded wrong answer is diagnosable, an
        # unbounded one is a bill.
        answer.text = (
            f"Could not conclude within {MAX_TOOL_ROUNDS} tool rounds. "
            f"Tools called: {[c.name for c in answer.tool_calls]}"
        )
        return answer
