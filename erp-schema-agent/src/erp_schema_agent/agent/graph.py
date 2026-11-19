"""The agent as a LangGraph state machine.

    screen -> retrieve -> generate -> validate -> execute -> answer -> ground -> END
                             ^           |           |
                             +-- repair -+-----------+   (at most MAX_REPAIR_ATTEMPTS)

* ``screen`` (deterministic) refuses requests that name credentials, system catalogs or
  SQL write statements before the model sees them.
* ``generate`` asks the model for structured JSON (decision, SQL, tables, assumptions,
  rationale). The model may refuse.
* ``validate`` runs the sqlglot guardrails; ``execute`` runs the re-rendered SQL as the
  read-only role. A rejection or database error goes back to ``generate`` with the
  reason, until the repair budget is spent.
* ``answer`` asks the model to write the answer and list its figures; ``ground`` checks
  every figure against the rows deterministically.

The graph only orchestrates. The model proposes; code validates and executes.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, TypedDict

from erp_sandbox.llm import ModelClient, ModelError, UsageTally
from langgraph.graph import END, START, StateGraph

from ..executor import QueryError, QueryResult, run_readonly
from ..guardrails import Verdict, validate_sql
from ..limits import MAX_REPAIR_ATTEMPTS, RECURSION_LIMIT
from ..retrieval.catalog import load_catalog
from ..retrieval.search import Retrieval, retrieve
from . import prompts
from .grounding import FigureCheck, check_figures

Executor = Callable[[str], QueryResult]

# Where a run stopped (AgentRun.blocked_by); the trace and evals compare against these.
SCREEN = "intent screen"
MODEL = "model"
VALIDATOR = "validator"
DATABASE = "database"
MODEL_ERROR = "model error"
RETRIEVAL_ERROR = "retrieval error"

# Deterministic intent screen: phrases that never belong to a business question. It is
# deliberately narrow (plain words like "pins" or "tokens" can be products); requests to
# change data in plain English are left to the model's refusal, then the validator and
# the role.
_SCREEN = [
    (re.compile(r"\b(passwords?|passwd|credentials?|api[ _-]?keys?|secret keys?|access tokens?|"
                r"refresh tokens?|auth(entication)? tokens?|encryption keys?|private keys?|"
                r"password (hash(es)?|salts?)|user ?pins?|pin codes?)\b", re.I),
     "asks for credentials or secrets"),
    (re.compile(r"\b(pg_[a-z_]+|information_schema)\b", re.I), "asks about database internals"),
    (re.compile(r"\b(drop|alter|create)\s+(table|schema|database|role|user|index|view|function|extension)\b"
                r"|\btruncate\s+(table\s+)?[\w.\"]+|\bdelete\s+from\b|\binsert\s+into\b|\bmerge\s+into\b"
                r"|\bupdate\s+[\w.\"]+\s+set\b|\bcopy\s+[\w.\"]+\s+(to|from)\b|\bgrant\s+\w+\s+on\b",
                re.I), "contains a data-modifying SQL statement"),
]


def screen_question(question: str) -> str | None:
    """Reason to refuse without calling the model, or None."""
    for pattern, reason in _SCREEN:
        if pattern.search(question):
            return reason
    return None


class AgentState(TypedDict, total=False):
    question: str
    retrieval: Retrieval
    generation: dict[str, Any]
    verdict: Verdict
    result: QueryResult | None
    generations: int
    failures: list[dict[str, str]]
    answer: dict[str, Any]
    figures: list[FigureCheck]
    status: str  # answered | refused | failed
    blocked_by: str  # one of the stage constants above
    reason: str


@dataclass
class AgentRun:
    question: str
    status: str
    blocked_by: str = ""
    reason: str = ""  # why the run was refused or failed
    retrieved_tables: list[str] = field(default_factory=list)
    retrieval_reasons: dict[str, str] = field(default_factory=dict)
    sql: str = ""
    executed_sql: str = ""
    tables_used: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    rationale: str = ""
    verdict: dict[str, Any] = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False
    failures: list[dict[str, str]] = field(default_factory=list)
    answer: str = ""
    caveats: list[str] = field(default_factory=list)
    figures: list[FigureCheck] = field(default_factory=list)
    generations: int = 0
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def repairs(self) -> int:
        """SQL generations after the first one."""
        return max(0, self.generations - 1)

    @property
    def grounded(self) -> bool:
        return all(f.grounded for f in self.figures)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["repairs"] = self.repairs
        data["grounded"] = self.grounded
        return data


def build_graph(model: ModelClient, executor: Executor, tally: UsageTally):
    catalog = load_catalog()
    allowed = frozenset(catalog.tables)
    sensitive = catalog.sensitive_tables

    def screen(state: AgentState) -> AgentState:
        reason = screen_question(state["question"])
        if reason:
            return {"status": "refused", "blocked_by": SCREEN, "reason": reason}
        return {}

    def retrieve_node(state: AgentState) -> AgentState:
        try:
            return {"retrieval": retrieve(state["question"]), "failures": []}
        except (OSError, ValueError, KeyError) as exc:
            return {"status": "failed", "blocked_by": RETRIEVAL_ERROR, "reason": str(exc)}

    def generate(state: AgentState) -> AgentState:
        failures = state.get("failures", [])
        try:
            result = model.complete_json(
                system=prompts.GENERATE_SYSTEM,
                prompt=prompts.generate_prompt(state["question"], state["retrieval"].context, failures),
                schema=prompts.GENERATE_SCHEMA,
                label=f"generate#{len(failures) + 1}",
            )
        except ModelError as exc:
            return {"status": "failed", "blocked_by": MODEL_ERROR, "reason": str(exc), "generation": {},
                    "verdict": None, "result": None}
        tally.add(result)
        data = result.data
        # A new attempt replaces the previous one's verdict and rows.
        fresh: AgentState = {"generation": data, "generations": state.get("generations", 0) + 1,
                             "verdict": None, "result": None}
        if data["decision"] == "refuse":
            return {**fresh, "status": "refused", "blocked_by": MODEL,
                    "reason": data["refusal_reason"] or "the model declined"}
        return fresh

    def validate(state: AgentState) -> AgentState:
        verdict = validate_sql(state["generation"]["sql"], allowed_tables=allowed, sensitive_tables=sensitive)
        if verdict.ok:
            return {"verdict": verdict}
        failure = {"stage": VALIDATOR, "sql": state["generation"]["sql"], "reason": verdict.reason}
        return {"verdict": verdict, "failures": [*state["failures"], failure]}

    def execute(state: AgentState) -> AgentState:
        try:
            return {"result": executor(state["verdict"].sql)}
        except QueryError as exc:
            failure = {"stage": DATABASE, "sql": state["verdict"].sql, "reason": str(exc)}
            return {"result": None, "failures": [*state["failures"], failure]}

    def answer(state: AgentState) -> AgentState:
        rows = state["result"]
        try:
            result = model.complete_json(
                system=prompts.ANSWER_SYSTEM,
                prompt=prompts.answer_prompt(state["question"], state["verdict"].sql, rows.columns, rows.rows,
                                             rows.truncated, state["generation"]["assumptions"]),
                schema=prompts.ANSWER_SCHEMA,
                label="answer",
            )
        except ModelError as exc:
            return {"status": "failed", "blocked_by": MODEL_ERROR, "reason": str(exc)}
        tally.add(result)
        return {"answer": result.data}

    def ground(state: AgentState) -> AgentState:
        rows = state["result"]
        checks = check_figures(state["answer"]["figures"], rows.columns, rows.rows)
        return {"figures": checks, "status": "answered"}

    def after_screen(state: AgentState) -> str:
        return END if state.get("status") else "retrieve"

    def after_retrieve(state: AgentState) -> str:
        return END if state.get("status") else "generate"

    def after_generate(state: AgentState) -> str:
        return END if state.get("status") else "validate"

    def repair_or_give_up(state: AgentState) -> str:
        return "generate" if len(state["failures"]) <= MAX_REPAIR_ATTEMPTS else "give_up"

    def after_validate(state: AgentState) -> str:
        return "execute" if state["verdict"].ok else repair_or_give_up(state)

    def after_execute(state: AgentState) -> str:
        return "answer" if state.get("result") is not None else repair_or_give_up(state)

    def give_up(state: AgentState) -> AgentState:
        last = state["failures"][-1]
        return {"status": "failed", "blocked_by": last["stage"], "reason": last["reason"]}

    def after_answer(state: AgentState) -> str:
        return END if state.get("status") else "ground"

    graph = StateGraph(AgentState)
    for name, fn in (("screen", screen), ("retrieve", retrieve_node), ("generate", generate),
                     ("validate", validate), ("execute", execute), ("answer", answer), ("ground", ground),
                     ("give_up", give_up)):
        graph.add_node(name, fn)
    graph.add_edge(START, "screen")
    graph.add_conditional_edges("screen", after_screen, ["retrieve", END])
    graph.add_conditional_edges("retrieve", after_retrieve, ["generate", END])
    graph.add_conditional_edges("generate", after_generate, ["validate", END])
    graph.add_conditional_edges("validate", after_validate, ["execute", "generate", "give_up"])
    graph.add_conditional_edges("execute", after_execute, ["answer", "generate", "give_up"])
    graph.add_conditional_edges("answer", after_answer, ["ground", END])
    graph.add_edge("ground", END)
    graph.add_edge("give_up", END)
    return graph.compile()


def run_agent(question: str, *, model: ModelClient, executor: Executor = run_readonly,
              tally: UsageTally | None = None) -> AgentRun:
    """Answer one question. Never raises for model, guardrail or database failures."""
    tally = tally if tally is not None else UsageTally()
    calls_before = tally.as_dict()
    state: AgentState = build_graph(model, executor, tally).invoke(
        {"question": question}, config={"recursion_limit": RECURSION_LIMIT}
    )
    retrieval = state.get("retrieval")
    generation = state.get("generation", {})
    verdict = state.get("verdict")
    result = state.get("result")
    answer = state.get("answer", {})
    after = tally.as_dict()
    return AgentRun(
        question=question,
        status=state.get("status", "failed"),
        blocked_by=state.get("blocked_by", ""),
        reason=state.get("reason", ""),
        retrieved_tables=[h.table for h in retrieval.tables] if retrieval else [],
        retrieval_reasons={h.table: h.reason for h in retrieval.tables} if retrieval else {},
        sql=generation.get("sql", ""),
        executed_sql=verdict.sql if verdict and verdict.ok else "",
        tables_used=generation.get("tables_used", []),
        assumptions=generation.get("assumptions", []),
        rationale=generation.get("rationale", ""),
        verdict=asdict(verdict) if verdict else {},
        columns=result.columns if result else [],
        rows=result.rows if result else [],
        truncated=result.truncated if result else False,
        failures=state.get("failures", []),
        answer=answer.get("answer", ""),
        caveats=answer.get("caveats", []),
        figures=state.get("figures", []),
        generations=state.get("generations", 0),
        usage={k: after[k] - calls_before[k] for k in ("calls", "live_calls", "cassette_hits")},
    )
