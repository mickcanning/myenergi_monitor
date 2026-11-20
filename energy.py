"""Home energy use over a day, week, month or year: per-bucket kWh plus the running total.

Periods and buckets follow local time (TZ, default Europe/London), so a day is midnight to midnight on
the clock and BST changes give 46- or 50-bucket days. Stored data is summed per UTC half-hour first,
which lines up with local buckets because UK offsets are whole hours.
"""
import calendar
import math
from bisect import bisect_right
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import estimate

PERIODS = ("day", "week", "month", "year")
# Where the energy went, bottom to top of the Energy usage chart: (key, label)
USES = ("home", "eddi", "charge", "zappi", "export")     # this order keeps neighbouring colours distinct
USE_LABELS = {"home": "Home", "eddi": "Hot water", "zappi": "Car", "charge": "Battery charging",
              "export": "Exported"}
BUCKET_WORD = {"day": "half hour", "week": "day", "month": "day", "year": "month"}


def _local_midnight(d, tz):
    return datetime.combine(d, time(0), tz)


def period_bounds(period, day, tz):
    """(start date, end date exclusive, title) of the period containing `day`."""
    if period == "day":
        return day, day + timedelta(days=1), day.strftime("%A %-d %B %Y")
    if period == "week":
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=7)
        last = end - timedelta(days=1)
        if start.year != last.year:
            title = f"{start:%-d %b %Y} – {last:%-d %b %Y}"
        elif start.month != last.month:
            title = f"{start:%-d %b} – {last:%-d %b %Y}"
        else:
            title = f"{start:%-d} – {last:%-d %b %Y}"
        return start, end, "Week of " + title
    if period == "month":
        start = day.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
        return start, end, start.strftime("%B %Y")
    start = day.replace(month=1, day=1)
    return start, start.replace(year=start.year + 1), str(start.year)


def shift(period, day, step):
    """The same period `step` periods earlier (-1) or later (+1)."""
    if period == "day":
        return day + timedelta(days=step)
    if period == "week":
        return day + timedelta(days=7 * step)
    if period == "month":
        m = day.month - 1 + step
        y, m = day.year + m // 12, m % 12 + 1
        return date(y, m, min(day.day, calendar.monthrange(y, m)[1]))
    y = day.year + step
    return date(y, day.month, min(day.day, calendar.monthrange(y, day.month)[1]))


def buckets(period, start, end, tz):
    """[(start_utc_ts, end_utc_ts, short label, long label)] covering the period in local time."""
    out = []
    if period == "day":
        t, stop = _local_midnight(start, tz).astimezone(timezone.utc), \
            _local_midnight(end, tz).astimezone(timezone.utc)
        while t < stop:
            lo = t.astimezone(tz)
            hi = (t + timedelta(minutes=30)).astimezone(tz)
            out.append((t.timestamp(), t.timestamp() + 1800, lo.strftime("%H:%M"),
                        f"{lo:%H:%M}–{hi:%H:%M}"))
            t += timedelta(minutes=30)
    elif period in ("week", "month"):
        d = start
        while d < end:
            a, b = _local_midnight(d, tz), _local_midnight(d + timedelta(days=1), tz)
            short = d.strftime("%a") if period == "week" else str(d.day)
            out.append((a.timestamp(), b.timestamp(), short, d.strftime("%a %-d %b")))
            d += timedelta(days=1)
    else:
        for m in range(1, 13):
            a = _local_midnight(date(start.year, m, 1), tz)
            b = _local_midnight(date(start.year + (m == 12), m % 12 + 1, 1), tz)
            out.append((a.timestamp(), b.timestamp(), a.strftime("%b"), a.strftime("%B %Y")))
    return out


def summarise(store, period, day, tz, now_ts):
    """Per-bucket and cumulative kWh for the period containing `day`: the best estimate (estimate.py), its
    approximate 95% range, and the plain sampled figure for comparison."""
    start, end, title = period_bounds(period, day, tz)
    bks = buckets(period, start, end, tz)
    lo, hi = bks[0][0], bks[-1][1]
    halves = estimate.half_hours(store, lo, min(hi, now_ts + 1800), now_ts)
    starts = [b[0] for b in bks]
    n = len(bks)
    arrs = [[0.0] * n for _ in estimate.FIELDS]
    kwh, from_grid, var, covered, measured, sampled, imp, exp_, g_batt, g_dev, eddi, zappi, charge = arrs
    for hh, vals in halves.items():
        i = bisect_right(starts, hh) - 1
        if 0 <= i < n:
            for arr, v in zip(arrs, vals):
                arr[i] += v or 0
    use_cum = {k: 0.0 for k in USES}
    rows, total, total_grid, total_var, total_sampled = [], 0.0, 0.0, 0.0, 0.0
    imp_t = {"imp": 0.0, "exp": 0.0, "home": 0.0, "batt": 0.0, "dev": 0.0}
    for k, (a, b, short, long) in enumerate(bks):
        e, g = kwh[k], min(max(from_grid[k], 0.0), max(kwh[k], 0.0))
        future = a >= now_ts
        span = max(1.0, min(b, now_ts) - a)                       # only the part of the bucket that has happened
        uses_k = {"home": e, "eddi": eddi[k], "charge": charge[k], "zappi": zappi[k], "export": exp_[k]}
        # where this slot's grid import went; anything we couldn't attribute (no solar & battery reading)
        dest = {"home": g, "batt": g_batt[k], "dev": g_dev[k]}
        unattr = max(imp[k] - sum(dest.values()), 0.0)
        if not future:
            total += e
            total_grid += g
            total_var += var[k]
            total_sampled += sampled[k]
            for key, v in (("imp", imp[k]), ("exp", exp_[k]), *dest.items()):
                imp_t[key] += v
            for key, v in uses_k.items():
                use_cum[key] += v
        rows.append({"start": a, "end": b, "label": short, "long": long, "kwh": None if future else e,
                     "grid": None if future else g, "solar": None if future else max(e - g, 0.0),
                     "err": None if future else estimate.ci95(var[k]),
                     "sampled": None if future else sampled[k],
                     "cum": None if future else total, "cum_err": None if future else estimate.ci95(total_var),
                     "coverage": None if future else min(1.0, covered[k] / span),
                     "measured": None if future else min(1.0, measured[k] / span),
                     "uses": None if future else uses_k,
                     "uses_cum": None if future else dict(use_cum),
                     "imp": None if future else imp[k], "exp": None if future else exp_[k],
                     "imp_home": None if future else g, "imp_batt": None if future else g_batt[k],
                     "imp_dev": None if future else g_dev[k], "imp_unattr": None if future else unattr,
                     "current": a <= now_ts < b})
    past = [r for r in rows if r["kwh"] is not None]
    spans = [min(r["end"], now_ts) - r["start"] for r in past]
    span = sum(spans)
    busiest = max(past, key=lambda r: r["kwh"]) if past and max(r["kwh"] for r in past) > 0 else None
    self_pct = round(100 * (1 - total_grid / total)) if total > 0.005 else None
    return {"period": period, "title": title, "start": start, "end": end, "rows": rows, "total": total,
            "total_err": estimate.ci95(total_var), "total_sampled": total_sampled,
            "total_grid": total_grid, "total_solar": max(total - total_grid, 0.0), "self_pct": self_pct,
            "coverage": sum(r["coverage"] * w for r, w in zip(past, spans)) / span if span else 0.0,
            "measured": sum(r["measured"] * w for r, w in zip(past, spans)) / span if span else 0.0,
            "busiest": busiest, "in_progress": lo <= now_ts < hi, "future": lo > now_ts,
            "uses": use_cum, "uses_total": sum(use_cum.values()),
            "imports": {**imp_t, "unattr": max(imp_t["imp"] - imp_t["home"] - imp_t["batt"] - imp_t["dev"], 0.0)},
            "bucket_word": BUCKET_WORD[period]}


def _nice_max(v):
    """The lowest round axis top above v whose step (1, 2, 2.5 or 5 x 10^n) gives 3-5 ticks."""
    if v <= 0:
        return 1.0, 0.25
    target = v * 1.04
    best = None
    e = math.floor(math.log10(target)) - 2
    for exp in (10 ** k for k in range(e, e + 3)):
        for m in (1, 2, 2.5, 5):
            step = m * exp
            n = math.ceil(target / step)
            if 3 <= n <= 5 and (best is None or n * step < best[0]):
                best = (n * step, step)
    return best


def chart(s):
    """Geometry for two stacked panels sharing the x axis: the running total (top) and kWh per bucket
    (bottom). Plots use a 1000 x 100 viewBox stretched to fit; labels are placed in percent so the
    page can draw them as normal-sized HTML text at any width."""
    rows = s["rows"]
    n = len(rows)
    col = 1000 / n

    # energy usage per slot, stacked bottom to top as steps (each slot holds its own total, so a layer drops
    # back to zero when that use stops); home is always shown, the rest only if they happened
    shown = [k for k in USES if k == "home" or s["uses"][k] >= 0.005]
    past = [r for r in rows if r["uses"] is not None]
    tops = [sum(r["uses"][k] for k in shown) for r in past]
    use_max, use_step = _nice_max(max(tops + [0]))
    cum_max, cum_step = _nice_max(max([(r["cum"] or 0) + (r["cum_err"] or 0) for r in rows] + [0]))
    bar_max, bar_step = _nice_max(max([(r["kwh"] or 0) + (r["err"] or 0) for r in rows] + [0]))

    def y(v, mx):
        return round(100 - v / mx * 100, 2)

    pts = [(0, 100)]                     # running total: 0 at the start, then the end of each bucket so far
    for i, r in enumerate(rows):
        if r["cum"] is None:
            break
        pts.append((round((i + 1) * col, 2), y(r["cum"], cum_max)))
    line = " ".join(f"{x},{yy}" for x, yy in pts)
    layers = []
    base = [0.0] * len(past)
    for k in shown:
        top = [bb + r["uses"][k] for bb, r in zip(base, past)]
        if past:
            up, down = [], []
            for i, (t, bb) in enumerate(zip(top, base)):
                x0, x1 = round(i * col, 2), round((i + 1) * col, 2)
                up += [f"{x0},{y(t, use_max)}", f"{x1},{y(t, use_max)}"]
                down += [f"{x0},{y(bb, use_max)}", f"{x1},{y(bb, use_max)}"]     # reversed below
            layers.append({"key": k, "label": USE_LABELS[k], "total": s["uses"][k],
                           "area": "M" + " L".join(up + list(reversed(down))) + " Z",
                           "line": " ".join(up)})
        base = top
    area = ("M0,100 L" + " L".join(f"{x},{yy}" for x, yy in pts) + f" L{pts[-1][0]},100 Z") if len(pts) > 1 else ""
    # likely range of the running total (approximate 95%), as a band around the line
    upper, lower = [(0, 100)], [(0, 100)]
    for i, r in enumerate(rows):
        if r["cum"] is None:
            break
        x = round((i + 1) * col, 2)
        upper.append((x, y(r["cum"] + r["cum_err"], cum_max)))
        lower.append((x, y(max(r["cum"] - r["cum_err"], 0), cum_max)))
    band = ("M" + " L".join(f"{x},{yy}" for x, yy in upper) + " L" +
            " L".join(f"{x},{yy}" for x, yy in reversed(lower)) + " Z") if len(upper) > 1 else ""

    pad = min(col * 0.14, 6)             # a small surface gap between neighbouring bars
    seam = 1.6                           # and between the two parts of a bar (about 2px at panel height)
    bars = []
    for i, r in enumerate(rows):
        if not r["kwh"]:
            continue
        x, w = round(i * col + pad / 2, 2), round(col - pad, 2)
        partial = r["coverage"] is not None and r["coverage"] < .9
        y_solar, y_top = y(r["solar"], bar_max), y(r["kwh"], bar_max)
        parts = []
        if r["solar"] > 0:
            parts.append({"cls": "solar", "y": y_solar, "h": round(100 - y_solar, 2)})
        if r["grid"] > 0 and y_solar - y_top >= 1:           # skip slivers too thin to see
            bottom = y_solar - (seam if parts and y_solar - y_top > seam * 2 else 0)
            parts.append({"cls": "grid", "y": y_top, "h": round(bottom - y_top, 2)})
        err = None
        if r["err"] and (y(r["kwh"] - r["err"], bar_max) - y(r["kwh"] + r["err"], bar_max)) >= 2:
            err = {"x": round(i * col + col / 2, 2), "y1": y(r["kwh"] + r["err"], bar_max),
                   "y2": y(max(r["kwh"] - r["err"], 0), bar_max), "w": round(min(col * 0.3, 12), 2)}
        bars.append({"x": x, "w": w, "partial": partial, "parts": parts, "err": err})

    xlabels = []
    for i, r in enumerate(rows):
        if s["period"] == "day":
            show, pos = i % 6 == 0, i * col
        elif s["period"] == "month":
            show, pos = i == 0 or (i + 1) % 5 == 0, (i + .5) * col
        else:
            show, pos = True, (i + .5) * col
        if show:
            minor = s["period"] == "day" and i % 12 != 0     # 03:00, 09:00...: hidden on narrow screens
            xlabels.append({"x": round(pos / 10, 2), "text": r["label"], "minor": minor})

    # grid import per slot, stacked by where it went (bottom to top)
    imp_max, imp_step = _nice_max(max([r["imp"] or 0 for r in rows] + [0]))
    imp_bars = []
    for i, r in enumerate(rows):
        if not r["imp"]:
            continue
        x, w = round(i * col + pad / 2, 2), round(col - pad, 2)
        parts, base = [], 0.0
        for cls, key in (("home", "imp_home"), ("batt", "imp_batt"), ("dev", "imp_dev"), ("unattr", "imp_unattr")):
            v = r[key] or 0
            y_lo, y_hi = y(base, imp_max), y(base + v, imp_max)
            base += v
            if y_lo - y_hi < 1:                          # too thin to see
                continue
            bottom = y_lo - (seam if parts and y_lo - y_hi > seam * 2 else 0)
            parts.append({"cls": cls, "y": y_hi, "h": round(bottom - y_hi, 2)})
        imp_bars.append({"x": x, "w": w, "parts": parts})

    def ticks(mx, step):
        out, v = [], step
        while v <= mx + 1e-9:
            out.append({"y": y(v, mx), "text": f"{v:g}"})
            v += step
        return out

    last = pts[-1] if len(pts) > 1 else None
    return {"line": line, "area": area, "band": band, "bars": bars, "xlabels": xlabels, "n": n,
            "layers": layers, "use_ticks": ticks(use_max, use_step),
            "cum_ticks": ticks(cum_max, cum_step), "bar_ticks": ticks(bar_max, bar_step),
            "imp_bars": imp_bars, "imp_ticks": ticks(imp_max, imp_step),
            "last": {"x": last[0] / 10, "y": last[1]} if last else None,
            "partial": any(b["partial"] for b in bars),
            "shown": [{"key": k, "label": USE_LABELS[k]} for k in shown],
            "hover": [{"l": r["long"], "k": None if r["kwh"] is None else round(r["kwh"], 3),
                       "i": None if r["imp"] is None else [round(r[k] or 0, 3) for k in
                                                           ("imp", "exp", "imp_home", "imp_batt", "imp_dev", "imp_unattr")],
                       "e": None if r["err"] is None else round(r["err"], 3),
                       "m": None if r["sampled"] is None else round(r["sampled"], 3),
                       "ce": None if r["cum_err"] is None else round(r["cum_err"], 3),
                       "ms": None if r["measured"] is None else round(r["measured"] * 100),
                       "s": None if r["solar"] is None else round(r["solar"], 3),
                       "g": None if r["grid"] is None else round(r["grid"], 3),
                       "c": None if r["cum"] is None else round(r["cum"], 3),
                       "u": None if r["uses"] is None else [round(r["uses"][k], 3) for k in shown],
                       "p": None if r["coverage"] is None else round(r["coverage"] * 100)} for r in rows]}
