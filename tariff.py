"""Electricity tariff: named rates with a local time range and a price per kWh, covering all 24 hours.

Ranges may wrap past midnight (23:00-08:00) and may overlap: where they do, the cheapest rate applies (e.g.
"Nightboost" 02:00-04:00 inside "Night" 23:00-08:00). A range whose start equals its end covers the whole day.
Costs only apply to energy imported from the grid.
"""
import json
import re
from datetime import datetime, timezone

MINUTES = 1440
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$", "AUD": "$", "CAD": "$", "NZD": "$", "IEP": "€"}


def _minutes(hhmm):
    m = _TIME.match(hhmm or "")
    return None if not m else int(m.group(1)) * 60 + int(m.group(2))


def covered_minutes(start, end):
    """Minutes of the day [0, 1440) a range covers; wraps past midnight; start == end is the whole day."""
    if start == end:
        return range(MINUTES)
    if start < end:
        return range(start, end)
    return list(range(start, MINUTES)) + list(range(0, end))


def parse(currency, names, starts, ends, prices):
    """Build a tariff from form fields. Returns (tariff, errors); rows left completely blank are ignored."""
    errors, rates = [], []
    currency = (currency or "").strip()[:8]
    if not currency:
        errors.append("Enter a currency, e.g. £ or GBP.")
    for i, (n, s, e, p) in enumerate(zip(names, starts, ends, prices), 1):
        n, s, e, p = (n or "").strip()[:40], (s or "").strip(), (e or "").strip(), (p or "").strip()
        if not (n or s or e or p):
            continue
        label = n or f"Rate {i}"
        if not n:
            errors.append(f"{label}: give it a name.")
        if _minutes(s) is None or _minutes(e) is None:
            errors.append(f"{label}: times must be HH:MM, e.g. 23:00.")
        try:
            price = float(p)
            if price < 0 or price > 100:
                raise ValueError
        except ValueError:
            errors.append(f"{label}: the price must be a number per kWh, e.g. 0.245.")
            price = None
        rates.append({"name": n, "start": s, "end": e, "price": price})
    if not rates:
        errors.append("Add at least one rate.")
    tariff = {"currency": currency, "rates": rates}
    if not errors:
        gaps = gaps_in(tariff)
        if gaps:
            errors.append("The rates must cover all 24 hours. Not covered: " + ", ".join(gaps) + ".")
    return tariff, errors


def table(tariff):
    """[(price, rate index)] for each minute of the day, the cheapest rate where they overlap; None if not covered."""
    out = [None] * MINUTES
    for i, r in enumerate(tariff.get("rates", [])):
        s, e = _minutes(r["start"]), _minutes(r["end"])
        if s is None or e is None or r.get("price") is None:
            continue
        for m in covered_minutes(s, e):
            if out[m] is None or r["price"] < out[m][0]:
                out[m] = (r["price"], i)
    return out


def gaps_in(tariff):
    """Uncovered parts of the day as 'HH:MM-HH:MM' strings."""
    t, gaps, start = table(tariff), [], None
    for m in range(MINUTES + 1):
        free = m < MINUTES and t[m] is None
        if free and start is None:
            start = m
        elif not free and start is not None:
            gaps.append(f"{start // 60:02d}:{start % 60:02d}-{m // 60 % 24:02d}:{m % 60:02d}")
            start = None
    return gaps


def schedule(tariff):
    """The day as it will be charged: [(start 'HH:MM', end 'HH:MM', rate name, price)], in time order."""
    t, out, start = table(tariff), [], 0
    for m in range(1, MINUTES + 1):
        if m == MINUTES or t[m] != t[start]:
            if t[start] is not None:
                r = tariff["rates"][t[start][1]]
                out.append((f"{start // 60:02d}:{start % 60:02d}", f"{m // 60 % 24:02d}:{m % 60:02d}", r["name"], r["price"]))
            start = m
    return out


def money(value, currency, places=2):
    """'£3.42' for a symbol, '3.42 EUR' for a code (known codes get their symbol)."""
    sym = SYMBOLS.get(currency.upper(), currency) if currency else ""
    text = f"{abs(value):,.{places}f}"
    sign = "-" if value < 0 else ""
    if len(sym) <= 2 and not sym.isalpha():
        return f"{sign}{sym}{text}"
    return f"{sign}{text} {sym}".strip()


def costs(store, tariff, buckets, now_ts, tz):
    """Cost of grid import per bucket: [{cost, kwh, by_rate: {name: (kWh, cost)}}], from the hub's measured
    per-minute import, falling back to sampled grid power for minutes it hasn't sent yet."""
    if not tariff or not tariff.get("rates") or not buckets:
        return None
    t = table(tariff)
    names = [r["name"] for r in tariff["rates"]]
    lo, hi = buckets[0][0], min(buckets[-1][1], now_ts)
    out = [{"cost": 0.0, "kwh": 0.0, "by_rate": {}} for _ in buckets]
    first = store.first_flow_poll()        # like the Energy tab's figures, from the first recorded day
    if first is not None:
        lo = max(lo, first // 86400 * 86400)
    if hi <= lo:
        return out
    starts = [b[0] for b in buckets]
    measured = {ts: imp for ts, imp in store._conn().execute(
        "SELECT ts, imp FROM minutes WHERE ts >= ? AND ts < ? AND imp IS NOT NULL", (int(lo), int(hi)))}
    sampled = {}
    if measured:
        recent_from = max(measured) + 60
    else:
        recent_from = lo
    for poll, grid in store._conn().execute(
            "SELECT poll, grid FROM flows WHERE poll >= ? AND poll < ? AND grid IS NOT NULL",
            (int(recent_from), int(hi))):
        m = poll // 60 * 60
        sampled.setdefault(m, []).append(max(grid, 0))
    joules = dict(measured)
    for m, ws in sampled.items():
        if m not in joules:
            joules[m] = sum(ws) / len(ws) * 60
    offsets = {}
    from bisect import bisect_right
    for m, j in joules.items():
        hour = m // 3600
        off = offsets.get(hour)
        if off is None:
            off = offsets[hour] = int(datetime.fromtimestamp(hour * 3600, timezone.utc).astimezone(tz)
                                      .utcoffset().total_seconds())
        rate = t[((m + off) // 60) % MINUTES]
        i = bisect_right(starts, m) - 1
        if rate is None or not (0 <= i < len(out)):
            continue
        kwh = (j or 0) / 3.6e6
        o = out[i]
        o["kwh"] += kwh
        o["cost"] += kwh * rate[0]
        k, c = o["by_rate"].get(names[rate[1]], (0.0, 0.0))
        o["by_rate"][names[rate[1]]] = (k + kwh, c + kwh * rate[0])
    return out


def load(store):
    raw = store.get_setting("tariff")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def save(store, tariff):
    store.set_setting("tariff", json.dumps(tariff))
