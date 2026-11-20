"""Harvi signal quality from the recorded data: is the Eddi hearing the Harvis better or worse?

The API gives no signal strength, so it is inferred from when reports arrive. A Harvi goes quiet for two
reasons: no current through its clamp (it powers itself from that current, so it can't transmit) or a poor
radio link (it transmits but the Eddi doesn't hear it). Only the second says anything about the antenna, so
every metric counts only time when the Harvi had current to power it ("powered"):

  powered minute   Grid Harvi: the hub's own measured grid import/export (independent of the Harvi) was at
                   least POWERED_W that minute. Other Harvis, or minutes without the hub's history: the
                   Harvi's readings before and after were both at least POWERED_W, and it wasn't silent for
                   longer than LONG_QUIET_S. (When the current stops the API keeps the last, high, reading, so
                   both sides of a no-current spell look powered; a poor link loses single reports, not quarter
                   hours, so a silence that long is taken as no current.)
  report rate      new reports received per powered minute. The headline number: higher is better. It falls in
                   proportion to the share of transmissions lost. (A Harvi transmits every ~20 s and polling every
                   15 s can see at most 4 per minute, so a perfect link shows roughly 2.5-3.)
  heard            the share of powered minutes with at least one report (less sensitive: a minute only counts
                   as missed when every transmission in it is lost).
  report interval  time between consecutive reports while powered (median and 95th percentile).
  silence          an interval of at least SILENCE_S while powered; counted per powered hour.

Minutes and intervals while the app wasn't polling don't count. The interference check splits the hit rate by
whether the immersion heater (Eddi) or the car charger (Zappi) was drawing power at the time.

Before/after comparisons use clock hours as the unit (minutes within an hour are not independent), comparing
the mean hourly report rate before and after a change with an approximate 95% interval (Welch).
"""
import json
import math
from bisect import bisect_left, bisect_right
from statistics import mean, median, variance

POWERED_W = 500          # current through the clamp needed before a quiet Harvi counts against the signal
SILENCE_S = 120          # an interval this long while powered is a silence
DEVICE_ON_W = 100        # immersion heater / car charger "on"
MAX_POLL_GAP_S = 120     # polling gaps longer than this are recording pauses, not silences
MIN_HOUR_MINUTES = 10    # an hour needs this many powered minutes to count in before/after comparisons
LONG_QUIET_S = 900       # without independent measurements, a silence longer than this is taken as no current
HOUR = 3600


def _empty():
    return {"powered": 0, "heard": 0, "reports": 0, "eddi_p": 0, "eddi_h": 0, "eddi_r": 0, "zappi_p": 0,
            "zappi_h": 0, "zappi_r": 0, "idle_p": 0, "idle_h": 0, "idle_r": 0, "silences": 0, "intervals": []}


def compute(store, start, end, grid_sno):
    """{hour start: {sno: counts}} for whole hours in [start, end)."""
    start, end = int(start) // HOUR * HOUR, -(-int(end) // HOUR) * HOUR
    pad = 3 * HOUR
    rows = store._conn().execute(
        "SELECT poll, sno, report, ct1 FROM readings WHERE device = 'harvi' AND poll >= ? AND poll < ? "
        "ORDER BY sno, poll", (start - pad, end + pad)).fetchall()
    polls = sorted({r[0] for r in store._conn().execute(
        "SELECT DISTINCT poll FROM readings WHERE poll >= ? AND poll < ?", (start - pad, end + pad))})
    mins = store.minutes(start, end)
    flows = store._conn().execute("SELECT poll, eddi, zappi FROM flows WHERE poll >= ? AND poll < ?",
                                  (start, end)).fetchall()
    sampled_on = {}
    for poll, ed, zp in flows:
        m = poll // 60 * 60
        e, z = sampled_on.get(m, (False, False))
        sampled_on[m] = (e or (ed or 0) >= DEVICE_ON_W, z or (zp or 0) >= DEVICE_ON_W)

    def recording(a, b):
        """True when polling ran with no pause longer than MAX_POLL_GAP_S across [a, b]."""
        i, j = bisect_left(polls, a - MAX_POLL_GAP_S), bisect_right(polls, b + MAX_POLL_GAP_S)
        seg = polls[i:j]
        if not seg or seg[0] > a or seg[-1] < b:
            return False
        return all(y - x <= MAX_POLL_GAP_S for x, y in zip(seg, seg[1:]))

    def device_on(m):
        meas = mins.get(m)
        if meas is not None and (meas[3] is not None or meas[4] is not None):
            return (meas[3] or 0) / 60 >= DEVICE_ON_W, (meas[4] or 0) / 60 >= DEVICE_ON_W
        return sampled_on.get(m, (False, False))

    by_sno = {}
    for poll, sno, report, w in rows:
        if report is None:
            continue
        reps = by_sno.setdefault(sno, [])
        if not reps or report > reps[-1][0]:
            reps.append((report, abs(w or 0)))

    out = {h: {} for h in range(start, end, HOUR)}
    for sno, reps in by_sno.items():
        times = [t for t, _ in reps]
        is_grid = str(sno) == str(grid_sno)
        for h in range(start, end, HOUR):
            out[h][sno] = _empty()
        # minutes
        for m in range(start, end, 60):
            if not recording(m, m + 59):
                continue
            meas = mins.get(m)
            if is_grid and meas is not None and meas[0] is not None:
                powered = abs((meas[0] or 0) - (meas[1] or 0)) / 60 >= POWERED_W
            else:
                k = bisect_right(times, m)                       # first report after the minute starts
                if k == 0 or k >= len(reps):
                    continue
                powered = (min(reps[k - 1][1], reps[k][1]) >= POWERED_W
                           and reps[k][0] - reps[k - 1][0] <= LONG_QUIET_S)
            if not powered:
                continue
            got = bisect_left(times, m + 60) - bisect_left(times, m)
            heard = got > 0
            c = out[m // HOUR * HOUR][sno]
            c["powered"] += 1
            c["heard"] += heard
            c["reports"] += got
            e_on, z_on = device_on(m)
            for key, on in (("eddi", e_on), ("zappi", z_on), ("idle", not e_on and not z_on)):
                if on:
                    c[f"{key}_p"] += 1
                    c[f"{key}_h"] += heard
                    c[f"{key}_r"] += got
        # intervals between reports while powered (assigned to the hour the interval ends in)
        for (t0, w0), (t1, w1) in zip(reps, reps[1:]):
            if not (start <= t1 < end) or not recording(t0, t1):
                continue
            ms = [mins.get(m) for m in range(t0 // 60 * 60, t1, 60)]
            if is_grid and ms and all(x is not None and x[0] is not None for x in ms):
                powered = all(abs((x[0] or 0) - (x[1] or 0)) / 60 >= POWERED_W for x in ms)
            else:
                powered = min(w0, w1) >= POWERED_W and t1 - t0 <= LONG_QUIET_S
            if not powered:
                continue
            c = out[t1 // HOUR * HOUR][sno]
            c["intervals"].append(t1 - t0)
            if t1 - t0 >= SILENCE_S:
                c["silences"] += 1
    return out


def hours(store, start, end, grid_sno, now):
    """compute(), using cached results for finished hours (their day's measured history is complete)."""
    start, end = int(start) // HOUR * HOUR, -(-int(end) // HOUR) * HOUR
    final_until = min(store.complete_until() or 0, now - HOUR)
    out = {}
    for h, sno, data in store._conn().execute(
            "SELECT hour, sno, data FROM signal_hours WHERE hour >= ? AND hour < ? AND grid = ?",
            (start, min(end, final_until), str(grid_sno))):
        out.setdefault(h, {})[sno] = json.loads(data)
    missing = [h for h in range(start, end, HOUR) if h not in out and h < now]
    runs, run = [], []
    for h in missing:
        if run and h != run[-1] + HOUR:
            runs.append(run)
            run = []
        run.append(h)
    if run:
        runs.append(run)
    chunks = [r[i:i + 24] for r in runs for i in range(0, len(r), 24)]      # a day at a time keeps memory small
    for r in chunks:
        got = compute(store, r[0], r[-1] + HOUR, grid_sno)
        out.update(got)
        rows = [(h, sno, str(grid_sno), json.dumps(c)) for h, per in got.items() if h + HOUR <= final_until
                for sno, c in per.items()]
        rows += [(h, "", str(grid_sno), "{}") for h, per in got.items() if h + HOUR <= final_until and not per]
        if rows:
            with store._conn() as c:
                c.executemany("INSERT OR REPLACE INTO signal_hours VALUES (?, ?, ?, ?)", rows)
    for per in out.values():
        per.pop("", None)
    return out


def _sum(hourly, sno, hs):
    tot = _empty()
    for h in hs:
        c = hourly.get(h, {}).get(sno)
        if not c:
            continue
        for k in tot:
            tot[k] = tot[k] + c[k]
    return tot


def summary(tot):
    """Readable metrics from summed counts."""
    iv = sorted(tot["intervals"])
    p_hours = tot["powered"] / 60
    return {
        "powered_h": p_hours,
        "hit": tot["heard"] / tot["powered"] if tot["powered"] else None,
        "rate": tot["reports"] / tot["powered"] if tot["powered"] else None,
        "median": median(iv) if iv else None,
        "p95": iv[min(len(iv) - 1, int(math.ceil(0.95 * len(iv))) - 1)] if iv else None,
        "silences": tot["silences"],
        "silence_rate": tot["silences"] / p_hours if p_hours >= 0.25 else None,
    }


def rate_ci(k, n, eff=4):
    """Approximate 95% interval for a rate of k reports in n minutes. Neighbouring minutes are correlated, so
    the variance is inflated by a rough design effect `eff`, which widens the interval."""
    if not n:
        return None
    r = k / n
    h = 1.96 * math.sqrt(max(k, 1) * eff) / n
    return max(0.0, r - h), r + h


def interference(hourly, snos, hs):
    """Reports per powered minute with the immersion heater on, and with the car charging, against minutes when
    neither was drawing power, per Harvi."""
    out = []
    for sno in snos:
        t = _sum(hourly, sno, hs)
        rows = []
        for key, label in (("eddi", "Immersion heater on"), ("zappi", "Car charging")):
            on_p, on_r, off_p, off_r = t[f"{key}_p"], t[f"{key}_r"], t["idle_p"], t["idle_r"]
            if not on_p:
                rows.append({"label": label, "on": None})
                continue
            on, off = on_r / on_p, (off_r / off_p if off_p else None)
            ci_on, ci_off = rate_ci(on_r, on_p), rate_ci(off_r, off_p)
            verdict = "Not enough data yet"
            if on_p >= 60 and off_p >= 60 and ci_on and ci_off:
                if ci_on[1] < ci_off[0]:
                    verdict = "Fewer reports get through while it's on: points to interference"
                elif ci_on[0] > ci_off[1]:
                    verdict = "More reports get through while it's on"
                else:
                    verdict = "No clear difference"
            rows.append({"label": label, "on": on, "on_p": on_p, "off": off, "off_p": off_p,
                         "ci_on": ci_on, "ci_off": ci_off, "verdict": verdict})
        out.append({"sno": sno, "rows": rows})
    return out


def _hour_values(hourly, sno, hs):
    """Per clock hour with enough powered minutes: (reports per powered minute, silences per powered hour)."""
    vals = []
    for h in hs:
        c = hourly.get(h, {}).get(sno)
        if c and c["powered"] >= MIN_HOUR_MINUTES:
            vals.append((c.get("reports", 0) / c["powered"], c["silences"] / (c["powered"] / 60)))
    return vals


def welch(a, b):
    """(mean b - mean a, half-width of an approximate 95% interval), or None."""
    if len(a) < 3 or len(b) < 3:
        return None
    d = mean(b) - mean(a)
    se = math.sqrt(variance(a) / len(a) + variance(b) / len(b))
    n = min(len(a), len(b))
    t = 1.96 if n >= 30 else {3: 4.30, 4: 3.18, 5: 2.78, 6: 2.57, 7: 2.45, 8: 2.36, 9: 2.31}.get(n, 2.2 if n < 15 else 2.1)
    return d, t * se


def compare(hourly, snos, before_hs, after_hs):
    """Before vs after a change, per Harvi."""
    out = []
    for sno in snos:
        a, b = _hour_values(hourly, sno, before_hs), _hour_values(hourly, sno, after_hs)
        sa, sb = summary(_sum(hourly, sno, before_hs)), summary(_sum(hourly, sno, after_hs))
        rate = welch([x[0] for x in a], [x[0] for x in b])
        sil = welch([x[1] for x in a], [x[1] for x in b])
        if rate is None:
            verdict, tone = "Not enough data yet", "none"
        elif rate[0] - rate[1] > 0:
            verdict, tone = "Better: more reports getting through", "good"
        elif rate[0] + rate[1] < 0:
            verdict, tone = "Worse: fewer reports getting through", "bad"
        else:
            verdict, tone = "No clear change yet", "none"
        pct = None
        if rate and sa["rate"]:
            pct = (100 * rate[0] / sa["rate"], 100 * rate[1] / sa["rate"])
        out.append({"sno": sno, "before": sa, "after": sb, "hours_before": len(a), "hours_after": len(b),
                    "rate_diff": rate, "rate_pct": pct, "silence_diff": sil, "verdict": verdict, "tone": tone})
    return out
