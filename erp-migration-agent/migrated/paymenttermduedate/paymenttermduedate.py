import datetime
import calendar
from decimal import Decimal

_UTC = datetime.timezone.utc


def _from_local(naive, tz):
    # PostgreSQL resolves ambiguous / nonexistent local times by picking the
    # later UTC instant (standard-time interpretation for US zones).
    c0 = naive.replace(tzinfo=tz, fold=0).astimezone(_UTC)
    c1 = naive.replace(tzinfo=tz, fold=1).astimezone(_UTC)
    return max(c0, c1)


def _local_midnight(d, tz):
    return _from_local(datetime.datetime(d.year, d.month, d.day), tz)


def _trunc(ts, tz):
    return _local_midnight(ts.astimezone(tz).date(), tz)


def _adddays(ts, days):
    if ts is None or days is None:
        return None
    secs = float(Decimal(86400) * Decimal(days))
    us = round(secs * 1000000.0)
    return ts + datetime.timedelta(microseconds=us)


def _add_months(ts, months, tz):
    if ts is None or months is None:
        return None
    n = int(Decimal(months))  # trunc toward zero
    loc = ts.astimezone(tz).replace(tzinfo=None)
    total = loc.year * 12 + (loc.month - 1) + n
    y, m = divmod(total, 12)
    m += 1
    day = min(loc.day, calendar.monthrange(y, m)[1])
    return _from_local(loc.replace(year=y, month=m, day=day), tz)


def paymenttermduedate(paymentterm_id, docdate, *, data):
    tz = data.session_tz
    due = None if docdate is None else _trunc(docdate, tz)
    rows = data.select('c_paymentterm', c_paymentterm_id=paymentterm_id)
    for p in rows:
        isfixed = p.get('isduefixed')
        if isfixed is not None and isfixed.rstrip(' ') == 'Y':
            if docdate is None:
                due = None
                continue
            loc = docdate.astimezone(tz)
            first = _local_midnight(datetime.date(loc.year, loc.month, 1), tz)
            tr = _trunc(docdate, tz)
            diff = tr - first
            diff_us = (diff.days * 86400 + diff.seconds) * 1000000 + diff.microseconds
            q = abs(diff_us) // 86400000000
            nodays = q if diff_us >= 0 else -q
            fmd = p.get('fixmonthday')
            due = _adddays(first, None if fmd is None else Decimal(fmd) - 1)
            due = _add_months(due, p.get('fixmonthoffset'), tz)
            cutoff = p.get('fixmonthcutoff')
            if cutoff is not None and Decimal(nodays) > Decimal(cutoff):
                due = _add_months(due, 1, tz)
        else:
            if docdate is None:
                due = None
            else:
                due = _adddays(_trunc(docdate, tz), p.get('netdays'))
    if due is None:
        return None
    return due.astimezone(tz)
