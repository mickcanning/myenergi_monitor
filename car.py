"""Car charging (Zappi): charging sessions, what each cost, and the cost per day with a rolling average.

Energy into the car comes from the hub's measured per-minute history (recorder.minutes.zappi), falling back to
the sampled Zappi power for the last few minutes the hub hasn't sent yet.

A session is a run of minutes with the Zappi drawing at least ON_W; pauses of up to GAP_S (e.g. Eco+ waiting
for surplus solar, or the car balancing) stay within the same session. A session belongs to the local day it
started on, so a charge that runs past midnight counts in full on the day it began.

Only grid energy costs money. Each minute's grid share is the measured grid import divided by everything being
powered then (as on the Energy tab): grid import / (import - export + solar & battery output). Solar & battery
output comes from our samples of the Generation CT (mean of the minute's samples, else interpolated between
samples up to INTERP_S apart). When it isn't known (e.g. days backfilled from the hub's history before recording
began) the car is costed for as much of the minute's import as it could have taken, min(car, import): an upper
bound, so costs are never understated, while charging from surplus solar (no import) still costs nothing.
The grid share is priced at the tariff rate in force that minute (local time, cheapest rate where rates overlap).
"""
from bisect import bisect_left, bisect_right
from datetime import date, datetime, time as dtime, timedelta, timezone

import tariff as tariffs

ON_W = 100              # the Zappi counts as charging at this power or more
GAP_S = 30 * 60         # pauses up to this long stay in the same session
MIN_KWH = 0.05          # shorter blips aren't listed as sessions
INTERP_S = 1800         # interpolate the solar reading across sample gaps up to this long
AVG_DAYS = 30           # the rolling average covers this many days
J_PER_KWH = 3.6e6
PERIODS = ("month", "year")


def _minute_power(store, lo, hi):
    """{minute ts: (car W, grid share 0..1)} for minutes the car drew power in [lo, hi)."""
    conn = store._conn()
    out = {}
    rows = conn.execute("SELECT ts, imp, exp, zappi FROM minutes WHERE ts >= ? AND ts < ? AND zappi > 0",
                        (int(lo), int(hi))).fetchall()
    last_measured = conn.execute("SELECT MAX(ts) FROM minutes WHERE ts >= ? AND ts < ? AND imp IS NOT NULL",
                                 (int(lo), int(hi))).fetchone()[0]
    recent_from = lo if last_measured is None else last_measured + 60

    # solar & battery samples around the measured charging minutes, to split their energy into grid and solar
    sol_t, sol_v = [], []
    if rows:
        for poll, so in conn.execute("SELECT poll, solar FROM flows WHERE poll >= ? AND poll < ? AND solar IS NOT NULL "
                                     "ORDER BY poll", (rows[0][0] - INTERP_S, rows[-1][0] + 60 + INTERP_S)):
            sol_t.append(poll)
            sol_v.append(so)

    def solar_at(m):
        a, b = bisect_left(sol_t, m), bisect_left(sol_t, m + 60)
        if b > a:
            return sum(sol_v[a:b]) / (b - a)
        k = bisect_right(sol_t, m + 30)
        if 0 < k < len(sol_t) and sol_t[k] - sol_t[k - 1] <= INTERP_S:
            t0, t1, v0, v1 = sol_t[k - 1], sol_t[k], sol_v[k - 1], sol_v[k]
            return v0 + (v1 - v0) * (m + 30 - t0) / (t1 - t0)
        return None

    for ts, imp, exp_, zj in rows:
        w = zj / 60
        if w < 1:
            continue
        s = solar_at(ts)
        if imp is None:
            share = 1.0
        elif s is None:
            # no solar & battery reading (days before recording began): the grid can't have supplied the car
            # more than the whole import, so cost at most that
            share = min(1.0, imp / 60 / w)
        else:
            imp_w, exp_w = imp / 60, (exp_ or 0) / 60
            demand = imp_w - exp_w + max(s, 0)
            share = min(1.0, imp_w / demand) if demand > 0 else 0.0
        out[ts] = (w, share)

    # the last few minutes, before the hub's history has them: sampled power
    per = {}
    for poll, grid, solar, zappi in conn.execute(
            "SELECT poll, grid, solar, zappi FROM flows WHERE poll >= ? AND poll < ? AND zappi IS NOT NULL",
            (int(recent_from), int(hi))):
        m = poll // 60 * 60
        if m in out:
            continue
        zp = max(zappi or 0, 0)
        if grid is None:
            share = 1.0
        else:
            dem = grid + max(solar or 0, 0)
            share = min(1.0, max(grid, 0) / dem) if dem > 0 else 0.0
        per.setdefault(m, []).append((zp, share))
    for m, xs in per.items():
        w = sum(z for z, _ in xs) / len(xs)
        if w >= 1:
            g = sum(z * s for z, s in xs) / len(xs)
            out[m] = (w, g / w)
    return out


def sessions(store, lo, hi, tariff, tz):
    """Charging sessions starting in [lo, hi), oldest first: {start, end, kwh, grid, solar, cost, by_rate,
    peak_w, minutes}. cost/by_rate are None without a tariff."""
    pad = 12 * 3600                                      # catch sessions that started before lo and ran on
    mins = _minute_power(store, lo - pad, hi + pad)
    table = tariffs.table(tariff) if tariff and tariff.get("rates") else None
    names = [r["name"] for r in tariff["rates"]] if table else []
    offsets = {}

    def price(m):
        hour = m // 3600
        off = offsets.get(hour)
        if off is None:
            off = offsets[hour] = int(datetime.fromtimestamp(hour * 3600, timezone.utc).astimezone(tz)
                                      .utcoffset().total_seconds())
        return table[((m + off) // 60) % 1440]

    out, cur = [], None
    for m in sorted(mins):
        w, share = mins[m]
        if w < ON_W:
            continue
        if cur is None or m - cur["last"] > GAP_S:
            if cur:
                out.append(cur)
            cur = {"start": m, "last": m, "kwh": 0.0, "grid": 0.0, "cost": 0.0 if table else None,
                   "by_rate": {}, "peak_w": 0.0, "minutes": 0}
        # count everything the car drew from the session's first minute on, including low-power minutes
        # in the pauses
        for t in range(cur["last"] + 60, m, 60):
            if t in mins:
                _add(cur, t, *mins[t], price if table else None, names)
        _add(cur, m, w, share, price if table else None, names)
        cur["last"] = m
        cur["peak_w"] = max(cur["peak_w"], w)
        cur["minutes"] += 1
    if cur:
        out.append(cur)
    res = []
    for s in out:
        if not (lo <= s["start"] < hi) or s["kwh"] < MIN_KWH:
            continue
        s["end"] = s.pop("last") + 60
        s["solar"] = max(s["kwh"] - s["grid"], 0.0)
        s["local_start"] = datetime.fromtimestamp(s["start"], tz)
        s["local_end"] = datetime.fromtimestamp(s["end"], tz)
        res.append(s)
    return res


def _add(s, m, w, share, price, names):
    kwh = w * 60 / J_PER_KWH
    g = kwh * share
    s["kwh"] += kwh
    s["grid"] += g
    if price:
        p = price(m)
        if p is not None:
            s["cost"] += g * p[0]
            k, c = s["by_rate"].get(names[p[1]], (0.0, 0.0))
            s["by_rate"][names[p[1]]] = (k + g, c + g * p[0])


def _midnight(d, tz):
    return datetime.combine(d, dtime(0), tz).timestamp()


def period_bounds(period, day):
    if period == "year":
        start = day.replace(month=1, day=1)
        return start, start.replace(year=start.year + 1), str(start.year)
    start = day.replace(day=1)
    return start, (start + timedelta(days=32)).replace(day=1), start.strftime("%B %Y")


def shift(period, day, step):
    if period == "year":
        return day.replace(year=day.year + step, day=1)
    m = day.month - 1 + step
    return date(day.year + m // 12, m % 12 + 1, 1)


def summarise(store, period, day, tz, now_ts, tariff):
    """Days of the period with their sessions, daily cost/kWh, and the rolling AVG_DAYS-day average cost per
    day (over days with data; a day counts once recording or the hub's history covers it)."""
    start, end, title = period_bounds(period, day)
    today = datetime.fromtimestamp(now_ts, tz).date()
    first = _first_day(store, tz)
    # sessions from AVG_DAYS before the period, so the rolling average is right from its first day
    lo_day = start - timedelta(days=AVG_DAYS)
    sess = sessions(store, _midnight(lo_day, tz), _midnight(min(end, today + timedelta(days=1)), tz), tariff, tz) \
        if first is not None else []
    by_day = {}
    for s in sess:
        by_day.setdefault(s["local_start"].date(), []).append(s)

    days, d = [], lo_day
    window = []                                           # (date, cost, kwh) of the trailing days with data
    priced = bool(tariff and tariff.get("rates"))
    while d < end and d <= today:
        ss = by_day.get(d, [])
        has_data = first is not None and d >= first
        cost = sum(s["cost"] or 0 for s in ss) if priced else None
        kwh = sum(s["kwh"] for s in ss)
        grid = sum(s["grid"] for s in ss)
        if has_data:
            window.append((d, cost or 0.0, kwh))
        window = [w for w in window if w[0] > d - timedelta(days=AVG_DAYS)]
        n = len(window)
        avg = (sum(w[1] for w in window) / n) if n and priced else None
        avg_kwh = (sum(w[2] for w in window) / n) if n else None
        if d >= start:
            by_rate = {}
            for s in ss:
                for name, (k, c) in s["by_rate"].items():
                    tk, tc = by_rate.get(name, (0.0, 0.0))
                    by_rate[name] = (tk + k, tc + c)
            days.append({"date": d, "sessions": ss, "cost": cost, "kwh": kwh, "grid": grid,
                         "solar": max(kwh - grid, 0.0), "by_rate": by_rate, "has_data": has_data,
                         "avg": avg, "avg_kwh": avg_kwh, "avg_n": n, "today": d == today})
        d += timedelta(days=1)

    in_period = [s for x in days for s in x["sessions"]]
    data_days = [x for x in days if x["has_data"]]
    total_cost = sum(x["cost"] or 0 for x in days) if priced else None
    total_kwh = sum(x["kwh"] for x in days)
    total_grid = sum(x["grid"] for x in days)
    by_rate = {}
    for x in days:
        for name, (k, c) in x["by_rate"].items():
            tk, tc = by_rate.get(name, (0.0, 0.0))
            by_rate[name] = (tk + k, tc + c)
    return {
        "period": period, "title": title, "start": start, "end": end, "days": days,
        "currency": (tariff or {}).get("currency", ""), "priced": priced,
        "sessions": len(in_period), "charge_days": sum(1 for x in days if x["sessions"]),
        "data_days": len(data_days), "first_day": first,
        "total_cost": total_cost, "total_kwh": total_kwh, "total_grid": total_grid,
        "total_solar": max(total_kwh - total_grid, 0.0),
        "per_kwh": (total_cost / total_kwh) if priced and total_kwh >= 0.05 else None,
        "per_session": (total_cost / len(in_period)) if priced and in_period else None,
        "per_day": (total_cost / len(data_days)) if priced and data_days else None,
        "avg_now": next((x["avg"] for x in reversed(days) if x["has_data"]), None),
        "by_rate": by_rate,
        "in_progress": start <= today < end, "future": start > today,
    }


def _first_day(store, tz):
    """The first local day we have car data for: recording or the hub's history, whichever is earlier."""
    conn = store._conn()
    a = conn.execute("SELECT MIN(ts) FROM minutes").fetchone()[0]
    b = conn.execute("SELECT MIN(poll) FROM flows").fetchone()[0]
    ts = min([x for x in (a, b) if x is not None], default=None)
    return None if ts is None else datetime.fromtimestamp(ts, tz).date()


RATE_COLOURS = 5


def _nice(v):
    import math
    if v <= 0:
        return 1.0, 0.25
    raw = v / 4
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return step * math.ceil(v / step - 1e-9), step


def chart(s, tariff):
    """Daily bars stacked by tariff band (or kWh without a tariff), plus the rolling average line."""
    days = s["days"]
    n = max(len(days), 1)
    col = 1000 / n
    priced = s["priced"]
    names = []
    for r in (tariff or {}).get("rates", []):
        if r["name"] not in names:
            names.append(r["name"])
    names += [nm for nm in s["by_rate"] if nm not in names]
    band = {nm: (f"r{i}" if i < RATE_COLOURS else "rx") for i, nm in enumerate(names)}
    val = (lambda x: x["cost"] or 0) if priced else (lambda x: x["kwh"])
    avg = (lambda x: x["avg"]) if priced else (lambda x: x["avg_kwh"])
    top = max([val(x) for x in days] + [avg(x) or 0 for x in days] + [0])
    mx, step = _nice(top)

    def y(v):
        return round(100 - v / mx * 100, 2)

    pad = min(col * 0.18, 6)
    seam = 1.6
    bars = []
    for i, x in enumerate(days):
        v = val(x)
        if v <= 0:
            continue
        bx, w = round(i * col + pad / 2, 2), round(col - pad, 2)
        parts = []
        if priced:
            base = 0.0
            for nm in names:
                c = x["by_rate"].get(nm, (0, 0))[1]
                y_lo, y_hi = y(base), y(base + c)
                base += c
                if y_lo - y_hi < 0.3:
                    continue
                bottom = y_lo - (seam if parts and y_lo - y_hi > seam * 2 else 0)
                parts.append({"cls": band[nm], "y": y_hi, "h": round(bottom - y_hi, 2)})
        else:
            parts.append({"cls": "kwh", "y": y(v), "h": round(100 - y(v), 2)})
        bars.append({"x": bx, "w": w, "parts": parts})
    pts = [(round((i + .5) * col, 2), y(avg(x))) for i, x in enumerate(days) if avg(x) is not None]
    line = " ".join(f"{a},{b}" for a, b in pts)
    ticks, v = [], step
    while v <= mx + 1e-9:
        ticks.append({"y": y(v), "v": v})
        v += step
    xlabels = []
    for i, x in enumerate(days):
        d = x["date"]
        if s["period"] == "year":
            show, text = d.day == 1, d.strftime("%b")
        else:
            show, text = d.day == 1 or d.day % 5 == 0, str(d.day)
        if show:
            xlabels.append({"x": round((i + .5) * col / 10, 2) if s["period"] != "year" else round(i * col / 10, 2),
                            "text": text})
    hover = [{"l": x["date"].strftime("%a %-d %b"), "c": None if x["cost"] is None else round(x["cost"], 4),
              "k": round(x["kwh"], 3), "g": round(x["grid"], 3),
              "a": None if x["avg"] is None else round(x["avg"], 4),
              "ak": None if x["avg_kwh"] is None else round(x["avg_kwh"], 3), "an": x["avg_n"],
              "d": x["has_data"],
              "s": [[ss["local_start"].strftime("%H:%M"), ss["local_end"].strftime("%H:%M"), round(ss["kwh"], 2),
                     None if ss["cost"] is None else round(ss["cost"], 4)] for ss in x["sessions"]]}
             for x in days]
    rates = [{"name": nm, "cls": band[nm], "kwh": s["by_rate"][nm][0], "cost": s["by_rate"][nm][1]}
             for nm in names if nm in s["by_rate"]]
    return {"bars": bars, "line": line, "last": ({"x": pts[-1][0] / 10, "y": pts[-1][1]} if pts else None),
            "ticks": ticks, "xlabels": xlabels, "hover": hover, "rates": rates, "n": len(days)}
