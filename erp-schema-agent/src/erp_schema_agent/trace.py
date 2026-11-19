"""Readable trace of one agent run, for the CLI and the demo."""

from __future__ import annotations

import textwrap
from typing import Any

from .agent.graph import MODEL, SCREEN, AgentRun

PREVIEW_ROWS = 8


def _cell(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.0f}" if value.is_integer() and abs(value) >= 1 else f"{value:,.2f}"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return "" if value is None else str(value)


def table(columns: list[str], rows: list[list[Any]], limit: int = PREVIEW_ROWS) -> str:
    shown = [[_cell(v) for v in row] for row in rows[:limit]]
    widths = [max([len(c)] + [len(r[i]) for r in shown]) for i, c in enumerate(columns)]
    lines = ["  " + " | ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True))]
    lines.append("  " + "-+-".join("-" * w for w in widths))
    lines += ["  " + " | ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)) for r in shown]
    if len(rows) > limit:
        lines.append(f"  ... {len(rows) - limit} more rows")
    return "\n".join(lines)


def format_run(run: AgentRun) -> str:
    out = [f"Question: {run.question}", ""]
    if run.blocked_by == SCREEN:
        out.append(f"BLOCKED by the intent screen: {run.reason}. The model was not called.")
        return "\n".join(out)
    if run.retrieved_tables:
        out.append("Retrieved tables:")
        out += [f"  - {t} ({run.retrieval_reasons.get(t, '')})" for t in run.retrieved_tables]
        out.append("")
    for i, failure in enumerate(run.failures, start=1):
        out.append(f"Attempt {i} rejected by the {failure['stage']}: {failure['reason']}")
        out.append(textwrap.indent(failure["sql"] or "(empty)", "    "))
    if run.failures:
        out.append("")
    if run.blocked_by == MODEL:
        out.append(f"REFUSED by the model: {run.reason}")
        return "\n".join(out)
    if run.status == "failed":
        out.append(f"FAILED ({run.blocked_by}): {run.reason}")
        return "\n".join(out)
    out.append("SQL (validated, as executed):")
    out.append(textwrap.indent(run.executed_sql, "    "))
    limit = run.verdict.get("limit_applied", "")
    out.append(f"Guardrails: passed; tables {', '.join(run.verdict.get('tables', []))}; LIMIT {limit}")
    if run.assumptions:
        out.append("Assumptions: " + "; ".join(run.assumptions))
    out.append("")
    out.append(f"Rows: {len(run.rows)}{' (row cap reached)' if run.truncated else ''}")
    if run.columns:
        out.append(table(run.columns, run.rows))
    out.append("")
    out.append("Answer:")
    out.append(textwrap.indent(textwrap.fill(run.answer, 88), "  "))
    if run.caveats:
        out.append("Caveats: " + "; ".join(run.caveats))
    if run.figures:
        out.append("Grounding:")
        for fig in run.figures:
            mark = "ok" if fig.grounded else "UNGROUNDED"
            out.append(f"  [{mark}] {fig.label} = {_cell(fig.value)} ({fig.kind}: {fig.evidence})")
    return "\n".join(out)
