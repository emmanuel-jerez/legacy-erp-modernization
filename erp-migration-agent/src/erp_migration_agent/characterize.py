"""Build characterization cases, record golden outputs, and split visible from held-out.

The rules are in docs/metric-contract.md ("Cases"). Everything here is deterministic given
the database and the model's (cassette-recorded) edge-case proposals.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import config, pgtypes
from .extract import LegacySource
from .oracle import CaseRejected, Oracle
from .targets import Target


def case_id(source: str, args: list[Any], fixture_spec: list[dict[str, Any]]) -> str:
    body = json.dumps({"args": args, "fixtures": fixture_spec}, sort_keys=True, separators=(",", ":"))
    prefix = "s" if source == "sampled" else "e"
    return f"{prefix}-{hashlib.sha256(body.encode()).hexdigest()[:12]}"


def _content_key(args: list[Any], fixtures: list[dict[str, Any]]) -> str:
    """Identity of a case as the oracle saw it: arguments plus the fixture rows actually inserted."""
    return json.dumps({"args": args, "fixtures": fixtures}, sort_keys=True)


def _case(source: str, name: str, rationale: str, args: list[Any], spec: list[dict[str, Any]],
          fixtures: list[dict[str, Any]], golden: dict[str, Any]) -> dict[str, Any]:
    return {"id": case_id(source, args, spec), "source": source, "name": name, "rationale": rationale,
            "args": args, "fixture_spec": spec, "fixtures": fixtures, "golden": golden}


def sample_cases(
    oracle: Oracle, source: LegacySource, target: Target
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Record golden outputs for the target's sampled rows. Returns ``(cases, rejected)``."""
    rows = oracle.query_readonly(target.sampling_sql, {"limit": config.MAX_SAMPLED_CASES})
    cases: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, row in enumerate(rows):
        name = f"sampled row {i + 1}"
        raw = [v if v is None else str(pgtypes.encode(v)) for v in row]
        try:
            args, golden, _ = oracle.record(source, raw)
        except CaseRejected as exc:
            rejected.append({"name": name, "reason": str(exc)})
            continue
        key = _content_key(args, [])
        if key in seen:
            continue
        seen.add(key)
        cases.append(_case("sampled", name, "real seeded data", args, [], [], golden))
    return cases, rejected


def edge_cases(
    oracle: Oracle, source: LegacySource, proposals: list[dict[str, Any]], existing: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate and record model-proposed edge cases. Returns ``(cases, rejected)``."""
    seen = {_content_key(c["args"], c["fixtures"]) for c in existing}
    cases: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for proposal in proposals[: config.MAX_EDGE_CASES]:
        name = proposal.get("name", "")
        spec = [
            {
                "table": f.get("table"),
                "id": f.get("id"),
                "clone_from": f.get("clone_from"),
                "set": {p["column"]: p["value"] for p in f.get("set", [])},
            }
            for f in proposal.get("fixtures", [])
        ]
        try:
            args, golden, fixtures = oracle.record(source, list(proposal.get("args", [])), spec)
        except CaseRejected as exc:
            rejected.append({"name": name, "reason": str(exc)})
            continue
        key = _content_key(args, fixtures)
        if key in seen:
            rejected.append({"name": name, "reason": "duplicate of an existing case"})
            continue
        seen.add(key)
        cases.append(_case("edge", name, proposal.get("rationale", ""), args, spec, fixtures, golden))
    return cases, rejected


def split_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark each case ``visible`` or ``held_out``: every third per source in sha256(id) order."""
    out: list[dict[str, Any]] = []
    for source in ("sampled", "edge"):
        group = sorted(
            (c for c in cases if c["source"] == source),
            key=lambda c: hashlib.sha256(c["id"].encode()).hexdigest(),
        )
        for i, case in enumerate(group):
            held = i % config.HELD_OUT_EVERY == config.HELD_OUT_EVERY - 1
            out.append({**case, "split": "held_out" if held else "visible"})
    return out
