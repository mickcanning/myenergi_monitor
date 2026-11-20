"""Run with:  python -m pytest -q   (or: python tests/test_app.py)"""
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module  # noqa: E402
from charts import LogFormatError, load_log  # noqa: E402

HEADER = "poll_utc,device,sno,report_utc,age_s,new_report,stale,eddi_age_s,ectp1,ectp2,ectp3\n"


def make_csv():
    """Two Harvis + an Eddi over 40 minutes; the grid Harvi has one long silence at ~4 kW."""
    t0 = datetime(2026, 11, 18, 23, 0, tzinfo=timezone.utc)
    lines = [HEADER]
    last = {"g": t0, "s": t0}
    for i in range(160):                       # 15 s polls
        t = t0 + timedelta(seconds=15 * i)
        for key, sno, ct in (("g", "111", 4000), ("s", "222", -3000)):
            silent = key == "g" and 60 <= i < 80          # 5-minute silence on the grid Harvi
            if not silent and i % 2 == 0:
                last[key] = t
            age = (t - last[key]).total_seconds()
            lines.append(f"{t.isoformat()},harvi,{sno},{last[key].isoformat()},{age},0,0,3,{ct},0,0\n")
        lines.append(f"{t.isoformat()},eddi,999,{t.isoformat()},4.0,1,0,,0,0,0\n")
    return "".join(lines)


class FakeClient:
    def status(self):
        return {
            "eddi": [{"sno": 999, "dat": "18-11-2026", "tim": "23:00:00", "ectp1": 5, "ectt1": "Internal Load",
                      "ectp2": 0, "ectt2": "None", "ectp3": 0, "ectt3": "None", "frq": 50.01, "vol": 2345, "sta": 3,
                      "isVHubEnabled": True, "fwv": "3202S5.754"}],
            "harvi": [{"sno": 111, "dat": "18-11-2026", "tim": "22:58:00", "ectp1": 4000, "ectt1": "Grid",
                       "fwv": "3170S0.000"}],
            "asn": "s18.myenergi.net",
        }


def make_app(tmp_path):
    a = app_module.create_app({"TESTING": True, "UPLOAD_DIR": str(tmp_path), "DB_PATH": str(tmp_path / "readings.db"), "HUB_SERIAL": "1", "API_KEY": "k",
                               "HARVI_NAMES": {"111": "Grid", "222": "Solar & Battery"}, "APP_PASSWORD": ""})
    a.extensions["client"] = FakeClient()
    return a


def test_dashboard_and_api(tmp_path):
    c = make_app(tmp_path).test_client()
    html = c.get("/live").get_data(as_text=True)
    assert "Eddi" in html and "Internal Load" in html and "Grid" in html
    assert "50.01 Hz" in html and "234.5 V" in html          # auto-scaled units
    data = c.get("/api/status").get_json()
    assert [d["kind"] for d in data["devices"]] == ["eddi", "harvi"]
    assert [d["is_hub"] for d in data["devices"]] == [True, False]
    assert html.count('class="hub-badge"') == 1 and "isVHubEnabled" not in html     # badge, not a raw field
    assert "Serial 999" in html and "Firmware 5.754" in html and 'title="fwv 3202S5.754"' in html
    assert html.count("Firmware ") == 1 and "Serial 111" in html                 # Harvi sends 0.000: no firmware shown
    assert c.get("/partials/devices").status_code == 200
    images = [d["image"] for d in data["devices"]]
    assert images[0].startswith("/static/img/devices/eddi.") and "harvi." in images[1]
    assert all(c.get(src).status_code == 200 for src in images)


def test_upload_and_report(tmp_path):
    c = make_app(tmp_path).test_client()
    r = c.post("/upload", data={"log": (io.BytesIO(make_csv().encode()), "log.csv")},
               content_type="multipart/form-data")
    assert r.status_code == 302
    page = c.get(r.headers["Location"])
    body = page.get_data(as_text=True)
    assert page.status_code == 200 and "Gaps of concern" in body and "Solar &amp; Battery" in body
    png = c.get(r.headers["Location"] + ".png")
    assert png.status_code == 200 and png.data[:8] == b"\x89PNG\r\n\x1a\n"
    dark = c.get(r.headers["Location"] + ".png?theme=dark")
    assert dark.status_code == 200 and dark.data[:8] == png.data[:8] and dark.data != png.data
    # switching the grid Harvi and thresholds still renders
    assert c.get(r.headers["Location"] + ".png?grid=222&thresh=60&gap=30&ct=100&ns=22&ne=6").status_code == 200


def test_current_report_persists(tmp_path):
    a = make_app(tmp_path)
    c = a.test_client()
    # the nav tab goes to the recorded-data report; no uploaded report to go back to yet
    assert 'href="/report"' in c.get("/upload").get_data(as_text=True)
    assert "last uploaded report" not in c.get("/upload").get_data(as_text=True)
    loc = c.post("/upload", data={"log": (io.BytesIO(make_csv().encode()), "log.csv")},
                 content_type="multipart/form-data").headers["Location"]
    # the upload page links back to the uploaded report
    assert f'href="{loc}"' in c.get("/upload").get_data(as_text=True)
    # chosen settings are remembered too
    c.get(loc + "?grid=222&thresh=45")
    assert f'href="{loc}?grid=222&amp;thresh=45"' in c.get("/upload").get_data(as_text=True)
    # ...and survive an app restart
    c2 = make_app(tmp_path).test_client()
    assert f'href="{loc}?grid=222&amp;thresh=45"' in c2.get("/upload").get_data(as_text=True)
    # a new upload replaces the current report
    loc2 = c2.post("/upload", data={"log": (io.BytesIO(make_csv().encode()), "again.csv")},
                   content_type="multipart/form-data").headers["Location"]
    assert loc2 != loc and f'href="{loc2}"' in c2.get("/upload").get_data(as_text=True)
    # a failed upload leaves the current report in place
    c2.post("/upload", data={"log": (io.BytesIO(b"nope"), "bad.csv")}, content_type="multipart/form-data")
    assert f'href="{loc2}"' in c2.get("/upload").get_data(as_text=True)


def test_ct_choice(tmp_path):
    c = make_app(tmp_path).test_client()
    page = lambda cl=c: cl.get("/live").get_data(as_text=True)
    # automatic: only the CT with a purpose is shown, unused ones are hidden
    html = page()
    assert "<td>Internal Load</td>" in html and "<td>None</td>" not in html and "1 of 3, automatic" in html
    # choose CT 1 and 2 for the Eddi
    r = c.post("/ct-prefs/eddi/999", data={"slot": ["1", "2", "9"]}, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 204
    html = page()
    assert "<td>None</td>" in html and "2 of 3, chosen by you" in html
    # the choice survives an app restart and is per device (the Harvi is still automatic)
    html = page(make_app(tmp_path).test_client())
    assert "2 of 3, chosen by you" in html and "1 of 1, automatic" in html
    # none selected, then back to automatic
    c.post("/ct-prefs/eddi/999", data={}, headers={"X-Requested-With": "fetch"})
    assert "No CTs selected" in page()
    c.post("/ct-prefs/eddi/999", data={"auto": "1"}, headers={"X-Requested-With": "fetch"})
    assert "1 of 3, automatic" in page()
    # without JavaScript the form redirects back to the dashboard; bad names are rejected
    assert c.post("/ct-prefs/eddi/999", data={"auto": "1"}).status_code == 302
    assert c.post("/ct-prefs/ed%20di/999", data={}).status_code in (400, 404)
    # the raw API still returns every CT
    cts = c.get("/api/status").get_json()["devices"][0]["cts"]
    assert len(cts) == 3


class TickingClient:
    """Each poll the Eddi reports again (new time, new load); the Harvi stays quiet after its first report."""
    def __init__(self):
        self.n = 0

    def status(self):
        self.n += 1
        return {
            "eddi": [{"sno": 999, "dat": "18-11-2026", "tim": f"23:00:{self.n:02d}", "ectp1": 100 * self.n,
                      "ectt1": "Internal Load"}],
            "harvi": [{"sno": 111, "dat": "18-11-2026", "tim": "22:58:00", "ectp1": 4000, "ectt1": "Grid"}],
        }


def test_ct_chart_builds_up(tmp_path):
    a = make_app(tmp_path)
    a.extensions["client"] = TickingClient()
    c = a.test_client()
    charts_of = lambda: [d["ct_chart"] for d in c.get("/api/status").get_json()["devices"]]
    eddi, harvi = charts_of()
    assert eddi["waiting"] and eddi["count"] == 1
    for _ in range(3):
        eddi, harvi = charts_of()
    assert not eddi["waiting"] and eddi["series"][0]["path"].count("L") == 3     # 4 readings -> 4 points
    assert harvi["waiting"] and harvi["count"] == 1                              # repeated report not re-added
    assert [s["v"] for s in __import__("json").loads(eddi["hover"])["s"]] == [[100, 200, 300, 400]]
    html = c.get("/partials/devices").get_data(as_text=True)
    assert 'class="ct-chart"' in html and "collecting readings" in html


def test_upload_rejects_bad_files(tmp_path):
    c = make_app(tmp_path).test_client()
    for name, content in (("x.txt", b"hello"), ("x.csv", b"a,b\n1,2\n"), ("x.csv", b"")):
        r = c.post("/upload", data={"log": (io.BytesIO(content), name)}, content_type="multipart/form-data")
        assert r.status_code == 400
    assert c.get("/report/../../etc/passwd").status_code == 404
    assert c.get("/report/" + "0" * 32).status_code == 404


def test_load_log_errors():
    try:
        load_log(io.StringIO("a,b\n1,2\n"))
    except LogFormatError as e:
        assert "missing columns" in str(e)
    else:
        raise AssertionError("expected LogFormatError")


class FakeOpener:
    """Stands in for urllib: the director replies with a scripted server header; any other host serves JSON."""
    def __init__(self, host, log, asn):
        self.host, self.log, self.asn = host, log, asn

    def open(self, url, timeout=None):
        import email.message, io, urllib.error
        self.log.append(self.host)
        headers = email.message.Message()
        if self.host == "director.myenergi.net" and url.endswith("/cgi-jstatus-H"):     # host discovery
            headers["X_MYENERGI-asn"] = self.asn.pop(0)
            if self.asn and self.asn[0] == "401":
                self.asn.pop(0)
                raise urllib.error.HTTPError(url, 401, "Unauthorized", headers, None)
        if self.host == "undefined":
            raise OSError(8, "nodename nor servname provided, or not known")
        r = io.BytesIO(b'[{"eddi": [{"sno": 1}]}]')
        r.headers = headers
        return r


def test_client_ignores_undefined_server_name():
    from myenergi_client import MyEnergiClient, MyEnergiError
    c = MyEnergiClient("1", "k", cache_seconds=0)
    log, asn = [], ["undefined"]
    c._opener = lambda host: FakeOpener(host, log, asn)
    assert c.status()["eddi"] == [{"sno": 1}]
    assert "undefined" not in log and log[-1] == "director.myenergi.net"     # fell back to the director
    # once a real server has worked, a later bad answer falls back to it
    asn += ["s18.myenergi.net"]
    c._host = None
    c.status()
    asn += ["undefined"]
    c._host = None
    c.status()
    assert log[-1] == "s18.myenergi.net"
    # a 401 carrying "undefined" with no server known yet is reported as an auth problem, not a DNS error
    c2 = MyEnergiClient("1", "k", cache_seconds=0)
    asn2 = ["undefined", "401"]
    c2._opener = lambda host: FakeOpener(host, [], asn2)
    try:
        c2.status()
    except MyEnergiError as e:
        assert "Authentication failed" in str(e)
    else:
        raise AssertionError("expected MyEnergiError")


def test_behind_proxy(tmp_path):
    def scheme_seen(proxy):
        a = make_app(tmp_path) if not proxy else app_module.create_app(
            {"TESTING": True, "UPLOAD_DIR": str(tmp_path), "DB_PATH": str(tmp_path / "readings.db"), "HUB_SERIAL": "1", "API_KEY": "k", "BEHIND_PROXY": True, "APP_PASSWORD": ""})
        a.add_url_rule("/_scheme", "_scheme", lambda: __import__("flask").request.url_root)
        return a.test_client().get("/_scheme", headers={"X-Forwarded-Proto": "https",
                                                         "X-Forwarded-Host": "energy.example.com"}).get_data(as_text=True)
    assert scheme_seen(False) == "http://localhost/"                 # headers ignored unless enabled
    assert scheme_seen(True) == "https://energy.example.com/"


def make_locked_app(tmp_path, password="s3cret pass", **extra):
    a = app_module.create_app({"TESTING": True, "UPLOAD_DIR": str(tmp_path / "up"), "DB_PATH": str(tmp_path / "readings.db"), "HUB_SERIAL": "1", "API_KEY": "k",
                               "APP_PASSWORD": password, "SECRET_KEY_FILE": str(tmp_path / "secret_key"),
                               "AUTH_FAIL_DELAY": 0, **extra})
    a.extensions["client"] = FakeClient()
    return a


def test_login(tmp_path):
    a = make_locked_app(tmp_path)
    c = a.test_client()
    # signed out: pages redirect to the login form, background calls get 401, health check stays open
    r = c.get("/report/abc?x=1")
    assert r.status_code == 302 and "/login?next=/report/abc?x%3D1" in r.headers["Location"]
    assert c.get("/api/status").status_code == 401
    assert c.get("/partials/devices").status_code == 401
    assert c.post("/ct-prefs/eddi/999", data={}).status_code == 401
    assert c.get("/healthz").status_code == 200
    assert c.get("/static/style.css").status_code == 200
    page = c.get("/login").get_data(as_text=True)
    assert 'type="password"' in page and "Live values" not in page and "Sign out" not in page
    # wrong password, then the right one (going back to where we were headed)
    assert c.post("/login", data={"password": "nope"}).status_code == 401
    r = c.post("/login", data={"password": "s3cret pass", "next": "/upload"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/upload")
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" not in cookie   # plain http here
    assert c.get("/api/status").status_code == 200
    assert "Sign out" in c.get("/").get_data(as_text=True)
    # off-site "next" targets are ignored
    c2 = a.test_client()
    for bad in ("//evil.com", "https://evil.com/", "/\\evil.com"):
        r = c2.post("/login", data={"password": "s3cret pass", "next": bad})
        assert r.headers["Location"] == "/", bad
        c2.post("/logout")
    # sign out
    c.post("/logout")
    assert c.get("/api/status").status_code == 401
    # the signing key persists, so a restart keeps you signed in; a new password signs everyone out
    c.post("/login", data={"password": "s3cret pass"})
    a2 = make_locked_app(tmp_path)
    c3 = a2.test_client()
    c3.set_cookie("session", c.get_cookie("session").value)
    assert c3.get("/api/status").status_code == 200
    a3 = make_locked_app(tmp_path, password="changed")
    c4 = a3.test_client()
    c4.set_cookie("session", c.get_cookie("session").value)
    assert c4.get("/api/status").status_code == 401


def test_login_secure_cookie_and_throttle(tmp_path):
    a = make_locked_app(tmp_path, BEHIND_PROXY=True)
    c = a.test_client()
    r = c.post("/login", data={"password": "s3cret pass"}, headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in r.headers["Set-Cookie"]                       # served over https via the proxy
    c = a.test_client()
    for _ in range(5):
        assert c.post("/login", data={"password": "wrong"}).status_code == 401
    r = c.post("/login", data={"password": "s3cret pass"})            # locked out, even with the right one
    assert r.status_code == 429 and "Too many" in r.get_data(as_text=True)


def recorded_status(ts):
    """API status at unix time ts: the Eddi reports every poll, the grid Harvi every 30 s with one
    5-minute silence (while 4 kW flows), the solar Harvi every 60 s."""
    from datetime import datetime as dt, timezone as tz
    def stamp(x):
        d = dt.fromtimestamp(x, tz.utc)
        return {"dat": d.strftime("%d-%m-%Y"), "tim": d.strftime("%H:%M:%S")}
    t0 = 1795000000                                   # fixed start so the silence lands in a known place
    grid_ts = ts - (ts % 30)
    if t0 + 3600 <= grid_ts < t0 + 3900:
        grid_ts = t0 + 3600 - 30
    return {"eddi": [{"sno": 999, **stamp(ts), "ectp1": 50, "ectt1": "Internal Load"}],
            "harvi": [{"sno": 111, **stamp(grid_ts), "ectp1": 4000, "ectt1": "Grid"},
                      {"sno": 222, **stamp(ts - (ts % 60)), "ectp1": -1500, "ectt1": "Generation"}]}


def fill_store(store, start, hours, step=15):
    import recorder
    # Test data only: skip the per-commit fsync, which makes thousands of tiny commits take minutes in Docker.
    store._conn().execute("PRAGMA synchronous = OFF")
    clock = {"t": start}
    rec = recorder.Recorder(store, lambda: recorded_status(clock["t"]), interval=step)
    for i in range(int(hours * 3600 / step)):
        clock["t"] = start + i * step
        rec.poll_once(now=clock["t"])
    return rec


def test_recorder_and_store(tmp_path):
    import recorder
    store = recorder.Store(tmp_path / "r.db")
    assert store.span() is None
    t0 = 1795000000
    rec = fill_store(store, t0, 2)
    lo, hi = store.span()
    assert lo == t0 and hi == t0 + 2 * 3600 - 15 and rec.last_ok == hi
    lg = store.load_log(t0, t0 + 3600)
    assert lg.serials("harvi") == ["111", "222"] and len(lg.rows[("harvi", "111")]) == 241
    r = lg.rows[("harvi", "111")][10]
    assert r.ct == 4000 and r.age == (r.t - r.report).total_seconds()
    assert ("zappi", "1") not in lg.rows and ("eddi", "999") in lg.rows
    # live chart points: one per distinct report
    pts = store.recent_points("harvi", "222", t0)
    assert len(pts) == 121 and pts[0][1] == {1: -1500.0}           # 2 h of 60 s reports, plus one just before t0
    # a failed poll is remembered and recording carries on
    def boom():
        raise RuntimeError("API down")
    rec.get_status = boom
    rec.poll_once(now=hi + 15)
    assert rec.last_error[1] == "API down" and store.span()[1] == hi
    # old rows are pruned past the retention period
    assert store.prune(t0 + 3600) > 0 and store.span()[0] == t0 + 3600


def make_recorded_app(tmp_path, hours=26, step=30):
    a = app_module.create_app({"TESTING": True, "UPLOAD_DIR": str(tmp_path / "up"), "DB_PATH": str(tmp_path / "r.db"),
                               "HUB_SERIAL": "1", "API_KEY": "k", "APP_PASSWORD": "",
                               "HARVI_NAMES": {"111": "Grid", "222": "Solar"}})
    a.extensions["client"] = FakeClient()
    fill_store(a.extensions["store"], 1795000000 - 3600 * (hours - 2), hours, step=step)
    return a


def test_stored_report(tmp_path):
    from datetime import datetime as dt, timezone as tz
    c = make_recorded_app(tmp_path).test_client()
    lo, hi = c.application.extensions["store"].span()
    fmt = lambda x: dt.fromtimestamp(x, tz.utc).strftime("%Y-%m-%dT%H:%M")
    # the nav goes to the recorded-data report; default range is the last 24 h of data
    page = c.get("/live").get_data(as_text=True)
    assert 'href="/report"' in page
    html = c.get("/report").get_data(as_text=True)
    assert 'type="datetime-local"' in html and f'value="{fmt(hi + 60)}"' in html
    assert f'min="{fmt(lo)}"' in html and "Last 7 days" in html
    assert "Gaps of concern" in html and "None found" not in html           # the ~5-minute silence is found
    # a narrower range, the chart for it, and the same in dark
    q = f"from={fmt(hi - 3 * 3600)}&to={fmt(hi - 3600)}&grid=111"
    html = c.get("/report?" + q).get_data(as_text=True)
    assert "None found with these settings" in html                          # silence is outside this window
    png = c.get("/report.png?" + q)
    assert png.status_code == 200 and png.data[:4] == b"\x89PNG"
    assert "attachment" not in png.headers.get("Content-Disposition", "inline")
    assert c.get("/report.png?" + q + "&theme=dark").data != png.data
    # out-of-range and back-to-front inputs are clamped with a note
    html = c.get(f"/report?from={fmt(hi)}&to={fmt(lo)}").get_data(as_text=True)
    assert "The start was after the end" in html
    html = c.get("/report?from=2000-01-01T00:00&to=2100-01-01T00:00").get_data(as_text=True)
    assert f'value="{fmt(lo)}"' in html or "at most 31 days" in html
    assert c.get("/report?from=garbage").status_code == 200


def test_stored_report_long_range_and_empty(tmp_path):
    c = make_recorded_app(tmp_path, hours=33 * 24, step=300).test_client()   # just needs > 31 days of data
    html = c.get("/report?from=2000-01-01T00:00&to=2100-01-01T00:00").get_data(as_text=True)
    assert "at most 31 days" in html
    # nothing recorded yet
    a = app_module.create_app({"TESTING": True, "UPLOAD_DIR": str(tmp_path / "up2"), "DB_PATH": str(tmp_path / "e.db"),
                               "HUB_SERIAL": "1", "API_KEY": "k", "APP_PASSWORD": ""})
    html = a.test_client().get("/report").get_data(as_text=True)
    assert "upload a log" in html and a.test_client().get("/report.png").status_code == 404


def test_live_chart_from_recorded_data(tmp_path):
    import time as _t
    import recorder
    a = make_app(tmp_path)
    store = a.extensions["store"]
    now = int(_t.time())
    def status_at(ts):
        from datetime import datetime as dt, timezone as tz
        d = dt.fromtimestamp(ts, tz.utc)
        return {"eddi": [{"sno": 999, "dat": d.strftime("%d-%m-%Y"), "tim": d.strftime("%H:%M:%S"),
                          "ectp1": 100, "ectt1": "Internal Load"}]}
    clock = {"t": now - 600}
    rec = recorder.Recorder(store, lambda: status_at(clock["t"]))
    for i in range(10):
        clock["t"] = now - 600 + i * 60
        rec.poll_once(now=clock["t"])
    a.extensions["recorder"] = rec                       # as if the background recorder were running
    eddi = a.test_client().get("/api/status").get_json()["devices"][0]
    assert not eddi["ct_chart"]["waiting"] and eddi["ct_chart"]["minutes"] == 9
    # a fresh recorder poll is reused by the dashboard instead of a second API call
    rec.latest = (_t.time(), {"zappi": [{"sno": 42, "ectp1": 7000, "ectt1": "Internal Load"}]})
    assert [d["kind"] for d in a.test_client().get("/api/status").get_json()["devices"]] == ["zappi"]
    rec.latest = (_t.time() - 3600, rec.latest[1])                  # stale: ask the API again
    assert a.test_client().get("/api/status").get_json()["devices"][0]["kind"] == "eddi"


def test_format_duration():
    from devices import format_duration
    assert [format_duration(x) for x in (0, 42, 59.6, 60, 312, 3600, 3605, 54025, 190000)] == \
        ["0s", "42s", "1m 0s", "1m 0s", "5m 12s", "1h 0m 0s", "1h 0m 5s", "15h 0m 25s", "2d 4h 46m 40s"]
    assert format_duration(None) == ""


def test_silences_respect_recording_gaps():
    from datetime import datetime as dt, timedelta as td, timezone as tz
    from charts import Row, silences, concerns, recording_gaps
    t0 = dt(2026, 11, 18, 10, 0, tzinfo=tz.utc)

    def polls(spec):
        """spec: list of (poll_offset_s, report_offset_s) -> rows polled every 15 s with 4 kW in the CT."""
        return [Row(t0 + td(seconds=p), t0 + td(seconds=r), p - r, 4000.0) for p, r in spec]

    # 1. continuous polling, Harvi silent 10:00:00 -> 10:05:00: one 300 s silence, as before
    rows = polls([(i, 0 if i < 300 else 300) for i in range(0, 600, 15)])
    s = silences(rows)
    assert [(x["gap"], x["cut_short"]) for x in s] == [(300.0, False)] and len(concerns(rows, 120, 500)) == 1
    # 2. app stopped 10:01 -> 11:00 and the Harvi's report is fresh when polling resumes: not a silence
    rows = polls([(i, i - i % 30) for i in range(0, 60, 15)] + [(i, i - i % 30) for i in range(3600, 3700, 15)])
    assert recording_gaps(rows) and concerns(rows, 120, 500) == []
    assert max(x["gap"] for x in silences(rows)) <= 45
    # 3. same outage, but after restart the cloud still shows the 10:00:30 report until 11:02: the cloud's
    #    timestamp proves it was silent the whole time, so that counts (ends at the new report)
    rows = polls([(i, 0 if i < 30 else 30) for i in range(0, 60, 15)] +
                 [(i, 30 if i < 3720 else 3720) for i in range(3600, 3800, 15)])
    s = silences(rows)
    assert s[-1]["gap"] == 3690 and not s[-1]["cut_short"]
    # 4. silent from 10:00 while polling until 10:10, then the app stops; afterwards the report is fresh:
    #    only the observed 10 minutes count, cut short at the last poll that still showed the old report
    rows = polls([(i, 0) for i in range(0, 615, 15)] + [(i, i) for i in range(5000, 5100, 15)])
    s = silences(rows)
    assert (s[0]["gap"], s[0]["cut_short"], s[0]["end"]) == (600.0, True, t0 + td(seconds=600))


def test_stored_report_with_recording_gap(tmp_path):
    """The app was down for 2 h in the middle of the data: no false gap of concern, gap shown on the page."""
    import recorder
    a = app_module.create_app({"TESTING": True, "UPLOAD_DIR": str(tmp_path / "up"), "DB_PATH": str(tmp_path / "r.db"),
                               "HUB_SERIAL": "1", "API_KEY": "k", "APP_PASSWORD": ""})
    store = a.extensions["store"]
    store._conn().execute("PRAGMA synchronous = OFF")
    t0 = 1795000000 - 6 * 3600
    def fresh_status(ts):                       # every device reports every 30 s, 4 kW in the grid CT
        from datetime import datetime as dt, timezone as tz
        d = dt.fromtimestamp(ts - ts % 30, tz.utc)
        stamp = {"dat": d.strftime("%d-%m-%Y"), "tim": d.strftime("%H:%M:%S")}
        return {"eddi": [{"sno": 999, **stamp, "ectp1": 0}], "harvi": [{"sno": 111, **stamp, "ectp1": 4000}]}
    clock = {"t": t0}
    rec = recorder.Recorder(store, lambda: fresh_status(clock["t"]))
    for ts in list(range(t0, t0 + 2 * 3600, 15)) + list(range(t0 + 4 * 3600, t0 + 6 * 3600, 15)):
        clock["t"] = ts
        rec.poll_once(now=ts)
    html = a.test_client().get("/report").get_data(as_text=True)
    assert "None found with these settings" in html                      # the 2 h outage is not a gap of concern
    assert "Not recording for 2h 0m 15s" in html and "1 pause," in html
    png = a.test_client().get("/report.png")
    assert png.status_code == 200 and png.data[:4] == b"\x89PNG"


def test_flow_values():
    import flow
    status = {
        "eddi": [{"sno": 1, "isVHubEnabled": True, "grd": 15, "gen": 0, "div": 1200, "sta": 3, "tp1": 55,
                  "ectp1": 1200, "ectt1": "Internal Load"}],
        "zappi": [{"sno": 2, "div": 0, "pst": "A", "zmo": 3, "ectp1": 0, "ectt1": "Internal Load"}],
        "harvi": [{"sno": 3, "ectp1": 3000, "ectt1": "Generation & Battery"},
                  {"sno": 4, "ectp1": -1000, "ectt1": "Grid"}],
    }
    v = flow.compute(status)
    assert v["grid"] == -1000 and v["solar"] == 3000 and v["eddi"] == 1200 and v["zappi"] == 0
    assert v["home"] == 800 and v["self_pct"] == 100                 # exporting: all use is self-powered
    b = flow.build(status)
    by = {n["key"]: n for n in b["nodes"]}
    assert by["solar"]["label"] == "Solar & battery" and by["grid"]["caption"] == ["Exporting"]
    assert by["eddi"]["caption"] == ["Heating", "Tank 55 °C"] and by["zappi"]["caption"] == ["Car unplugged", "Eco+ mode"]
    assert by["grid"]["active"] and by["zappi"]["link"]["cls"] == "idle"
    # importing at night, no Grid CT: falls back to the hub's grd; battery charging shows as negative solar
    status["harvi"] = [{"sno": 3, "ectp1": -500, "ectt1": "Generation & Battery"}]
    status["eddi"][0].update(grd=2000, div=0)
    v = flow.compute(status)
    assert v["grid"] == 2000 and v["home"] == 1500 and v["self_pct"] == 0
    assert {n["key"]: n for n in flow.build(status)["nodes"]}["solar"]["caption"] == ["Battery charging"]
    assert flow.fmt_power(873) == "873 W" and flow.fmt_power(-1234) == "1.2 kW" and flow.fmt_power(None) == "–"


def test_overview_page(tmp_path):
    c = make_app(tmp_path).test_client()
    html = c.get("/").get_data(as_text=True)
    assert "<h1>Overview</h1>" in html and 'class="flow"' in html and 'class="on">Overview' in html
    assert "4.0 kW" in html and "Hot water" in html and "Importing" in html     # 4000 W on the Grid CT
    assert 'href="/live#eddi-999"' in html and 'href="/live#harvi-111"' in html
    assert c.get("/partials/overview").status_code == 200
    assert 'id="eddi-999"' in c.get("/live").get_data(as_text=True)


def test_energy_buckets_and_totals(tmp_path):
    from datetime import date, datetime as dt
    from zoneinfo import ZoneInfo
    import energy, recorder
    tz = ZoneInfo("Europe/London")
    st = recorder.Store(tmp_path / "e.db")
    st._conn().execute("PRAGMA synchronous = OFF")
    day0 = dt(2026, 3, 28, tzinfo=tz).timestamp()          # Sat 28 Mar; clocks go forward on Sun 29 Mar
    rows = [(int(t), 0, 0, 0, 0, 1000.0) for t in range(int(day0), int(day0) + 3 * 86400, 15)]   # 1 kW flat
    with st._conn() as c:
        c.executemany("INSERT INTO flows VALUES (?, ?, ?, ?, ?, ?)", rows)
    now = day0 + 3 * 86400 + 3600
    s = energy.summarise(st, "day", date(2026, 3, 28), tz, now)
    assert len(s["rows"]) == 48 and abs(s["total"] - 24) < 0.01 and s["rows"][0]["label"] == "00:00"
    assert abs(s["rows"][-1]["cum"] - 24) < 0.01 and s["coverage"] > 0.99
    dst = energy.summarise(st, "day", date(2026, 3, 29), tz, now)          # a 23-hour day
    assert len(dst["rows"]) == 46 and abs(dst["total"] - 23) < 0.01
    assert [r["label"] for r in dst["rows"][2:4]] == ["02:00", "02:30"]   # 01:00-01:59 doesn't exist that day
    wk = energy.summarise(st, "week", date(2026, 3, 28), tz, now)
    assert [r["label"] for r in wk["rows"]] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert abs(wk["total"] - (24 + 23)) < 0.05 and wk["rows"][0]["kwh"] == 0 and wk["coverage"] < 0.5
    # periods that run into the future stop at now; a recording pause isn't stretched
    cur = energy.summarise(st, "day", date(2026, 3, 31), tz, now)
    assert cur["in_progress"] and cur["rows"][-1]["kwh"] is None and abs(cur["total"] - 1) < 0.02
    yr = energy.summarise(st, "year", date(2026, 3, 31), tz, now)
    assert len(yr["rows"]) == 12 and abs(yr["rows"][2]["kwh"] - 72) < 0.1 and yr["rows"][3]["kwh"] is None   # April: not yet
    assert energy.shift("month", date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert energy.period_bounds("week", date(2026, 11, 20), tz)[2] == "Week of 16 – 22 Nov 2026"
    assert s["self_pct"] == 100 and s["total_grid"] == 0           # grid 0 W: all from solar/battery

    # the grid share: 1 kW home + 1 kW hot water, importing 500 W -> a quarter of home use came from the grid;
    # with the battery charging at 1 kW too, 1.5 kW imported over 3 kW of demand -> half
    st2 = recorder.Store(tmp_path / "split.db")
    t1 = int(dt(2026, 6, 1, tzinfo=tz).timestamp())
    with st2._conn() as c:
        c.executemany("INSERT INTO flows VALUES (?, ?, ?, ?, ?, ?)",
                      [(t, 500, 1500, 1000, 0, 1000) for t in range(t1, t1 + 3600, 15)] +
                      [(t, 1500, -1000, 1000, 0, 1000) for t in range(t1 + 3600, t1 + 7200, 15)] +
                      [(t, None, 0, 0, 0, 1000) for t in range(t1 + 7200, t1 + 10800, 15)])
    sp = energy.summarise(st2, "day", date(2026, 6, 1), tz, t1 + 86400)
    hours = [(round(r["solar"], 3), round(r["grid"], 3)) for r in sp["rows"][:6]]
    assert hours[:2] == [(0.375, 0.125)] * 2 and hours[2:4] == [(0.25, 0.25)] * 2   # per half hour
    assert hours[4] == (0.0, 0.5) and hours[5][0] == 0 and hours[5][1] > 0.49   # grid unknown: counted as grid
    # (the very last reading has no next poll, so its 15 s aren't counted)
    assert sp["self_pct"] == round(100 * 1.25 / sp["total"])
    ch2 = energy.chart(sp)
    assert [p["cls"] for p in ch2["bars"][0]["parts"]] == ["solar", "grid"] and ch2["bars"][4]["parts"][0]["cls"] == "grid"
    assert energy._nice_max(14.4) == (15, 5) and energy._nice_max(579)[0] == 750

    ch = energy.chart(s)
    assert len(ch["bars"]) == 48 and ch["last"]["x"] == 100.0 and [l["text"] for l in ch["xlabels"]][:2] == ["00:00", "03:00"]


def test_flow_recording_and_backfill(tmp_path):
    import recorder
    st = recorder.Store(tmp_path / "b.db")
    fill_store(st, 1795000000, 1, step=60)                  # recorded before flows existed...
    with st._conn() as c:
        c.execute("DELETE FROM flows")
    rec = recorder.Recorder(st, lambda: recorded_status(1795000000 + 3600))
    rec.poll_once(now=1795000000 + 3600)                      # ...the next poll records a flow and backfills
    n, home = st._conn().execute("SELECT COUNT(*), MIN(home) FROM flows").fetchone()
    assert n == 61 and home == 4000 + -1500 - 50                 # grid + generation - eddi internal load
    assert st.backfill_flows(recorded_status(1795000000)) == 0   # nothing left to fill


def test_energy_page(tmp_path):
    a = make_recorded_app(tmp_path, hours=6, step=60)
    a.config["NOW"] = 1795000000 + 3 * 3600
    c = a.test_client()
    html = c.get("/energy").get_data(as_text=True)
    assert "Home energy use" in html and "Energy usage" in html and ">Running total <" not in html and "Per half hour" in html
    assert 'class="use-area use-home"' in html and 'class="use-area use-charge"' in html   # battery charging layer
    # this data imports 4 kW against 4 kW of demand (home, hot water, battery charging): all from the grid
    assert "From solar &amp; battery" in html and 'class="bar grid' in html and 'class="bar solar' not in html
    assert "Imported from the grid" in html and 'class="eplot imports"' in html and 'class="ibar home"' in html
    assert 'class="on" aria-current="true">Day' in html and 'class="eplot cum"' in html
    assert 'href="/energy" class="on">Energy</a>' in html and 'class="on">Overview' not in html   # its own tab
    for p in ("week", "month", "year"):
        assert c.get(f"/energy?period={p}").status_code == 200
    assert c.get("/energy?period=bogus&date=nope").status_code == 200
    assert "hasn't started yet" in c.get("/energy?period=year&date=2099-01-01").get_data(as_text=True)
    assert "No readings were recorded" in c.get("/energy?period=day&date=2020-01-01").get_data(as_text=True)
    assert 'href="/energy"' in c.get("/").get_data(as_text=True)          # the Overview's Home node links here


def test_estimate_combines_measured_and_sampled(tmp_path):
    import estimate, recorder
    st = recorder.Store(tmp_path / "est.db")
    t0 = 1795000000 // 1800 * 1800
    # samples: solar alternates 800/1200 W (mean 1 kW); the sampled home is 1.9 kW (it missed some spikes)
    rows = [(t, 1400, 800 if (t // 15) % 2 else 1200, 500, 0, 1900) for t in range(t0, t0 + 3600, 15)
            if not (t0 + 600 <= t < t0 + 900)]                       # a 5-minute polling gap
    with st._conn() as c:
        c.executemany("INSERT INTO flows VALUES (?, ?, ?, ?, ?, ?)", rows)
    # the hub measured 1.5 kW import and 0.5 kW to the Eddi every minute of the first 50 minutes
    st.save_minutes("2026-11-18", [(m, 1500 * 60, 0, 500 * 60, None, None) for m in range(t0, t0 + 3000, 60)], complete=False)
    hh = estimate.compute(st, t0, t0 + 3600)
    home, grid, var, covered, measured, sampled, imp, exp_, g_batt, g_dev, eddi, zappi, charge = hh[t0]
    assert abs(imp - 0.75) < 1e-9 and exp_ == 0 and g_batt == 0
    assert abs(eddi - 0.25) < 1e-9 and zappi == 0 and charge == 0     # old-style rows: diversion counts as Eddi
    assert abs(g_dev - 0.15) < 0.01 and abs(grid + g_batt + g_dev - imp) < 0.01   # import fully accounted for
    assert abs(home - 1.0) < 0.01                       # 1.5 kW + 1 kW solar - 0.5 kW Eddi = 2 kW for 30 min
    assert abs(grid - 0.6) < 0.01                       # 1.5 kW of the 2.5 kW demand came from the grid
    assert covered == measured == 1800                  # the polling gap is filled by interpolation
    # sampled only: 1.9 kW for the ~25 minutes polled (the reading before the gap counts for up to 60 s)
    assert 0 < estimate.ci95(var) < 0.05 and 1.9 * 25 / 60 < sampled < 1.9 * 26.5 / 60
    home2, _, _, covered2, measured2 = hh[t0 + 1800][:5]
    assert covered2 == 1800 and measured2 == 1200       # last 10 minutes: no measured data yet -> samples
    assert abs(home2 - (2.0 * 20 + 1.9 * 10) / 60) < 0.01
    # no variation in the solar readings -> no sampling error
    with st._conn() as c:
        c.execute("UPDATE flows SET solar = 1000")
    assert estimate.compute(st, t0, t0 + 1800)[t0][2] == 0
    # final half hours are cached once their day's history is complete
    st.save_minutes(_day(t0), [(m, 1500 * 60, 0, 500 * 60, 500 * 60, 0) for m in range(t0, t0 + 3600, 60)], complete=True)
    with st._conn() as c:
        c.execute("INSERT OR REPLACE INTO history_days VALUES (?, 0, 1)", (_day(t0),))
    got = estimate.half_hours(st, t0, t0 + 3600, now=t0 + 86400 * 2)
    assert set(st.cached_hh(t0, t0 + 3600)) == {t0, t0 + 1800} and abs(got[t0 + 1800][0] - 1.0) < 0.01


def _day(ts):
    from datetime import datetime as dt, timezone as tz
    return dt.fromtimestamp(ts, tz.utc).strftime("%Y-%m-%d")


def test_history_sync(tmp_path):
    import recorder
    st = recorder.Store(tmp_path / "h.db")
    now = 1795000000
    with st._conn() as c:
        c.execute("INSERT INTO flows VALUES (?, 0, 0, 0, 0, 0)", (now - 2 * 86400,))   # recording began 2 days ago
    calls = []

    def history(kind, sno, day):
        calls.append((kind, day))
        from datetime import datetime as dt, timezone as tz
        d0 = int(dt.strptime(day, "%Y-%m-%d").replace(tzinfo=tz.utc).timestamp())
        if kind == "eddi":
            return [(m, {"imp": 600, "exp": 60, "h1d": 120}) for m in range(d0, d0 + 86400 + 60, 60)]
        return [(m, {"imp": 999, "h1b": 240}) for m in range(d0, d0 + 86400, 60)]

    status = {"eddi": [{"sno": 1, "isVHubEnabled": True}], "zappi": [{"sno": 2}], "harvi": [{"sno": 3}]}
    rec = recorder.Recorder(st, lambda: status, get_history=history)
    days = rec.sync_history(status, now)
    assert days == [_day(now), _day(now - 86400), _day(now - 2 * 86400)]           # newest first
    assert {k for k, _ in calls} == {"eddi", "zappi"}
    hist = st.history_days()
    assert not hist[_day(now)][1] and hist[_day(now - 86400)][1]                     # today isn't complete
    m = st.minutes(now - 86400, now - 86400 + 60)
    assert list(m.values()) == [(600, 60, 360, 120, 240)]   # hub's import/export; Eddi + Zappi, then each
    assert st._conn().execute("SELECT COUNT(*) FROM minutes").fetchone()[0] == 3 * 1440   # the 1441st minute is dropped
    calls.clear()
    assert rec.sync_history(status, now + 600) == [_day(now)] and len(calls) == 2   # only today again


def test_estimate_overnight_battery_charging(tmp_path):
    """Off-peak: 3.9 kW imported while the battery charges at 3.4 kW -> the home used 0.5 kW."""
    import estimate, recorder
    st = recorder.Store(tmp_path / "night.db")
    t0 = 1795000000 // 1800 * 1800
    with st._conn() as c:
        c.executemany("INSERT INTO flows VALUES (?, ?, ?, ?, ?, ?)",
                      [(t, 3900, -3400, 0, 0, 500) for t in range(t0, t0 + 1800, 15)])
    st.save_minutes("2026-11-18", [(m, 3900 * 60, 0, 0, 0, 0) for m in range(t0, t0 + 1800, 60)], complete=False)
    st.save_minutes("2026-11-17", [(m, 1000 * 60, 0, 0, 0, 0) for m in range(t0 - 1800, t0, 60)], complete=False)
    hh = estimate.compute(st, t0 - 1800, t0 + 1800)
    home, grid, _, _, _, _, imp, _, g_batt, g_dev, _, _, charge = hh[t0]
    assert abs(charge - 1.7) < 1e-6
    assert abs(imp - 1.95) < 1e-9 and abs(home - 0.25) < 1e-6 and abs(grid - 0.25) < 1e-6
    assert abs(g_batt - 1.7) < 1e-6 and g_dev == 0
    # before recording started there's measured import but no solar & battery reading: counted, not attributed
    before = hh[t0 - 1800]
    assert abs(before[6] - 0.5) < 1e-9 and before[0] == before[1] == before[8] == 0 and before[3] == 0


def test_minutes_table_upgrade(tmp_path):
    """A database from before Eddi and Zappi were kept apart gains the columns and refetches its history."""
    import sqlite3, recorder
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript("""CREATE TABLE minutes (ts INTEGER PRIMARY KEY, imp REAL, exp REAL, div REAL);
                       CREATE TABLE history_days (day TEXT PRIMARY KEY, fetched INTEGER, complete INTEGER);
                       INSERT INTO minutes VALUES (60, 1, 2, 3); INSERT INTO history_days VALUES ('2026-11-19', 1, 1);""")
    c.commit()
    c.close()
    st = recorder.Store(db)
    assert st.minutes(0, 120) == {60: (1, 2, 3, None, None)} and st.history_days() == {}


if __name__ == "__main__":
    import tempfile
    for fn in (test_dashboard_and_api, test_upload_and_report, test_current_report_persists,
               test_ct_choice, test_ct_chart_builds_up, test_upload_rejects_bad_files, test_behind_proxy,
               test_login, test_login_secure_cookie_and_throttle, test_recorder_and_store,
               test_stored_report, test_stored_report_long_range_and_empty, test_live_chart_from_recorded_data,
               test_stored_report_with_recording_gap, test_overview_page, test_energy_buckets_and_totals,
               test_flow_recording_and_backfill, test_energy_page, test_estimate_combines_measured_and_sampled,
               test_history_sync, test_estimate_overnight_battery_charging, test_minutes_table_upgrade):
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
            print("ok", fn.__name__)
    test_load_log_errors()
    print("ok test_load_log_errors")
    test_format_duration()
    print("ok test_format_duration")
    test_flow_values()
    print("ok test_flow_values")
    test_silences_respect_recording_gaps()
    print("ok test_silences_respect_recording_gaps")
    test_client_ignores_undefined_server_name()
    print("ok test_client_ignores_undefined_server_name")
