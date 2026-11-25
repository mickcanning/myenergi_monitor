"""Best estimate of home energy use per half hour, combining measured and sampled data.

Home use isn't measured by anything directly; it is  grid (import - export) + solar & battery - diverted.

  grid, diverted   The hub's per-minute history (recorder.minutes): energy measured continuously, so short
                   spikes between our polls are counted, and it covers times the app wasn't polling.
  solar & battery  Only known from our 15 s samples of the Generation (& Battery) CT. Each minute uses the
                   mean of its samples; minutes with no sample are filled by linear interpolation between the
                   samples either side (up to INTERP_MAX_S apart).

Uncertainty: the sampled solar value for a minute is a mean of k instantaneous readings, so its error is
estimated as sigma^2 / k, where sigma^2 is the short-term variance of the solar readings around that half
hour, taken from successive differences so steady changes don't count as noise (an interpolated minute counts
as a single reading). Minute errors are summed as independent, which is
approximate: neighbouring readings are correlated, so the true spread can be somewhat wider.
When the measured history isn't available yet (the last few minutes, before the next sync) the minute falls
back to the sampled home power, with its own sampling error.
"""
import math
from bisect import bisect_left, bisect_right
from statistics import pvariance

HH = 1800
SUSPECT_AGE_S = 60          # a solar reading this old that contradicts the measured import is taken as 0 W
INTERP_MAX_S = 1800            # fill gaps in the solar samples up to this long; longer gaps stay unknown
J_PER_KWH = 3.6e6


def _var(xs):
    """Short-term variance of a run of readings, from the differences between consecutive ones
    (von Neumann: mean squared successive difference / 2). Unlike the plain variance it isn't inflated by a
    step or a trend, e.g. the battery switching from idle to charging, which is real change, not noise."""
    if len(xs) < 3:
        return None
    d = [(b - a) ** 2 for a, b in zip(xs, xs[1:])]
    return min(sum(d) / (2 * len(d)), pvariance(xs))


FIELDS = ("home", "grid", "var", "covered", "measured", "sampled", "imp", "exp", "grid_batt", "grid_dev",
          "eddi", "zappi", "charge")


def compute(store, start, end):
    """{hh: tuple in FIELDS order} for half hours in [start, end) (both multiples of 1800):
    home kWh, of which from the grid kWh, variance kWh^2, covered s, measured s, sampled-only kWh,
    grid import kWh, grid export kWh, grid import that charged the battery kWh, and that went to the
    Eddi/Zappi kWh, then all energy to the Eddi (hot water), the Zappi (car) and into the battery kWh. Import and export are the hub's measured totals whenever its history has the minute,
    even if we have no solar samples then (so they match the myenergi app); the split of the import needs
    the solar & battery reading as well."""
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

        home = grid = var = covered = measured = imp_j = exp_j = g_batt = g_dev = 0.0
        eddi_j = zappi_j = charge_j = 0.0
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
            if meas is not None and meas[0] is not None:
                imp_j += meas[0]
                exp_j += meas[1] or 0
            if meas is not None and meas[0] is not None and s_w is not None:
                imp, exp_, div = (x / 60 for x in (meas[0], meas[1] or 0, meas[2] or 0))
                # A Harvi that has gone quiet keeps its last reading. If that reading is getting old and says the
                # battery is charging harder than the measured import could supply, it has most likely stopped.
                if s_w < 0 and imp - exp_ + s_w - div < 0 and any(
                        (x[6] or 0) > SUSPECT_AGE_S for x in here):
                    s_w, s_var = 0.0, 0.0
                h = max(0.0, imp - exp_ + s_w - div)
                charge = max(-s_w, 0.0)
                demand = h + div + charge
                f = min(1.0, imp / demand) if demand > 0 else 0.0
                g = h * f
                g_batt += charge * f * 60
                g_dev += div * f * 60
                charge_j += charge * 60
                if meas[3] is None and meas[4] is None:     # history saved before Eddi/Zappi were split
                    eddi_j += div * 60
                else:
                    eddi_j += (meas[3] or 0)
                    zappi_j += (meas[4] or 0)
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
                g = gb = gd = ip = ep = 0.0
                for s in hs:
                    gr, so, ed, zp, hm = (s[1], s[2] or 0, s[3] or 0, s[4] or 0, s[5])
                    dem = hm + ed + zp + max(-so, 0)
                    if gr is None:
                        g += hm
                        continue
                    f = min(1.0, max(gr, 0) / dem) if dem > 0 else 0.0
                    g += hm * f
                    gb += max(-so, 0) * f
                    gd += (ed + zp) * f
                    ip += max(gr, 0)
                    ep += max(-gr, 0)
                ed_w = sum(s[3] or 0 for s in hs) / len(hs)
                zp_w = sum(s[4] or 0 for s in hs) / len(hs)
                ch_w = sum(max(-(s[2] or 0), 0) for s in hs) / len(hs)
                k = len(hs)
                home += h * 60
                grid += g / k * 60
                g_batt += gb / k * 60
                g_dev += gd / k * 60
                eddi_j += ed_w * 60
                zappi_j += zp_w * 60
                charge_j += ch_w * 60
                if meas is None:                        # (measured import already counted above)
                    imp_j += ip / k * 60
                    exp_j += ep / k * 60
                var += home_var / len(hs) * 3600
                covered += 60
        smp = sampled.get(hh)
        res[hh] = (home / J_PER_KWH, grid / J_PER_KWH, var / J_PER_KWH ** 2, covered, measured,
                   smp[0] if smp else 0.0, imp_j / J_PER_KWH, exp_j / J_PER_KWH,
                   g_batt / J_PER_KWH, g_dev / J_PER_KWH, eddi_j / J_PER_KWH, zappi_j / J_PER_KWH,
                   charge_j / J_PER_KWH)
    return res


def half_hours(store, start, end, now):
    """Like compute(), using cached results for half hours that are final (their day's measured history is
    complete), and caching any newly final ones."""
    start, end = int(start) // HH * HH, -(-int(end) // HH) * HH
    first = store.first_flow_poll()
    if first is not None:
        # the hub's history is backfilled for days before recording began (for the car page), but home use
        # needs our solar & battery samples, so the estimate starts on the first recorded (UTC) day
        start = max(start, first // 86400 * 86400)
        if start >= end:
            return {}
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
