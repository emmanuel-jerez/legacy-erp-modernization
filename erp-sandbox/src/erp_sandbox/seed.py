"""Load the fictional Fernhollow catalog and placeholder history into iDempiere.

Everything this module writes is fictional or synthetic and says so in the data:
partner names start with "Placeholder" or "Demo Customer", and every order description
reads "Synthetic placeholder ...". It extends iDempiere's bundled GardenWorld tenant
instead of creating a new one, so the legacy demo data stays alongside (realistic noise
for the agents to ignore).

Rows are inserted directly with SQL, not through iDempiere's document engine, so seeded
orders carry no accounting facts. IDs come from ``AD_Sequence`` so records created later
in the web UI never collide with seeded ones. The generator uses a fixed random seed:
the same code on a fresh image produces the same business data and record IDs, which
the eval answer keys rely on. Only the ``*_uu`` UUID columns differ between machines.

Rows iDempiere's model classes would add on save are added here too, from the same
sources their ``afterSave`` uses: ``M_Product_Category_Acct`` and ``C_BP_Employee_Acct``
from ``C_AcctSchema_Default``, ``M_Product_Acct`` from the category's accounts, and
``C_BP_Customer_Acct``/``C_BP_Vendor_Acct`` from the partner group's accounts. Without
them, documents created later in the web UI cannot be completed or posted.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import psycopg

from . import config
from .catalog import build_catalog

RANDOM_SEED = 20261119
CLIENT = config.CLIENT_ID
ORG = config.ORG_ID
USER = 100  # SuperUser
SALES_REP = 101  # GardenAdmin
UOM_EACH = 100
TAX_CATEGORY = 107
TAX_STANDARD = 104
USD = 100
COUNTRY_US = 100
WAREHOUSE = 103  # HQ Warehouse
LOCATOR = 101  # Default HQ Locator
PRICE_LIST_SALES = 101
PLV_SALES = 104  # Standard 2003, the current sales price list version
PRICE_LIST_PURCHASE = 102
PLV_PURCHASE = 103
DOCTYPE_SO = 132  # Standard Order
DOCTYPE_PO = 126  # Purchase Order
TERM_IMMEDIATE = 105
TERM_NET30 = 100
GROUP_CUSTOMER = 103
GROUP_VENDOR = 104
REGIONS = {103: "CA", 108: "NY", 113: "FL", 116: "WA", 127: "IL", 132: "TX"}
REGION_WEIGHTS = [34, 16, 14, 10, 11, 15]

HISTORY_START = date(2025, 12, 1)
HISTORY_END = date(2026, 11, 15)  # "today" in the seeded world
N_CUSTOMERS = 320
N_SALES_ORDERS = 9000
CENT = Decimal("0.01")

VENDORS = [
    ("PH-VEND-A", "Placeholder Contract Manufacturer A", (18, 35)),
    ("PH-VEND-B", "Placeholder Contract Manufacturer B", (25, 45)),
    ("PH-VEND-C", "Placeholder Co-Packer C", (10, 21)),
]


def money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Product:
    id: int
    sku: str
    name: str
    product_type: str
    price: Decimal
    list_price: Decimal
    cost: Decimal
    vendor_id: int
    lead_days: int
    popularity: float


class Ids:
    """Allocates table IDs from iDempiere's AD_Sequence, like the application does."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def take(self, table: str, n: int = 1) -> list[int]:
        row = self.conn.execute(
            "UPDATE ad_sequence SET currentnext = currentnext + %s "
            "WHERE name = %s AND istableid = 'Y' AND ad_client_id = 0 RETURNING currentnext - %s",
            (n, table, n),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"no table sequence for {table}")
        first = int(row[0])
        return list(range(first, first + n))


def _audit(ts: datetime) -> dict[str, Any]:
    return {
        "ad_client_id": CLIENT,
        "isactive": "Y",
        "created": ts,
        "createdby": USER,
        "updated": ts,
        "updatedby": USER,
    }


def _insert(conn: psycopg.Connection, table: str, rows: list[dict[str, Any]], uu: str | None = None) -> None:
    if not rows:
        return
    columns = list(rows[0].keys())
    mismatched = [i for i, row in enumerate(rows) if row.keys() != rows[0].keys()]
    if mismatched:
        # The column list comes from the first row; a key on only some rows would be dropped.
        raise ValueError(f"{table}: rows {mismatched[:5]} have different keys from row 0")
    placeholders = ", ".join(["%s"] * len(columns))
    uu_sql = f", {uu}" if uu else ""
    uu_val = ", gen_random_uuid()::text" if uu else ""
    stmt = f"INSERT INTO {table} ({', '.join(columns)}{uu_sql}) VALUES ({placeholders}{uu_val})"
    with conn.cursor() as cur:
        cur.executemany(stmt, [tuple(row[c] for c in columns) for row in rows])


_AUDIT_COLUMNS = {"ad_client_id", "ad_org_id", "isactive", "created", "createdby", "updated", "updatedby"}


def _copy_companions(conn: psycopg.Connection, *, target: str, key: str, ids: list[int], source: str,
                     where: str, params: tuple[Any, ...], ts: datetime) -> None:
    """Give each new record the companion rows iDempiere would create on save.

    Copies every column the target shares with ``source`` (accounting schema and the
    ``*_acct`` account combinations), one set of rows per new ID.
    """
    shared = [
        row[0]
        for row in conn.execute(
            "SELECT t.column_name FROM information_schema.columns t "
            "JOIN information_schema.columns s ON s.table_schema = t.table_schema "
            " AND s.table_name = %s AND s.column_name = t.column_name "
            "WHERE t.table_schema = current_schema() AND t.table_name = %s ORDER BY t.ordinal_position",
            (source, target),
        )
        if row[0] not in _AUDIT_COLUMNS | {key, f"{target}_uu", f"{source}_uu"}
    ]
    cols = ", ".join(shared)
    conn.execute(
        f"INSERT INTO {target} ({key}, ad_client_id, ad_org_id, isactive, created, createdby, updated, updatedby, "
        f"{cols}, {target}_uu) "
        f"SELECT x.id, %s, 0, 'Y', %s, %s, %s, %s, {', '.join('s.' + c for c in shared)}, gen_random_uuid()::text "
        f"FROM unnest(%s::numeric[]) AS x(id) CROSS JOIN (SELECT * FROM {source} WHERE {where}) s",
        (CLIENT, ts, USER, ts, USER, ids, *params),
    )


def _already_seeded(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM m_product_category WHERE ad_client_id = %s AND value = 'FERNHOLLOW'", (CLIENT,)
    ).fetchone()
    return row is not None


def _partner(conn: psycopg.Connection, ids: Ids, *, value: str, name: str, group: int, customer: bool,
             region_id: int, street: str, city: str, postal: str, ts: datetime) -> tuple[int, int]:
    bp_id = ids.take("C_BPartner")[0]
    loc_id = ids.take("C_Location")[0]
    bpl_id = ids.take("C_BPartner_Location")[0]
    _insert(conn, "c_bpartner", [{
        **_audit(ts), "c_bpartner_id": bp_id, "ad_org_id": 0, "value": value, "name": name,
        "description": "Synthetic placeholder partner (not a real person or company)",
        "c_bp_group_id": group, "iscustomer": "Y" if customer else "N", "isvendor": "N" if customer else "Y",
        "c_paymentterm_id": TERM_IMMEDIATE if customer else None,
        "po_paymentterm_id": None if customer else TERM_NET30,
        "m_pricelist_id": PRICE_LIST_SALES if customer else None,
        "po_pricelist_id": None if customer else PRICE_LIST_PURCHASE,
    }], uu="c_bpartner_uu")
    # MBPartner.afterSave creates all three, whatever the partner's customer/vendor flags.
    for target in ("c_bp_customer_acct", "c_bp_vendor_acct"):
        _copy_companions(conn, target=target, key="c_bpartner_id", ids=[bp_id], source="c_bp_group_acct",
                         where="c_bp_group_id = %s", params=(group,), ts=ts)
    _copy_companions(conn, target="c_bp_employee_acct", key="c_bpartner_id", ids=[bp_id],
                     source="c_acctschema_default", where="ad_client_id = %s", params=(CLIENT,), ts=ts)
    _insert(conn, "c_location", [{
        **_audit(ts), "c_location_id": loc_id, "ad_org_id": 0, "address1": street, "city": city,
        "postal": postal, "c_country_id": COUNTRY_US, "c_region_id": region_id, "regionname": REGIONS[region_id],
    }], uu="c_location_uu")
    _insert(conn, "c_bpartner_location", [{
        **_audit(ts), "c_bpartner_location_id": bpl_id, "ad_org_id": 0, "name": f"{city}, {REGIONS[region_id]}",
        "isbillto": "Y", "isshipto": "Y", "ispayfrom": "Y", "isremitto": "Y", "c_bpartner_id": bp_id,
        "c_location_id": loc_id,
    }], uu="c_bpartner_location_uu")
    return bp_id, bpl_id


def _seasonal_weight(day: date) -> float:
    weight = 1.0 + (0.25 if day.weekday() in (5, 6) else 0.0)
    if day.month == 12 and day.day <= 20:
        weight *= 1.8  # holiday gifting
    if day.month == 11 and 24 <= day.day <= 30:
        weight *= 3.0  # Black Friday week (not reached in 2026 history)
    if day.month in (5, 6):
        weight *= 1.25  # sun care season
    return weight


_SEEDED_PARTNERS = (
    "c_bpartner_id IN (SELECT c_bpartner_id FROM c_bpartner WHERE value LIKE 'DEMO-C%%' OR value LIKE 'PH-VEND-%%')"
)
SEEDED_TABLES = [
    # (table, id column or None, filter) in delete order
    ("m_replenish", None, "m_product_id IN (SELECT m_product_id FROM m_product WHERE value LIKE 'FH-%%')"),
    ("m_storageonhand", None, "m_product_id IN (SELECT m_product_id FROM m_product WHERE value LIKE 'FH-%%')"),
    ("c_orderline", "c_orderline_id", "c_order_id IN (SELECT c_order_id FROM c_order WHERE documentno LIKE 'FH-%%')"),
    ("c_order", "c_order_id", "documentno LIKE 'FH-%%'"),
    ("m_product_acct", None, "m_product_id IN (SELECT m_product_id FROM m_product WHERE value LIKE 'FH-%%')"),
    ("m_product_category_acct", None,
     "m_product_category_id IN (SELECT m_product_category_id FROM m_product_category WHERE value = 'FERNHOLLOW')"),
    ("c_bp_customer_acct", None, _SEEDED_PARTNERS),
    ("c_bp_vendor_acct", None, _SEEDED_PARTNERS),
    ("c_bp_employee_acct", None, _SEEDED_PARTNERS),
    ("m_product_po", None, "m_product_id IN (SELECT m_product_id FROM m_product WHERE value LIKE 'FH-%%')"),
    ("m_productprice", "m_productprice_id",
     "m_product_id IN (SELECT m_product_id FROM m_product WHERE value LIKE 'FH-%%')"),
    ("m_product", "m_product_id", "value LIKE 'FH-%%'"),
    ("c_bpartner_location", "c_bpartner_location_id",
     "c_bpartner_id IN (SELECT c_bpartner_id FROM c_bpartner WHERE value LIKE 'DEMO-C%%' OR value LIKE 'PH-VEND-%%')"),
    ("c_location", "c_location_id", "address1 LIKE '%%Placeholder%%' AND c_location_id >= 1000000"),
    ("c_bpartner", "c_bpartner_id", "value LIKE 'DEMO-C%%' OR value LIKE 'PH-VEND-%%'"),
    ("m_product_category", "m_product_category_id", "value = 'FERNHOLLOW'"),
]


def unseed(conn: psycopg.Connection) -> None:
    """Remove everything ``seed`` wrote and rewind the table sequences it advanced.

    For development before the seed template is frozen; records created in the web UI
    that reference seeded rows would block the delete (foreign keys) and should be
    removed first, or the whole stack reset.
    """
    for table, _, where in SEEDED_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE ad_client_id = %s AND ({where})", (CLIENT,))
    for table, id_column, _ in SEEDED_TABLES:
        if id_column is None:
            continue
        conn.execute(
            f"UPDATE ad_sequence SET currentnext = GREATEST(1000000, "
            f"(SELECT COALESCE(MAX({id_column}), 0) + 1 FROM {table})) "
            "WHERE istableid = 'Y' AND ad_client_id = 0 AND lower(name) = %s",
            (table,),
        )


def seed(conn: psycopg.Connection) -> dict[str, int]:
    """Seed the ERP inside the caller's transaction. Returns row counts."""
    if _already_seeded(conn):
        raise RuntimeError("Fernhollow data is already present; reset the stack to reseed")
    rng = random.Random(RANDOM_SEED)
    ids = Ids(conn)
    seeded_at = datetime(2025, 11, 20, 9, 0, 0)

    # Product category
    category_id = ids.take("M_Product_Category")[0]
    _insert(conn, "m_product_category", [{
        **_audit(seeded_at), "m_product_category_id": category_id, "ad_org_id": 0, "value": "FERNHOLLOW",
        "name": "Fernhollow Skincare", "description": "Fictional demo brand", "plannedmargin": 0,
    }], uu="m_product_category_uu")
    _copy_companions(conn, target="m_product_category_acct", key="m_product_category_id", ids=[category_id],
                     source="c_acctschema_default", where="ad_client_id = %s", params=(CLIENT,), ts=seeded_at)

    # Vendors
    vendor_ids: list[tuple[int, int, tuple[int, int]]] = []
    for value, name, lead in VENDORS:
        bp_id, bpl_id = _partner(conn, ids, value=value, name=name, group=GROUP_VENDOR, customer=False,
                                 region_id=103, street="1 Placeholder Industrial Way", city="Demo City",
                                 postal="90001", ts=seeded_at)
        vendor_ids.append((bp_id, bpl_id, lead))

    # Products, prices, vendor terms
    catalog = build_catalog()
    products: list[Product] = []
    product_rows, sales_prices, purchase_prices, po_rows = [], [], [], []
    for item in catalog["products"]:
        for variant in item["variants"]:
            pid = ids.take("M_Product")[0]
            vendor_id, _, lead = rng.choice(vendor_ids)
            price = money(variant["price"])
            list_price = money(variant["compare_at_price"]) if variant["compare_at_price"] else price
            cost = money(float(price) * rng.uniform(0.17, 0.31))
            name = item["title"] if variant["title"] in ("One size", "Kit") else f"{item['title']} {variant['title']}"
            popularity = rng.lognormvariate(0, 0.8) * (0.45 if "2-Pack" in variant["title"] else 1.0)
            products.append(Product(pid, variant["sku"], name, item["product_type"], price, list_price, cost,
                                    vendor_id, rng.randint(*lead), popularity))
            product_rows.append({
                **_audit(seeded_at), "m_product_id": pid, "ad_org_id": 0, "value": variant["sku"], "name": name,
                "description": item["description"][:255], "help": f"{catalog['brand']} catalog item.",
                "sku": variant["sku"], "group1": item["product_type"], "group2": item["handle"],
                "weight": Decimal(variant["grams"]) / 1000, "c_uom_id": UOM_EACH,
                "m_product_category_id": category_id, "c_taxcategory_id": TAX_CATEGORY, "producttype": "I",
                "issold": "Y", "ispurchased": "Y", "isstocked": "Y", "discontinued": "N",
            })
    # Retire two variants so "active products" questions have a trap.
    retired = {products[5].id, products[-3].id}
    for row in product_rows:
        if row["m_product_id"] in retired:
            row["isactive"] = "N"
            row["discontinued"] = "Y"

    for p in products:
        sales_prices.append({
            **_audit(seeded_at), "m_productprice_id": ids.take("M_ProductPrice")[0], "ad_org_id": 0,
            "m_pricelist_version_id": PLV_SALES, "m_product_id": p.id,
            "pricelist": p.list_price, "pricestd": p.price, "pricelimit": money(float(p.price) * 0.85),
        })
        purchase_prices.append({
            **_audit(seeded_at), "m_productprice_id": ids.take("M_ProductPrice")[0], "ad_org_id": 0,
            "m_pricelist_version_id": PLV_PURCHASE, "m_product_id": p.id,
            "pricelist": p.cost, "pricestd": p.cost, "pricelimit": p.cost,
        })
        po_rows.append({
            **_audit(seeded_at), "m_product_id": p.id, "c_bpartner_id": p.vendor_id, "ad_org_id": 0,
            "iscurrentvendor": "Y", "c_uom_id": UOM_EACH, "c_currency_id": USD, "pricelist": p.cost,
            "pricepo": p.cost, "vendorproductno": f"V-{p.sku}", "order_min": 100,
            "order_pack": 50, "deliverytime_promised": p.lead_days,
        })
    _insert(conn, "m_product", product_rows, uu="m_product_uu")
    _copy_companions(conn, target="m_product_acct", key="m_product_id", ids=[p.id for p in products],
                     source="m_product_category_acct", where="m_product_category_id = %s",
                     params=(category_id,), ts=seeded_at)
    _insert(conn, "m_productprice", sales_prices + purchase_prices, uu="m_productprice_uu")
    _insert(conn, "m_product_po", po_rows, uu="m_product_po_uu")

    # Customers
    customers: list[tuple[int, int]] = []
    for n in range(1, N_CUSTOMERS + 1):
        region_id = rng.choices(list(REGIONS), weights=REGION_WEIGHTS)[0]
        customers.append(_partner(
            conn, ids, value=f"DEMO-C{n:04d}", name=f"Demo Customer {n:04d}", group=GROUP_CUSTOMER,
            customer=True, region_id=region_id, street=f"{100 + n} Placeholder Ave", city="Demo City",
            postal="00000", ts=seeded_at,
        ))
    customer_weights = [rng.paretovariate(1.6) for _ in customers]  # repeat buyers

    sellable = [p for p in products if p.id not in retired]
    days = [HISTORY_START + timedelta(d) for d in range((HISTORY_END - HISTORY_START).days + 1)]
    day_weights = [_seasonal_weight(d) * (1 + 0.6 * i / len(days)) for i, d in enumerate(days)]  # growth
    shipped: dict[int, int] = {p.id: 0 for p in products}

    order_rows, line_rows = [], []
    so_dates = sorted(rng.choices(days, weights=day_weights, k=N_SALES_ORDERS))
    for n, day in enumerate(so_dates, start=1):
        order_id = ids.take("C_Order")[0]
        bp_id, bpl_id = rng.choices(customers, weights=customer_weights)[0]
        ordered_at = datetime.combine(day, datetime.min.time()) + timedelta(minutes=rng.randint(0, 1439))
        age = (HISTORY_END - day).days
        status = "VO" if rng.random() < 0.015 else ("IP" if age <= 2 and rng.random() < 0.5 else "CO")
        n_lines = rng.choices([1, 2, 3], weights=[62, 28, 10])[0]
        drawn = rng.choices(sellable, weights=[p.popularity for p in sellable], k=n_lines)
        picks = list({p.id: p for p in drawn}.values())
        total = Decimal(0)
        for line_no, product in enumerate(picks, start=1):
            qty = rng.choices([1, 2, 3], weights=[80, 16, 4])[0]
            net = product.price * qty
            delivered = qty if status == "CO" and age > 3 else 0
            if status == "VO":
                qty_ordered, net_amt = 0, Decimal(0)
            else:
                qty_ordered, net_amt = qty, net
                total += net
            shipped[product.id] += delivered
            line_rows.append({
                **_audit(ordered_at), "c_orderline_id": ids.take("C_OrderLine")[0], "ad_org_id": ORG,
                "c_order_id": order_id, "line": line_no * 10, "dateordered": ordered_at,
                "datepromised": ordered_at + timedelta(days=3),
                "datedelivered": ordered_at + timedelta(days=2) if delivered else None,
                "m_warehouse_id": WAREHOUSE, "c_uom_id": UOM_EACH, "c_currency_id": USD, "c_tax_id": TAX_STANDARD,
                "m_product_id": product.id, "c_bpartner_id": bp_id, "c_bpartner_location_id": bpl_id,
                "qtyentered": qty, "qtyordered": qty_ordered, "qtydelivered": delivered,
                "qtyinvoiced": delivered, "qtyreserved": qty_ordered - delivered if status != "VO" else 0,
                "priceentered": product.price, "priceactual": product.price, "pricelist": product.list_price,
                "pricelimit": money(float(product.price) * 0.85), "linenetamt": net_amt,
                "processed": "Y", "m_attributesetinstance_id": 0,
            })
        order_rows.append({
            **_audit(ordered_at), "c_order_id": order_id, "ad_org_id": ORG, "documentno": f"FH-SO-{n:06d}",
            "description": "Synthetic placeholder sales order", "issotrx": "Y", "docstatus": status,
            "docaction": "--" if status == "VO" else ("CL" if status == "CO" else "CO"), "processed": "Y",
            "isapproved": "Y", "isdelivered": "Y" if status == "CO" and age > 3 else "N",
            "c_doctype_id": DOCTYPE_SO, "c_doctypetarget_id": DOCTYPE_SO, "dateordered": ordered_at,
            "datepromised": ordered_at + timedelta(days=3), "dateacct": ordered_at, "c_bpartner_id": bp_id,
            "c_bpartner_location_id": bpl_id, "bill_bpartner_id": bp_id, "bill_location_id": bpl_id,
            "c_currency_id": USD, "paymentrule": "K", "c_paymentterm_id": TERM_IMMEDIATE, "invoicerule": "D",
            "deliveryrule": "A", "freightcostrule": "I", "deliveryviarule": "S", "priorityrule": "5",
            "m_warehouse_id": WAREHOUSE, "m_pricelist_id": PRICE_LIST_SALES, "salesrep_id": SALES_REP,
            "totallines": total, "grandtotal": total,
        })

    # Purchase orders: restock each vendor's products roughly monthly.
    received: dict[int, int] = {p.id: 0 for p in products}
    po_number = 0
    by_vendor: dict[int, list[Product]] = {}
    for p in products:
        by_vendor.setdefault(p.vendor_id, []).append(p)
    vendor_location = {bp: bpl for bp, bpl, _ in vendor_ids}
    for vendor_id, vendor_products in by_vendor.items():
        day = HISTORY_START - timedelta(days=rng.randint(10, 20))
        while day <= HISTORY_END:
            po_number += 1
            order_id = ids.take("C_Order")[0]
            ordered_at = datetime.combine(day, datetime.min.time()) + timedelta(hours=10)
            lead = max(p.lead_days for p in vendor_products)
            arrives = day + timedelta(days=lead)
            status = "CO" if day < HISTORY_END - timedelta(days=4) else "DR"
            is_received = status == "CO" and arrives <= HISTORY_END
            total = Decimal(0)
            line_no = 0
            for product in vendor_products:
                if product.id in retired or rng.random() < 0.25:
                    continue
                line_no += 1
                monthly = shipped[product.id] / 11.5
                qty = max(100, int(-(-monthly * rng.uniform(0.9, 1.4) // 50) * 50))
                total += product.cost * qty
                if is_received:
                    received[product.id] += qty
                line_rows.append({
                    **_audit(ordered_at), "c_orderline_id": ids.take("C_OrderLine")[0], "ad_org_id": ORG,
                    "c_order_id": order_id, "line": line_no * 10, "dateordered": ordered_at,
                    "datepromised": datetime.combine(arrives, datetime.min.time()),
                    "datedelivered": datetime.combine(arrives, datetime.min.time()) if is_received else None,
                    "m_warehouse_id": WAREHOUSE, "c_uom_id": UOM_EACH, "c_currency_id": USD,
                    "c_tax_id": TAX_STANDARD, "m_product_id": product.id, "c_bpartner_id": vendor_id,
                    "c_bpartner_location_id": vendor_location[vendor_id], "qtyentered": qty, "qtyordered": qty,
                    "qtydelivered": qty if is_received else 0, "qtyinvoiced": qty if is_received else 0,
                    "qtyreserved": 0 if is_received else qty, "priceentered": product.cost,
                    "priceactual": product.cost, "pricelist": product.cost, "pricelimit": product.cost,
                    "linenetamt": product.cost * qty, "processed": "Y" if status == "CO" else "N",
                    "m_attributesetinstance_id": 0,
                })
            order_rows.append({
                **_audit(ordered_at), "c_order_id": order_id, "ad_org_id": ORG, "documentno": f"FH-PO-{po_number:05d}",
                "description": "Synthetic placeholder purchase order", "issotrx": "N", "docstatus": status,
                "docaction": "CL" if status == "CO" else "CO", "processed": "Y" if status == "CO" else "N",
                "isapproved": "Y" if status == "CO" else "N", "isdelivered": "Y" if is_received else "N",
                "c_doctype_id": DOCTYPE_PO if status == "CO" else 0, "c_doctypetarget_id": DOCTYPE_PO,
                "dateordered": ordered_at, "datepromised": datetime.combine(arrives, datetime.min.time()),
                "dateacct": ordered_at, "c_bpartner_id": vendor_id,
                "c_bpartner_location_id": vendor_location[vendor_id], "bill_bpartner_id": vendor_id,
                "bill_location_id": vendor_location[vendor_id], "c_currency_id": USD, "paymentrule": "P",
                "c_paymentterm_id": TERM_NET30, "invoicerule": "I", "deliveryrule": "A", "freightcostrule": "I",
                "deliveryviarule": "D", "priorityrule": "5", "m_warehouse_id": WAREHOUSE,
                "m_pricelist_id": PRICE_LIST_PURCHASE, "salesrep_id": SALES_REP, "totallines": total,
                "grandtotal": total,
            })
            day += timedelta(days=rng.randint(26, 38))

    _insert(conn, "c_order", order_rows, uu="c_order_uu")
    _insert(conn, "c_orderline", line_rows, uu="c_orderline_uu")

    # On-hand stock and reorder points. Opening stock keeps history consistent; a few
    # products are deliberately left near or below their reorder point.
    storage, replenish = [], []
    window = 60
    recent_cutoff = HISTORY_END - timedelta(days=window)
    recent: dict[int, int] = {p.id: 0 for p in products}
    for row in line_rows:
        order_date = row["dateordered"].date()
        if row["c_bpartner_id"] not in vendor_location and order_date > recent_cutoff:
            recent[row["m_product_id"]] += row["qtyordered"]
    for index, p in enumerate(products):
        daily = recent[p.id] / window
        reorder_point = max(20, int(round(daily * (p.lead_days + 7))))
        level_max = max(reorder_point * 3, 300)
        opening = int(shipped[p.id] / 11.5 * rng.uniform(1.0, 2.0)) + 40
        onhand = max(0, opening + received[p.id] - shipped[p.id])
        if index % 7 == 3:
            onhand = int(reorder_point * rng.uniform(0.2, 0.9))  # below reorder point
        if p.id in retired:
            onhand = 0
        storage.append({
            "ad_client_id": CLIENT, "ad_org_id": ORG, "isactive": "Y", "created": seeded_at, "createdby": USER,
            "updated": seeded_at, "updatedby": USER, "m_attributesetinstance_id": 0, "m_locator_id": LOCATOR,
            "m_product_id": p.id, "qtyonhand": onhand, "datematerialpolicy": datetime(2025, 11, 20),
        })
        if p.id not in retired:
            replenish.append({
                **_audit(seeded_at), "m_product_id": p.id, "m_warehouse_id": WAREHOUSE, "ad_org_id": ORG,
                "replenishtype": "1", "level_min": reorder_point, "level_max": level_max,
            })
    _insert(conn, "m_storageonhand", storage, uu="m_storageonhand_uu")
    _insert(conn, "m_replenish", replenish, uu="m_replenish_uu")

    return {
        "products": len(products),
        "retired_products": len(retired),
        "vendors": len(vendor_ids),
        "customers": len(customers),
        "orders": len(order_rows),
        "order_lines": len(line_rows),
        "storage_rows": len(storage),
        "replenish_rows": len(replenish),
    }
