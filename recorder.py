"""Always-on recording: a background thread polls the myenergi API and stores every poll in SQLite.

Each stored row has the same meaning as a row of a monitor.py CSV (poll time, device, report time, report
age, CT readings), so the log report can be drawn from any stored time range without running monitor.py.
"""
import logging
import math
import sqlite3
import threading
import time
from datetime import datetime, timezone

import flow
from charts import Log, Row
from devices import summarise

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    poll   INTEGER NOT NULL,   -- poll time, unix seconds (UTC)
    device TEXT    NOT NULL,   -- eddi / zappi / harvi / libbi ...
    sno    TEXT    NOT NULL,
    report INTEGER,            -- the device's own last-report time, unix seconds; NULL if unknown
    age    REAL,               -- poll - report, seconds
    ct1 REAL, ct2 REAL, ct3 REAL
);
CREATE INDEX IF NOT EXISTS readings_poll ON readings (poll);
CREATE INDEX IF NOT EXISTS readings_device ON readings (device, sno, poll);
-- The power-flow figures behind the Overview, one row per poll (W; NULL when not measured).
CREATE TABLE IF NOT EXISTS flows (
    poll  INTEGER PRIMARY KEY,
    grid  REAL, solar REAL, eddi REAL, zappi REAL, home REAL
);
"""

# Each reading counts for the time until the next poll, but never more than this: across a recording
# pause the last value is not stretched over hours.
MAX_STEP_S = 60


def _utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


class Store:
    """SQLite store. One connection per thread; the default rollback journal (not WAL) is used because
    Unraid's /mnt/user share is a FUSE filesystem where WAL's shared memory file is unreliable."""

    def __init__(self, path):
        self.path = str(path)
        self._local = threading.local()
        self._conn().executescript(SCHEMA)

    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            self._local.conn = c
        return c

    def insert(self, poll_ts, devices):
        rows = []
        for d in devices:
            report = None
            if d["report_utc"]:
                report = int(datetime.strptime(d["report_utc"], "%Y-%m-%d %H:%M:%S")
                             .replace(tzinfo=timezone.utc).timestamp())
            ct = {c["slot"]: _num(c["watts"]) for c in d["cts"]}
            rows.append((int(poll_ts), d["kind"], d["sno"], report, d["age_s"], ct.get(1), ct.get(2), ct.get(3)))
        with self._conn() as c:
            c.executemany("INSERT INTO readings VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def span(self):
        """(first, last) poll time as unix seconds, or None when nothing is stored yet."""
        lo, hi = self._conn().execute("SELECT MIN(poll), MAX(poll) FROM readings").fetchone()
        return None if lo is None else (lo, hi)

    def last_poll_before(self, ts):
        return self._conn().execute("SELECT MAX(poll) FROM readings WHERE poll <= ?", (int(ts),)).fetchone()[0]

    def load_log(self, start_ts, end_ts):
        """A charts.Log of the Harvi and Eddi rows polled in [start, end]; same shape as a loaded CSV."""
        lg = Log()
        cur = self._conn().execute(
            "SELECT poll, device, sno, report, age, ct1 FROM readings "
            "WHERE poll BETWEEN ? AND ? AND device IN ('harvi', 'eddi') ORDER BY poll",
            (int(start_ts), int(end_ts)))
        for poll, device, sno, report, age, ct1 in cur:
            lg.rows[(device, sno)].append(Row(_utc(poll), _utc(report) if report is not None else None,
                                              math.nan if age is None else age,
                                              math.nan if ct1 is None else ct1))
        return lg

    def recent_points(self, kind, sno, since_ts):
        """CT readings for the live chart: one point per distinct report since `since_ts`."""
        out = []
        for report, *cts in self._conn().execute(
                "SELECT report, ct1, ct2, ct3 FROM readings WHERE device = ? AND sno = ? AND poll >= ? "
                "AND report IS NOT NULL ORDER BY poll", (kind, sno, int(since_ts))):
            if out and report <= out[-1][0]:
                continue
            out.append((report, {i + 1: v for i, v in enumerate(cts) if v is not None}))
        return out

    def insert_flow(self, poll_ts, v):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO flows VALUES (?, ?, ?, ?, ?, ?)",
                      (int(poll_ts), v["grid"], v["solar"], v["eddi"], v["zappi"], v["home"]))

    def backfill_flows(self, status):
        """Work out flows for polls recorded before the flows table existed, from the stored CT readings.
        CT purposes aren't stored per reading, so the current ones (from `status`) are used. Returns rows added."""
        c = self._conn()
        first = c.execute("SELECT MIN(poll) FROM flows").fetchone()[0]
        names = {}                                     # (device, sno) -> {slot: CT name}
        for kind, recs in status.items():
            for r in recs if isinstance(recs, list) else []:
                if isinstance(r, dict) and "sno" in r:
                    names[(kind, str(r["sno"]))] = {i: r.get(f"ectt{i}") or "None" for i in (1, 2, 3)}
        if not names:
            return 0
        where, args = ("WHERE poll < ?", (first,)) if first is not None else ("", ())
        rows, added, polls = [], 0, {}

        def flush():
            nonlocal added
            out = []
            for poll, devs in polls.items():
                st = {}
                for (kind, sno), cts in devs.items():
                    rec = {"sno": sno}
                    for slot, w in cts.items():
                        rec[f"ectp{slot}"], rec[f"ectt{slot}"] = w, names.get((kind, sno), {}).get(slot, "None")
                    if kind in ("eddi", "zappi"):     # diverted power = the device's Internal Load CT
                        rec["div"] = sum(w for slot, w in cts.items()
                                         if str(rec[f"ectt{slot}"]).lower() == "internal load")
                    st.setdefault(kind, []).append(rec)
                v = flow.compute(st)
                out.append((poll, v["grid"], v["solar"], v["eddi"], v["zappi"], v["home"]))
            with self._conn() as cc:
                cc.executemany("INSERT OR IGNORE INTO flows VALUES (?, ?, ?, ?, ?, ?)", out)
            added += len(out)
            polls.clear()

        for poll, kind, sno, *cts in c.execute(
                f"SELECT poll, device, sno, ct1, ct2, ct3 FROM readings {where} ORDER BY poll", args).fetchall():
            if len(polls) >= 5000 and poll not in polls:
                flush()
            polls.setdefault(poll, {})[(kind, sno)] = {i + 1: w for i, w in enumerate(cts) if w is not None}
        if polls:
            flush()
        return added

    def energy_by_half_hour(self, start_ts, end_ts):
        """{half-hour start (unix s): (home kWh, of which from the grid kWh, seconds covered)} for [start, end).

        Each reading counts until the next poll, capped at MAX_STEP_S. The home's grid share is the grid
        import as a share of everything being supplied at that moment (home + hot water + car + battery
        charging); the rest came from solar or the battery. When grid power is unknown, the home's use counts
        as grid, so self-powered is never overstated."""
        q = """
            SELECT (poll / 1800) * 1800 AS hh, SUM(w * d) / 3600000.0, SUM(g * d) / 3600000.0, SUM(d) FROM (
                SELECT poll, w, d,
                       CASE WHEN grid IS NULL THEN w
                            WHEN demand <= 0 THEN 0
                            ELSE w * MIN(1.0, MAX(grid, 0) / demand) END AS g
                FROM (
                    SELECT poll, home AS w, grid,
                           home + COALESCE(eddi, 0) + COALESCE(zappi, 0) + MAX(-COALESCE(solar, 0), 0) AS demand,
                           MIN(LEAD(poll) OVER (ORDER BY poll) - poll, ?) AS d
                    FROM flows WHERE poll >= ? AND poll < ?))
            WHERE d IS NOT NULL AND w IS NOT NULL GROUP BY hh"""
        return {hh: (kwh, grid, cov) for hh, kwh, grid, cov in
                self._conn().execute(q, (MAX_STEP_S, int(start_ts), int(end_ts) + MAX_STEP_S))
                if hh < end_ts}

    def prune(self, before_ts):
        with self._conn() as c:
            c.execute("DELETE FROM flows WHERE poll < ?", (int(before_ts),))
            return c.execute("DELETE FROM readings WHERE poll < ?", (int(before_ts),)).rowcount


class Recorder(threading.Thread):
    """Polls every `interval` seconds for as long as the process runs."""

    def __init__(self, store, get_status, interval=15, retention_days=180):
        super().__init__(name="recorder", daemon=True)
        self.store, self.get_status = store, get_status
        self.interval, self.retention_s = max(5, interval), retention_days * 86400
        self.stop_event = threading.Event()
        self.last_ok = None          # unix time of the last successful poll
        self.last_error = None       # (unix time, message) of the most recent failure
        self.latest = None           # (unix time, raw status) of the last successful poll, for the dashboard
        self._last_prune = 0.0
        self._backfilled = False

    def poll_once(self, now=None):
        now = now or time.time()
        try:
            status = self.get_status()
            self.store.insert(now, summarise(status, now=_utc(now)))
            self.store.insert_flow(now, flow.compute(status))
            self.last_ok, self.latest = now, (now, status)
            if not self._backfilled:                 # once per start: flows for polls recorded before they existed
                self._backfilled = True
                try:
                    n = self.store.backfill_flows(status)
                    if n:
                        log.info("recorder: worked out power flows for %s earlier polls", n)
                except sqlite3.Error as e:
                    log.warning("recorder: flow backfill failed: %s", e)
        except Exception as e:                       # keep recording through API or network hiccups
            self.last_error = (now, str(e))
            log.warning("recorder: poll failed: %s", e)
        if now - self._last_prune > 3600:
            self._last_prune = now
            try:
                self.store.prune(now - self.retention_s)
            except sqlite3.Error as e:
                log.warning("recorder: prune failed: %s", e)

    def run(self):
        log.info("recorder: polling every %s s into %s", self.interval, self.store.path)
        next_t = time.time()
        while not self.stop_event.is_set():
            self.poll_once()
            next_t += self.interval
            delay = next_t - time.time()
            if delay < 0:                            # fell behind (slow API): don't burst to catch up
                next_t, delay = time.time(), 0
            self.stop_event.wait(delay)
