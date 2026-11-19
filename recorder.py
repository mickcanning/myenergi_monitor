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
"""


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

    def prune(self, before_ts):
        with self._conn() as c:
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

    def poll_once(self, now=None):
        now = now or time.time()
        try:
            status = self.get_status()
            self.store.insert(now, summarise(status, now=_utc(now)))
            self.last_ok, self.latest = now, (now, status)
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
