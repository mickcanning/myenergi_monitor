"""myenergi monitor: live device values from the myenergi API, and report charts from monitor.py logs."""
import json
import os
import re
import time
import uuid
from functools import lru_cache
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
import io

import charts
import live_chart
from devices import summarise
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
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

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

    # CT readings seen since the app started; grows each time the dashboard polls.
    ct_history = live_chart.CtHistory()

    # A photo dropped into static/img/devices/ (e.g. zappi.jpg) wins over the bundled illustration.
    image_dir = Path(app.static_folder) / "img" / "devices"

    def device_image(kind):
        for ext in ("webp", "png", "jpg", "jpeg", "svg"):
            if (image_dir / f"{kind}.{ext}").is_file():
                return f"img/devices/{kind}.{ext}"
        return "img/devices/device.svg"

    def live_devices():
        devices = apply_ct_prefs(summarise(get_client().status(), app.config["HARVI_NAMES"]))
        for d in devices:
            d["image"] = url_for("static", filename=device_image(d["kind"]))
            ct_history.record(d)
            d["ct_chart"] = live_chart.build(ct_history.points(d), d["cts"])
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

    @app.get("/")
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
        if cur:
            link = url_for("report", log_id=cur["id"]) + ("?" + cur["query"] if cur["query"] else "")
        else:
            link = url_for("upload")
        return {"report_link": link, "has_current_report": cur is not None}

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
        return render_template("report.html", log_id=log_id, log=log, params=params, stats=stats,
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

    return app


if __name__ == "__main__":
    # Bound to localhost only: the page shows your energy data and uses your API key.
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
