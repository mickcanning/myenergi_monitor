"""Minimal client for the (unofficial) myenergi cloud API.

Authentication is HTTP Digest: username = hub serial number, password = API key
(generate one at myaccount.myenergi.com > Products > Advanced).
"""
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DIRECTOR = "director.myenergi.net"
# The director sometimes answers "undefined" in its server header; only trust real myenergi hostnames.
_HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.myenergi\.net$", re.I)


class MyEnergiError(Exception):
    """Raised for any problem talking to the myenergi API (message is user-safe)."""


class MyEnergiClient:
    def __init__(self, hub_serial, api_key, cache_seconds=10, timeout=15):
        if not hub_serial or not api_key:
            raise MyEnergiError("Hub serial and API key are not configured. See .env.example.")
        self.hub_serial = str(hub_serial)
        self.api_key = api_key
        self.cache_seconds = cache_seconds
        self.timeout = timeout
        self._host = None
        self._last_good_host = None   # fallback when the director sends a bad server name
        self._cache = None          # (monotonic time, data)
        self._lock = threading.Lock()

    # -- plumbing -----------------------------------------------------------
    def _opener(self, host):
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, f"https://{host}/", self.hub_serial, self.api_key)
        return urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(mgr),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    @staticmethod
    def _valid_host(asn):
        asn = (asn or "").strip()
        return asn if _HOST_RE.match(asn) else None

    def _discover_host(self):
        """The director answers with the real server in the X_MYENERGI-asn header.

        It occasionally sends "undefined" there; using that as a hostname fails with a confusing DNS
        error, so fall back to the last server that worked (or the director itself) instead.
        """
        try:
            with self._opener(DIRECTOR).open(f"https://{DIRECTOR}/cgi-jstatus-H", timeout=self.timeout) as r:
                return self._valid_host(r.headers.get("X_MYENERGI-asn")) or self._last_good_host or DIRECTOR
        except urllib.error.HTTPError as e:
            asn = self._valid_host(e.headers.get("X_MYENERGI-asn") if e.headers else None)
            if asn or self._last_good_host:
                return asn or self._last_good_host
            raise

    def _get_json(self, path):
        try:
            if self._host is None:
                self._host = self._discover_host()
            with self._opener(self._host).open(f"https://{self._host}{path}", timeout=self.timeout) as r:
                data = json.load(r)
            self._last_good_host = self._host
            return data
        except urllib.error.HTTPError as e:
            self._host = None
            if e.code == 401:
                raise MyEnergiError("Authentication failed: check the hub serial number and API key.") from None
            raise MyEnergiError(f"myenergi API returned HTTP {e.code}.") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self._host = None
            raise MyEnergiError(f"Could not reach the myenergi API ({getattr(e, 'reason', e)}).") from None
        except ValueError:
            raise MyEnergiError("myenergi API returned a response that is not valid JSON.") from None

    # -- public -------------------------------------------------------------
    def status(self):
        """Everything on the hub, flattened to {"eddi": [...], "harvi": [...], ...}.

        cgi-jstatus-* is documented by the community as a list of single-key
        dicts; a plain dict is handled too. Results are cached briefly so several
        browser tabs don't hammer the API.
        """
        with self._lock:
            now = time.monotonic()
            if self._cache and now - self._cache[0] < self.cache_seconds:
                return self._cache[1]
            data = self._get_json("/cgi-jstatus-*")
            if isinstance(data, dict):
                data = [data]
            out = {}
            for item in data if isinstance(data, list) else []:
                if not isinstance(item, dict):
                    continue
                for key, value in item.items():
                    if isinstance(value, list):
                        out.setdefault(key, []).extend(v for v in value if isinstance(v, dict))
                    else:
                        out[key] = value
            self._cache = (now, out)
            return out

    HISTORY_LETTERS = {"eddi": "E", "zappi": "Z"}

    def day_history(self, kind, sno, day):
        """The device's per-minute energy for one UTC day (`day` = 'YYYY-MM-DD'), as
        [(unix minute start, {field: joules})]. Fields include imp/exp (grid import/export, measured by the hub's
        grid CT) and h1d/h1b... (energy the device diverted or boosted). Measured continuously, not sampled."""
        letter = self.HISTORY_LETTERS.get(kind)
        if not letter or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise MyEnergiError(f"No history for {kind}.")
        data = self._get_json(f"/cgi-jday-{letter}{sno}-{day}")
        recs = next(iter(data.values()), []) if isinstance(data, dict) else data
        out = []
        for r in recs if isinstance(recs, list) else []:
            try:
                ts = datetime(int(r["yr"]), int(r["mon"]), int(r["dom"]), int(r.get("hr", 0)), int(r.get("min", 0)),
                              tzinfo=timezone.utc).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            out.append((int(ts), {k: v for k, v in r.items() if isinstance(v, (int, float)) and
                                  k not in ("yr", "mon", "dom", "hr", "min", "v1", "frq")}))
        return out
