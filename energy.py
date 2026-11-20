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

PERIODS = ("day", "week", "month", "year")
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
    """Per-bucket and cumulative kWh for the period containing `day`."""
    start, end, title = period_bounds(period, day, tz)
    bks = buckets(period, start, end, tz)
    lo, hi = bks[0][0], bks[-1][1]
    halves = store.energy_by_half_hour(lo, min(hi, now_ts + 1800))
    starts = [b[0] for b in bks]
    kwh = [0.0] * len(bks)
    from_grid = [0.0] * len(bks)
    covered = [0.0] * len(bks)
    for hh, (e, g, cov) in halves.items():
        i = bisect_right(starts, hh) - 1
        if 0 <= i < len(bks):
            kwh[i] += e or 0
            from_grid[i] += g or 0
            covered[i] += cov or 0
    rows, total, total_grid = [], 0.0, 0.0
    for (a, b, short, long), e, g, cov in zip(bks, kwh, from_grid, covered):
        future = a >= now_ts
        span = max(1.0, min(b, now_ts) - a)                       # only the part of the bucket that has happened
        coverage = None if future else min(1.0, cov / span)
        g = min(max(g, 0.0), max(e, 0.0))
        if not future:
            total += e
            total_grid += g
        rows.append({"start": a, "end": b, "label": short, "long": long, "kwh": None if future else e,
                     "grid": None if future else g, "solar": None if future else max(e - g, 0.0),
                     "cum": None if future else total, "coverage": coverage,
                     "current": a <= now_ts < b})
    past = [r for r in rows if r["kwh"] is not None]
    seen = sum(r["coverage"] * (min(r["end"], now_ts) - r["start"]) for r in past)
    span = sum(min(r["end"], now_ts) - r["start"] for r in past)
    busiest = max(past, key=lambda r: r["kwh"]) if past and max(r["kwh"] for r in past) > 0 else None
    self_pct = round(100 * (1 - total_grid / total)) if total > 0.005 else None
    return {"period": period, "title": title, "start": start, "end": end, "rows": rows, "total": total,
            "total_grid": total_grid, "total_solar": max(total - total_grid, 0.0), "self_pct": self_pct,
            "coverage": (seen / span) if span else 0.0, "busiest": busiest,
            "in_progress": lo <= now_ts < hi, "future": lo > now_ts, "bucket_word": BUCKET_WORD[period]}


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

    cum_max, cum_step = _nice_max(max([r["cum"] or 0 for r in rows] + [0]))
    bar_max, bar_step = _nice_max(max([r["kwh"] or 0 for r in rows] + [0]))

    def y(v, mx):
        return round(100 - v / mx * 100, 2)

    pts = [(0, 100)]                     # running total: 0 at the start, then the end of each bucket so far
    for i, r in enumerate(rows):
        if r["cum"] is None:
            break
        pts.append((round((i + 1) * col, 2), y(r["cum"], cum_max)))
    line = " ".join(f"{x},{yy}" for x, yy in pts)
    area = ("M0,100 L" + " L".join(f"{x},{yy}" for x, yy in pts) + f" L{pts[-1][0]},100 Z") if len(pts) > 1 else ""

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
        bars.append({"x": x, "w": w, "partial": partial, "parts": parts})

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

    def ticks(mx, step):
        out, v = [], step
        while v <= mx + 1e-9:
            out.append({"y": y(v, mx), "text": f"{v:g}"})
            v += step
        return out

    last = pts[-1] if len(pts) > 1 else None
    return {"line": line, "area": area, "bars": bars, "xlabels": xlabels, "n": n,
            "cum_ticks": ticks(cum_max, cum_step), "bar_ticks": ticks(bar_max, bar_step),
            "last": {"x": last[0] / 10, "y": last[1]} if last else None,
            "partial": any(b["partial"] for b in bars),
            "hover": [{"l": r["long"], "k": None if r["kwh"] is None else round(r["kwh"], 3),
                       "s": None if r["solar"] is None else round(r["solar"], 3),
                       "g": None if r["grid"] is None else round(r["grid"], 3),
                       "c": None if r["cum"] is None else round(r["cum"], 3),
                       "p": None if r["coverage"] is None else round(r["coverage"] * 100)} for r in rows]}
