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
                      "ectp2": 0, "ectt2": "None", "ectp3": 0, "ectt3": "None", "frq": 50.01, "vol": 2345, "sta": 3}],
            "harvi": [{"sno": 111, "dat": "18-11-2026", "tim": "22:58:00", "ectp1": 4000, "ectt1": "Grid"}],
            "asn": "s18.myenergi.net",
        }


def make_app(tmp_path):
    a = app_module.create_app({"TESTING": True, "UPLOAD_DIR": str(tmp_path), "HUB_SERIAL": "1", "API_KEY": "k",
                               "HARVI_NAMES": {"111": "Grid", "222": "Solar & Battery"}})
    a.extensions["client"] = FakeClient()
    return a


def test_dashboard_and_api(tmp_path):
    c = make_app(tmp_path).test_client()
    html = c.get("/").get_data(as_text=True)
    assert "Eddi" in html and "Internal Load" in html and "Grid" in html
    assert "50.01 Hz" in html and "234.5 V" in html          # auto-scaled units
    data = c.get("/api/status").get_json()
    assert [d["kind"] for d in data["devices"]] == ["eddi", "harvi"]
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
    # no report yet: the nav tab goes to the upload page
    assert 'href="/upload"' in c.get("/").get_data(as_text=True)
    loc = c.post("/upload", data={"log": (io.BytesIO(make_csv().encode()), "log.csv")},
                 content_type="multipart/form-data").headers["Location"]
    # visiting the live tab and coming back: the tab now points at the report
    assert f'href="{loc}"' in c.get("/").get_data(as_text=True)
    # chosen settings are remembered too
    c.get(loc + "?grid=222&thresh=45")
    assert f'href="{loc}?grid=222&amp;thresh=45"' in c.get("/").get_data(as_text=True)
    # ...and survive an app restart
    c2 = make_app(tmp_path).test_client()
    assert f'href="{loc}?grid=222&amp;thresh=45"' in c2.get("/").get_data(as_text=True)
    # a new upload replaces the current report
    loc2 = c2.post("/upload", data={"log": (io.BytesIO(make_csv().encode()), "again.csv")},
                   content_type="multipart/form-data").headers["Location"]
    assert loc2 != loc and f'href="{loc2}"' in c2.get("/").get_data(as_text=True)
    # a failed upload leaves the current report in place
    c2.post("/upload", data={"log": (io.BytesIO(b"nope"), "bad.csv")}, content_type="multipart/form-data")
    assert f'href="{loc2}"' in c2.get("/").get_data(as_text=True)


def test_ct_choice(tmp_path):
    c = make_app(tmp_path).test_client()
    page = lambda cl=c: cl.get("/").get_data(as_text=True)
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


if __name__ == "__main__":
    import tempfile
    for fn in (test_dashboard_and_api, test_upload_and_report, test_current_report_persists,
               test_ct_choice, test_ct_chart_builds_up, test_upload_rejects_bad_files):
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
            print("ok", fn.__name__)
    test_load_log_errors()
    print("ok test_load_log_errors")
    test_client_ignores_undefined_server_name()
    print("ok test_client_ignores_undefined_server_name")
