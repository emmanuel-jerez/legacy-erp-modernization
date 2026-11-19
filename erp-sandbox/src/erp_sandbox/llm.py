"""Model adapter shared by the demo agents.

Every model step in the portfolio goes through ``ModelClient.complete_json``: a system
prompt, a user prompt and a JSON Schema in, schema-valid JSON out. The model proposes;
the calling code validates and executes. Nothing in this module runs tools.

``ClaudeCodeClient`` reaches Claude through the locally installed, logged-in Claude Code
CLI (``claude -p``) instead of the Claude API. That keeps the demo on the owner's own
machine and login; a production build would put an Anthropic SDK client behind the same
interface. Each call strips Claude Code's own context so the model sees only what we send:

* ``--system-prompt-file`` replaces Claude Code's default system prompt.
* ``--tools ""`` removes every built-in tool; only structured output remains.
* ``--setting-sources ""`` and ``--strict-mcp-config`` skip user/project settings, hooks
  and MCP servers; ``--disable-slash-commands`` drops skills.
* The process runs in an empty working directory, so no CLAUDE.md or AGENTS.md is found.
* ``--no-session-persistence`` keeps demo calls out of the session history.

``erp-sandbox llm-smoke`` proves the isolation holds on this machine.

Cassettes make runs reproducible: each response is stored under a hash of
(model, effort, system, prompt, schema), so evals and demos can replay offline with
identical results. Modes, set by argument or ``ERP_LLM_MODE``:

* ``replay``: cassettes only; a missing cassette raises ``CassetteMiss``.
* ``record`` (default): use a cassette when present, otherwise call live and save it.
* ``live``: always call live and overwrite the cassette.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
MODES = ("replay", "record", "live")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class ModelError(RuntimeError):
    """The model call failed or returned output that does not satisfy the schema."""


class CassetteMiss(ModelError):
    """Replay mode found no recorded response for this exact request."""


@dataclass(frozen=True)
class ModelResult:
    data: dict[str, Any]
    model: str
    effort: str
    key: str
    from_cassette: bool
    duration_ms: int | None = None
    cost_usd_estimate: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)


class ModelClient(Protocol):
    def complete_json(
        self, *, system: str, prompt: str, schema: dict[str, Any], label: str = ""
    ) -> ModelResult: ...


def request_key(model: str, effort: str, system: str, prompt: str, schema: dict[str, Any]) -> str:
    """Stable hash of everything that determines the model's answer."""
    payload = json.dumps(
        {"model": model, "effort": effort, "system": system, "prompt": prompt, "schema": schema},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validator(schema: dict[str, Any]) -> Draft202012Validator:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"invalid JSON Schema: {exc.message}") from exc
    return Draft202012Validator(schema)


def _check(validator: Draft202012Validator, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ModelError(f"structured output is {type(data).__name__}, expected object")
    try:
        validator.validate(data)
    except ValidationError as exc:
        raise ModelError(f"structured output fails schema: {exc.message}") from exc
    return data


class ClaudeCodeClient:
    """Claude via the local ``claude -p`` CLI, with cassette record/replay."""

    def __init__(
        self,
        *,
        cassette_dir: str | Path,
        model: str = DEFAULT_MODEL,
        effort: str | None = None,
        mode: str | None = None,
        timeout_s: float = 600.0,
        max_attempts: int = 2,
        claude_bin: str = "claude",
    ) -> None:
        self.model = model
        self.effort = effort or os.environ.get("ERP_LLM_EFFORT", DEFAULT_EFFORT)
        self.mode = mode or os.environ.get("ERP_LLM_MODE", "record")
        if self.effort not in EFFORTS:
            raise ValueError(f"effort must be one of {EFFORTS}, got {self.effort!r}")
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        self.cassette_dir = Path(cassette_dir)
        self.timeout_s = timeout_s
        self.max_attempts = max(1, max_attempts)
        self.claude_bin = claude_bin
        self._workdir: Path | None = None

    def complete_json(
        self, *, system: str, prompt: str, schema: dict[str, Any], label: str = ""
    ) -> ModelResult:
        validator = _validator(schema)
        key = request_key(self.model, self.effort, system, prompt, schema)
        path = self.cassette_dir / f"{key[:32]}.json"

        if self.mode in ("replay", "record") and path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                return ModelResult(
                    data=_check(validator, saved["response"]),
                    model=saved["model"],
                    effort=saved["effort"],
                    key=key,
                    from_cassette=True,
                    duration_ms=saved.get("duration_ms"),
                    cost_usd_estimate=saved.get("cost_usd_estimate"),
                    usage=saved.get("usage", {}),
                )
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ModelError(f"unreadable cassette {path}: {exc}") from exc
        if self.mode == "replay":
            raise CassetteMiss(f"no cassette for {label or 'request'} ({key[:12]}) in {self.cassette_dir}")

        result = self._call_live(system=system, prompt=prompt, schema=schema, validator=validator, key=key)
        self._save(path, label=label, system=system, prompt=prompt, schema=schema, result=result)
        return result

    def _command(self, system_file: Path, schema: dict[str, Any]) -> list[str]:
        return [
            self.claude_bin,
            "-p",
            "--model", self.model,
            "--effort", self.effort,
            "--system-prompt-file", str(system_file),
            "--tools", "",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--output-format", "json",
            "--json-schema", json.dumps(schema, separators=(",", ":")),
        ]

    def _empty_workdir(self) -> Path:
        if self._workdir is None or not self._workdir.exists():
            self._workdir = Path(tempfile.mkdtemp(prefix="erp-llm-"))
        return self._workdir

    def _call_live(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        validator: Draft202012Validator,
        key: str,
    ) -> ModelResult:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._call_once(system=system, prompt=prompt, schema=schema, validator=validator, key=key)
            except ModelError as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    time.sleep(5 * attempt)
        assert last_error is not None
        raise last_error

    def _call_once(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        validator: Draft202012Validator,
        key: str,
    ) -> ModelResult:
        started = time.monotonic()
        # The system prompt goes through a file and the user prompt through stdin: large
        # prompts would exceed the OS limit on a single command-line argument (E2BIG).
        # The file lives outside the empty working directory the CLI runs in.
        try:
            prompt_dir = tempfile.TemporaryDirectory(prefix="erp-llm-prompt-")
            system_file = Path(prompt_dir.name) / "system.txt"
            system_file.write_text(system, encoding="utf-8")
        except OSError as exc:
            raise ModelError(f"could not write the system prompt file: {exc}") from exc
        with prompt_dir:
            return self._run(system_file, prompt, schema, validator, key, started)

    def _run(
        self,
        system_file: Path,
        prompt: str,
        schema: dict[str, Any],
        validator: Draft202012Validator,
        key: str,
        started: float,
    ) -> ModelResult:
        try:
            proc = subprocess.run(
                self._command(system_file, schema),
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self._empty_workdir(),
                timeout=self.timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ModelError(f"Claude Code CLI not found: {self.claude_bin}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ModelError(f"claude -p timed out after {self.timeout_s:.0f}s") from exc
        except OSError as exc:
            raise ModelError(f"could not run claude -p: {exc}") from exc

        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            detail = (proc.stderr or proc.stdout).strip()[:500]
            raise ModelError(f"claude -p exited {proc.returncode} without JSON: {detail}") from exc
        if not isinstance(out, dict):
            raise ModelError(f"claude -p printed JSON {type(out).__name__}, expected an object")

        if proc.returncode != 0 or out.get("is_error") or out.get("subtype") != "success":
            raise ModelError(f"claude -p failed ({out.get('subtype')}): {str(out.get('result'))[:500]}")
        if "structured_output" not in out:
            raise ModelError(f"no structured output (likely a refusal): {str(out.get('result'))[:500]}")

        usage = {k: v for k, v in (out.get("usage") or {}).items() if "tokens" in k}
        return ModelResult(
            data=_check(validator, out["structured_output"]),
            model=self.model,
            effort=self.effort,
            key=key,
            from_cassette=False,
            duration_ms=out.get("duration_ms") or int((time.monotonic() - started) * 1000),
            cost_usd_estimate=out.get("total_cost_usd"),
            usage=usage,
        )

    def _save(
        self,
        path: Path,
        *,
        label: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        result: ModelResult,
    ) -> None:
        record = {
            "label": label,
            "model": result.model,
            "effort": result.effort,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "duration_ms": result.duration_ms,
            "cost_usd_estimate": result.cost_usd_estimate,
            "usage": result.usage,
            "request": {"system": system, "prompt": prompt, "schema": schema},
            "response": result.data,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            raise ModelError(f"model call succeeded but the cassette could not be saved to {path}: {exc}") from exc


class ScriptedModelClient:
    """Deterministic stand-in for unit tests: returns queued responses in order."""

    def __init__(self, responses: list[dict[str, Any]], *, model: str = "scripted", effort: str = "n/a") -> None:
        self._responses = list(responses)
        self.model = model
        self.effort = effort
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self, *, system: str, prompt: str, schema: dict[str, Any], label: str = ""
    ) -> ModelResult:
        self.calls.append({"system": system, "prompt": prompt, "schema": schema, "label": label})
        if not self._responses:
            raise ModelError(f"scripted client has no response left for {label or 'request'}")
        data = _check(_validator(schema), self._responses.pop(0))
        return ModelResult(
            data=data,
            model=self.model,
            effort=self.effort,
            key=request_key(self.model, self.effort, system, prompt, schema),
            from_cassette=False,
        )


@dataclass
class UsageTally:
    """Aggregates model calls for an eval or demo report."""

    calls: int = 0
    live_calls: int = 0
    cassette_hits: int = 0
    duration_ms: int = 0
    cost_usd_estimate: float = 0.0
    models: set[str] = field(default_factory=set)
    efforts: set[str] = field(default_factory=set)

    def add(self, result: ModelResult) -> None:
        self.calls += 1
        self.models.add(result.model)
        self.efforts.add(result.effort)
        if result.from_cassette:
            self.cassette_hits += 1
        else:
            self.live_calls += 1
        self.duration_ms += result.duration_ms or 0
        self.cost_usd_estimate += result.cost_usd_estimate or 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "live_calls": self.live_calls,
            "cassette_hits": self.cassette_hits,
            "model_duration_ms": self.duration_ms,
            "cost_usd_estimate_as_recorded": round(self.cost_usd_estimate, 4),
            "models": sorted(self.models),
            "efforts": sorted(self.efforts),
        }
