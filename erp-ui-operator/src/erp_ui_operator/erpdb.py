"""Database side of the operator: verification, task derivation and cleanup.

Success is defined here, not in the browser. After the UI flow, ``verify_run`` queries the
``idempiere`` database as the ERP owner and checks the order the run created. The checks
are plain SQL plus a pure comparison (``compare``) so they can be unit-tested with
fixtures.

Each run tags its order twice (see ``RunTag``): ``Description`` starts with
``erp-ui-operator run <id>:`` and ``POReference`` is ``UIOP-<id>`` (the column holds 20
characters). Cleanup deletes only unprocessed drafts that carry both tags.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

import psycopg

from erp_sandbox import config, db
from erp_sandbox.seed import WAREHOUSE as HQ_WAREHOUSE_ID

from .task import TaskSpec

TAG_PREFIX = "erp-ui-operator run "
REF_PREFIX = "UIOP-"
RUN_ID_PATTERN = re.compile(r"^[a-z0-9]{1,15}$")  # no LIKE wildcards; fits POReference(20)


@dataclass(frozen=True)
class RunTag:
    """The marks a run leaves on its order, and the SQL patterns that find them."""

    run_id: str

    def __post_init__(self) -> None:
        if not RUN_ID_PATTERN.match(self.run_id):
            raise ValueError(f"run id must match {RUN_ID_PATTERN.pattern}, got {self.run_id!r}")

    @property
    def prefix(self) -> str:
        return f"{TAG_PREFIX}{self.run_id}"

    def description(self, spec: TaskSpec) -> str:
        return f"{self.prefix}: {spec.name}"[:255]

    @property
    def po_reference(self) -> str:
        return f"{REF_PREFIX}{self.run_id}"

    @property
    def description_like(self) -> str:
        return self.prefix + ":%"


ANY_RUN_DESCRIPTION_LIKE = TAG_PREFIX + "%"
ANY_RUN_REFERENCE_LIKE = REF_PREFIX + "%"


@dataclass(frozen=True)
class OrderRow:
    c_order_id: int
    documentno: str
    docstatus: str
    issotrx: str
    processed: str
    c_bpartner_id: int
    vendor: str
    description: str
    poreference: str | None
    date_promised: dt.date | None


@dataclass(frozen=True)
class LineRow:
    c_order_id: int
    line: int
    sku: str | None  # None for a line without a product (e.g. a charge line)
    qty: int | float


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class Verification:
    passed: bool
    checks: list[Check] = field(default_factory=list)
    order: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "checks": [c.as_dict() for c in self.checks], "order": self.order}

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


def compare(
    spec: TaskSpec,
    tag: RunTag,
    orders: list[OrderRow],
    lines: list[LineRow],
    *,
    vendor_ids: list[int],
    fingerprint_before: dict[str, Any] | None = None,
    fingerprint_after: dict[str, Any] | None = None,
) -> Verification:
    """Check the rows a run produced against its task spec. Pure function.

    ``vendor_ids`` are the active vendors named ``spec.vendor``; exactly one must exist and
    the order must point at it.
    """
    checks: list[Check] = []
    checks.append(Check("exactly_one_tagged_order", len(orders) == 1, f"{len(orders)} order(s) tagged {tag.prefix!r}"))
    if len(orders) != 1:
        if fingerprint_before is not None:
            checks.append(_untouched(fingerprint_before, fingerprint_after))
        return Verification(passed=False, checks=checks)

    o = orders[0]
    checks.append(Check("purchase_order", o.issotrx == "N", f"IsSOTrx={o.issotrx}"))
    checks.append(Check("still_draft", o.docstatus == "DR" and o.processed == "N", f"DocStatus={o.docstatus}, Processed={o.processed}"))
    checks.append(
        Check(
            "vendor",
            len(vendor_ids) == 1 and o.c_bpartner_id == vendor_ids[0],
            f"C_BPartner_ID={o.c_bpartner_id} ({o.vendor!r}); vendors named {spec.vendor!r}: {vendor_ids}",
        )
    )
    checks.append(Check("po_reference", o.poreference == tag.po_reference, f"POReference={o.poreference!r}"))
    checks.append(
        Check(
            "promised_date",
            o.date_promised == spec.promised_date,
            f"DatePromised={o.date_promised}, expected {spec.promised_date}",
        )
    )
    own = [ln for ln in lines if ln.c_order_id == o.c_order_id]
    got = Counter((ln.sku, ln.qty) for ln in own)
    want = Counter((ln.sku, ln.qty) for ln in spec.lines)
    missing = sorted((want - got).elements(), key=str)
    extra = sorted((got - want).elements(), key=str)
    detail = f"{len(own)} line(s); missing={missing or 'none'}; unexpected={extra or 'none'}"
    checks.append(Check("lines_match", not missing and not extra, detail))
    if fingerprint_before is not None:
        checks.append(_untouched(fingerprint_before, fingerprint_after))

    order = {
        "c_order_id": o.c_order_id,
        "documentno": o.documentno,
        "docstatus": o.docstatus,
        "vendor": o.vendor,
        "description": o.description,
        "poreference": o.poreference,
        "date_promised": o.date_promised.isoformat() if o.date_promised else None,
        "lines": [{"line": ln.line, "sku": ln.sku, "qty": ln.qty} for ln in own],
    }
    return Verification(passed=all(c.passed for c in checks), checks=checks, order=order)


def _untouched(before: dict[str, Any], after: dict[str, Any] | None) -> Check:
    changed = sorted(k for k in before if after is None or before.get(k) != after.get(k))
    return Check(
        "existing_records_untouched",
        not changed,
        "orders, lines, partners and products outside this run unchanged" if not changed else f"changed: {changed}",
    )


# ---------------------------------------------------------------------------- SQL

def connect() -> psycopg.Connection:
    return db.connect(config.ERP_DB)


def fetch_run(conn: psycopg.Connection, tag: RunTag) -> tuple[list[OrderRow], list[LineRow]]:
    rows = conn.execute(
        """
        SELECT o.c_order_id, o.documentno, o.docstatus, o.issotrx, o.processed, o.c_bpartner_id, bp.name,
               o.description, o.poreference, o.datepromised::date
          FROM adempiere.c_order o
          JOIN adempiere.c_bpartner bp ON bp.c_bpartner_id = o.c_bpartner_id
         WHERE o.ad_client_id = %s AND o.description LIKE %s
         ORDER BY o.c_order_id
        """,
        (config.CLIENT_ID, tag.description_like),
    ).fetchall()
    # iDempiere ids are NUMERIC columns; psycopg returns Decimal, so cast to int.
    orders = [OrderRow(int(r[0]), r[1], r[2], r[3], r[4], int(r[5]), *r[6:]) for r in rows]
    line_rows = conn.execute(
        """
        SELECT l.c_order_id, l.line, p.value, l.qtyordered
          FROM adempiere.c_orderline l
          LEFT JOIN adempiere.m_product p ON p.m_product_id = l.m_product_id
         WHERE l.c_order_id = ANY(%s)
         ORDER BY l.c_order_id, l.line
        """,
        ([o.c_order_id for o in orders],),
    ).fetchall()
    lines = [LineRow(int(r[0]), int(r[1]), r[2], _quantity(r[3])) for r in line_rows]
    return orders, lines


def _quantity(value: Any) -> int | float:
    """Whole quantities as int (so they compare equal to the spec); fractions stay visible."""
    f = float(value)
    return int(f) if f.is_integer() else f


def vendor_ids(conn: psycopg.Connection, vendor: str) -> list[int]:
    rows = conn.execute(
        """SELECT c_bpartner_id FROM adempiere.c_bpartner
            WHERE ad_client_id = %s AND name = %s AND isvendor = 'Y' AND isactive = 'Y'
            ORDER BY c_bpartner_id""",
        (config.CLIENT_ID, vendor),
    ).fetchall()
    return [int(r[0]) for r in rows]


def fingerprint(conn: psycopg.Connection) -> dict[str, Any]:
    """Counts and last-update stamps of records this tool must never change.

    The web UI stamps ``Updated`` on every save, so a UI edit to an existing order,
    partner or product changes ``max(updated)``.
    """
    not_ours = "coalesce(o.description, '') NOT LIKE %s"
    queries: dict[str, tuple[str, tuple[Any, ...]]] = {
        "c_order": (
            f"SELECT count(*), max(o.updated) FROM adempiere.c_order o WHERE o.ad_client_id = %s AND {not_ours}",
            (config.CLIENT_ID, ANY_RUN_DESCRIPTION_LIKE),
        ),
        "c_orderline": (
            f"SELECT count(*), max(l.updated) FROM adempiere.c_orderline l JOIN adempiere.c_order o USING (c_order_id) "
            f"WHERE o.ad_client_id = %s AND {not_ours}",
            (config.CLIENT_ID, ANY_RUN_DESCRIPTION_LIKE),
        ),
        "c_bpartner": ("SELECT count(*), max(updated) FROM adempiere.c_bpartner WHERE ad_client_id = %s", (config.CLIENT_ID,)),
        "m_product": ("SELECT count(*), max(updated) FROM adempiere.m_product WHERE ad_client_id = %s", (config.CLIENT_ID,)),
    }
    out: dict[str, Any] = {}
    for name, (sql, params) in queries.items():
        count, updated = conn.execute(sql, params).fetchone()
        out[name] = [count, updated.isoformat() if updated else None]
    return out


def verify_run(spec: TaskSpec, tag: RunTag, fingerprint_before: dict[str, Any] | None = None) -> Verification:
    with connect() as conn:
        orders, lines = fetch_run(conn, tag)
        ids = vendor_ids(conn, spec.vendor)
        after = fingerprint(conn) if fingerprint_before is not None else None
    return compare(
        spec, tag, orders, lines, vendor_ids=ids, fingerprint_before=fingerprint_before, fingerprint_after=after
    )


def unknown_skus(conn: psycopg.Connection, skus: list[str]) -> list[str]:
    rows = conn.execute(
        "SELECT value FROM adempiere.m_product WHERE ad_client_id = %s AND isactive = 'Y' AND value = ANY(%s)",
        (config.CLIENT_ID, skus),
    ).fetchall()
    found = {r[0] for r in rows}
    return [s for s in skus if s not in found]


# ---------------------------------------------------------------------- derivation

def reorder_qty(on_hand: float, level_max: float, order_min: float, order_pack: float) -> int:
    """Quantity to bring stock back to the maximum level, respecting vendor minimum and pack size."""
    qty = max(level_max - on_hand, order_min, 1)
    if order_pack > 0:
        qty = math.ceil(qty / order_pack) * order_pack
    return int(math.ceil(qty))


def derive_task(
    conn: psycopg.Connection,
    vendor: str,
    promised_date: dt.date,
    *,
    max_lines: int | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Task spec for the vendor's active products whose HQ on-hand stock is below the reorder point.

    Uses M_Replenish rows that replenish automatically (type 1 "reorder below minimum" or
    2 "maintain maximum") for HQ Warehouse, M_StorageOnHand for stock and the current
    vendor in M_Product_PO for sourcing. Open purchase orders are not netted out; the
    README lists that limitation.
    """
    rows = conn.execute(
        """
        SELECT p.value, coalesce(s.qty, 0), r.level_max,
               coalesce(po.order_min, 0), coalesce(po.order_pack, 0)
          FROM adempiere.m_product p
          JOIN adempiere.m_replenish r ON r.m_product_id = p.m_product_id AND r.m_warehouse_id = %s
                                      AND r.isactive = 'Y' AND r.replenishtype IN ('1', '2')
          JOIN adempiere.m_product_po po ON po.m_product_id = p.m_product_id AND po.iscurrentvendor = 'Y' AND po.isactive = 'Y'
          JOIN adempiere.c_bpartner bp ON bp.c_bpartner_id = po.c_bpartner_id
          LEFT JOIN (
                SELECT soh.m_product_id, sum(soh.qtyonhand) AS qty
                  FROM adempiere.m_storageonhand soh
                  JOIN adempiere.m_locator loc ON loc.m_locator_id = soh.m_locator_id
                 WHERE loc.m_warehouse_id = %s
                 GROUP BY soh.m_product_id
          ) s ON s.m_product_id = p.m_product_id
         WHERE p.ad_client_id = %s AND p.isactive = 'Y' AND p.value LIKE 'FH-%%'
           AND bp.name = %s AND bp.isvendor = 'Y'
           AND coalesce(s.qty, 0) < r.level_min
         ORDER BY p.value
        """,
        (HQ_WAREHOUSE_ID, HQ_WAREHOUSE_ID, config.CLIENT_ID, vendor),
    ).fetchall()
    lines = [
        {"sku": sku, "qty": reorder_qty(float(onhand), float(lmax), float(omin), float(opack))}
        for sku, onhand, lmax, omin, opack in rows
    ]
    if max_lines is not None:
        lines = lines[:max_lines]
    return {
        "name": name or f"Reorder below reorder point - {vendor}"[:60],
        "vendor": vendor,
        "promised_date": promised_date.isoformat(),
        "lines": lines,
    }


# -------------------------------------------------------------------------- cleanup

@dataclass(frozen=True)
class TaggedDraft:
    c_order_id: int
    documentno: str
    description: str
    lines: int

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> "TaggedDraft":
        return cls(int(row[0]), row[1], row[2], int(row[3]))


_SELECT_TAGGED = """
    SELECT o.c_order_id, o.documentno, o.description,
           (SELECT count(*) FROM adempiere.c_orderline l WHERE l.c_order_id = o.c_order_id)
      FROM adempiere.c_order o
     WHERE o.ad_client_id = %s AND o.issotrx = 'N' AND o.docstatus = 'DR' AND o.processed = 'N'
       AND o.description LIKE %s AND o.poreference LIKE %s
     ORDER BY o.c_order_id
"""


def _tag_patterns(tag: RunTag | None) -> tuple[str, str]:
    if tag is None:
        return ANY_RUN_DESCRIPTION_LIKE, ANY_RUN_REFERENCE_LIKE
    return tag.description_like, tag.po_reference


def cleanup(
    *,
    tag: RunTag | None = None,
    confirm: Callable[[list[TaggedDraft]], bool] = lambda drafts: True,
) -> tuple[list[TaggedDraft], int]:
    """Delete this tool's draft purchase orders in one transaction.

    Lists the tagged, unprocessed drafts and asks ``confirm`` before taking any lock.
    Then, in one transaction, re-selects them ``FOR UPDATE``, refuses if the set changed,
    and deletes their tax rows, lines, recent-item entries and headers. Any mismatch or
    error rolls the whole transaction back. Returns (drafts found, orders deleted).
    """
    desc_like, ref_like = _tag_patterns(tag)
    params = (config.CLIENT_ID, desc_like, ref_like)
    with connect() as conn:
        drafts = [TaggedDraft.from_row(r) for r in conn.execute(_SELECT_TAGGED, params).fetchall()]
        conn.rollback()
        if not drafts or not confirm(drafts):
            return drafts, 0
        with conn.transaction():
            locked = [TaggedDraft.from_row(r) for r in conn.execute(_SELECT_TAGGED + " FOR UPDATE OF o", params).fetchall()]
            ids = [d.c_order_id for d in drafts]
            if [d.c_order_id for d in locked] != ids:
                raise RuntimeError("the tagged drafts changed while you were confirming; nothing deleted")
            conn.execute("DELETE FROM adempiere.c_ordertax WHERE c_order_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM adempiere.c_orderline WHERE c_order_id = ANY(%s)", (ids,))
            conn.execute(
                """DELETE FROM adempiere.ad_recentitem
                    WHERE record_id = ANY(%s)
                      AND ad_table_id = (SELECT ad_table_id FROM adempiere.ad_table WHERE tablename = 'C_Order')""",
                (ids,),
            )
            deleted = conn.execute(
                """DELETE FROM adempiere.c_order
                    WHERE c_order_id = ANY(%s) AND issotrx = 'N' AND docstatus = 'DR' AND processed = 'N'
                      AND description LIKE %s AND poreference LIKE %s""",
                (ids, desc_like, ref_like),
            ).rowcount
            if deleted != len(drafts):
                raise RuntimeError(f"cleanup expected to delete {len(drafts)} orders, deleted {deleted}; rolled back")
    return drafts, deleted
