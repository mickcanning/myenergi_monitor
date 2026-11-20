"""The Overview: where power is coming from and going to right now, worked out from the latest poll.

Roles, not products, are what matter at a glance:
  sources  Solar (and battery, when the CT is "Generation & Battery")  and  the Grid
  loads    Home, hot water (Eddi) and the car (Zappi)

Values:
  grid     sum of CTs named "Grid" (+ import, - export); the hub's own `grd` when no CT is named Grid
  solar    sum of CTs whose name contains "Generation" or "Battery" (+ generating/discharging, - charging)
  eddi     the Eddi's `div` (power going to the heater)
  zappi    the Zappi's `div` (power going to the car)
  home     what's left: grid + solar - eddi - zappi (an estimate: nothing measures the home directly)
"""
import math
from datetime import datetime, timezone

from devices import parse_ts

IDLE_W = 20            # below this a link is drawn as idle
# A Harvi powers itself from the current through its clamp, so it falls silent when that current stops, and
# the API keeps returning its last reading. A solar & battery reading older than this is taken as ~0 W
# (e.g. the battery stopped charging at 02:00 but the Harvi's last report still said -3 kW until 05:00).
SOLAR_STALE_S = 300

EDDI_STATES = {0: "Starting", 1: "Waiting for surplus", 2: "Grid demand response", 3: "Heating",
               4: "Boosting", 5: "Tank at max temperature", 6: "Stopped"}
ZAPPI_PLUG = {"A": "Car unplugged", "B1": "Car connected", "B2": "Waiting for car", "C1": "Ready to charge",
              "C2": "Charging", "F": "Fault"}
ZAPPI_MODES = {1: "Fast", 2: "Eco", 3: "Eco+", 4: "Stopped"}

# 24x24 line icons, drawn with currentColor.
ICONS = {
    "solar": '<circle cx="9" cy="9" r="3.6"/><path d="M9 1.8v1.8M9 14.4v1.8M1.8 9h1.8M14.4 9h1.8M3.9 3.9l1.3 1.3'
             'M12.8 12.8l1.3 1.3M3.9 14.1l1.3-1.3M12.8 5.2l1.3-1.3"/><rect x="12.5" y="15" width="9" height="6.5" rx="1.3"/>'
             '<path d="M21.5 17.3h.9v2h-.9M17.4 16.4l-1.4 2h2l-1.4 2"/>',
    "grid": '<path d="M12 2 7.5 22M12 2l4.5 20M7 7h10M5 11h14M8.5 11 15 17M15.5 11 9 17M7 17h10"/>',
    "home": '<path d="M3 11.5 12 4l9 7.5M5.5 9.5V20h13V9.5"/><path d="M10 20v-5.5h4V20"/>',
    "eddi": '<rect x="6" y="2.5" width="12" height="19" rx="3"/>'
            '<path d="M12 8.5c-2 2.4-3 4-3 5.5a3 3 0 0 0 6 0c0-1.5-1-3.1-3-5.5z"/>',
    "zappi": '<path d="M2.5 17v-3.5L5 8.5h10.5l3.5 5h1.5a1.5 1.5 0 0 1 1.5 1.5v2z"/>'
             '<circle cx="7" cy="17.5" r="2"/><circle cx="17" cy="17.5" r="2"/><path d="M5.5 13.5h13"/>',
}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def fmt_power(w):
    """873 -> '873 W', 1234 -> '1.2 kW'."""
    if w is None:
        return "–"
    a = abs(w)
    return f"{a / 1000:.1f} kW" if a >= 1000 else f"{a:.0f} W"


def _cts(status, match):
    total, found = 0.0, False
    for kind, recs in status.items():
        if not isinstance(recs, list):
            continue
        for rec in recs:
            if not isinstance(rec, dict):
                continue
            for i in range(1, 7):
                name = str(rec.get(f"ectt{i}") or "").strip().lower()
                w = _num(rec.get(f"ectp{i}"))
                if w is not None and match(name):
                    total, found = total + w, True
    return total if found else None


def _records(status, kind):
    return [r for r in status.get(kind, []) or [] if isinstance(r, dict)]


def is_hub_rec(r):
    return str(r.get("isVHubEnabled", "")).lower() in ("true", "1")


def _hub(status):
    for kind in ("eddi", "zappi", "libbi"):
        for r in _records(status, kind):
            if str(r.get("isVHubEnabled", "")).lower() in ("true", "1"):
                return r
    for kind in ("eddi", "zappi", "libbi"):
        if _records(status, kind):
            return _records(status, kind)[0]
    return {}


def _solar_age(status, now):
    """Seconds since the newest report among the devices whose CTs measure solar & battery, or None."""
    ages = []
    for recs in status.values():
        for r in recs if isinstance(recs, list) else []:
            if not isinstance(r, dict):
                continue
            if any(("generation" in str(r.get(f"ectt{i}", "")).lower() or "battery" in str(r.get(f"ectt{i}", "")).lower())
                   and r.get(f"ectp{i}") is not None for i in range(1, 7)):
                ts = parse_ts(r)
                if ts:
                    ages.append((now - ts).total_seconds())
    return min(ages) if ages else None


def compute(status, now=None):
    """The numbers behind the Overview, independent of layout."""
    now = now or datetime.now(timezone.utc)
    hub = _hub(status)
    grid = _cts(status, lambda n: n == "grid")
    if grid is None:
        grid = _num(hub.get("grd"))
    solar = _cts(status, lambda n: "generation" in n or "battery" in n)
    solar_age = _solar_age(status, now)
    solar_stale = solar is not None and solar_age is not None and solar_age > SOLAR_STALE_S
    if solar_stale:
        solar = 0.0
    solar_has_battery = any("battery" in str(r.get(f"ectt{i}", "")).lower()
                            for recs in status.values() if isinstance(recs, list)
                            for r in recs if isinstance(r, dict) for i in range(1, 7))

    eddis, zappis = _records(status, "eddi"), _records(status, "zappi")
    eddi = sum(_num(r.get("div")) or 0 for r in eddis) if eddis else None
    zappi = sum(_num(r.get("div")) or 0 for r in zappis) if zappis else None

    home = None
    if grid is not None or solar is not None:
        home = max(0.0, (grid or 0) + (solar or 0) - (eddi or 0) - (zappi or 0))

    used = (home or 0) + (eddi or 0) + (zappi or 0)
    self_pct = None
    if grid is not None and used >= IDLE_W:
        self_pct = round(100 * min(1.0, max(0.0, 1 - max(grid, 0) / used)))

    return {"grid": grid, "solar": solar, "solar_age": solar_age, "solar_stale": solar_stale,
            "solar_has_battery": solar_has_battery, "eddi": eddi,
            "zappi": zappi, "home": home, "self_pct": self_pct,
            "eddi_rec": eddis[0] if eddis else None, "zappi_rec": zappis[0] if zappis else None}


def _eddi_caption(rec):
    """[state, tank temperature] as two short lines."""
    lines = [EDDI_STATES.get(int(_num(rec.get("sta")) or 0), "")]
    t = _num(rec.get("tp1"))
    if t is not None and 0 < t < 127:              # 127 = no probe fitted
        lines.append(f"Tank {t:.0f} °C")
    return [x for x in lines if x]


def _zappi_caption(rec):
    """[plug state, charge mode] as two short lines."""
    lines = [ZAPPI_PLUG.get(str(rec.get("pst", "")), ""), ZAPPI_MODES.get(int(_num(rec.get("zmo")) or 0), "")]
    if lines[1]:
        lines[1] += " mode"
    return [x for x in lines if x]


# Layout in a 380 x 430 viewBox: sources across the top, the hub in the middle, loads across the bottom.
CENTRE, HUB_R, NODE_R = (190, 205), 42, 32


def _link(pos, w, outward, cls):
    """A line between the hub edge and a node edge, with an arrowhead pointing the way power flows.
    `outward` = power flows from the hub to the node when w is positive."""
    cx, cy = CENTRE
    x, y = pos
    d = math.hypot(x - cx, y - cy)
    ux, uy = (x - cx) / d, (y - cy) / d
    a = (cx + ux * (HUB_R + 4), cy + uy * (HUB_R + 4))
    b = (x - ux * (NODE_R + 4), y - uy * (NODE_R + 4))
    active = w is not None and abs(w) >= IDLE_W
    to_node = (w or 0) > 0 if outward else (w or 0) < 0
    mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    angle = math.degrees(math.atan2(uy, ux)) + (0 if to_node else 180)
    return {"x1": round(a[0], 1), "y1": round(a[1], 1), "x2": round(b[0], 1), "y2": round(b[1], 1),
            "mx": round(mx, 1), "my": round(my, 1), "angle": round(angle, 1),
            "active": active, "cls": cls if active else "idle"}


def build(status):
    """Everything the Overview template draws."""
    v = compute(status)
    nodes = []

    def add(key, pos, label, w, caption, outward, cls, top):
        link = _link(pos, w, outward, cls)
        nodes.append({"key": key, "x": pos[0], "y": pos[1], "label": label, "value": fmt_power(w),
                      "caption": caption, "icon": ICONS[key], "link": link, "active": link["active"],
                      "cls": link["cls"], "top": top})

    if v["solar"] is not None:
        s = v["solar"]
        if v["solar_stale"]:
            cap = "Idle (Harvi quiet)"
        elif s <= -IDLE_W:
            cap = "Battery charging"
        elif s >= IDLE_W:
            cap = "Generating"
        else:
            cap = "Idle"
        add("solar", (85, 80), "Solar & battery" if v["solar_has_battery"] else "Solar", s, [cap], False,
            "src-solar", True)
    if v["grid"] is not None:
        g = v["grid"]
        cap = "Importing" if g >= IDLE_W else "Exporting" if g <= -IDLE_W else "Idle"
        add("grid", (295, 80), "Grid", g, [cap], False, "src-grid" if g > 0 else "src-solar", True)

    bottom = [k for k in ("home", "eddi", "zappi") if v[k] is not None]
    xs = {1: [190], 2: [115, 265], 3: [62, 190, 318]}.get(len(bottom), [])
    for k, x in zip(bottom, xs):
        w, y = v[k], 318
        if k == "home":
            add(k, (x, y), "Home", w, ["Using" if w >= IDLE_W else "Idle"], True, "load", False)
        elif k == "eddi":
            add(k, (x, y), "Hot water", w, _eddi_caption(v["eddi_rec"]), True, "load", False)
        else:
            add(k, (x, y), "Car", w, _zappi_caption(v["zappi_rec"]), True, "load", False)

    return {"nodes": nodes, "self_pct": v["self_pct"], "centre": CENTRE, "hub_r": HUB_R, "node_r": NODE_R,
            "values": v}
