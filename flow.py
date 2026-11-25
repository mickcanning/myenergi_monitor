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
    "sun": '<circle cx="12" cy="12" r="4.5"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6'
           'M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/>',
    "battery": '<rect x="3" y="7" width="16" height="10" rx="2"/><path d="M19 10.5h2v3h-2M8 12h6M11 9v6"/>',
    "grid": '<path d="M12 2 7.5 22M12 2l4.5 20M7 7h10M5 11h14M8.5 11 15 17M15.5 11 9 17M7 17h10"/>',
    "home": '<path d="M3 11.5 12 4l9 7.5M5.5 9.5V20h13V9.5"/><path d="M10 20v-5.5h4V20"/>',
    "eddi": '<rect x="6" y="2.5" width="12" height="19" rx="3"/>'
            '<path d="M12 8.5c-2 2.4-3 4-3 5.5a3 3 0 0 0 6 0c0-1.5-1-3.1-3-5.5z"/>',
    "zappi": '<path d="M2.5 17v-3.5L5 8.5h10.5l3.5 5h1.5a1.5 1.5 0 0 1 1.5 1.5v2z"/>'
             '<path d="M5.5 13.5h13"/><circle class="tyre" cx="7" cy="17.5" r="2.2"/>'
             '<circle class="tyre" cx="17" cy="17.5" r="2.2"/>',
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


def tank_temp(rec):
    """The Eddi's probe 1 reading, or None. 127 means no probe. Without a probe some Eddis report a fixed value
    (62 here, unchanged through a boost), so callers only show it once it has been seen to change."""
    t = _num((rec or {}).get("tp1"))
    return t if t is not None and 0 < t < 127 else None


def _eddi_caption(rec, show_temp=False, tank=None):
    """[state, tank temperature] as two short lines. The temperature comes from `tank` (e.g. the heating
    system's hot water probe) when given, else from the Eddi only when it's known to be a real reading."""
    lines = [EDDI_STATES.get(int(_num(rec.get("sta")) or 0), "")]
    t = tank if tank is not None else (tank_temp(rec) if show_temp else None)
    if t is not None:
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
SOC_R = 37             # the battery's state-of-charge gauge, just outside its node
GAUGE_START, GAUGE_SWEEP = 135, 270     # degrees, clockwise from east: 0% at bottom-left, 100% at bottom-right


def _link(pos, w, outward, cls, edge=NODE_R + 4):
    """A line between the hub edge and a node edge, with an arrowhead pointing the way power flows.
    `outward` = power flows from the hub to the node when w is positive."""
    cx, cy = CENTRE
    x, y = pos
    d = math.hypot(x - cx, y - cy)
    ux, uy = (x - cx) / d, (y - cy) / d
    a = (cx + ux * (HUB_R + 4), cy + uy * (HUB_R + 4))
    b = (x - ux * edge, y - uy * edge)
    active = w is not None and abs(w) >= IDLE_W
    to_node = (w or 0) > 0 if outward else (w or 0) < 0
    mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    angle = math.degrees(math.atan2(uy, ux)) + (0 if to_node else 180)
    src, dst = (a, b) if to_node else (b, a)              # source -> destination, the way the arrowheads travel
    # arrowheads glide along the link, faster with more power (log scale): `dur` seconds per 30 units of travel,
    # ~2.2 s at 100 W and ~0.9 s at 3 kW. A few are spread along the link so it always shows the direction.
    dur = min(2.8, max(0.6, 2.2 - 0.9 * math.log10(max(abs(w or 0), IDLE_W) / 100)))
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    travel = length / 30 * dur                            # seconds for one arrowhead to cross the link
    count = max(2, round(length / 26))
    arrows = [round(-travel * k / count, 3) for k in range(count)]   # staggered start times
    return {"x1": round(a[0], 1), "y1": round(a[1], 1), "x2": round(b[0], 1), "y2": round(b[1], 1),
            "fx1": round(src[0], 1), "fy1": round(src[1], 1), "fx2": round(dst[0], 1), "fy2": round(dst[1], 1),
            "travel": round(travel, 3), "arrows": arrows,
            "mx": round(mx, 1), "my": round(my, 1), "angle": round(angle, 1),
            "active": active, "cls": cls if active else "idle"}


def split_solar(combined, battery):
    """Split the Harvi's live solar & battery reading using FusionSolar ({soc, solar W, age s}).

    Solar changes slowly, so it is FusionSolar's latest 5-minute reading (up to ~10 minutes old). The battery is
    what reacts to the house second by second, so it gets the rest of the live reading: Solar + Battery always
    equals what the Harvi measures. Returns (solar W, battery W: + discharging into the house, - charging)."""
    solar = battery.get("solar")
    solar = 0.0 if solar is None or solar < IDLE_W else solar      # night: a few W of noise on the strings
    return solar, combined - solar


def soc_gauge(pos, soc):
    """A speedometer-style state-of-charge gauge around a node: a 270° arc from bottom-left (0%) clockwise over
    the top to bottom-right (100%), open at the bottom where the node's link leaves, filled up to `soc`. Returns
    the dash patterns for the track and the filled arc (a circle rotated to start at 0%) and the rotation."""
    x, y = pos
    circ = 2 * math.pi * SOC_R
    sweep = circ * GAUGE_SWEEP / 360
    return {"track": f"{sweep:.1f} {circ:.1f}", "fill": f"{sweep * soc / 100:.1f} {circ:.1f}",
            "rotate": f"{GAUGE_START} {x} {y}"}


def build(status, show_tank_temp=False, tank=None, rooms=None, battery=None):
    """Everything the Overview template draws. `battery` ({soc, solar W, age s} from FusionSolar) splits the
    solar & battery node into Solar and Battery; without it there's one combined node."""
    v = compute(status)
    nodes = []

    def add(key, pos, label, w, caption, outward, cls, top, **extra):
        link = _link(pos, w, outward, cls, extra.pop("edge", NODE_R + 4))
        nodes.append({"key": key, "x": pos[0], "y": pos[1], "label": label, "value": fmt_power(w),
                      "caption": caption, "icon": ICONS[extra.pop("icon", key)], "link": link, "active": link["active"],
                      "cls": link["cls"], "top": top, "show_caption": True, **extra})

    split = v["solar"] is not None and battery is not None and battery.get("soc") is not None
    if split:
        s, b = split_solar(v["solar"], battery)
        mins = max(1, round((battery.get("age") or 0) / 60))
        add("solar", (62, 80), "Solar", s, ["Generating" if s >= IDLE_W else "Idle", f"{mins} min ago"], False,
            "src-solar", True, icon="sun")
        soc = max(0.0, min(100.0, battery["soc"]))
        state = "Charging" if b <= -IDLE_W else "Discharging" if b >= IDLE_W else "Idle"
        # charging draws power from the hub like a load; discharging supplies it like a source
        add("battery", (190, 80), f"Battery {soc:.0f}%", b, [state], False, "src-solar" if b > 0 else "load",
            True, show_caption=False, soc=soc, edge=SOC_R + 5, gauge=soc_gauge((190, 80), soc))
    elif v["solar"] is not None:
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
        add("grid", (318 if split else 295, 80), "Grid", g, [cap], False, "src-grid" if g > 0 else "src-solar", True)

    bottom = [k for k in ("home", "eddi", "zappi") if v[k] is not None]
    xs = {1: [190], 2: [115, 265], 3: [62, 190, 318]}.get(len(bottom), [])
    for k, x in zip(bottom, xs):
        w, y = v[k], 318
        if k == "home":
            cap = [f"{r['name']} {r['current']:.1f} °C" for r in (rooms or [])[:2]] or \
                ["Using" if w >= IDLE_W else "Idle"]           # room temperatures from the heating system
            add(k, (x, y), "Home", w, cap, True, "load", False)
        elif k == "eddi":
            add(k, (x, y), "Hot water", w, _eddi_caption(v["eddi_rec"], show_tank_temp, tank), True, "load", False)
        else:
            add(k, (x, y), "Car", w, _zappi_caption(v["zappi_rec"]), True, "load", False)

    return {"nodes": nodes, "self_pct": v["self_pct"], "centre": CENTRE, "hub_r": HUB_R, "node_r": NODE_R, "soc_r": SOC_R,
            "values": v, "split": split}
