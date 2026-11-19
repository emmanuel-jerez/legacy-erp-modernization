"""Every hard limit the agent enforces, in one place.

Prompts, MCP tool descriptions, the database role and the README quote these numbers
by interpolating them, never by retyping them.
"""

from __future__ import annotations

# Rows: the validator injects or caps LIMIT at this value, and the executor never
# fetches more.
MAX_ROWS = 200
# Database statement timeout, applied by the role default and again per transaction.
STATEMENT_TIMEOUT_MS = 5_000
# Further database session limits for the read-only role.
LOCK_TIMEOUT_MS = 1_000
IDLE_IN_TRANSACTION_TIMEOUT_MS = 10_000
ROLE_CONNECTION_LIMIT = 20
CONNECT_TIMEOUT_S = 5
# Longest SQL text the validator will parse.
MAX_SQL_CHARS = 8_000
