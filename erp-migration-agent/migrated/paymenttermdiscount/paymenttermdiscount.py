import datetime
import decimal
from decimal import Decimal, ROUND_HALF_UP, ROUND_HALF_EVEN

_CTX = decimal.Context(prec=2000, rounding=ROUND_HALF_UP, Emin=-999999, Emax=999999)


def _dscale(d):
    e = d.as_tuple().exponent
    if not isinstance(e, int):
        return 0
    return max(0, -e)


def _nbase_weight_first(d):
    # PostgreSQL NBASE=10000 weight and first digit of a numeric
    if d.is_zero():
        return 0, 0
    a = d.copy_abs()
    e = a.adjusted()
    w = e // 4
    first = int(_CTX.divide_int(a, _CTX.power(Decimal(10000), w)) if w >= 0
                else _CTX.divide_int(_CTX.multiply(a, _CTX.power(Decimal(10000), -w)), Decimal(1)))
    return w, first


def _pg_div(n1, n2):
    # emulate numeric_div result scale selection (select_div_scale)
    w1, f1 = _nbase_weight_first(n1)
    w2, f2 = _nbase_weight_first(n2)
    qweight = w1 - w2
    if f1 <= f2:
        qweight -= 1
    rscale = 16 - qweight * 4
    rscale = max(rscale, _dscale(n1), _dscale(n2))
    rscale = max(rscale, 0)
    rscale = min(rscale, 1000)
    q = _CTX.divide(n1, n2)
    return q.quantize(Decimal(1).scaleb(-rscale), rounding=ROUND_HALF_UP, context=_CTX)


def _adddays(dt, days):
    if dt is None or days is None:
        return None
    seconds = _CTX.multiply(Decimal(86400), days)
    us = _CTX.multiply(seconds, Decimal(1000000)).to_integral_value(rounding=ROUND_HALF_EVEN)
    utc = dt.astimezone(datetime.timezone.utc)
    return utc + datetime.timedelta(microseconds=int(us))


def _trunc_date(dt, tz):
    if dt is None:
        return None
    return dt.astimezone(tz).date()


def _client_country(client_id, data):
    best = None
    if client_id is not None:
        for cl in data.select('ad_client', ad_client_id=client_id):
            lang = cl.get('ad_language')
            if lang is None:
                continue
            for l in data.select('ad_language', ad_language=lang):
                cc = l.get('countrycode')
                if cc is None:
                    continue
                for co in data.select('c_country', countrycode=cc):
                    cid = co.get('c_country_id')
                    if cid is not None and (best is None or cid > best):
                        best = cid
    return Decimal(100) if best is None else best


def _next_business_day(d, client_id, data):
    # d is a date (LA) or None
    if d is None:
        return None
    one = datetime.timedelta(days=1)
    while True:
        wd = d.weekday()
        if wd == 5:
            d = d + datetime.timedelta(days=2)
        elif wd == 6:
            d = d + one
        is_holiday = False
        country = _client_country(client_id, data)
        rows = []
        if client_id is not None:
            start = datetime.datetime(d.year, d.month, d.day)
            for r in data.select('c_nonbusinessday', ad_client_id=client_id):
                if r.get('isactive') != 'Y':
                    continue
                d1 = r.get('date1')
                if d1 is None or d1 < start:
                    continue
                c = r.get('c_country_id')
                c = Decimal(0) if c is None else c
                if not (c == 0 or c == country):
                    continue
                rows.append(r)
            rows.sort(key=lambda r: r['date1'])
        for r in rows:
            if d != r['date1'].date():
                break
            d = d + one
            is_holiday = True
        if not is_holiday:
            break
    return d


def paymenttermdiscount(amount, currency_id, paymentterm_id, docdate, paydate, *, data):
    tz = data.session_tz
    v_currency = currency_id
    if v_currency is not None and v_currency == 0:
        best = None
        if paymentterm_id is not None:
            for pt in data.select('c_paymentterm', c_paymentterm_id=paymentterm_id):
                cl = pt.get('ad_client_id')
                if cl is None:
                    continue
                n_ci = len(data.select('ad_clientinfo', ad_client_id=cl))
                if n_ci == 0:
                    continue
                for s in data.select('c_acctschema', ad_client_id=cl):
                    c = s.get('c_currency_id')
                    if c is not None and (best is None or c > best):
                        best = c
        v_currency = Decimal(0) if best is None else best

    v_precision = None
    for r in data.select('c_currency', c_currency_id=v_currency):
        v_precision = r.get('stdprecision')

    v_min = None
    prec_int = None
    if v_precision is not None:
        prec_int = int(v_precision.to_integral_value(rounding=ROUND_HALF_UP))
        v_min = Decimal(1).scaleb(-prec_int)

    if amount is None or paymentterm_id is None or docdate is None:
        return Decimal(0)

    discount = Decimal(0)
    pay_d = _trunc_date(paydate, tz)
    for p in data.select('c_paymentterm', c_paymentterm_id=paymentterm_id):
        d1 = _trunc_date(_adddays(_adddays(docdate, p.get('discountdays')), p.get('gracedays')), tz)
        d2 = _trunc_date(_adddays(_adddays(docdate, p.get('discountdays2')), p.get('gracedays')), tz)
        if p.get('isnextbusinessday') == 'Y':
            d1 = _next_business_day(d1, p.get('ad_client_id'), data)
            d2 = _next_business_day(d2, p.get('ad_client_id'), data)
        if d1 is not None and pay_d is not None and d1 >= pay_d:
            pct = p.get('discount')
            discount = None if pct is None else _pg_div(_CTX.multiply(amount, pct), Decimal(100))
        elif d2 is not None and pay_d is not None and d2 >= pay_d:
            pct = p.get('discount2')
            discount = None if pct is None else _pg_div(_CTX.multiply(amount, pct), Decimal(100))

    if discount is not None and v_min is not None:
        if discount > -v_min and discount < v_min:
            discount = Decimal(0)

    if discount is None:
        discount = Decimal(0)
    if prec_int is None:
        return None
    res = discount.quantize(Decimal(1).scaleb(-prec_int), rounding=ROUND_HALF_UP, context=_CTX)
    if prec_int < 0:
        res = res.quantize(Decimal(1), context=_CTX)
    if res.is_zero():
        res = res.copy_abs()
    return res
