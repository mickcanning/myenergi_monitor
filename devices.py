"""Turn the raw myenergi status payload into something easy to display."""
import re
from datetime import datetime, timezone

DEVICE_ORDER = ["eddi", "zappi", "libbi", "harvi"]
LABELS = {"eddi": "Eddi", "zappi": "Zappi", "libbi": "Libbi", "harvi": "Harvi"}
_CT_KEY = re.compile(r"^ect[pt]\d$")
_SHOWN = {"sno", "dat", "tim", "frq", "vol", "isVHubEnabled", "fwv"}


def parse_ts(rec):
    """'dat' = dd-mm-YYYY, 'tim' = HH:MM:SS, both UTC."""
    if "dat" not in rec or "tim" not in rec:
        return None
    try:
        return datetime.strptime(f"{rec['dat']} {rec['tim']}", "%d-%m-%Y %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _scaled(value, divisor_if_over, divisor):
    """Best-effort unit scaling: the API's units for frq/vol vary by device."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v / divisor if v > divisor_if_over else v


def is_hub(rec):
    """An Eddi or Zappi with isVHubEnabled set is the hub: the master that reports the system to the cloud."""
    return str(rec.get("isVHubEnabled", "")).strip().lower() in ("true", "1")


def firmware(rec):
    """fwv looks like '3202S5.754': the part after 'S' is the version myenergi's app shows (5.754).
    Harvis report '...S0.000' (no version over the API), which is treated as not reported."""
    raw = str(rec.get("fwv") or "").strip()
    version = raw.rsplit("S", 1)[-1] if "S" in raw else raw
    if not version or version.strip("0.") == "":
        return {"raw": raw or None, "version": None}
    return {"raw": raw, "version": version}


def format_duration(seconds):
    """42 -> '42s', 312 -> '5m 12s', 54025 -> '15h 0m 25s', 190000 -> '2d 4h 46m 40s'."""
    if seconds is None:
        return ""
    s = max(0, int(round(seconds)))
    parts, started = [], False
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        n, s = divmod(s, size)
        if n or started:                 # from the first non-zero unit down, keep every unit (e.g. 1h 0m 5s)
            parts.append(f"{n}{unit}")
            started = True
    parts.append(f"{s}s")
    return " ".join(parts)


def freshness(age):
    """Neutral wording: a quiet Harvi is not necessarily a fault (it runs on harvested power)."""
    if age is None:
        return "unknown"
    if age <= 60:
        return "fresh"
    if age <= 300:
        return "delayed"
    return "silent"


def summarise(status, names=None, now=None):
    """Return a list of device dicts, ordered Eddi, Zappi, Libbi, Harvi, then anything else."""
    names = names or {}
    now = now or datetime.now(timezone.utc)
    others = sorted(
        k for k, v in status.items()
        if k not in DEVICE_ORDER and isinstance(v, list) and v and isinstance(v[0], dict) and "sno" in v[0]
    )
    devices = []
    for kind in [k for k in DEVICE_ORDER if k in status] + others:
        for rec in status.get(kind, []):
            sno = str(rec.get("sno", ""))
            ts = parse_ts(rec)
            age = (now - ts).total_seconds() if ts else None
            cts = []
            for i in (1, 2, 3):
                watts = rec.get(f"ectp{i}")
                if watts is None:
                    continue
                cts.append({"slot": i, "name": rec.get(f"ectt{i}") or "None", "watts": watts})
            extra = {k: v for k, v in rec.items() if k not in _SHOWN and not _CT_KEY.match(k)}
            devices.append({
                "kind": kind,
                "label": LABELS.get(kind, kind.title()),
                "is_hub": is_hub(rec),
                "firmware": firmware(rec),
                "sno": sno,
                "name": names.get(sno),
                "report_utc": ts.strftime("%Y-%m-%d %H:%M:%S") if ts else None,
                "age_s": None if age is None else round(age, 1),
                "freshness": freshness(age),
                "cts": cts,
                "frequency_hz": _scaled(rec.get("frq"), 100, 100),
                "voltage_v": _scaled(rec.get("vol"), 1000, 10),
                "extra": extra,
            })
    return devices
