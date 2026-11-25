"""Always-on recording: a background thread polls the myenergi API and stores every poll in SQLite.

Each stored row has the same meaning as a row of a monitor.py CSV (poll time, device, report time, report
age, CT readings), so the log report can be drawn from any stored time range without running monitor.py.
"""
import logging
import math
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone

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
    grid  REAL, solar REAL, eddi REAL, zappi REAL, home REAL,
    solar_age REAL      -- seconds since the solar & battery Harvi last reported (see flow.SOLAR_STALE_S)
);
-- The hub's own per-minute energy history (joules, measured continuously rather than sampled):
-- grid import/export, and what the Eddi and Zappi diverted or boosted.
CREATE TABLE IF NOT EXISTS minutes (
    ts INTEGER PRIMARY KEY,    -- minute start, unix seconds (UTC)
    imp REAL, exp REAL, div REAL,  -- div = eddi + zappi
    eddi REAL, zappi REAL
);
-- Which UTC days of history have been fetched; complete = the day was over when it was fetched.
CREATE TABLE IF NOT EXISTS history_days (
    day TEXT PRIMARY KEY, fetched INTEGER, complete INTEGER
);
-- Finished half-hour home energy estimates (see estimate.FIELDS), so long ranges draw quickly. It's only a
-- cache: when its columns change it gets a new name and the old one is dropped.
-- Hourly Harvi signal-quality counts (see harvi_signal.py), cached once their hour is final.
CREATE TABLE IF NOT EXISTS signal_hours (
    hour INTEGER, sno TEXT, grid TEXT, data TEXT, PRIMARY KEY (hour, sno, grid)
);
-- Things changed to improve the Harvi signal (e.g. "new antenna"), for before/after comparisons.
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, note TEXT NOT NULL
);
-- App settings, e.g. the electricity tariff (JSON).
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
-- Zone temperatures from an EPH Ember heating system (optional), every few minutes.
CREATE TABLE IF NOT EXISTS temps (
    ts INTEGER NOT NULL, zone TEXT NOT NULL, current REAL, target REAL, active INTEGER, hot_water INTEGER,
    PRIMARY KEY (ts, zone)
);
-- Distinct tank temperatures each Eddi has reported: shown only once it has reported more than one, since
-- without a probe some report a fixed value.
-- Huawei FusionSolar 5-minute readings (optional, see fusionsolar.py): battery state of charge %, battery W
-- (+ charging, - discharging) and solar W (the PV strings' DC power).
CREATE TABLE IF NOT EXISTS huawei (ts INTEGER PRIMARY KEY, soc REAL, battery REAL, solar REAL);
-- Which local days of FusionSolar history have been fetched; complete = the day was over when it was fetched.
CREATE TABLE IF NOT EXISTS huawei_days (day TEXT PRIMARY KEY, fetched INTEGER, complete INTEGER);
CREATE TABLE IF NOT EXISTS tank_temps (sno TEXT, value REAL, PRIMARY KEY (sno, value));
DROP TABLE IF EXISTS energy_hh;
DROP TABLE IF EXISTS energy_hh2;
CREATE TABLE IF NOT EXISTS energy_hh3 (
    hh INTEGER PRIMARY KEY,
    home REAL, grid REAL, var REAL, covered REAL, measured REAL, sampled REAL,
    imp REAL, exp REAL, grid_batt REAL, grid_dev REAL, eddi REAL, zappi REAL, charge REAL
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
        c = self._conn()
        c.executescript(SCHEMA)
        if "solar_age" not in {r[1] for r in c.execute("PRAGMA table_info(flows)")}:
            with c:
                c.execute("ALTER TABLE flows ADD COLUMN solar_age REAL")
        cols = {r[1] for r in c.execute("PRAGMA table_info(minutes)")}
        if "eddi" not in cols:          # older database: keep the Eddi and Zappi apart from now on
            with c:
                c.execute("ALTER TABLE minutes ADD COLUMN eddi REAL")
                c.execute("ALTER TABLE minutes ADD COLUMN zappi REAL")
                c.execute("DELETE FROM history_days")        # so the next sync fetches them again

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
            c.execute("INSERT OR REPLACE INTO flows VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (int(poll_ts), v["grid"], v["solar"], v["eddi"], v["zappi"], v["home"], v.get("solar_age")))

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
                cc.executemany("INSERT OR IGNORE INTO flows (poll, grid, solar, eddi, zappi, home) "
                               "VALUES (?, ?, ?, ?, ?, ?)", out)
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

    def fix_stale_solar(self, status):
        """Fill in solar_age for flows recorded without it, from the stored Harvi report ages, and treat
        long-stale solar & battery readings as 0 W (recomputing home). Returns rows corrected."""
        c = self._conn()
        if c.execute("SELECT 1 FROM flows WHERE solar_age IS NULL LIMIT 1").fetchone() is None:
            return 0
        snos = [(kind, str(r["sno"])) for kind, recs in status.items() for r in (recs if isinstance(recs, list) else [])
                if isinstance(r, dict) and "sno" in r and any(
                    "generation" in str(r.get(f"ectt{i}", "")).lower() or "battery" in str(r.get(f"ectt{i}", "")).lower()
                    for i in range(1, 7))]
        if not snos:
            with c:
                c.execute("UPDATE flows SET solar_age = -1 WHERE solar_age IS NULL")     # nothing to check
            return 0
        marks = " OR ".join("(r.device = ? AND r.sno = ?)" for _ in snos)
        args = [x for pair in snos for x in pair]
        fixed = 0
        with c:
            c.execute(f"""UPDATE flows SET solar_age = (
                              SELECT MIN(r.age) FROM readings r WHERE r.poll = flows.poll AND ({marks}))
                          WHERE solar_age IS NULL""", args)
            fixed = c.execute(
                "UPDATE flows SET solar = 0, home = MAX(0, COALESCE(grid, 0) - COALESCE(eddi, 0) - COALESCE(zappi, 0)) "
                "WHERE solar_age > ? AND solar IS NOT NULL AND solar != 0", (flow.SOLAR_STALE_S,)).rowcount
            c.execute("UPDATE flows SET solar_age = -1 WHERE solar_age IS NULL")
            if fixed:
                c.execute("DELETE FROM energy_hh3")              # recompute the energy figures
        return fixed

    # -- tank temperature ------------------------------------------------------
    def note_tank_temps(self, status):
        rows = [(str(r["sno"]), t) for r in status.get("eddi", []) or []
                if isinstance(r, dict) and "sno" in r and (t := flow.tank_temp(r)) is not None]
        if rows:
            with self._conn() as c:
                c.executemany("INSERT OR IGNORE INTO tank_temps VALUES (?, ?)", rows)

    def tank_temp_varies(self, sno):
        return self._conn().execute("SELECT COUNT(*) FROM tank_temps WHERE sno = ?", (str(sno),)).fetchone()[0] > 1

    # -- Ember zone temperatures ------------------------------------------------
    def insert_temps(self, ts, zones):
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO temps VALUES (?, ?, ?, ?, ?, ?)",
                          [(int(ts), z["name"], z["current"], z["target"], int(z["active"]), int(z["hot_water"]))
                           for z in zones])

    def tank_temps(self, start_ts, end_ts):
        """[(ts, current, target, active)] for the hot water zone in [start, end)."""
        return self._conn().execute(
            "SELECT ts, current, target, active FROM temps WHERE hot_water = 1 AND ts >= ? AND ts < ? ORDER BY ts",
            (int(start_ts), int(end_ts))).fetchall()

    def room_temps(self, start_ts, end_ts):
        """{zone name: [(ts, current)]} for the heating (non hot water) zones in [start, end)."""
        out = {}
        for ts, zone, cur in self._conn().execute(
                "SELECT ts, zone, current FROM temps WHERE hot_water = 0 AND current IS NOT NULL AND ts >= ? AND ts < ? "
                "ORDER BY zone, ts", (int(start_ts), int(end_ts))):
            out.setdefault(zone, []).append((ts, cur))
        return out

    def latest_temps(self):
        """(ts, [{name, current, target, active, hot_water}]) from the newest Ember reading, or None."""
        c = self._conn()
        ts = c.execute("SELECT MAX(ts) FROM temps").fetchone()[0]
        if ts is None:
            return None
        return ts, [{"name": z, "current": cur, "target": tgt, "active": bool(act), "hot_water": bool(hw)}
                    for z, cur, tgt, act, hw in c.execute(
                        "SELECT zone, current, target, active, hot_water FROM temps WHERE ts = ? ORDER BY zone", (ts,))]

    def latest_tank_temp(self):
        """(ts, current, target, active) of the newest hot water reading, or None."""
        return self._conn().execute(
            "SELECT ts, current, target, active FROM temps WHERE hot_water = 1 ORDER BY ts DESC LIMIT 1").fetchone()

    # -- settings ----------------------------------------------------------------
    def get_setting(self, key):
        row = self._conn().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key, value):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))

    # -- change log -------------------------------------------------------------
    def changes(self):
        return [{"id": i, "ts": ts, "note": note} for i, ts, note in
                self._conn().execute("SELECT id, ts, note FROM changes ORDER BY ts")]

    def add_change(self, ts, note):
        with self._conn() as c:
            c.execute("INSERT INTO changes (ts, note) VALUES (?, ?)", (int(ts), note))

    def delete_change(self, change_id):
        with self._conn() as c:
            c.execute("DELETE FROM changes WHERE id = ?", (int(change_id),))

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

    # -- measured minute history -------------------------------------------
    def save_minutes(self, day, rows, complete, now=None):
        """rows: [(minute ts, imp J, exp J, div J)] for one UTC day."""
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO minutes (ts, imp, exp, div, eddi, zappi) VALUES (?, ?, ?, ?, ?, ?)",
                          rows)
            c.execute("INSERT OR REPLACE INTO history_days VALUES (?, ?, ?)",
                      (day, int(now or time.time()), 1 if complete else 0))
            start = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
            c.execute("DELETE FROM energy_hh3 WHERE hh >= ? AND hh < ?", (start, start + 86400))
            c.execute("DELETE FROM signal_hours WHERE hour >= ? AND hour < ?", (start, start + 86400))

    def history_days(self):
        return {d: (f, bool(cp)) for d, f, cp in self._conn().execute("SELECT * FROM history_days")}

    def minutes(self, start_ts, end_ts):
        return {ts: (imp, exp, div, eddi, zappi) for ts, imp, exp, div, eddi, zappi in self._conn().execute(
            "SELECT ts, imp, exp, div, eddi, zappi FROM minutes WHERE ts >= ? AND ts < ?",
            (int(start_ts), int(end_ts)))}

    def complete_until(self):
        """End (unix s) of the latest UTC day whose history is complete, or None."""
        d = self._conn().execute("SELECT MAX(day) FROM history_days WHERE complete = 1").fetchone()[0]
        return None if d is None else \
            datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() + 86400

    def flow_samples(self, start_ts, end_ts):
        return self._conn().execute(
            "SELECT poll, grid, solar, eddi, zappi, home, solar_age FROM flows WHERE poll >= ? AND poll < ? ORDER BY poll",
            (int(start_ts), int(end_ts))).fetchall()

    def first_flow_poll(self):
        return self._conn().execute("SELECT MIN(poll) FROM flows").fetchone()[0]

    # -- Huawei FusionSolar ---------------------------------------------------
    def save_huawei_day(self, day, rows, complete, now=None):
        """rows: [(ts, soc %, battery W, solar W)] for one local day (an ISO date)."""
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO huawei VALUES (?, ?, ?, ?)", rows)
            c.execute("INSERT OR REPLACE INTO huawei_days VALUES (?, ?, ?)",
                      (day, int(now or time.time()), 1 if complete else 0))

    def huawei_days(self):
        return {d: bool(cp) for d, cp in self._conn().execute("SELECT day, complete FROM huawei_days")}

    def huawei(self, start_ts, end_ts):
        """[(ts, soc, battery W, solar W)] in [start, end), oldest first."""
        return self._conn().execute("SELECT ts, soc, battery, solar FROM huawei WHERE ts >= ? AND ts < ? ORDER BY ts",
                                    (int(start_ts), int(end_ts))).fetchall()

    def latest_huawei(self):
        """(ts, soc, battery W, solar W) of the newest reading with a state of charge, or None."""
        return self._conn().execute(
            "SELECT ts, soc, battery, solar FROM huawei WHERE soc IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()

    # -- cached half-hour estimates ------------------------------------------
    def cached_hh(self, start_ts, end_ts):
        return {r[0]: r[1:] for r in self._conn().execute(
            "SELECT hh, home, grid, var, covered, measured, sampled, imp, exp, grid_batt, grid_dev, eddi, zappi, charge "
            "FROM energy_hh3 WHERE hh >= ? AND hh < ?", (int(start_ts), int(end_ts)))}

    def cache_hh(self, rows):
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO energy_hh3 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def prune(self, before_ts):
        """Delete raw readings (every device's full status, about 1.9 MB a day) older than before_ts. Everything
        else (power flows, the hub's minute history, temperatures, signal metrics; about 0.3 MB a day) is
        kept indefinitely for the Energy and Car history."""
        with self._conn() as c:
            return c.execute("DELETE FROM readings WHERE poll < ?", (int(before_ts),)).rowcount


class Recorder(threading.Thread):
    """Polls every `interval` seconds for as long as the process runs."""

    HISTORY_EVERY_S = 600          # fetch the hub's minute history this often
    HISTORY_DAYS_PER_SYNC = 10     # at most this many days per sync, so a long backfill is spread out

    EMBER_EVERY_S = 300            # read Ember zone temperatures this often (it's a cloud service)
    HUAWEI_EVERY_S = 300           # FusionSolar has a reading every 5 minutes
    HUAWEI_DAYS_PER_SYNC = 4       # days of FusionSolar history per sync besides today (2 requests a day)

    def __init__(self, store, get_status, interval=15, retention_days=180, get_history=None, get_ember=None,
                 history_from=365, get_huawei_now=None, get_huawei_day=None, tz=timezone.utc):
        super().__init__(name="recorder", daemon=True)
        self.store, self.get_status, self.get_history = store, get_status, get_history
        self.get_ember = get_ember
        self._last_ember = 0.0
        self._last_history = 0.0
        self.interval, self.retention_s = max(5, interval), retention_days * 86400
        self.history_from = history_from       # backfill the hub's history to this date, or this many days back
        self.get_huawei_now, self.get_huawei_day, self.tz = get_huawei_now, get_huawei_day, tz
        self.huawei_now = None       # (unix time, fusionsolar.parse_flow dict) from the last FusionSolar read
        self._last_huawei = 0.0
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
            self.store.insert_flow(now, flow.compute(status, now=_utc(now)))
            self.store.note_tank_temps(status)
            if self.get_ember and now - self._last_ember >= self.EMBER_EVERY_S:
                self._last_ember = now
                try:
                    zones = self.get_ember()
                    if zones:
                        self.store.insert_temps(now, zones)
                except Exception as e:
                    log.warning("recorder: Ember read failed: %s", e)
            self.last_ok, self.latest = now, (now, status)
            if (self.get_huawei_now or self.get_huawei_day) and now - self._last_huawei >= self.HUAWEI_EVERY_S:
                self._last_huawei = now
                try:
                    self.sync_huawei(now)
                except Exception as e:
                    log.warning("recorder: FusionSolar read failed: %s", e)
            if self.get_history and now - self._last_history >= self.HISTORY_EVERY_S:
                self._last_history = now
                try:
                    self.sync_history(status, now)
                except Exception as e:
                    log.warning("recorder: history sync failed: %s", e)
            if not self._backfilled:                 # once per start: flows for polls recorded before they existed
                self._backfilled = True
                try:
                    n = self.store.backfill_flows(status)
                    if n:
                        log.info("recorder: worked out power flows for %s earlier polls", n)
                    n = self.store.fix_stale_solar(status)
                    if n:
                        log.info("recorder: treated %s stale solar & battery readings as 0 W", n)
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

    def sync_history(self, status, now=None):
        """Fetch the hub's measured per-minute history for every UTC day back to `history_from` that isn't
        complete yet, newest first (today is refetched each sync). Days before recording began are backfilled
        too, a few per sync: the car charging page uses them. Returns the days fetched."""
        if not self.get_history:
            return []
        now = now or time.time()
        first = self.store.first_flow_poll()
        if first is None:
            return []
        devices = [(kind, str(r["sno"]), flow.is_hub_rec(r)) for kind in ("eddi", "zappi")
                   for r in status.get(kind, []) or [] if isinstance(r, dict) and "sno" in r]
        if not devices:
            return []
        hub = next((d for d in devices if d[2]), devices[0])
        known = self.store.history_days()
        days, d = [], _utc(now).date()
        oldest = self._oldest_day(now)
        while d >= oldest:
            name = d.isoformat()
            if not known.get(name, (0, False))[1]:
                days.append(name)
            d -= timedelta(days=1)
        done = []
        for day in days[:self.HISTORY_DAYS_PER_SYNC]:            # newest first
            try:
                per_min = {}
                for kind, sno, _ in devices:
                    for ts, f in self.get_history(kind, sno, day):
                        m = per_min.setdefault(ts, [None, None, 0.0, 0.0])
                        if (kind, sno) == hub[:2]:
                            m[0], m[1] = f.get("imp", 0), f.get("exp", 0)
                        used = sum(v for k, v in f.items() if len(k) == 3 and k[0] == "h" and k[1] in "123"
                                   and k[2] in "db")
                        m[2 if kind == "eddi" else 3] += used
            except Exception as e:                       # try again next sync
                log.warning("recorder: history for %s failed: %s", day, e)
                break
            day_start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            rows = [(ts, m[0], m[1], m[2] + m[3], m[2], m[3]) for ts, m in per_min.items()
                    if m[0] is not None and day_start <= ts < day_start + 86400]
            self.store.save_minutes(day, rows, complete=now > day_start + 86400 + 1800, now=now)
            done.append(day)
        return done

    def _oldest_day(self, now):
        return self.history_from if isinstance(self.history_from, date) else \
            _utc(now - int(self.history_from) * 86400).date()

    def sync_huawei(self, now=None):
        """Read FusionSolar's current figures, then its 5-minute history for today and for up to
        HUAWEI_DAYS_PER_SYNC earlier local days back to `history_from` that aren't complete yet, newest first.
        Returns the days fetched."""
        now = now or time.time()
        if self.get_huawei_now:
            cur = self.get_huawei_now()
            if cur is not None:
                self.huawei_now = (now, cur)
        if not self.get_huawei_day:
            return []
        known = self.store.huawei_days()
        today = datetime.fromtimestamp(now, self.tz).date()
        days, d, oldest = [], today, self._oldest_day(now)
        while d >= oldest and len(days) <= self.HUAWEI_DAYS_PER_SYNC:
            if not known.get(d.isoformat(), False):
                days.append(d)
            d -= timedelta(days=1)
        done = []
        for d in days:
            rows = self.get_huawei_day(d)
            if rows is None:                         # FusionSolar unreachable: try again next sync
                break
            end = datetime.combine(d + timedelta(days=1), datetime.min.time(), self.tz).timestamp()
            self.store.save_huawei_day(d.isoformat(), rows, complete=now > end + 1800, now=now)
            done.append(d.isoformat())
        return done

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
