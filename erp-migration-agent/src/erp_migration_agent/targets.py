"""The three legacy functions this repo ports, and how real inputs are sampled for each.

Sampling SQL returns one row per case with the function's arguments in order, typed as
the parameters are. Rows are ordered by ``md5`` of a seed row id, so the sample is spread
across the data and identical on every machine (the seed's ids are deterministic).
"""

from __future__ import annotations

from dataclasses import dataclass

from erp_sandbox.config import CLIENT_ID

# "2%10 Net 30", the only seeded payment term with an early-payment discount.
DISCOUNT_TERM_ID = 106


@dataclass(frozen=True)
class Target:
    name: str
    difficulty: str
    why: str
    sampling_sql: str


_SAMPLED_ORDERS = f"""
    SELECT row_number() OVER (ORDER BY md5(c_order_id::text)) AS n, o.*
    FROM c_order o
    WHERE ad_client_id = {CLIENT_ID} AND grandtotal <> 0
    ORDER BY md5(c_order_id::text)
    LIMIT %(limit)s
"""

TARGETS: dict[str, Target] = {
    "currencyround": Target(
        name="currencyround",
        difficulty="warm-up",
        why="One table read, NULL pass-through, a costing flag, and PostgreSQL rounding (half away from zero, fixed scale).",
        sampling_sql=f"""
            WITH o AS ({_SAMPLED_ORDERS}),
            r AS (SELECT row_number() OVER (ORDER BY c_conversion_rate_id) AS n, multiplyrate FROM c_conversion_rate),
            cur AS (
                SELECT row_number() OVER (ORDER BY c_currency_id) AS n, c_currency_id FROM (
                    SELECT DISTINCT ON (stdprecision, costingprecision) c_currency_id
                    FROM c_currency ORDER BY stdprecision, costingprecision, c_currency_id
                ) d
            )
            SELECT o.grandtotal * r.multiplyrate, cur.c_currency_id::numeric,
                   CASE WHEN o.n %% 2 = 0 THEN 'Y' ELSE 'N' END
            FROM o
            JOIN r ON r.n = 1 + (o.n %% (SELECT count(*) FROM r))
            JOIN cur ON cur.n = 1 + (o.n %% (SELECT count(*) FROM cur))
            ORDER BY o.n
        """,
    ),
    "paymenttermduedate": Target(
        name="paymenttermduedate",
        difficulty="branching",
        why="Fixed-day versus net-days branches, a month cutoff, month arithmetic and day truncation in the session time zone.",
        sampling_sql=f"""
            WITH o AS ({_SAMPLED_ORDERS}),
            t AS (SELECT row_number() OVER (ORDER BY c_paymentterm_id) AS n, c_paymentterm_id FROM c_paymentterm)
            SELECT CASE WHEN o.n %% 2 = 0 THEN o.c_paymentterm_id
                        ELSE (SELECT c_paymentterm_id FROM t WHERE t.n = 1 + (o.n %% (SELECT count(*) FROM t))) END,
                   o.dateordered::timestamptz
            FROM o ORDER BY o.n
        """,
    ),
    "paymenttermdiscount": Target(
        name="paymenttermdiscount",
        difficulty="multi-table",
        why="Reads payment terms, currencies, client accounting schemas and holidays (through nextbusinessday); discount windows, grace days and rounding.",
        sampling_sql=f"""
            WITH o AS ({_SAMPLED_ORDERS}),
            t AS (SELECT row_number() OVER (ORDER BY c_paymentterm_id) AS n, c_paymentterm_id FROM c_paymentterm),
            k AS (SELECT n, d FROM unnest(ARRAY[0, 9, 10, 11, 12, 30]) WITH ORDINALITY AS u(d, n))
            SELECT o.grandtotal, o.c_currency_id,
                   CASE WHEN o.n %% 2 = 0 THEN {DISCOUNT_TERM_ID}
                        ELSE (SELECT c_paymentterm_id FROM t WHERE t.n = 1 + (o.n %% (SELECT count(*) FROM t))) END,
                   o.dateordered::timestamptz,
                   o.dateordered::timestamptz
                     + make_interval(days => (SELECT d FROM k WHERE k.n = 1 + (o.n %% (SELECT count(*) FROM k))))
            FROM o ORDER BY o.n
        """,
    ),
}

WARM_UP = "currencyround"
