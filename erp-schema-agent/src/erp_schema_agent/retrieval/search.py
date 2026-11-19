"""Lexical retrieval over the schema catalog, expanded along the real FK graph.

No embedding model is available locally, so ranking is BM25 over iDempiere's own
dictionary text (table and column names split into words, dictionary Name,
Description and Help, and the screens each table appears on). Three steps:

1. **Glossary**: curated business terms ("reorder point", "revenue", "state") pin the
   tables a tenant expert would start from (``data/glossary.json``).
2. **Lexical**: BM25 over table documents, down-weighting empty tables and views.
3. **FK expansion**: shortest FK paths between the seed tables add the bridge tables a
   join needs (C_Order -> C_BPartner_Location -> C_Location -> C_Region), and the best
   lexical FK neighbours of the seeds are added after them.

Embeddings would slot in at step 2, as a second scorer fused with BM25 (for example
reciprocal-rank fusion), over the same table documents.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .. import settings
from .catalog import Catalog, Column, Table, load_catalog

GLOSSARY_PATH = Path(__file__).resolve().parent.parent / "data" / "glossary.json"
# Context budgets (how much schema the model sees), not enforced limits.
MAX_TABLES = 10
LEXICAL_SEEDS = 4
FK_NEIGHBOURS = 2
MAX_PATH_EDGES = 3
HUB_DEGREE = 100  # tables this connected (AD_User, AD_Org ...) never act as join bridges
DESCRIBED_COLUMNS = 28
MIN_RELATIVE_SCORE = 0.5  # lexical seeds and neighbours need half the best BM25 score
K1, B = 1.2, 0.75

STOPWORDS = frozenset(
    "a an and are as at be by for from has have how i in is it its of on or our per show "
    "that the their them this to was we what when which who whose will with list give each "
    "all any me my do does did many much there top".split()
)
AUDIT_COLUMNS = frozenset({"created", "createdby", "updated", "updatedby", "ad_org_id", "ad_client_id"})
ALWAYS_DESCRIBE = frozenset(
    {"value", "name", "documentno", "docstatus", "issotrx", "isactive", "discontinued", "dateordered",
     "qtyordered", "qtydelivered", "linenetamt", "grandtotal", "qtyonhand", "level_min", "level_max",
     "pricestd", "pricelist", "m_pricelist_version_id", "iscurrentvendor", "deliverytime_promised",
     "c_region_id", "regionname", "description", "sku"}
)


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    words = re.split(r"[^a-z0-9]+", spaced.lower())
    return [_stem(w) for w in words if len(w) > 1 and w not in STOPWORDS]


@dataclass(frozen=True)
class TableHit:
    table: str
    score: float
    reason: str  # "glossary", "lexical", "join path" or "fk neighbour"
    label: str = ""


@dataclass(frozen=True)
class Retrieval:
    question: str
    tables: list[TableHit]
    joins: list[tuple[str, str, str, str]]
    glossary_terms: list[str]
    context: str = field(repr=False, default="")


def _document(table: Table) -> list[str]:
    doc = tokens(table.name.replace("_", " ")) * 3 + tokens(table.ad_name) * 3 + tokens(table.label) * 3
    for window in table.windows:
        doc += tokens(window) * 2
    doc += tokens(table.description) + tokens(table.help)
    for col in table.columns:
        if col.name in AUDIT_COLUMNS:
            continue
        doc += _column_words(col)
    return doc


class _Index:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.docs = {name: Counter(_document(t)) for name, t in catalog.tables.items()}
        self.lengths = {name: sum(doc.values()) for name, doc in self.docs.items()}
        self.avg_len = sum(self.lengths.values()) / max(1, len(self.lengths))
        df: Counter[str] = Counter()
        for doc in self.docs.values():
            df.update(doc.keys())
        n = len(self.docs)
        self.idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}
        self.adjacency: dict[str, set[str]] = {name: set() for name in catalog.tables}
        for src, _, dst, _ in catalog.fks:
            if src != dst:
                self.adjacency[src].add(dst)
                self.adjacency[dst].add(src)

    def bm25(self, query: list[str]) -> dict[str, float]:
        scores: dict[str, float] = {}
        terms = set(query)
        ordered = sorted(terms)  # a fixed summation order keeps scores bit-identical across runs
        for name, doc in self.docs.items():
            score = 0.0
            norm = K1 * (1 - B + B * self.lengths[name] / self.avg_len)
            for term in ordered:
                tf = doc.get(term, 0)
                if tf:
                    score += self.idf[term] * tf * (K1 + 1) / (tf + norm)
            if score:
                table = self.catalog.tables[name]
                label = set(tokens(table.label))
                if terms and terms == label:
                    score *= 1.5  # the query is the dictionary name ("business partner")
                elif terms and terms <= label:
                    score *= 1.2
                scores[name] = score * (0.25 if table.rows == 0 else 0.5 if table.rows < 0 else 1.0)
        return scores

    def shortest_path(self, start: str, goal: str) -> list[str]:
        """Shortest FK path (at most MAX_PATH_EDGES edges), never bridging through hubs."""
        queue: deque[list[str]] = deque([[start]])
        seen = {start}
        while queue:
            path = queue.popleft()
            if len(path) > MAX_PATH_EDGES:
                continue
            for nxt in sorted(self.adjacency[path[-1]]):
                if nxt == goal:
                    return path + [nxt]
                if nxt in seen or len(self.adjacency[nxt]) > HUB_DEGREE or self.catalog.tables[nxt].rows <= 0:
                    continue
                seen.add(nxt)
                queue.append(path + [nxt])
        return []


@lru_cache(maxsize=1)
def _index() -> _Index:
    return _Index(load_catalog())


@lru_cache(maxsize=1)
def load_glossary() -> dict[str, list]:
    return json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))


def _glossary_tables(question: str) -> tuple[list[str], list[str]]:
    text = " " + re.sub(r"[^a-z0-9-]+", " ", question.lower()) + " "
    tables: list[str] = []
    terms: list[str] = []
    for entry in load_glossary()["terms"]:
        hit = next((t for t in entry["terms"] if f" {t} " in text), None)
        if hit:
            terms.append(hit)
            tables += [t for t in entry["tables"] if t not in tables]
    return tables, terms


def _ranked(scores: dict[str, float]) -> list[str]:
    return [t for t, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]


def search_schema(query: str, *, limit: int = MAX_TABLES) -> list[TableHit]:
    """Plain lexical search, for the MCP ``search_schema`` tool."""
    index = _index()
    scores = index.bm25(tokens(query))
    return [TableHit(t, round(scores[t], 3), "lexical", index.catalog.tables[t].label) for t in _ranked(scores)[:limit]]


def retrieve(question: str, *, max_tables: int = MAX_TABLES) -> Retrieval:
    index = _index()
    catalog = index.catalog
    scores = index.bm25(tokens(question))
    lexical = [t for t in _ranked(scores) if catalog.tables[t].rows > 0]
    glossary, terms = _glossary_tables(question)

    hits: dict[str, TableHit] = {}

    def add(table: str, reason: str) -> None:
        if table not in hits and table in catalog.tables and len(hits) < max_tables:
            hits[table] = TableHit(table, round(scores.get(table, 0.0), 3), reason, catalog.tables[table].label)

    for table in glossary:
        add(table, "glossary")
    # With glossary anchors, a lexical match must sit near them in the FK graph: a word
    # like "revenue" also matches C_RevenueRecognition, which no sales question needs.
    anchors = set(glossary)
    top = scores[lexical[0]] if lexical else 0.0
    strong = [t for t in lexical if scores[t] >= MIN_RELATIVE_SCORE * top]
    if anchors:
        strong = [t for t in strong if index.adjacency[t] & anchors][: LEXICAL_SEEDS // 2]
    for table in strong[:LEXICAL_SEEDS]:
        add(table, "lexical")
    seeds = list(hits)
    core = [t for t in seeds if t in anchors] or seeds
    for i, a in enumerate(core):
        for b in core[i + 1:]:
            if b in index.adjacency[a]:
                continue
            for bridge in index.shortest_path(a, b)[1:-1]:
                add(bridge, "join path")
    neighbours = sorted(
        {n for s in seeds for n in index.adjacency[s] if n not in hits
         and scores.get(n, 0) >= MIN_RELATIVE_SCORE * top and catalog.tables[n].rows > 0},
        key=lambda n: (-scores[n], n),
    )
    for table in neighbours[:FK_NEIGHBOURS]:
        add(table, "fk neighbour")

    selected = list(hits.values())
    joins = catalog.joins_between({h.table for h in selected})
    return Retrieval(question, selected, joins, terms, context=render_context(question, selected, joins, catalog))


def _value_list(values: dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in values.items())


def _column_words(col: Column) -> list[str]:
    return tokens(col.name.replace("_", " ")) + tokens(col.label)


def render_column(col: Column) -> str:
    parts = [f"  - {col.name} ({col.data_type})"]
    if col.label or col.description:
        parts.append(": " + " - ".join(p for p in (col.label, col.description) if p))
    if col.fk_table:
        parts.append(f" -> {col.fk_table}")
    if col.values:
        parts.append(f" [values: {_value_list(col.values)}]")
    return "".join(parts)


def render_table(table: Table, *, focus: set[str] | None = None, linked: set[str] | None = None,
                 max_described: int | None = DESCRIBED_COLUMNS) -> str:
    """Markdown-ish description of one table: header, dictionary text, columns."""
    rows = "view" if table.rows < 0 else f"{table.rows:,} rows"
    lines = [f"### {table.name} ({table.ad_name}: {table.label or 'no dictionary name'}; {rows})"]
    if table.description:
        lines.append(table.description)
    if table.windows:
        lines.append("Screens: " + ", ".join(table.windows[:6]))
    focus = focus or set()
    linked = linked or set()
    key = f"{table.name}_id"

    def priority(col: Column) -> int:
        if col.name == key:
            return 0
        if col.fk_table and col.fk_table in linked:
            return 1
        if col.name in focus:
            return 2
        if col.name in ALWAYS_DESCRIBE:
            return 3
        if col.name in AUDIT_COLUMNS or col.name.endswith("_uu"):
            return 9
        return 5

    ranked = sorted(table.columns, key=priority)
    if max_described is None:
        described, rest = ranked, []
    else:
        described = [c for c in ranked if priority(c) < 5][:max_described]
        rest = [c for c in table.columns if c not in described]
    order = {c.name: i for i, c in enumerate(table.columns)}
    lines.append("Columns:")
    lines += [render_column(c) for c in sorted(described, key=lambda c: order[c.name])]
    if rest:
        lines.append("  Other columns: " + ", ".join(c.name for c in rest))
    return "\n".join(lines)


def render_context(question: str, hits: list[TableHit], joins: list[tuple[str, str, str, str]],
                   catalog: Catalog) -> str:
    q_tokens = set(tokens(question))
    selected = {h.table for h in hits}
    out = ["Tenant conventions:"]
    out += [f"- {c}" for c in load_glossary()["conventions"]]
    out.append("")
    out.append(f"Relevant tables (most relevant first; all in schema {settings.SCHEMA}):")
    for hit in hits:
        table = catalog.tables[hit.table]
        focus = {c.name for c in table.columns if q_tokens & set(_column_words(c))}
        out.append("")
        out.append(render_table(table, focus=focus, linked=selected))
    if joins:
        out.append("")
        out.append("Foreign keys between these tables:")
        out += [f"  - {a}.{ac} = {b}.{bc}" for a, ac, b, bc in joins]
    return "\n".join(out)
