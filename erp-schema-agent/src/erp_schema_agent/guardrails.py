"""SQL layer of the guardrails: deterministic validation of model-written SQL.

``validate_sql`` parses with sqlglot's PostgreSQL dialect and accepts only:

* exactly one statement, rooted in a SELECT (or a UNION/INTERSECT/EXCEPT of SELECTs),
  with optional CTEs, and no data-modifying or DDL node anywhere in the tree;
* no ``SELECT ... INTO`` and no locking clause (``FOR UPDATE`` / ``FOR SHARE``);
* tables from the ``adempiere`` allow-list only (no ``pg_*``, ``information_schema``
  or other schemas). A name counts as a CTE only where PostgreSQL would resolve it to
  one: inside the WITH's own query, or in a later CTE of the same WITH;
* functions sqlglot models as standard SQL (minus the ones that reveal the server or
  session) plus an allow-list of side-effect-free PostgreSQL functions; anything else
  (``pg_sleep``, ``set_config``, ``nextval``, ``dblink``, ``query_to_xml`` ...) is rejected;
* no cast to a ``reg*`` type (a catalog lookup) and no session keyword such as ``user``;
* no identifier that names a credential column, no whole-row reference to a table
  (``SELECT u FROM ad_user u``), and no ``SELECT *`` over a table that has credentials.

It then injects ``LIMIT MAX_ROWS`` or caps a larger limit and returns the re-rendered
SQL, which is what gets executed. A rejection carries a reason written for the model,
which sees it on the repair attempt. The database role enforces the data boundaries
independently (see ``roles.py``): no write privilege and no grant on credential columns.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from . import limits, settings
from .policy import is_sensitive_column

# PostgreSQL functions sqlglot does not model as typed expressions but that are safe
# (no side effects, no file, network, session or sequence access).
SAFE_POSTGRES_FUNCTIONS = frozenset(
    {
        "age", "date_part", "make_date", "make_interval", "make_timestamp", "to_char", "to_number",
        "to_date", "to_timestamp", "justify_days", "initcap", "btrim", "format", "sign",
        "width_bucket", "percent_rank", "cume_dist", "dense_rank", "rank", "ntile", "lead",
        "first_value", "last_value", "nth_value", "mode", "percentile_disc", "string_agg",
        "bool_and", "bool_or", "every", "regexp_match", "translate", "div", "mod",
        "power", "sqrt", "exp", "ln", "log", "date_bin", "isfinite", "array_length", "array_to_string",
    }
)
# Typed sqlglot functions that leak server or session details rather than ERP data.
SESSION_FUNCTION_KEYS = frozenset(
    {
        "currentuser", "currentrole", "currentschema", "currentschemas", "currentdatabase",
        "currentcatalog", "currentversion", "sessionuser", "user",
    }
)
# Bare keywords sqlglot parses as columns but PostgreSQL evaluates as session values.
SESSION_KEYWORDS = frozenset(
    {"user", "current_user", "current_role", "session_user", "current_catalog", "current_schema"}
)
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
    exp.TruncateTable, exp.Command, exp.Set, exp.Transaction, exp.Commit, exp.Rollback,
    exp.Copy, exp.Grant, exp.Use, exp.Pragma, exp.LoadData,
)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""
    sql: str = ""
    tables: list[str] = field(default_factory=list)
    limit_applied: str = ""  # "injected", "capped", "kept" or ""


def _reject(reason: str) -> Verdict:
    return Verdict(ok=False, reason=reason)


def _unmodelled_function_name(node: exp.Func) -> str | None:
    """Lower-case name of a function sqlglot does not model (``exp.Anonymous``), else None."""
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return None


def _limit_value(node: exp.Expression | None) -> int | None:
    if not isinstance(node, exp.Limit):
        return None  # absent, or FETCH FIRST, which gets replaced
    expr = node.expression
    if isinstance(expr, exp.Literal) and expr.is_int:
        return int(expr.this)
    return None


def _is_cte_reference(table: exp.Table) -> bool:
    """True when PostgreSQL would resolve this unqualified name to a CTE in scope."""
    name = table.name.lower()
    child: exp.Expression = table
    node = table.parent
    while node is not None:
        if isinstance(node, exp.With):
            # Inside the k-th CTE body only CTEs 0..k-1 are visible (non-recursive WITH);
            # a self-reference resolves to the real table, so it is checked as one.
            ctes = node.expressions
            k = next((i for i, cte in enumerate(ctes) if cte is child), len(ctes))
            if any(cte.alias_or_name.lower() == name for cte in ctes[:k]):
                return True
        elif isinstance(node, exp.Query) and child is not node.args.get("with_"):
            with_ = node.args.get("with_")
            if with_ is not None and any(cte.alias_or_name.lower() == name for cte in with_.expressions):
                return True
        child, node = node, node.parent
    return False


def validate_sql(
    sql: str,
    *,
    allowed_tables: Collection[str],
    sensitive_tables: Collection[str],
    max_rows: int = limits.MAX_ROWS,
) -> Verdict:
    """Check one model-written query. Returns the SQL to execute, or a reason to retry."""
    max_rows = max(1, min(max_rows, limits.MAX_ROWS))
    text = (sql or "").strip()
    if not text:
        return _reject("The SQL is empty. Write one SELECT statement.")
    if len(text) > limits.MAX_SQL_CHARS:
        return _reject(f"The SQL is longer than {limits.MAX_SQL_CHARS} characters. Simplify the query.")
    try:
        statements = [s for s in sqlglot.parse(text, read="postgres") if s is not None]
    except SqlglotError as exc:
        first = str(exc).splitlines()[0][:300]
        return _reject(f"The SQL could not parse as PostgreSQL: {first}")
    if len(statements) != 1:
        return _reject(f"Write exactly one statement; found {len(statements)}.")
    root = statements[0]

    if not isinstance(root, (exp.Select, exp.SetOperation)):
        return _reject(
            f"Write only a single SELECT (optionally WITH ... SELECT); got {root.key.upper()}. "
            "This agent is read-only and never modifies data."
        )
    for node in root.walk():
        if isinstance(node, exp.Into):
            return _reject("SELECT INTO creates a table; it is not allowed. Return rows with a plain SELECT.")
        if isinstance(node, exp.Lock):
            return _reject("Row-locking clauses (FOR UPDATE/SHARE) are not allowed in read-only queries.")
        if isinstance(node, FORBIDDEN_NODES):
            return _reject(
                f"Write only a single SELECT; the query contains a {node.key.upper()} clause. "
                "This agent is read-only and never modifies data."
            )

    allowed = {t.lower() for t in allowed_tables}
    tables: set[str] = set()
    aliases: dict[str, str] = {}
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            continue  # a table function such as generate_series(); checked as a function below
        name = table.name.lower()
        schema = table.db.lower()
        if table.catalog or (schema and schema != settings.SCHEMA):
            return _reject(
                f"Table {table.sql(dialect='postgres')} is not allowed: only tables in the "
                f"{settings.SCHEMA} schema may be queried."
            )
        if not schema and _is_cte_reference(table):
            continue
        if name not in allowed:
            return _reject(
                f"Table {name} is not allowed: it is not in the {settings.SCHEMA} schema allow-list "
                "(system catalogs, information_schema and unknown tables are blocked). "
                "Use only tables from the schema context."
            )
        tables.add(name)
        aliases[table.alias_or_name.lower()] = name

    for func in root.find_all(exp.Func):
        name = _unmodelled_function_name(func)
        if (name is not None and name not in SAFE_POSTGRES_FUNCTIONS) or func.key in SESSION_FUNCTION_KEYS:
            shown = name or func.sql_name().lower()
            return _reject(
                f"The function {shown}() is not allowed: only standard SQL and side-effect-free "
                "PostgreSQL functions may be used."
            )

    for cast in root.find_all(exp.Cast):
        target = cast.args.get("to")
        if target is not None and target.sql(dialect="postgres").strip().lower().startswith("reg"):
            return _reject(
                f"Casting to {target.sql(dialect='postgres')} looks names up in the system catalogs; "
                "it is not allowed."
            )

    for ident in root.find_all(exp.Identifier):
        if is_sensitive_column(ident.name):
            return _reject(f"{ident.name} is a sensitive column (credentials) and cannot be read.")
    for column in root.find_all(exp.Column):
        if not column.table and column.name.lower() in SESSION_KEYWORDS:
            return _reject(f"{column.name} reveals session details, not ERP data; it is not allowed.")
        if not column.table and column.name.lower() in aliases:
            return _reject(
                f"{column.name} is a whole-row reference to a table; name the columns you need instead."
            )
    sensitive = {t.lower() for t in sensitive_tables} & tables
    stars = [
        s for s in root.find_all(exp.Star) if not isinstance(s.parent, (exp.Count, exp.Func))
    ]
    if sensitive and stars:
        return _reject(
            f"SELECT * is not allowed on {', '.join(sorted(sensitive))}: it has sensitive columns. "
            "Name the columns you need."
        )

    current = _limit_value(root.args.get("limit"))
    if root.args.get("limit") is None:
        applied = "injected"
    elif current is not None and 0 <= current <= max_rows:
        applied = "kept"
    else:
        applied = "capped"
    if applied != "kept":
        root = root.limit(max_rows, copy=False)
    return Verdict(
        ok=True,
        sql=root.sql(dialect="postgres"),
        tables=sorted(tables),
        limit_applied=applied,
    )
