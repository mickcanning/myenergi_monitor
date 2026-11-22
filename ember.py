"""Optional: zone temperatures from an EPH Controls Ember heating system (hot water tank probe, rooms).

Uses pyephember, an unofficial client for the Ember cloud API, so it may stop working if EPH change their API;
every call here fails softly (returns None) so the rest of the app carries on. Read-only: nothing here changes
any heating setting. Enabled when EMBER_EMAIL and EMBER_PASSWORD are set.
"""
import logging
import threading
import time

log = logging.getLogger(__name__)

HOT_WATER_DEVICE_TYPE = 4      # Ember's device type for a hot water zone (heating zones are 2)


class EmberClient:
    def __init__(self, email, password, cache_seconds=60):
        self.email, self.password, self.cache_seconds = email, password, cache_seconds
        self._api = None
        self._cache = None              # (monotonic time, zones)
        self._lock = threading.Lock()
        self.last_error = None

    def _connect(self):
        from pyephember import pyephember as pe       # imported lazily: only needed when Ember is configured
        self._pe = pe
        self._api = pe.EphEmber(self.email, self.password)

    def zones(self):
        """[{name, current, target, active, hot_water}] or None when Ember can't be reached."""
        with self._lock:
            now = time.monotonic()
            if self._cache and now - self._cache[0] < self.cache_seconds:
                return self._cache[1]
            try:
                if self._api is None:
                    self._connect()
                raw = self._api.get_zones()
                pe = self._pe
                out = []
                for z in raw:
                    out.append({"name": pe.zone_name(z), "current": _num(pe.zone_current_temperature(z)),
                                "target": _num(pe.zone_target_temperature(z)), "active": bool(pe.zone_is_active(z)),
                                "hot_water": z.get("deviceType") == HOT_WATER_DEVICE_TYPE
                                or "water" in str(pe.zone_name(z)).lower()})
                self._cache = (now, out)
                self.last_error = None
                return out
            except Exception as e:                    # unofficial API: never let it break the app
                self.last_error = str(e)
                self._api = None                      # log in again next time
                log.warning("ember: couldn't read zones: %s", e)
                return None


def hot_water(zones):
    """The hot water zone from zones(), or None."""
    return next((z for z in zones or [] if z["hot_water"]), None)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
