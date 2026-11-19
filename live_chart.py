"""Rolling CT-load history for the live dashboard, and the geometry for its small inline chart."""
import json
import math
from collections import deque
from datetime import datetime, timezone

MAX_POINTS = 480          # ~2 h at the default 15 s refresh
W, H = 1000, 100          # SVG viewBox; stretched to the card, strokes don't scale


class CtHistory:
    """In-memory, per device: one entry per *new* report (deduplicated by the device's report time)."""

    def __init__(self, maxlen=MAX_POINTS):
        self.maxlen = maxlen
        self.data = {}    # "kind:sno" -> deque[(epoch_s, {slot: watts})]

    def record(self, device):
        if not device["cts"] or not device["report_utc"]:
            return
        t = datetime.strptime(device["report_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        q = self.data.setdefault(f"{device['kind']}:{device['sno']}", deque(maxlen=self.maxlen))
        if q and q[-1][0] >= t:
            return        # same report as last time (a quiet Harvi): nothing new to plot
        q.append((t, {c["slot"]: _watts(c["watts"]) for c in device["cts"]}))

    def points(self, device):
        return list(self.data.get(f"{device['kind']}:{device['sno']}", ()))


def _watts(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return math.nan


def _or_none(v):
    return None if v is None or math.isnan(v) else v


def _nice_ticks(lo, hi, n=3):
    span = hi - lo
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.floor(lo / step) * step
    ticks, v = [], first
    while v <= hi + step * 1e-9:
        ticks.append(round(v, 6))
        v += step
    return ticks


def build(points, cts):
    """Chart geometry for the CTs currently shown on a card, or None if there's nothing to draw yet."""
    shown = [c for c in cts if c.get("shown")]
    if not shown:
        return None
    if len(points) < 2:
        return {"waiting": True, "count": len(points)}
    t0, t1 = points[0][0], points[-1][0]
    vals = [p[1].get(c["slot"]) for p in points for c in shown]
    vals = [v for v in vals if v is not None and not math.isnan(v)]
    lo, hi = min(vals + [0.0]), max(vals + [0.0])
    if hi - lo < 100:                       # keep a flat/idle trace from filling the whole height
        hi = lo + 100
    ticks = _nice_ticks(lo, hi)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])

    def x(t):
        return (t - t0) / (t1 - t0) * W

    def y(v):
        return H - (v - lo) / (hi - lo) * H

    series = []
    for c in shown:
        segs, cur = [], []
        for t, row in points:
            v = row.get(c["slot"])
            if v is None or math.isnan(v):
                if cur:
                    segs.append(cur)
                cur = []
            else:
                cur.append(f"{x(t):.1f},{y(v):.1f}")
        if cur:
            segs.append(cur)
        path = " ".join("M" + " L".join(s) for s in segs)
        series.append({"slot": c["slot"], "name": c["name"], "cls": f"s{c['slot']}", "path": path})   # colour follows the CT slot (CSS)

    hover = {
        "t": [p[0] for p in points],
        "x": [round(x(p[0]) / W * 100, 2) for p in points],
        "s": [{"name": f"CT {c['slot']} {c['name']}", "cls": f"s{c['slot']}",
               "v": [_or_none(p[1].get(c["slot"])) for p in points]} for c in shown],
    }
    return {
        "waiting": False,
        "series": series,
        "ticks": [{"label": f"{v:,.0f}", "top": round(y(v) / H * 100, 2), "zero": v == 0} for v in ticks],
        "start": datetime.fromtimestamp(t0, timezone.utc).strftime("%H:%M"),
        "end": datetime.fromtimestamp(t1, timezone.utc).strftime("%H:%M"),
        "minutes": round((t1 - t0) / 60),
        "hover": json.dumps(hover, separators=(",", ":"), allow_nan=False),
    }
