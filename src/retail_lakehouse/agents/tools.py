"""Agent tools as Unity Catalog functions.

Why UC functions and not Python callables
------------------------------------------
A Python function passed to an agent is a tool the agent can use. A Unity Catalog function is a
tool the agent can use *and* that the platform governs: it has an owner, a grant, a comment,
lineage to the tables it reads, and an audit trail of every invocation. When someone asks "what
can this agent see?", the answer is a `SHOW FUNCTIONS` rather than a code review.

That distinction is the whole argument for putting agents inside the data platform rather than
beside it. An agent whose tools are ungoverned Python is an agent whose blast radius is whatever
the service account can reach.

The narrow-tool principle
--------------------------
Each function answers one question and returns a bounded result. The tempting alternative — a
single `run_sql(query)` tool — is far more capable and far worse: it makes the agent's reach equal
to the credential's reach, turns every prompt injection into arbitrary SQL, and makes
"is this answer grounded?" unanswerable because the query is generated rather than declared.

Narrow tools also make AGT-006 achievable. An agent that can only call three functions discovers
quickly that a question none of them answers is a question it cannot answer, which is the
behaviour we want. An agent holding `run_sql` will always produce *something*.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Each tool is declared once, here, and used three times: to create the UC function, to build the
# JSON schema the model sees, and to dispatch a call. Declaring it once means the three cannot
# drift apart — a tool whose schema promises a parameter its SQL ignores is a silent wrong answer.


@dataclass(frozen=True)
class Tool:
    name: str
    comment: str
    params: list[tuple[str, str, str]]  # (name, sql_type, description)
    returns: str
    body: str

    def create_sql(self, catalog: str) -> str:
        # Every comment goes through _q. The first version interpolated them raw and a comment
        # containing the word 'unknown' in quotes produced PARSE_SYNTAX_ERROR at position 141 of a
        # generated statement — the same escaping bug the silver work hit with
        # create_streaming_table(schema=...). Generated DDL is string concatenation wearing a
        # library's clothes, and the tool descriptions here are prose, so they will contain
        # apostrophes.
        args = ", ".join(f"{n} {t} COMMENT {_q(d)}" for n, t, d in self.params)
        return (
            f"CREATE OR REPLACE FUNCTION {catalog}.gold.{self.name}({args})\n"
            f"RETURNS TABLE {self.returns}\n"
            f"COMMENT {_q(self.comment)}\n"
            f"RETURN {self.body.format(catalog=catalog)}"
        )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.comment,
                "parameters": {
                    "type": "object",
                    "properties": {
                        n: {"type": _json_type(t), "description": d} for n, t, d in self.params
                    },
                    "required": [n for n, _, _ in self.params],
                },
            },
        }


def _q(text: str) -> str:
    """Single-quote a SQL string literal, doubling any embedded apostrophes."""
    escaped = text.replace("'", "''")
    return f"'{escaped}'"


def _json_type(sql_type: str) -> str:
    return {"INT": "integer", "BIGINT": "integer", "STRING": "string", "DATE": "string"}[sql_type]


TOOLS: list[Tool] = [
    Tool(
        name="agent_promo_performance",
        comment=(
            "Sales by promotion exposure for one product in one week. Exposure is three-state: "
            "display, mailer, both, not_promoted, or unknown. 'unknown' means the week falls "
            "outside the 9-101 window where promotion data was collected, so the promotional "
            "status is not recorded rather than absent. Serves promo performance questions."
        ),
        params=[
            ("product_id", "BIGINT", "Product identifier"),
            ("week", "INT", "Week number, 1 to 102"),
        ],
        returns="(promo_exposure STRING, sales_amt DOUBLE, baskets BIGINT, stores BIGINT)",
        body=(
            "SELECT promo_exposure, sales_amt, baskets, stores "
            "FROM {catalog}.gold.agg_promo_performance "
            "WHERE product_id = agent_promo_performance.product_id "
            "AND week_no = agent_promo_performance.week"
        ),
    ),
    Tool(
        name="agent_store_day",
        comment=(
            "Daily totals for one store on one date: revenue, baskets, distinct households, "
            "lines, and average basket value. Use for store performance and anomaly questions."
        ),
        params=[
            ("store", "INT", "Store identifier"),
            ("on_date", "DATE", "Date in YYYY-MM-DD form"),
        ],
        returns=(
            "(sales_amt DOUBLE, baskets BIGINT, households BIGINT, lines BIGINT, "
            "avg_basket_amt DOUBLE)"
        ),
        body=(
            "SELECT sales_amt, baskets, households, lines, avg_basket_amt "
            "FROM {catalog}.gold.agg_store_daily "
            "WHERE store_id = agent_store_day.store "
            "AND transaction_date = agent_store_day.on_date"
        ),
    ),
    Tool(
        name="agent_household_profile",
        comment=(
            "Behavioural profile for one household: recency in days, basket frequency, total "
            "spend, category breadth, and the share of spend on coupon and on promotion. "
            "Demographics are NOT included -- only 801 of 2,500 households have them, so any "
            "demographic answer would be unavailable for two thirds of the population."
        ),
        params=[("household", "BIGINT", "Household key")],
        returns=(
            "(recency_days INT, frequency_baskets BIGINT, monetary_amt DOUBLE, "
            "distinct_departments BIGINT, coupon_share_of_spend DOUBLE, "
            "promo_share_of_spend DOUBLE)"
        ),
        body=(
            "SELECT recency_days, frequency_baskets, monetary_amt, distinct_departments, "
            "coupon_share_of_spend, promo_share_of_spend "
            "FROM {catalog}.gold.agg_household_rfm "
            "WHERE household_key = agent_household_profile.household"
        ),
    ),
]

BY_NAME = {t.name: t for t in TOOLS}


def schemas() -> list[dict[str, Any]]:
    return [t.schema() for t in TOOLS]
