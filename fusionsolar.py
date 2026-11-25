"""Optional: battery and solar figures from a Huawei FusionSolar plant (SUN2000 inverter, LUNA2000 battery).

Huawei's installer-only Modbus and its OpenAPI aren't always available to an owner, so this reads the FusionSolar
website with your own login through fusion_solar_py (an unofficial client). Every call fails softly (returns None)
so the rest of the app carries on. Read-only: nothing here changes any setting (the library's
active_power_control is never called). Enabled when FUSIONSOLAR_USER and FUSIONSOLAR_PASSWORD are set.

FusionSolar keeps one reading every 5 minutes and publishes it about 5 minutes later, so "now" is up to 10
minutes old. The login session is saved to a file and reused, because frequent logins bring up a captcha.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, time as dtime

log = logging.getLogger(__name__)

# The library's own browser identity, which a fresh login sets but reused cookies don't.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/119.0.0.0 Safari/537.36")
LOGIN_BACKOFF_S = 1800          # after a failed login, wait this long before trying again (captcha guard)
NO_VALUE = 1e300                # FusionSolar fills slots without a reading with 1.8e308

# Device history signals (5-minute readings).
BATTERY_SIGNALS = ["30005", "30007"]            # charge(+)/discharge(-) kW, state of charge %
INVERTER_SIGNALS = ["31001", "31002", "31004", "31005", "30017"]  # PV1 V, A, PV2 V, A, total input kW
PV_STRINGS = (("31001", "31002"), ("31004", "31005"))


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if abs(f) > NO_VALUE else f


def parse_flow(flow):
    """The plant's energy-flow diagram -> {soc %, battery W (+ charging), solar W, load W, charged_kwh,
    discharged_kwh (today), mode} with None for anything missing."""
    out = dict.fromkeys(("soc", "battery", "solar", "load", "charged_kwh", "discharged_kwh", "mode"))
    for n in (flow.get("data") or {}).get("flow", {}).get("nodes", []) or []:
        kind = str(n.get("name", "")).rsplit(".", 1)[-1]
        tips = n.get("deviceTips") or {}
        if kind == "energy_store":
            out["soc"] = _f(tips.get("SOC"))
            kw = _f(tips.get("BATTERY_POWER"))
            out["battery"] = None if kw is None else kw * 1000
            out["charged_kwh"] = _f(tips.get("CHARGE_CAPACITY"))
            out["discharged_kwh"] = _f(tips.get("DISCHARGE_CAPACITY"))
            out["mode"] = tips.get("CHARGE_MODE_VALUE") or None
        elif kind == "string":                       # the PV strings: solar power, kW
            kw = _f(n.get("value"))
            out["solar"] = None if kw is None else kw * 1000
        elif kind == "electricalLoad":
            kw = _f(n.get("value"))
            out["load"] = None if kw is None else kw * 1000
    return out


def _points(sig):
    """{slot start (unix s): value} from one device-history signal, skipping empty slots."""
    out = {}
    for p in (sig or {}).get("pmDataList", []) or []:
        v, t = _f(p.get("counterValue")), p.get("startTime")
        if v is not None and isinstance(t, (int, float)):
            out[int(t)] = v
    return out


def parse_history(battery, inverter):
    """Battery and inverter day histories -> [(ts, soc %, battery W (+ charging), solar W)], one per
    5-minute slot that has any reading. Solar is the PV strings' DC power (volts x amps)."""
    soc, batt = _points((battery or {}).get("30007")), _points((battery or {}).get("30005"))
    pv = {}
    for v_id, a_id in PV_STRINGS:
        volts, amps = _points((inverter or {}).get(v_id)), _points((inverter or {}).get(a_id))
        for t in volts.keys() & amps.keys():
            pv[t] = pv.get(t, 0.0) + volts[t] * amps[t]
    rows = []
    for t in sorted(soc.keys() | batt.keys() | pv.keys()):
        b = batt.get(t)
        rows.append((t, soc.get(t), None if b is None else b * 1000, pv.get(t)))
    return rows


class FusionSolarClient:
    def __init__(self, user, password, subdomain="region01eu5", session_path=None):
        self.user, self.password, self.subdomain = user, password, subdomain or "region01eu5"
        self.session_path = session_path
        self._client = None
        self._ids = None                 # (plant, battery, inverter)
        self._lock = threading.Lock()
        self._no_login_until = 0.0
        self.last_error = None

    # -- session ----------------------------------------------------------------
    def _connect(self):
        from fusion_solar_py.client import FusionSolarClient as Lib   # lazy: only needed when configured
        cookies = None
        if self.session_path:
            try:
                with open(self.session_path) as f:
                    cookies = json.load(f)
            except (OSError, ValueError):
                cookies = None
        if cookies:
            try:
                c = Lib(self.user, self.password, huawei_subdomain=self.subdomain, cookies=cookies)
                if c.is_session_active():
                    c._session.headers["User-Agent"] = UA
                    if c.keep_alive():               # sets the "roarand" token POST requests need
                        return c
            except Exception as e:                  # noqa: BLE001 - stale cookies: log in afresh
                log.info("fusionsolar: saved session not usable (%s); logging in", type(e).__name__)
        if time.monotonic() < self._no_login_until:
            raise RuntimeError("waiting before logging in again after a failed login")
        try:
            c = Lib(self.user, self.password, huawei_subdomain=self.subdomain)
        except Exception:
            self._no_login_until = time.monotonic() + LOGIN_BACKOFF_S
            raise
        log.info("fusionsolar: logged in")
        return c

    def _save_session(self):
        if not (self.session_path and self._client):
            return
        try:
            with open(self.session_path, "w") as f:
                json.dump(self._client.get_cookies(), f)
            os.chmod(self.session_path, 0o600)
        except OSError as e:
            log.warning("fusionsolar: couldn't save the session: %s", e)

    def _call(self, fn):
        """Run fn(client) with a working session; None (and the error noted) on any failure."""
        with self._lock:
            try:
                if self._client is None:
                    self._client = self._connect()
                    self._save_session()
                result = fn(self._client)
                self.last_error = None
                return result
            except Exception as e:                  # unofficial API: never let it break the app
                self.last_error = f"{type(e).__name__}: {e}"
                self._client = None                  # reconnect (reusing the saved session) next time
                log.warning("fusionsolar: %s", self.last_error)
                return None

    def _find_ids(self, c):
        if self._ids is None:
            plant = c.get_plant_ids()[0]
            nodes = c.get_plant_flow(plant)["data"]["flow"]["nodes"]
            dev = {str(n.get("name", "")).rsplit(".", 1)[-1]: (n.get("devIds") or [None])[0] for n in nodes}
            self._ids = (plant, dev.get("energy_store"), dev.get("inverter"))
        return self._ids

    # -- data ---------------------------------------------------------------------
    def now(self):
        """parse_flow() of the plant's current energy flow, or None."""
        def get(c):
            plant = self._find_ids(c)[0]
            return parse_flow(c.get_plant_flow(plant))
        return self._call(get)

    def day(self, d, tz):
        """parse_history() rows for local date d, or None when FusionSolar can't be read."""
        def get(c):
            _, battery, inverter = self._find_ids(c)
            # Any time within the day selects it; noon keeps clear of DST changes and time-zone edges.
            ms = int(datetime.combine(d, dtime(12), tz).timestamp() * 1000)
            b = c.get_battery_day_stats(battery, signalIds=BATTERY_SIGNALS, query_time=ms) if battery else {}
            i = c.get_inverter_day_stats(inverter, signalIds=INVERTER_SIGNALS, query_time=ms) if inverter else {}
            return parse_history(b, i)
        return self._call(get)
