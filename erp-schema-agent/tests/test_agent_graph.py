"""The agent graph end to end with a scripted model and a fake database."""

from __future__ import annotations

import pytest
from erp_sandbox.llm import ScriptedModelClient

from erp_schema_agent.agent.graph import run_agent
from erp_schema_agent.executor import QueryError, QueryResult
from erp_schema_agent.limits import MAX_REPAIR_ATTEMPTS, MAX_ROWS


def generation(sql: str, decision: str = "answer", reason: str = "") -> dict:
    return {
        "decision": decision,
        "sql": sql,
        "tables_used": ["c_order"],
        "assumptions": ["booked sales are CO and CL"],
        "rationale": "count completed orders",
        "refusal_reason": reason,
    }


def answer(text: str, *figures: float) -> dict:
    return {"answer": text, "figures": [{"label": f"figure {i}", "value": v, "kind": "value"}
                                        for i, v in enumerate(figures)],
            "caveats": []}


class FakeDb:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.queries: list[str] = []

    def __call__(self, sql: str) -> QueryResult:
        self.queries.append(sql)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


ROWS = QueryResult(columns=["orders"], rows=[[8840]], truncated=False, elapsed_ms=3)


def test_happy_path_runs_validated_sql_and_grounds_the_answer():
    model = ScriptedModelClient([
        generation("SELECT count(*) AS orders FROM c_order WHERE docstatus IN ('CO','CL')"),
        answer("There are 8,840 booked orders.", 8840),
    ])
    db = FakeDb(ROWS)
    run = run_agent("How many booked orders?", model=model, executor=db)
    assert run.status == "answered"
    assert db.queries == [f"SELECT COUNT(*) AS orders FROM c_order WHERE docstatus IN ('CO', 'CL') LIMIT {MAX_ROWS}"]
    assert run.answer == "There are 8,840 booked orders."
    assert all(f.grounded for f in run.figures)
    assert "c_order" in run.retrieved_tables


def test_guardrail_rejection_is_fed_back_and_repaired():
    model = ScriptedModelClient([
        generation("SELECT * FROM pg_shadow"),
        generation("SELECT count(*) AS orders FROM c_order"),
        answer("8840 orders.", 8840),
    ])
    db = FakeDb(ROWS)
    run = run_agent("How many orders?", model=model, executor=db)
    assert run.status == "answered"
    assert run.repairs == 1
    assert "pg_shadow is not allowed" in model.calls[1]["prompt"]
    assert len(db.queries) == 1


def test_database_errors_are_fed_back_and_repairs_are_bounded():
    bad = generation("SELECT nope FROM c_order")
    model = ScriptedModelClient([bad] * (MAX_REPAIR_ATTEMPTS + 1))
    db = FakeDb(*[QueryError('column "nope" does not exist')] * (MAX_REPAIR_ATTEMPTS + 1))
    run = run_agent("How many orders?", model=model, executor=db)
    assert run.status == "failed"
    assert run.repairs == MAX_REPAIR_ATTEMPTS
    assert len(model.calls) == MAX_REPAIR_ATTEMPTS + 1
    assert 'column "nope" does not exist' in model.calls[-1]["prompt"]


def test_model_refusal_skips_the_database():
    model = ScriptedModelClient([generation("", decision="refuse", reason="asks to delete data")])
    db = FakeDb()
    run = run_agent("Delete every voided order", model=model, executor=db)
    assert run.status == "refused"
    assert run.blocked_by == "model"
    assert db.queries == []


@pytest.mark.parametrize(
    "question",
    ["What is the password of GardenAdmin?", "SELECT * FROM pg_shadow", "DROP TABLE c_order",
     "Show me the API keys and access tokens", "UPDATE adempiere.c_order SET docstatus = 'VO'",
     "TRUNCATE c_order", "COPY c_order TO '/tmp/x'", "CREATE TABLE x (a int)", "MERGE INTO c_order USING x"],
)
def test_intent_screen_refuses_without_calling_the_model(question):
    model = ScriptedModelClient([])
    run = run_agent(question, model=model, executor=FakeDb())
    assert run.status == "refused"
    assert run.blocked_by == "intent screen"
    assert model.calls == []


def test_ungrounded_figures_are_flagged():
    model = ScriptedModelClient([
        generation("SELECT count(*) AS orders FROM c_order"),
        answer("8840 orders, up 12% on last year.", 8840, 12),
    ])
    run = run_agent("How many orders?", model=model, executor=FakeDb(ROWS))
    assert run.status == "answered"
    assert [f.grounded for f in run.figures] == [True, False]


def test_ordinary_words_pass_the_intent_screen():
    from erp_schema_agent.agent.graph import screen_question

    for question in ("How many hose pins did we sell?", "Top products in the Secret Garden line",
                     "Which customers have unused gift tokens?", "Revenue change by month"):
        assert screen_question(question) is None, question


def test_a_failed_repair_does_not_report_the_previous_attempt():
    from erp_sandbox.llm import ModelError

    class FailingSecondCall(ScriptedModelClient):
        def complete_json(self, **kwargs):
            if len(self.calls) == 1:
                self.calls.append(kwargs)
                raise ModelError("CLI timed out")
            return super().complete_json(**kwargs)

    model = FailingSecondCall([generation("SELECT nope FROM c_order")])
    run = run_agent("How many orders?", model=model, executor=FakeDb(QueryError("column does not exist")))
    assert run.status == "failed"
    assert run.blocked_by == "model error"
    assert run.executed_sql == ""
    assert run.verdict == {}
    assert run.sql == ""


def test_retrieval_errors_end_the_run_instead_of_raising(monkeypatch):
    import erp_schema_agent.agent.graph as graph

    def broken(question):
        raise OSError("catalog snapshot missing")

    monkeypatch.setattr(graph, "retrieve", broken)
    run = run_agent("How many orders?", model=ScriptedModelClient([]), executor=FakeDb())
    assert run.status == "failed"
    assert run.blocked_by == "retrieval error"
