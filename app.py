"""myenergi monitor: live device values from the myenergi API, and report charts from monitor.py logs."""
import json
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
import io
from urllib.parse import urlencode

import charts
import energy
import flow
import recorder
from auth import init_auth
import live_chart
from devices import format_duration, summarise
from myenergi_client import MyEnergiClient, MyEnergiError

ID_RE = re.compile(r"^[0-9a-f]{32}$")
UPLOAD_MAX_AGE_S = 7 * 24 * 3600


def load_dotenv(path=".env"):
    """Tiny .env loader (KEY=VALUE per line); real environment variables win."""
    p = Path(__file__).with_name(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_names(text):
    """'1234567=Solar & Battery,7654321=Grid' -> {'1234567': 'Solar & Battery', ...}"""
    out = {}
    for part in (text or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _num(name, default, lo, hi, cast=float):
    try:
        v = cast(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(v, lo), hi)


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        HUB_SERIAL=os.environ.get("MYENERGI_HUB_SERIAL"),
        API_KEY=os.environ.get("MYENERGI_API_KEY"),
        GRID_HARVI_SNO=os.environ.get("GRID_HARVI_SNO"),
        HARVI_NAMES=parse_names(os.environ.get("HARVI_NAMES")),
        REFRESH_SECONDS=int(os.environ.get("REFRESH_SECONDS", "15")),
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        UPLOAD_DIR=os.path.join(app.instance_path, "uploads"),
        DB_PATH=os.path.join(app.instance_path, "readings.db"),
        RECORD=os.environ.get("RECORD", "1").lower() not in ("0", "false", "no"),
        POLL_SECONDS=int(os.environ.get("POLL_SECONDS", "15")),
        RETENTION_DAYS=int(os.environ.get("RETENTION_DAYS", "180")),
        TIMEZONE=os.environ.get("TZ") or "Europe/London",    # day/week/month boundaries on the Energy page
    )
    if test_config:
        app.config.update(test_config)
    if app.config.get("BEHIND_PROXY", os.environ.get("BEHIND_PROXY", "").lower() in ("1", "true", "yes")):
        # Behind a reverse proxy (e.g. nginx doing HTTPS): trust one hop of X-Forwarded-* headers so the app
        # sees the real scheme, host and client address. Only enable this when a proxy is in front.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    init_auth(app)   # no-op unless APP_PASSWORD is set
    app.add_template_filter(format_duration, "duration")
    app.add_template_filter(lambda w: flow.fmt_power(flow._num(w)), "power")
    os.makedirs(os.path.dirname(app.config["DB_PATH"]) or ".", exist_ok=True)
    store = app.extensions["store"] = recorder.Store(app.config["DB_PATH"])

    def get_client():
        if "client" not in app.extensions:
            app.extensions["client"] = MyEnergiClient(app.config["HUB_SERIAL"], app.config["API_KEY"])
        return app.extensions["client"]

    def default_grid(serials):
        names = app.config["HARVI_NAMES"]
        configured = app.config["GRID_HARVI_SNO"]
        if configured in serials:
            return configured
        for s in serials:
            if "grid" in names.get(s, "").lower():
                return s
        return serials[0]

    # ---- always-on recording -----------------------------------------------
    # Polls the API in the background for as long as the app runs (one gunicorn worker = one recorder).
    rec = None
    if app.config["RECORD"] and app.config["HUB_SERIAL"] and app.config["API_KEY"] and not app.config.get("TESTING"):
        rec = app.extensions["recorder"] = recorder.Recorder(
            store, lambda: get_client().status(), app.config["POLL_SECONDS"], app.config["RETENTION_DAYS"],
            get_history=lambda kind, sno, day: get_client().day_history(kind, sno, day))
        rec.start()

    # ---- live values -------------------------------------------------------
    # Which CT clamps to display is a per-device choice, kept on disk (single-user local app).
    ct_prefs_file = Path(app.config["UPLOAD_DIR"]) / "ct_prefs.json"

    def get_ct_prefs():
        try:
            data = json.loads(ct_prefs_file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def apply_ct_prefs(devices):
        """Mark each CT as shown/hidden. Default ("auto"): show CTs that have a purpose assigned."""
        prefs = get_ct_prefs()
        for d in devices:
            key = f"{d['kind']}:{d['sno']}"
            chosen = prefs.get(key)
            in_use = [c["slot"] for c in d["cts"] if str(c["name"]).strip().lower() not in ("none", "")]
            auto = in_use or [c["slot"] for c in d["cts"]]
            slots = chosen if isinstance(chosen, list) else auto
            d["ct_key"], d["ct_mode"] = key, ("custom" if isinstance(chosen, list) else "auto")
            for c in d["cts"]:
                c["shown"] = c["slot"] in slots
        return devices

    # Live CT chart: drawn from the recorded readings when recording is on (so it survives restarts);
    # otherwise from readings seen since the app started, growing each time the dashboard polls.
    ct_history = live_chart.CtHistory()
    CHART_WINDOW_S = 2 * 3600

    # A photo dropped into static/img/devices/ (e.g. zappi.jpg) wins over the bundled illustration.
    image_dir = Path(app.static_folder) / "img" / "devices"

    def device_image(kind):
        for ext in ("webp", "png", "jpg", "jpeg", "svg"):
            if (image_dir / f"{kind}.{ext}").is_file():
                return f"img/devices/{kind}.{ext}"
        return "img/devices/device.svg"

    def current_status():
        """The recorder's latest poll when it's fresh (one API call serves both), else ask the API."""
        rec = app.extensions.get("recorder")
        if rec and rec.latest and time.time() - rec.latest[0] < 2 * rec.interval + 5:
            return rec.latest[1]
        return get_client().status()

    def live_devices():
        devices = apply_ct_prefs(summarise(current_status(), app.config["HARVI_NAMES"]))
        for d in devices:
            d["image"] = url_for("static", filename=device_image(d["kind"]))
            if app.extensions.get("recorder"):
                points = store.recent_points(d["kind"], d["sno"], time.time() - CHART_WINDOW_S)
            else:
                ct_history.record(d)
                points = ct_history.points(d)
            d["ct_chart"] = live_chart.build(points, d["cts"])
        return devices

    @app.post("/ct-prefs/<kind>/<sno>")
    def set_ct_prefs(kind, sno):
        if not (re.fullmatch(r"[a-z0-9_]{1,20}", kind) and re.fullmatch(r"[0-9A-Za-z_-]{1,20}", sno)):
            abort(400)
        key, prefs = f"{kind}:{sno}", get_ct_prefs()
        if request.form.get("auto"):
            prefs.pop(key, None)                                   # back to the automatic choice
        else:
            prefs[key] = sorted({int(s) for s in request.form.getlist("slot") if s in ("1", "2", "3")})
        ct_prefs_file.write_text(json.dumps(prefs))
        if request.headers.get("X-Requested-With") == "fetch":
            return "", 204
        return redirect(url_for("dashboard"))

    @app.get("/healthz")
    def healthz():
        """Liveness check for Docker: answers without calling the myenergi API."""
        return "ok", 200, {"Content-Type": "text/plain"}

    def overview_context():
        status = current_status()
        devices = apply_ct_prefs(summarise(status, app.config["HARVI_NAMES"]))
        for d in devices:
            d["image"] = url_for("static", filename=device_image(d["kind"]))
        hub = next((d for d in devices if d["is_hub"]), devices[0] if devices else None)
        return {"flow": flow.build(status), "devices": devices, "error": None,
                "reading_utc": hub and hub["report_utc"], "reading_age": hub and hub["age_s"]}

    @app.get("/")
    def overview():
        try:
            ctx = overview_context()
        except MyEnergiError as e:
            ctx = {"flow": None, "devices": [], "error": str(e)}
        return render_template("overview.html", refresh=app.config["REFRESH_SECONDS"], **ctx)

    @app.get("/partials/overview")
    def overview_partial():
        try:
            return render_template("_overview.html", **overview_context())
        except MyEnergiError as e:
            return render_template("_overview.html", flow=None, devices=[], error=str(e)), 502

    @app.get("/energy")
    def energy_page():
        """Home energy use for a day (per half hour), week or month (per day) or year (per month)."""
        try:
            tz = ZoneInfo(app.config["TIMEZONE"])
        except (ZoneInfoNotFoundError, ValueError):
            tz = ZoneInfo("UTC")
        now_ts = app.config.get("NOW") or time.time()
        today = datetime.fromtimestamp(now_ts, tz).date()
        period = request.args.get("period", "day")
        if period not in energy.PERIODS:
            period = "day"
        try:
            day = date.fromisoformat(request.args.get("date", ""))
        except ValueError:
            day = today
        s = energy.summarise(store, period, day, tz, now_ts)
        nav = {"prev": energy.shift(period, day, -1).isoformat(), "next": energy.shift(period, day, 1).isoformat(),
               "today": today.isoformat(), "has_next": not s["in_progress"] and not s["future"],
               "is_now": s["in_progress"]}
        return render_template("energy.html", s=s, ch=energy.chart(s), nav=nav, day=day.isoformat(),
                               tz_name=app.config["TIMEZONE"], periods=energy.PERIODS)

    @app.get("/live")
    def dashboard():
        try:
            devices, error = live_devices(), None
        except MyEnergiError as e:
            devices, error = [], str(e)
        return render_template("dashboard.html", devices=devices, error=error,
                               refresh=app.config["REFRESH_SECONDS"])

    @app.get("/partials/devices")
    def devices_partial():
        try:
            return render_template("_devices.html", devices=live_devices(), error=None)
        except MyEnergiError as e:
            return render_template("_devices.html", devices=[], error=str(e)), 502

    @app.get("/api/status")
    def api_status():
        try:
            return jsonify(devices=live_devices(), fetched_at=time.time())
        except MyEnergiError as e:
            return jsonify(error=str(e)), 502

    # ---- uploaded logs -----------------------------------------------------
    def upload_path(log_id):
        if not ID_RE.match(log_id):
            abort(404)
        p = Path(app.config["UPLOAD_DIR"]) / f"{log_id}.csv"
        if not p.exists():
            abort(404)
        return p

    @lru_cache(maxsize=4)
    def load_cached(path, mtime):
        with open(path, "rb") as f:
            return charts.load_log(f)

    # The "current report" is remembered on disk (this is a single-user local app), so it
    # survives switching tabs and restarting the app until a new log is uploaded.
    pointer = Path(app.config["UPLOAD_DIR"]) / "current_report.json"

    def get_current():
        try:
            cur = json.loads(pointer.read_text())
            if ID_RE.match(cur["id"]) and (Path(app.config["UPLOAD_DIR"]) / f"{cur['id']}.csv").exists():
                return {"id": cur["id"], "query": cur.get("query", "")}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def set_current(log_id, query=""):
        pointer.write_text(json.dumps({"id": log_id, "query": query}))

    @app.context_processor
    def nav_links():
        cur = get_current()
        upload_link = None
        if cur:
            upload_link = url_for("report", log_id=cur["id"]) + ("?" + cur["query"] if cur["query"] else "")
        # "Graphs" opens the report on recorded data; an uploaded CSV report stays one click away.
        return {"report_link": url_for("stored_report"), "upload_report_link": upload_link,
                "has_current_report": cur is not None}

    def cleanup_uploads():
        cutoff = time.time() - UPLOAD_MAX_AGE_S
        cur = get_current()
        for p in Path(app.config["UPLOAD_DIR"]).glob("*.csv"):
            if cur and p.stem == cur["id"]:
                continue                       # never delete the report being shown
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)

    @app.route("/upload", methods=["GET", "POST"])
    def upload():
        error = None
        if request.method == "POST":
            f = request.files.get("log")
            if not f or not f.filename:
                error = "Choose a CSV file first."
            elif not f.filename.lower().endswith(".csv"):
                error = "Only .csv files written by monitor.py are supported."
            else:
                data = f.read()
                try:
                    charts.load_log(io.BytesIO(data))        # validate before keeping it
                except charts.LogFormatError as e:
                    error = str(e)
                else:
                    cleanup_uploads()
                    log_id = uuid.uuid4().hex
                    (Path(app.config["UPLOAD_DIR"]) / f"{log_id}.csv").write_bytes(data)
                    set_current(log_id)
                    return redirect(url_for("report", log_id=log_id))
        return render_template("upload.html", error=error), (400 if error else 200)

    def report_params(log):
        serials = log.serials("harvi")
        grid = request.args.get("grid")
        if grid not in serials:
            grid = default_grid(serials)
        return dict(
            grid_sno=grid,
            thresh_s=_num("thresh", 30, 1, 3600),
            gap_s=_num("gap", 120, 1, 36000),
            ct_w=_num("ct", 500, 0, 100000),
            night_start=_num("ns", 23, 0, 23, int),
            night_end=_num("ne", 8, 0, 23, int),
        )

    @app.get("/report/<log_id>")
    def report(log_id):
        p = upload_path(log_id)
        log = load_cached(str(p), p.stat().st_mtime)
        params = report_params(log)
        stats = charts.analyse(log, **params)
        cur = get_current()
        if cur and cur["id"] == log_id and cur["query"] != request.query_string.decode():
            set_current(log_id, request.query_string.decode())      # remember the chosen settings too
        return render_template("report.html", source="upload", png_url=url_for("report_png", log_id=log_id),
                               log_id=log_id, log=log, params=params, stats=stats,
                               serials=log.serials("harvi"), names=app.config["HARVI_NAMES"],
                               query=request.query_string.decode())

    @lru_cache(maxsize=8)
    def render_cached(path, mtime, names, **params):
        log = load_cached(path, mtime)
        return charts.render_png(log, names=dict(names), **params)

    @app.get("/report/<log_id>.png")
    def report_png(log_id):
        p = upload_path(log_id)
        log = load_cached(str(p), p.stat().st_mtime)
        params = report_params(log)
        theme = "dark" if request.args.get("theme") == "dark" else "light"
        png = render_cached(str(p), p.stat().st_mtime, tuple(sorted(app.config["HARVI_NAMES"].items())),
                            theme=theme, **params)
        return send_file(io.BytesIO(png), mimetype="image/png", download_name=f"harvi_report_{log_id[:8]}.png")

    # ---- report from recorded data -------------------------------------------
    MAX_SPAN = timedelta(days=31)          # keeps chart drawing quick (~0.5M rows at 15 s polling)
    DT_FMT = "%Y-%m-%dT%H:%M"              # <input type="datetime-local">, read as UTC like the rest of the app

    def parse_dt(s):
        try:
            return datetime.strptime(s or "", DT_FMT).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def stored_range():
        """The chosen [start, end], clamped to the recorded data; defaults to the last 24 hours."""
        span = store.span()
        if not span:
            return None
        lo = datetime.fromtimestamp(span[0], timezone.utc).replace(second=0)
        hi = datetime.fromtimestamp(span[1], timezone.utc).replace(second=0) + timedelta(minutes=1)
        clamp = lambda d: min(max(d, lo), hi)                               # noqa: E731
        end = clamp(parse_dt(request.args.get("to")) or hi)
        start = clamp(parse_dt(request.args.get("from")) or end - timedelta(hours=24))
        note = None
        if start > end:
            start, end, note = end, start, "The start was after the end, so the two were swapped."
        elif start == end:                     # empty range (e.g. both outside the data): widen to 24 h
            start = max(lo, end - timedelta(hours=24))
            end = max(end, min(hi, start + timedelta(hours=24)))
        if end - start > MAX_SPAN:
            start, note = end - MAX_SPAN, "Reports cover at most 31 days, so the start was moved forward."
        presets = [(label, {"from": max(lo, hi - d).strftime(DT_FMT), "to": hi.strftime(DT_FMT)})
                   for label, d in (("Last 6 h", timedelta(hours=6)), ("Last 24 h", timedelta(days=1)),
                                    ("Last 7 days", timedelta(days=7)), ("Last 31 days", MAX_SPAN))]
        return {"lo": lo, "hi": hi, "start": start, "end": end, "note": note, "presets": presets,
                "from_s": start.strftime(DT_FMT), "to_s": end.strftime(DT_FMT)}

    @lru_cache(maxsize=2)
    def stored_log(start_ts, end_ts, last_poll):      # last_poll in the key: new data invalidates the cache
        return store.load_log(start_ts, end_ts)

    @lru_cache(maxsize=4)
    def render_stored(start_ts, end_ts, last_poll, names, **params):
        return charts.render_png(stored_log(start_ts, end_ts, last_poll), names=dict(names), **params)

    def stored_inputs():
        r = stored_range()
        if r is None:
            return None, None, None
        start_ts, end_ts = int(r["start"].timestamp()), int(r["end"].timestamp())
        key = (start_ts, end_ts, store.last_poll_before(end_ts))
        return r, key, stored_log(*key)

    def recording_status():
        rec = app.extensions.get("recorder")
        fmt = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")   # noqa: E731
        return {"on": rec is not None, "interval": app.config["POLL_SECONDS"],
                "retention": app.config["RETENTION_DAYS"],
                "last_ok": fmt(rec.last_ok) if rec and rec.last_ok else None,
                "last_error": (fmt(rec.last_error[0]), rec.last_error[1]) if rec and rec.last_error and
                              (not rec.last_ok or rec.last_error[0] > rec.last_ok) else None}

    @app.get("/report")
    def stored_report():
        r, key, lg = stored_inputs()
        status = recording_status()
        if r is None or not lg.serials("harvi"):
            return render_template("report_empty.html", rng=r, recording=status)
        params = report_params(lg)
        query = urlencode({**request.args.to_dict(), "from": r["from_s"], "to": r["to_s"]})
        return render_template("report.html", source="stored", png_url=url_for("stored_report_png"),
                               rng=r, recording=status, log=lg, params=params,
                               stats=charts.analyse(lg, **params), serials=lg.serials("harvi"),
                               names=app.config["HARVI_NAMES"], query=query)

    @app.get("/report.png")
    def stored_report_png():
        r, key, lg = stored_inputs()
        if r is None or not lg.serials("harvi"):
            abort(404)
        theme = "dark" if request.args.get("theme") == "dark" else "light"
        png = render_stored(*key, tuple(sorted(app.config["HARVI_NAMES"].items())), theme=theme,
                            **report_params(lg))
        name = f"harvi_report_{r['start']:%Y%m%d-%H%M}_{r['end']:%Y%m%d-%H%M}.png"
        return send_file(io.BytesIO(png), mimetype="image/png", download_name=name)

    return app


if __name__ == "__main__":
    # Bound to localhost only: the page shows your energy data and uses your API key.
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", "5001")), debug=False)
