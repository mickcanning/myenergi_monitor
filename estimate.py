"""Best estimate of home energy use per half hour, combining measured and sampled data.

Home use isn't measured by anything directly; it is  grid (import - export) + solar & battery - diverted.

  grid, diverted   The hub's per-minute history (recorder.minutes): energy measured continuously, so short
                   spikes between our polls are counted, and it covers times the app wasn't polling.
  solar & battery  Only known from our 15 s samples of the Generation (& Battery) CT. Each minute uses the
                   mean of its samples; minutes with no sample are filled by linear interpolation between the
                   samples either side (up to INTERP_MAX_S apart).

Uncertainty: the sampled solar value for a minute is a mean of k instantaneous readings, so its error is
estimated as sigma^2 / k, where sigma^2 is the variance of the solar readings around that half hour (an
interpolated minute counts as a single reading). Minute errors are summed as independent, which is
approximate: neighbouring readings are correlated, so the true spread can be somewhat wider.
When the measured history isn't available yet (the last few minutes, before the next sync) the minute falls
back to the sampled home power, with its own sampling error.
"""
import math
from bisect import bisect_left, bisect_right
from statistics import pvariance

HH = 1800
INTERP_MAX_S = 1800            # fill gaps in the solar samples up to this long; longer gaps stay unknown
J_PER_KWH = 3.6e6


def _var(xs):
    return pvariance(xs) if len(xs) >= 2 else None


def compute(store, start, end):
    """{hh: (home kWh, from grid kWh, variance kWh^2, covered s, measured s, sampled kWh)} for half hours
    in [start, end) (both multiples of 1800)."""
    out = {}
    day = 86400
    a = start
    while a < end:                                    # a day at a time keeps memory small over long ranges
        b = min(end, (a // day + 1) * day)
        out.update(_compute_chunk(store, a, b))
        a = b
    return out


def _compute_chunk(store, start, end):
    mins = store.minutes(start, end)
    samples = store.flow_samples(start - INTERP_MAX_S, end + INTERP_MAX_S)
    sampled = store.energy_by_half_hour(start, end)
    t_all = [s[0] for s in samples]
    sol = [(s[0], s[2]) for s in samples if s[2] is not None]
    t_sol = [t for t, _ in sol]
    has_solar = bool(sol)

    res = {}
    for hh in range(int(start), int(end), HH):
        # spread of the readings around this half hour, for the sampling error
        lo, hi = bisect_left(t_sol, hh - HH), bisect_left(t_sol, hh + 2 * HH)
        sol_var = _var([v for _, v in sol[lo:hi]]) or 0.0
        i0, i1 = bisect_left(t_all, hh - HH), bisect_left(t_all, hh + 2 * HH)
        home_var = _var([s[5] for s in samples[i0:i1] if s[5] is not None]) or 0.0

        home = grid = var = covered = measured = 0.0
        for m in range(hh, hh + HH, 60):
            j0, j1 = bisect_left(t_all, m), bisect_left(t_all, m + 60)
            here = samples[j0:j1]

            # solar & battery for this minute (W) and its error variance (W^2)
            if not has_solar:
                s_w, s_var = 0.0, 0.0
            else:
                vals = [s[2] for s in here if s[2] is not None]
                if vals:
                    s_w, s_var = sum(vals) / len(vals), sol_var / len(vals)
                else:
                    k = bisect_right(t_sol, m + 30)
                    if 0 < k < len(sol) and sol[k][0] - sol[k - 1][0] <= INTERP_MAX_S:
                        (t0, v0), (t1, v1) = sol[k - 1], sol[k]
                        s_w = v0 + (v1 - v0) * (m + 30 - t0) / (t1 - t0)
                        s_var = sol_var
                    else:
                        s_w = s_var = None

            meas = mins.get(m)
            if meas is not None and meas[0] is not None and s_w is not None:
                imp, exp_, div = (x / 60 for x in (meas[0], meas[1] or 0, meas[2] or 0))
                h = max(0.0, imp - exp_ + s_w - div)
                demand = h + div + max(-s_w, 0.0)
                g = h * min(1.0, imp / demand) if demand > 0 else 0.0
                home += h * 60
                grid += g * 60
                var += s_var * 3600
                covered += 60
                measured += 60
            elif here:                                  # no measured minute yet: use the samples as before
                hs = [s for s in here if s[5] is not None]
                if not hs:
                    continue
                h = sum(s[5] for s in hs) / len(hs)
                g = 0.0
                for s in hs:
                    gr, so, ed, zp, hm = (s[1], s[2] or 0, s[3] or 0, s[4] or 0, s[5])
                    dem = hm + ed + zp + max(-so, 0)
                    g += hm if gr is None else (hm * min(1.0, max(gr, 0) / dem) if dem > 0 else 0)
                g /= len(hs)
                home += h * 60
                grid += g * 60
                var += home_var / len(hs) * 3600
                covered += 60
        smp = sampled.get(hh)
        res[hh] = (home / J_PER_KWH, grid / J_PER_KWH, var / J_PER_KWH ** 2, covered, measured,
                   smp[0] if smp else 0.0)
    return res


def half_hours(store, start, end, now):
    """Like compute(), using cached results for half hours that are final (their day's measured history is
    complete), and caching any newly final ones."""
    start, end = int(start) // HH * HH, -(-int(end) // HH) * HH
    final_until = min(store.complete_until() or 0, now - 3600)
    cached = store.cached_hh(start, min(end, final_until))
    out = {hh: v for hh, v in cached.items()}
    missing = [hh for hh in range(start, end, HH) if hh not in out and hh < now]
    # compute contiguous runs of missing half hours
    runs, run = [], []
    for hh in missing:
        if run and hh != run[-1] + HH:
            runs.append(run)
            run = []
        run.append(hh)
    if run:
        runs.append(run)
    new_final = []
    for r in runs:
        got = compute(store, r[0], r[-1] + HH)
        out.update(got)
        new_final += [(hh, *v) for hh, v in got.items() if hh + HH <= final_until]
    if new_final:
        store.cache_hh(new_final)
    return out


def ci95(var_kwh2):
    """Half-width of an approximate 95% range from a variance."""
    return 1.96 * math.sqrt(max(var_kwh2, 0.0))
