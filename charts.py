"""Load a monitor.py CSV log, analyse Harvi report gaps, and render the report chart."""
from __future__ import annotations

import csv
import io
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
from matplotlib.ticker import AutoMinorLocator, FuncFormatter, MaxNLocator  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

REQUIRED = ["poll_utc", "device", "sno", "report_utc", "age_s", "ectp1"]

INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e6e5e1", "#fcfcfb"
SHADE, RED = "#efeeea", "#e34948"
# Categorical slots in fixed order; the Grid Harvi always takes slot 1 (blue).
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#6250d6"]

# Report chart colours per page theme; dark is the same hues stepped for a dark surface.
THEMES = {
    "light": dict(ink=INK, ink2=INK2, grid=GRID, surf=SURF, shade=SHADE, red=RED, series=SERIES,
                  spine="#c9c8c3", zero="#9d9c97"),
    "dark": dict(ink="#f0efec", ink2="#c3c2b7", grid="#383835", surf="#1a1a19", shade="#262624", red="#e66767",
                 series=["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9"],
                 spine="#4a4945", zero="#6b6a65"),
}


class LogFormatError(ValueError):
    """The uploaded file isn't a log written by monitor.py."""


@dataclass
class Row:
    t: datetime
    report: datetime | None
    age: float
    ct: float


class Log:
    def __init__(self):
        self.rows = defaultdict(list)   # (device, sno) -> [Row] in poll order

    def serials(self, device):
        return sorted(sno for (dev, sno) in self.rows if dev == device)

    @property
    def start(self):
        return min(r[0].t for r in self.rows.values() if r)

    @property
    def end(self):
        return max(r[-1].t for r in self.rows.values() if r)


def _float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return math.nan


def load_log(fileobj):
    """Parse a CSV written by monitor.py. Accepts a text or binary file object."""
    raw = fileobj.read()
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
    if missing:
        raise LogFormatError(
            "This doesn't look like a log from monitor.py (missing columns: " + ", ".join(missing) + ")."
        )
    log = Log()
    try:
        for r in reader:
            report = datetime.fromisoformat(r["report_utc"]) if r["report_utc"] else None
            log.rows[(r["device"], r["sno"])].append(
                Row(datetime.fromisoformat(r["poll_utc"]), report, _float(r["age_s"]), _float(r["ectp1"]))
            )
    except ValueError as e:
        raise LogFormatError(f"Could not read a timestamp in the file ({e}).") from None
    if not log.serials("harvi"):
        raise LogFormatError("The file contains no Harvi rows.")
    return log


POLL_GAP_MIN_S = 120   # a pause between polls longer than this (or 4 poll intervals) means recording stopped


def recording_gap_threshold(rows):
    steps = [(b.t - a.t).total_seconds() for a, b in zip(rows, rows[1:])]
    return max(POLL_GAP_MIN_S, 4 * statistics.median(steps)) if steps else POLL_GAP_MIN_S


def recording_gaps(rows):
    """(start, end) of each pause in polling, e.g. while the app or container wasn't running."""
    th = recording_gap_threshold(rows)
    return [(a.t, b.t) for a, b in zip(rows, rows[1:]) if (b.t - a.t).total_seconds() > th]


def silences(rows):
    """Silences between distinct report times, counted only as far as polling actually observed them.

    The cloud's report time proves the device was silent from its last report until the last poll that still
    showed it. With continuous polling the next report turns up within one poll interval, so the silence runs to
    that new report. If polling paused before the new report was seen (app not running), what happened in the
    pause is unknown: the silence is cut short at the last poll that still showed the old report ("cut_short").
    """
    th = recording_gap_threshold(rows)
    out, last, prev = [], None, None          # last: first row of the current report; prev: previous polled row
    for r in rows:
        if r.report is None:
            prev = r
            continue
        if last is not None and r.report != last.report:
            if prev is not None and (r.t - prev.t).total_seconds() > th:
                seen = prev if prev.report == last.report else last
                end, ct_after, cut = seen.t, seen.ct, True
            else:
                end, ct_after, cut = r.report, r.ct, False
            gap = (end - last.report).total_seconds()
            if gap > 0:
                out.append({"start": last.report, "end": end, "gap": gap,
                            "ct_before": last.ct, "ct_after": ct_after, "cut_short": cut})
        if last is None or r.report != last.report:
            last = r
        prev = r
    return out


def _broken(rows, value, th):
    """x/y lists with a NaN between rows separated by a recording gap, so lines aren't drawn across it."""
    xs, ys, prev = [], [], None
    for r in rows:
        if prev is not None and (r.t - prev.t).total_seconds() > th:
            xs.append(prev.t + timedelta(seconds=1))      # just after the last poll, so step lines stop there
            ys.append(math.nan)
        xs.append(r.t)
        ys.append(value(r))
        prev = r
    return xs, ys


def concerns(rows, gap_s, ct_w):
    """Silences of at least gap_s while more than ct_w watts flowed on both sides."""
    return [e for e in silences(rows)
            if e["gap"] >= gap_s and abs(e["ct_before"]) > ct_w and abs(e["ct_after"]) > ct_w]


def _in_night(t, night_start, night_end):
    h = t.hour
    return (h >= night_start or h < night_end) if night_start > night_end else (night_start <= h < night_end)


def _pct(values, q):
    v = sorted(x for x in values if not math.isnan(x))
    return v[min(len(v) - 1, int(len(v) * q))] if v else math.nan


def analyse(log, grid_sno, thresh_s=30, gap_s=120, ct_w=500, night_start=23, night_end=8):
    """Numbers for the report's tables."""
    result = {"harvis": [], "concerns": [], "eddi_max_age": None, "polls": 0, "start": log.start, "end": log.end}
    grid_rows = log.rows[("harvi", grid_sno)] if ("harvi", grid_sno) in log.rows else []
    gaps = recording_gaps(grid_rows)
    result["recording_gaps"] = gaps
    result["recording_gap_s"] = sum((b - a).total_seconds() for a, b in gaps)
    eddi_ages = [r.age for (dev, _), rows in log.rows.items() if dev == "eddi" for r in rows]
    if eddi_ages:
        result["eddi_max_age"] = max(eddi_ages)
    for sno in log.serials("harvi"):
        rows = log.rows[("harvi", sno)]
        ages = [r.age for r in rows]
        night = [r.age for r in rows if _in_night(r.t, night_start, night_end)]
        sil = silences(rows)
        result["polls"] = max(result["polls"], len(rows))
        result["harvis"].append({
            "sno": sno, "is_grid": sno == grid_sno, "polls": len(rows),
            "median_age": statistics.median(ages), "p95_age": _pct(ages, 0.95), "max_age": max(ages),
            "pct_over": 100 * sum(a > thresh_s for a in ages) / len(ages),
            "night_pct_over": (100 * sum(a > thresh_s for a in night) / len(night)) if night else None,
            "night_max": max(night) if night else None,
            "silences_over": sum(s["gap"] > thresh_s for s in sil), "silences": len(sil),
            "max_gap": max((s["gap"] for s in sil), default=0),
        })
    if ("harvi", grid_sno) in log.rows:
        result["concerns"] = concerns(log.rows[("harvi", grid_sno)], gap_s, ct_w)
    return result


def _style(ax, pal):
    ax.set_facecolor(pal["surf"])
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(pal["spine"])
    # Y graduation: about 4.5 labelled values per inch of panel height (so the short CT panels
    # don't get crowded), plus an unlabelled minor gridline halfway between each.
    height_in = ax.get_position().height * ax.figure.get_figheight()
    ax.yaxis.set_major_locator(MaxNLocator(nbins=max(4, round(height_in * 4.5)), steps=[1, 2, 2.5, 5, 10]))
    ax.yaxis.set_minor_locator(AutoMinorLocator(2))
    ax.grid(axis="y", which="major", color=pal["grid"], linewidth=0.8)
    ax.grid(axis="y", which="minor", color=pal["grid"], linewidth=0.5, alpha=0.5)
    ax.tick_params(length=0, which="both")


def _shade_nights(ax, start, end, night_start, night_end, shade=SHADE):
    day = start.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    while day <= end:
        if night_start > night_end:
            a, b = day + timedelta(hours=night_start), day + timedelta(days=1, hours=night_end)
        else:
            a, b = day + timedelta(hours=night_start), day + timedelta(hours=night_end)
        ax.axvspan(a, b, color=shade, zorder=0, linewidth=0)
        day += timedelta(days=1)


def _shade_gaps(ax, gaps, pal):
    """Hatch the periods when nothing was being polled, so they aren't mistaken for data."""
    for a, b in gaps:
        ax.axvspan(a, b, facecolor=pal["surf"], edgecolor=pal["spine"], hatch="////", linewidth=0, zorder=0.5)


# Major tick spacing in minutes, with how many minor divisions each one gets.
_TIME_STEPS = [(5, 5), (10, 2), (15, 3), (20, 2), (30, 2), (60, 4), (120, 2), (180, 3), (240, 4), (360, 6),
               (720, 4), (1440, 4), (2880, 2), (4320, 3), (10080, 7)]


def _time_axis(axes, start, end, pal, max_labels=22):
    """Dense, regular time ticks: the smallest 'nice' step that fits max_labels across the range, unlabelled
    minor ticks in between, faint vertical gridlines. Labels are one line: the date at midnight, else the time."""
    span_min = max(1.0, (end - start).total_seconds() / 60)
    if span_min > 36 * 60:
        max_labels = min(max_labels, 16)              # "12 Nov" labels are a little wider than "14:00"
    step, minor_n = next(((s, n) for s, n in _TIME_STEPS if span_min / s <= max_labels), _TIME_STEPS[-1])
    if step < 60:
        major = mdates.MinuteLocator(byminute=range(0, 60, step))
    elif step < 1440:
        major = mdates.HourLocator(byhour=range(0, 24, step // 60))
    else:
        major = mdates.DayLocator(interval=step // 1440)

    def label(x, _pos):
        d = mdates.num2date(x)
        return d.strftime("%d %b") if step >= 1440 or (d.hour, d.minute) == (0, 0) else d.strftime("%H:%M")

    for a in axes:
        a.xaxis.set_major_locator(major)
        a.xaxis.set_major_formatter(FuncFormatter(label))
        a.xaxis.set_minor_locator(AutoMinorLocator(minor_n))
        a.grid(axis="x", which="major", color=pal["grid"], linewidth=0.6, alpha=0.8)
        a.tick_params(axis="x", which="minor", length=3, color=pal["spine"])
        a.tick_params(axis="x", which="major", length=5, color=pal["spine"], labelsize=8.5)


def _zoom_window(conc):
    """Window around the concerning silences, at most ~3 hours wide."""
    lo, hi = min(e["start"] for e in conc), max(e["end"] for e in conc)
    pad = timedelta(minutes=15)
    if hi - lo > timedelta(hours=3):
        worst = max(conc, key=lambda e: e["gap"])
        lo, hi = worst["start"] - timedelta(minutes=45), worst["end"] + timedelta(minutes=45)
    return lo - pad, hi + pad


def render_png(log, grid_sno, names=None, thresh_s=30, gap_s=120, ct_w=500, night_start=23, night_end=8,
               theme="light"):
    """Render the report chart and return PNG bytes."""
    names = names or {}
    pal = THEMES.get(theme, THEMES["light"])
    INK, INK2, SURF, RED, SERIES = pal["ink"], pal["ink2"], pal["surf"], pal["red"], pal["series"]  # noqa: N806
    harvis = log.serials("harvi")
    if grid_sno not in harvis:
        grid_sno = harvis[0]
    others = [s for s in harvis if s != grid_sno]
    colour = {grid_sno: SERIES[0], **{s: SERIES[(i + 1) % len(SERIES)] for i, s in enumerate(others)}}
    label = {s: names.get(s) or f"Harvi {s}" for s in harvis}

    grows = log.rows[("harvi", grid_sno)]
    conc = concerns(grows, gap_s, ct_w)
    start, end = log.start, log.end
    gaps = recording_gaps(grows)
    gap_th = {sno: recording_gap_threshold(log.rows[("harvi", sno)]) for sno in harvis}

    ratios = [1, 0.55]
    if conc:
        ratios += [1, 0.55]
    ratios += [1, 0.55] * len(others)
    plt.rcParams.update({"font.family": "DejaVu Sans", "text.color": INK, "axes.labelcolor": INK2,
                         "xtick.color": INK2, "ytick.color": INK2})
    fig = plt.figure(figsize=(11, 2.55 * sum(ratios) + 0.8), facecolor=SURF)
    gs = fig.add_gridspec(len(ratios), 1, height_ratios=ratios, hspace=0.55)
    title_kw = dict(loc="left", fontsize=12, fontweight="bold", color=INK, pad=10)
    lbl = dict(color=INK, fontsize=8.5, va="bottom", zorder=6,
               bbox=dict(facecolor=SURF, edgecolor="none", pad=1.5, alpha=0.95))
    row = 0
    full_axes = []

    def red_spans(ax):
        for e in conc:
            ax.axvspan(e["start"], e["end"], color=RED, alpha=0.35, zorder=1, linewidth=0)

    def power(ax, rows, colr, ylabel, title):
        _style(ax, pal)
        ax.axhline(0, color=pal["zero"], linewidth=0.8, zorder=2)
        ax.step(*_broken(rows, lambda r: r.ct / 1000, recording_gap_threshold(rows)), where="post", color=colr,
                linewidth=1.2, zorder=3)
        ax.set_ylabel(ylabel, fontsize=9)
        ax.set_title(title, loc="left", fontsize=11, fontweight="bold", color=INK, pad=8)

    # --- Grid Harvi, full run
    ax = fig.add_subplot(gs[row]); row += 1
    full_axes.append(ax)
    _style(ax, pal)
    _shade_nights(ax, start, end, night_start, night_end, pal['shade'])
    _shade_gaps(ax, gaps, pal)
    red_spans(ax)
    ax.plot(*_broken(grows, lambda r: r.age, gap_th[grid_sno]), color=colour[grid_sno], linewidth=1.2, zorder=3)
    for e in conc:
        pk = max(r.age for r in grows if e["start"] <= r.t <= e["end"])
        ax.plot(e["end"] - timedelta(seconds=15), pk, marker="o", markersize=9, markerfacecolor="none",
                markeredgecolor=RED, markeredgewidth=1.8, zorder=4)
    ax.axhline(thresh_s, color=INK, linewidth=1.0, linestyle=(0, (4, 3)), zorder=2)
    ax.text(start + (end - start) * 0.01, thresh_s + 6, f"{thresh_s:g} s threshold", **lbl)
    ymax = max([r.age for r in grows if not math.isnan(r.age)] + [thresh_s * 2])
    ax.set_ylim(0, ymax * 1.15)
    ax.set_ylabel("Seconds since last report")
    ax.set_title(f"{label[grid_sno]} (grid CT): report age" + (", gaps of concern ringed in red" if conc else ""), **title_kw)
    ax.plot([], [], color=colour[grid_sno], linewidth=2, label=label[grid_sno])
    if gaps:
        ax.fill_between([], [], facecolor=pal["surf"], edgecolor=pal["spine"], hatch="////", linewidth=0,
                        label="Not recording (no polls)")
    if conc:
        ax.plot([], [], marker="o", linestyle="", markersize=8, markerfacecolor="none", markeredgecolor=RED,
                markeredgewidth=1.8, label=f"Silence of {gap_s:g}+ s with over {ct_w:g} W in the CT")
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK2)
    pax = fig.add_subplot(gs[row], sharex=ax); row += 1
    full_axes.append(pax)
    power(pax, grows, colour[grid_sno], "Grid CT (kW)", "Grid CT reading at each report (last value held between reports)")
    _shade_nights(pax, start, end, night_start, night_end, pal['shade'])
    _shade_gaps(pax, gaps, pal)
    red_spans(pax)

    # --- zoom on the concerning silences
    if conc:
        z0, z1 = _zoom_window(conc)
        zrows = [r for r in grows if z0 <= r.t <= z1]
        zax = fig.add_subplot(gs[row]); row += 1
        _style(zax, pal)
        _shade_nights(zax, z0, z1, night_start, night_end, pal['shade'])
        _shade_gaps(zax, gaps, pal)
        red_spans(zax)
        zax.plot(*_broken(zrows, lambda r: r.age, gap_th[grid_sno]), color=colour[grid_sno], linewidth=1.4, zorder=3)
        zax.axhline(thresh_s, color=INK, linewidth=1.0, linestyle=(0, (4, 3)), zorder=2)
        zax.text(z0 + (z1 - z0) * 0.01, thresh_s + 6, f"{thresh_s:g} s threshold", **lbl)
        zmax = max(e["gap"] for e in conc)
        zax.set_ylim(0, zmax * 1.4)
        zax.set_xlim(z0, z1)
        zax.set_ylabel("Seconds since last report")
        for e in conc:
            if z0 <= e["end"] <= z1:
                pk = max((r.age for r in zrows if e["start"] <= r.t <= e["end"]), default=e["gap"])
                zax.annotate(f"{e['gap']:.0f} s\nends {e['end']:%H:%M}", xy=(e["end"], pk),
                             xytext=(e["end"], pk + zmax * 0.04), ha="center", va="bottom", fontsize=9, color=INK)
        zax.set_title(f"Zoom: {z0:%H:%M} to {z1:%H:%M}, silences with current flowing in the CT", **title_kw)
        zax.tick_params(labelbottom=False)
        zp = fig.add_subplot(gs[row], sharex=zax); row += 1
        power(zp, zrows, colour[grid_sno], "Grid CT (kW)", "Grid CT reading in the same window")
        _shade_nights(zp, z0, z1, night_start, night_end, pal['shade'])
        _shade_gaps(zp, gaps, pal)
        red_spans(zp)
        zp.set_xlim(z0, z1)
        _time_axis([zax, zp], z0, z1, pal)

    # --- other Harvis, full run
    for sno in others:
        rows = log.rows[("harvi", sno)]
        use_min = max((r.age for r in rows if not math.isnan(r.age)), default=0) > 900
        div, unit = (60, "Minutes") if use_min else (1, "Seconds")
        ax = fig.add_subplot(gs[row], sharex=full_axes[0]); row += 1
        full_axes.append(ax)
        _style(ax, pal)
        _shade_nights(ax, start, end, night_start, night_end, pal['shade'])
        _shade_gaps(ax, gaps, pal)
        ax.plot(*_broken(rows, lambda r: r.age / div, gap_th[sno]), color=colour[sno], linewidth=1.2, zorder=3)
        ax.set_ylim(bottom=0)
        ax.set_ylabel(f"{unit} since last report")
        ax.set_title(f"{label[sno]}: report age (long gaps usually mean little current in its CT)", **title_kw)
        ax.plot([], [], color=colour[sno], linewidth=2, label=label[sno])
        ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK2)
        pax = fig.add_subplot(gs[row], sharex=full_axes[0]); row += 1
        full_axes.append(pax)
        power(pax, rows, colour[sno], f"{label[sno]} CT (kW)"[:28], f"{label[sno]} CT reading at each report (last value held)")
        _shade_nights(pax, start, end, night_start, night_end, pal['shade'])
        _shade_gaps(pax, gaps, pal)

    # x axis on the full-run panels: only the last one shows tick labels
    for a in full_axes:
        a.tick_params(labelbottom=False)
    full_axes[-1].tick_params(labelbottom=True)
    full_axes[1].tick_params(labelbottom=True)
    full_axes[0].set_xlim(start, end)
    _time_axis(full_axes, start, end, pal)
    full_axes[-1].set_xlabel(
        f"Time (UTC). Grey bands: {night_start:02d}:00 to {night_end:02d}:00. "
        "Power sign as reported by the CT; the line holds the last value between reports."
        + (" Hatched: not recording." if gaps else ""),
        fontsize=8.5, color=INK2, loc="left")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, facecolor=SURF, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
