from decimal import Decimal, Context, ROUND_HALF_UP, MAX_EMAX, MIN_EMIN

_INT4_MIN = -2147483648
_INT4_MAX = 2147483647
_NUMERIC_MAX_RESULT_SCALE = 2000


def _pg_int(v):
    """PL/pgSQL assignment numeric -> int4: round half away from zero, range-checked."""
    if v is None:
        return None
    d = Decimal(v)
    if d.is_nan() or d.is_infinite():
        raise ValueError("integer out of range")
    ctx = Context(prec=max(len(d.as_tuple().digits) + 10, 50), Emax=MAX_EMAX, Emin=MIN_EMIN,
                  rounding=ROUND_HALF_UP)
    i = int(d.quantize(Decimal(1), context=ctx))
    if i < _INT4_MIN or i > _INT4_MAX:
        raise ValueError("integer out of range")
    return i


def _nz(d):
    # PostgreSQL numeric has no negative zero
    if isinstance(d, Decimal) and d.is_finite() and d == 0 and d.is_signed():
        return d.copy_abs()
    return d


def _pg_round(amount, scale):
    if scale is None:
        return None
    if amount.is_nan() or amount.is_infinite():
        return amount
    scale = max(scale, -_NUMERIC_MAX_RESULT_SCALE)
    scale = min(scale, _NUMERIC_MAX_RESULT_SCALE)
    t = amount.as_tuple()
    prec = len(t.digits) + abs(t.exponent) + abs(scale) + 50
    ctx = Context(prec=prec, Emax=MAX_EMAX, Emin=MIN_EMIN, rounding=ROUND_HALF_UP)
    q = amount.quantize(Decimal(1).scaleb(-scale, context=ctx), context=ctx)
    if scale < 0:
        q = q.quantize(Decimal(1), context=ctx)
    return _nz(q)


def currencyround(p_amount, p_curto_id, p_costing, *, data):
    if p_amount is None or p_curto_id is None:
        return p_amount if p_amount is None else _nz(p_amount)

    rows = data.select('c_currency', c_currency_id=p_curto_id)
    rows = [r for r in rows if r.get('c_currency_id') is not None
            and Decimal(r['c_currency_id']) == Decimal(p_curto_id)]
    std_vals = [r['stdprecision'] for r in rows if r.get('stdprecision') is not None]
    cost_vals = [r['costingprecision'] for r in rows if r.get('costingprecision') is not None]
    std_max = max(std_vals) if std_vals else None
    cost_max = max(cost_vals) if cost_vals else None

    v_std = _pg_int(std_max)
    v_cost = _pg_int(cost_max)

    if v_std is None:
        return _nz(p_amount)

    if p_costing is not None and p_costing == 'Y':
        return _pg_round(p_amount, v_cost)

    return _pg_round(p_amount, v_std)
