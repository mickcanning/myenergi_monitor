#!/usr/bin/env python3
"""
Harvi link monitor (indirect link-reliability indicator) - multiple Harvis + Eddi cross-check.

myenergi exposes no RSSI/link-quality value. This script polls the myenergi
cloud API and records, for EACH Harvi on the hub, how fresh its last report is
(UTC "dat"/"tim"). It also records how fresh the Eddi's own report is, to help
separate two causes of a stale Harvi:

  * Harvi stale, Eddi fresh  -> the Eddi is online but not hearing the Harvi
                                (RF link problem: distance, obstruction, channel).
  * Harvi stale, Eddi stale  -> the Eddi itself isn't reaching the cloud
                                (internet / cloud problem, not the Harvi link).

Usage:
  export MYENERGI_HUB_SERIAL=12345678
  export MYENERGI_API_KEY=your_api_key        # myaccount.myenergi.com > Products > Advanced
  python3 harvi_link_monitor.py --interval 30 --stale 120 --csv harvi_eddi_links.csv

Stop with Ctrl+C to print a per-Harvi summary. Standard library only.
"""
import argparse
import csv
import json
import os
import signal
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone

DIRECTOR = "director.myenergi.net"
HEADER = ["poll_utc", "device", "sno", "report_utc", "age_s", "new_report", "stale",
          "eddi_age_s", "ectp1", "ectp2", "ectp3"]


def make_opener(host, user, key):
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, f"https://{host}/", user, key)
    return urllib.request.build_opener(
        urllib.request.HTTPDigestAuthHandler(mgr),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )


def discover_host(user, key):
    """The director replies with the real server in the X_MYENERGI-asn header."""
    op = make_opener(DIRECTOR, user, key)
    try:
        with op.open(f"https://{DIRECTOR}/cgi-jstatus-H", timeout=15) as r:
            return r.headers.get("X_MYENERGI-asn") or DIRECTOR
    except Exception as e:
        hdrs = getattr(e, "headers", None)
        if hdrs and hdrs.get("X_MYENERGI-asn"):
            return hdrs["X_MYENERGI-asn"]
        raise


def fetch_status(host, user, key):
    """Fetch everything on the hub; return (harvi_records, eddi_records).

    cgi-jstatus-* is documented by the community as a list of single-key dicts
    ([{"eddi": [...]}, {"harvi": [...]}, ...]); a plain dict is handled too.
    """
    op = make_opener(host, user, key)
    with op.open(f"https://{host}/cgi-jstatus-*", timeout=15) as r:
        data = json.load(r)
    if isinstance(data, dict):
        data = [data]
    harvis, eddis = [], []
    for item in data:
        if isinstance(item, dict):
            harvis.extend(item.get("harvi", []) or [])
            eddis.extend(item.get("eddi", []) or [])
    return harvis, eddis


def parse_ts(rec):
    """'dat' = dd-mm-YYYY, 'tim' = HH:MM:SS, both UTC."""
    if "dat" not in rec or "tim" not in rec:
        return None
    return datetime.strptime(f"{rec['dat']} {rec['tim']}", "%d-%m-%Y %H:%M:%S").replace(
        tzinfo=timezone.utc
    )


class Stats:
    def __init__(self):
        self.polls = 0
        self.stale = 0
        self.stale_eddi_fresh = 0   # radio suspected
        self.stale_eddi_stale = 0   # cloud/internet suspected
        self.stale_eddi_unknown = 0
        self.last_ts = None
        self.max_age = 0.0
        self.gaps = []  # seconds between distinct consecutive report timestamps


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=int, default=30, help="poll period in seconds (default 30)")
    ap.add_argument("--stale", type=int, default=120,
                    help="report age (s) above which a device counts as 'stale' (default 120)")
    ap.add_argument("--csv", default="harvi_eddi_links.csv", help="CSV output path")
    ap.add_argument("--serial", default=os.environ.get("MYENERGI_HUB_SERIAL"), help="hub serial")
    ap.add_argument("--key", default=os.environ.get("MYENERGI_API_KEY"), help="API key")
    args = ap.parse_args()

    if not args.serial or not args.key:
        sys.exit("Set MYENERGI_HUB_SERIAL and MYENERGI_API_KEY (or use --serial/--key).")

    # Refuse to append to a CSV written by an older version of the script (different columns).
    if os.path.exists(args.csv) and os.path.getsize(args.csv) > 0:
        with open(args.csv, newline="") as f:
            first = next(csv.reader(f), [])
        if first != HEADER:
            sys.exit(f"{args.csv} has a different column layout; choose a new file with --csv.")

    host = discover_host(args.serial, args.key)
    print(f"Using API host {host}; logging to {args.csv}. Ctrl+C to stop.")

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))

    errors = 0
    stats = {}       # harvi serial -> Stats
    eddi_last = {}   # eddi serial -> last report timestamp (for new_report flag)

    new_file = not os.path.exists(args.csv) or os.path.getsize(args.csv) == 0
    with open(args.csv, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(HEADER)
        while not stop["flag"]:
            now = datetime.now(timezone.utc)
            try:
                harvis, eddis = fetch_status(host, args.serial, args.key)
                if not harvis:
                    raise RuntimeError("no harvi in API response")

                # Eddi rows first; the freshest Eddi age is used for the cross-check.
                eddi_ages = []
                for rec in eddis:
                    sno = str(rec.get("sno", "unknown"))
                    ts = parse_ts(rec)
                    age = (now - ts).total_seconds() if ts else float("nan")
                    new = ts is not None and ts != eddi_last.get(sno)
                    if ts is not None:
                        eddi_last[sno] = ts
                        eddi_ages.append(age)
                    w.writerow([now.isoformat(timespec="seconds"), "eddi", sno,
                                ts.isoformat() if ts else "", round(age, 1), int(new),
                                int(age > args.stale), "", rec.get("ectp1"),
                                rec.get("ectp2"), rec.get("ectp3")])
                    print(f"{now:%H:%M:%S}  eddi  {sno}  age={age:6.0f}s  "
                          f"{'NEW ' if new else '    '}{'STALE' if age > args.stale else ''}")
                eddi_age = min(eddi_ages) if eddi_ages else None

                for rec in harvis:
                    sno = str(rec.get("sno", "unknown"))
                    st = stats.setdefault(sno, Stats())
                    ts = parse_ts(rec)
                    age = (now - ts).total_seconds() if ts else float("nan")
                    new = ts is not None and ts != st.last_ts
                    if new and st.last_ts is not None:
                        st.gaps.append((ts - st.last_ts).total_seconds())
                    if ts is not None:
                        st.last_ts = ts
                    stale = age > args.stale
                    st.polls += 1
                    if age == age:  # not NaN
                        st.max_age = max(st.max_age, age)
                    note = ""
                    if stale:
                        st.stale += 1
                        if eddi_age is None:
                            st.stale_eddi_unknown += 1
                            note = "STALE (eddi unknown)"
                        elif eddi_age <= args.stale:
                            st.stale_eddi_fresh += 1
                            note = "STALE, eddi fresh -> RF link?"
                        else:
                            st.stale_eddi_stale += 1
                            note = "STALE, eddi stale too -> cloud/internet?"
                    w.writerow([now.isoformat(timespec="seconds"), "harvi", sno,
                                ts.isoformat() if ts else "", round(age, 1),
                                int(new), int(stale),
                                "" if eddi_age is None else round(eddi_age, 1),
                                rec.get("ectp1"), rec.get("ectp2"), rec.get("ectp3")])
                    print(f"{now:%H:%M:%S}  harvi {sno}  age={age:6.0f}s  "
                          f"{'NEW ' if new else '    '}{note}")
                f.flush()
            except Exception as e:
                errors += 1
                print(f"{now:%H:%M:%S}  error: {e}")
            for _ in range(args.interval):
                if stop["flag"]:
                    break
                time.sleep(1)

    print(f"\n--- summary (API errors: {errors}) ---")
    for sno, st in stats.items():
        print(f"\nHarvi {sno}")
        print(f"  polls: {st.polls}  stale (> {args.stale}s): {st.stale} "
              f"({100 * st.stale / st.polls:.1f}%)  max age: {st.max_age:.0f}s")
        if st.stale:
            print(f"  of the stale polls: eddi fresh (RF link suspected): {st.stale_eddi_fresh}, "
                  f"eddi also stale (cloud/internet suspected): {st.stale_eddi_stale}, "
                  f"eddi unknown: {st.stale_eddi_unknown}")
        if st.gaps:
            g = sorted(st.gaps)
            print(f"  distinct reports: {len(g) + 1}  median gap: {g[len(g) // 2]:.0f}s  max gap: {g[-1]:.0f}s")


if __name__ == "__main__":
    main()
