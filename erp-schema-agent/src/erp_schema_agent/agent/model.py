"""The model client the CLI, demo, evals and MCP server share."""

from __future__ import annotations

from pathlib import Path

from erp_sandbox.llm import ClaudeCodeClient

from .. import settings


def make_model(mode: str | None = None, cassette_dir: Path | None = None) -> ClaudeCodeClient:
    """Claude through the local Claude Code CLI, with cassettes under ``cassettes/``.

    ``mode`` is ``replay``, ``record`` or ``live``; ``None`` uses ``ERP_LLM_MODE`` (default
    ``record``). Model and effort stay at the erp-sandbox defaults.
    """
    return ClaudeCodeClient(cassette_dir=cassette_dir or settings.CASSETTE_DIR, mode=mode)
