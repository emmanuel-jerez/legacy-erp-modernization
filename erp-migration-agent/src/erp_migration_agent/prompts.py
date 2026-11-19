"""System prompts, user prompts and JSON Schemas for each model step.

Prompts must be byte-identical across runs for cassette replay, so they are built only
from deterministic inputs (legacy source, snapshot, spec, cases sorted by id) and never
include run ids, timestamps or paths. Held-out cases never reach any prompt here.
"""

from __future__ import annotations

import json
from typing import Any

from . import config, pgtypes
from .extract import LegacySource

SYSTEM = """You modernize legacy ERP business rules. You are given a PL/pgSQL function from \
iDempiere (a 20-year-old open-source ERP) and you help port it to Python that behaves \
identically, bug for bug. The legacy function is the oracle: when the source and your intuition \
disagree, the source wins, including its quirks. Reply only with JSON matching the schema; a \
harness validates and executes what you propose."""

_STR_OR_NULL = {"type": ["string", "null"]}

ANALYZE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary", "inputs", "output", "branches", "table_dependencies", "null_semantics",
        "rounding_semantics", "date_time_semantics", "edge_cases", "porting_risks",
    ],
    "properties": {
        "summary": {"type": "string"},
        "inputs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "pg_type", "meaning", "null_behavior"],
                "properties": {k: {"type": "string"} for k in ("name", "pg_type", "meaning", "null_behavior")},
            },
        },
        "output": {
            "type": "object",
            "additionalProperties": False,
            "required": ["pg_type", "meaning", "null_behavior"],
            "properties": {k: {"type": "string"} for k in ("pg_type", "meaning", "null_behavior")},
        },
        "branches": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["condition", "behavior"],
                "properties": {"condition": {"type": "string"}, "behavior": {"type": "string"}},
            },
        },
        "table_dependencies": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["table", "columns", "purpose"],
                "properties": {
                    "table": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "purpose": {"type": "string"},
                },
            },
        },
        "null_semantics": {"type": "string"},
        "rounding_semantics": {"type": "string"},
        "date_time_semantics": {"type": "string"},
        "edge_cases": {"type": "array", "items": {"type": "string"}},
        "porting_risks": {"type": "array", "items": {"type": "string"}},
    },
}

EDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cases"],
    "properties": {
        "cases": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "rationale", "args", "fixtures"],
                "properties": {
                    "name": {"type": "string"},
                    "rationale": {"type": "string"},
                    "args": {"type": "array", "items": _STR_OR_NULL},
                    "fixtures": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["table", "id", "clone_from", "set"],
                            "properties": {
                                "table": {"type": "string"},
                                "id": {"type": "integer"},
                                "clone_from": {"type": "integer"},
                                "set": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": ["column", "value"],
                                        "properties": {"column": {"type": "string"}, "value": _STR_OR_NULL},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
    },
}

PORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["module_source", "notes"],
    "properties": {"module_source": {"type": "string"}, "notes": {"type": "string"}},
}

REPAIR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["diagnosis", "module_source"],
    "properties": {"diagnosis": {"type": "string"}, "module_source": {"type": "string"}},
}

_PY_TYPES = {
    pgtypes.NUMERIC: "decimal.Decimal",
    pgtypes.INTEGER: "int",
    pgtypes.BIGINT: "int",
    pgtypes.TIMESTAMPTZ: "timezone-aware datetime.datetime (tzinfo is data.session_tz)",
    pgtypes.TIMESTAMP: "naive datetime.datetime",
    pgtypes.DATE: "datetime.date",
    pgtypes.VARCHAR: "str",
    pgtypes.TEXT: "str",
    pgtypes.CHAR: "str",
    pgtypes.BOOLEAN: "bool",
}
SMALL_TABLE_ROWS = 12


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def source_bundle(source: LegacySource, snapshot: dict[str, Any]) -> str:
    fn = source.function
    parts = [f"## Legacy function adempiere.{fn.name}({fn.identity_args}) RETURNS {fn.return_type} [{fn.volatility}]",
             "```sql", fn.definition.strip(), "```"]
    if source.helpers:
        parts.append("## Schema functions it calls (directly, transitively, or through operators)")
        for h in source.helpers:
            parts += ["```sql", h.definition.strip(), "```"]
    if source.operators:
        parts.append("## Operators defined in the adempiere schema")
        parts += [f"- {o}" for o in source.operators]
    parts.append("## Tables it reads (columns the source names; primary key first)")
    for table, cols in source.tables.items():
        rows = snapshot["tables"][table]["rows"]
        parts.append(f"- {table} ({len(rows)} rows): " + ", ".join(f"{c} {t}" for c, t in cols.items()))
        if len(rows) <= SMALL_TABLE_ROWS:
            names = list(cols)
            for row in rows:
                parts.append("    " + _json(dict(zip(names, row))))
    parts.append(
        f"## Session\nThe oracle runs with TimeZone = '{snapshot['session_tz']}' and "
        f"search_path = adempiere, pg_catalog. Unqualified function calls resolve against adempiere first."
    )
    return "\n".join(parts)


def contract(source: LegacySource) -> str:
    fn = source.function
    params = ", ".join(fn.arg_names)
    arg_lines = "\n".join(
        f"  - {n}: {t} -> {_PY_TYPES[pgtypes.normalize_type(t)]} or None"
        for n, t in zip(fn.arg_names, fn.arg_types)
    )
    ret = pgtypes.normalize_type(fn.return_type)
    tables = "\n".join(f"  - {t}: {', '.join(c)}" for t, c in source.tables.items())
    return f"""## Port contract
Write one Python module that defines, at top level:

    def {fn.name}({params}, *, data):

- Arguments arrive positionally in the legacy order, already converted the way psycopg converts them:
{arg_lines}
  SQL NULL arrives as None.
- Read tables only through `data.select(table, **equals) -> list[dict]`. Table and column names are lower case.
  Rows come in primary-key order with typed values (numeric -> Decimal, timestamp -> naive datetime,
  character -> str). Equality is SQL equality: a None filter value matches nothing, like `col = NULL`.
  Filter anything else in Python. Available tables and columns:
{tables}
- `data.session_tz` is the PostgreSQL session TimeZone as a tzinfo. Legacy date logic (casts to date,
  TRUNC, interval and day arithmetic) happens in that zone. Python adds timedelta to an aware datetime
  in wall-clock time; PostgreSQL's `+ interval '1 second' * n` adds elapsed time. They differ across
  daylight-saving changes.
- Return {_PY_TYPES[ret]}, or None exactly where the legacy function returns NULL.
- Parity is exact (docs/metric-contract.md): numeric results must equal PostgreSQL's text form, value
  and scale (2.20 is not 2.2), and a negative zero never matches; timestamptz results must be aware
  and the same instant; if the legacy function raises an error, raise an exception.
- PostgreSQL numeric arithmetic is exact and ROUND(numeric, n) rounds half away from zero; Python's
  default decimal context has 28 digits and banker's rounding.
- Allowed imports: decimal, datetime, math, dataclasses, typing, enum, calendar. No other imports,
  no I/O, no exec/eval/getattr/setattr, no dunder attribute access, no global statements.
- Pure and deterministic: never read the current time or use randomness."""


def _render_case(case: dict[str, Any], arg_names: list[str], include_expected: bool) -> str:
    item: dict[str, Any] = {"id": case["id"], "args": dict(zip(arg_names, case["args"]))}
    if case.get("fixtures"):
        item["fixture_rows"] = case["fixtures"]
    if include_expected:
        item["expected"] = case["golden"].get("value") if "value" in case["golden"] else {"raises": case["golden"]["error"]}
    return _json(item)


def visible_only(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((c for c in cases if c["split"] == "visible"), key=lambda c: c["id"])


def analyze_prompt(source: LegacySource, snapshot: dict[str, Any]) -> str:
    return f"""{source_bundle(source, snapshot)}

## Task
Analyze adempiere.{source.function.name} and return a structured spec for a faithful Python port:
its inputs and output, every branch, which tables and columns it reads, and its NULL, rounding and
date/time semantics. List concrete edge cases worth testing and the risks a porter is likely to miss
(PostgreSQL versus Python semantics, quirks a modern rewrite would "fix" by accident)."""


def edge_prompt(source: LegacySource, snapshot: dict[str, Any], spec: dict[str, Any]) -> str:
    fn = source.function
    arg_formats = "\n".join(f"  - {n} ({t})" for n, t in zip(fn.arg_names, fn.arg_types))
    fixture_tables = ", ".join(t for t, pk in source.primary_keys.items() if pk == f"{t}_id")
    return f"""{source_bundle(source, snapshot)}

## Spec from the analysis step
{_json(spec)}

## Task
Propose at most {config.MAX_EDGE_CASES} edge cases that would expose a wrong port: boundaries, NULLs,
unusual dates (month ends, leap days, daylight-saving changes in the session time zone), rounding
ties, and configurations the seed data lacks. The seed data is already sampled separately, so focus
on what it does not cover.

Each case gives `args` positionally, as strings in PostgreSQL input syntax or null:
{arg_formats}
Timestamps with time zone may include an offset (e.g. 2026-03-08T01:30:00-08:00); without one they are
read in the session time zone.

A case may add up to {config.MAX_FIXTURES_PER_CASE} `fixtures`: synthetic rows inserted for that case only
(in a transaction that is rolled back). A fixture copies the row whose primary key is `clone_from`,
takes the new primary key `id` (must be between {config.FIXTURE_ID_MIN} and {config.FIXTURE_ID_MAX}),
and overrides the columns in `set` (values as strings in PostgreSQL input syntax, or null). Fixtures
are allowed in: {fixture_tables}. The harness assigns unique name/value/uuid columns itself. Reference a
fixture from `args` by its id."""


def port_prompt(source: LegacySource, snapshot: dict[str, Any], spec: dict[str, Any], cases: list[dict[str, Any]]) -> str:
    names = source.function.arg_names
    lines = "\n".join(_render_case(c, names, True) for c in visible_only(cases))
    return f"""{source_bundle(source, snapshot)}

## Spec from the analysis step
{_json(spec)}

{contract(source)}

## Characterization cases (golden outputs recorded from the legacy function)
Expected values use the encoded form: numeric as exact text, timestamps as ISO 8601. Fixture rows were
present in the table for that case only; `data.select` returns them alongside the snapshot rows.
{lines}

## Task
Write the module. Put any assumption you could not confirm from the source in `notes`."""


def repair_prompt(
    source: LegacySource,
    snapshot: dict[str, Any],
    spec: dict[str, Any],
    module_source: str,
    visible_report: dict[str, Any],
    cases: list[dict[str, Any]],
    max_listed: int = 12,
) -> str:
    names = source.function.arg_names
    by_id = {c["id"]: c for c in cases}
    failure = visible_report.get("runner_failure")
    mismatches = sorted(visible_report["mismatches"], key=lambda m: m["case_id"])
    listed = []
    for m in mismatches[:max_listed]:
        case = by_id[m["case_id"]]
        assert case["split"] == "visible", "held-out cases must never reach a prompt"
        actual = m["actual"]
        got = actual.get("value") if actual.get("ok") else {"raised": actual.get("error")}
        listed.append(_json({**json.loads(_render_case(case, names, True)), "port_returned": got,
                             "port_type": actual.get("py_type"), "why": m["reason"]}))
    problem = (
        f"The harness could not run the module: {failure}"
        if failure
        else f"{visible_report['matched']} of {visible_report['total']} visible cases match. "
        f"{len(mismatches)} mismatch; up to {max_listed} are listed."
    )
    return f"""{source_bundle(source, snapshot)}

## Spec from the analysis step
{_json(spec)}

{contract(source)}

## Current module
```python
{module_source}
```

## Problem
{problem}
{chr(10).join(listed)}

## Task
Diagnose the root cause from the legacy source (not by special-casing these inputs) and return the
complete corrected module."""
