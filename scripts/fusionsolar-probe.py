#!/usr/bin/env python3
"""Check that the app can read your Huawei battery and solar from the FusionSolar website (read-only).

Setup (once):
    .venv/bin/pip install fusion_solar_py
    add to .env (git ignores it):
        FUSIONSOLAR_USER=your-login
        FUSIONSOLAR_PASSWORD=your-password
        FUSIONSOLAR_SUBDOMAIN=uni004eu5        # the start of the address after you log in

Run:
    .venv/bin/python scripts/fusionsolar-probe.py              # now, plus today's 5-minute history
    .venv/bin/python scripts/fusionsolar-probe.py --depth      # also: how many days back the history goes

It uses the app's own fusionsolar.py, so what works here works in the app. The login session is saved in
instance/fusionsolar-session.json (git ignores instance/) and reused; delete it to force a fresh login.
"""
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fusionsolar  # noqa: E402


def load_dotenv(path=ROOT / ".env"):
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip('"').strip("'"))


def fmt(v, unit="", nd=0):
    return "—" if v is None else f"{v:.{nd}f}{unit}"


def main():
    load_dotenv()
    user, password = os.environ.get("FUSIONSOLAR_USER"), os.environ.get("FUSIONSOLAR_PASSWORD")
    if not user or not password:
        sys.exit("Set FUSIONSOLAR_USER and FUSIONSOLAR_PASSWORD in .env first (see the top of this script).")
    tz = ZoneInfo(os.environ.get("TZ") or "Europe/London")
    (ROOT / "instance").mkdir(exist_ok=True)
    fs = fusionsolar.FusionSolarClient(user, password, os.environ.get("FUSIONSOLAR_SUBDOMAIN"),
                                       session_path=str(ROOT / "instance" / "fusionsolar-session.json"))

    now = fs.now()
    if now is None:
        sys.exit(f"Couldn't read FusionSolar: {fs.last_error}")
    print(f"Plant {fs._ids[0]}, battery {fs._ids[1]}, inverter {fs._ids[2]}")
    print(f"Now: battery {fmt(now['soc'], '%')} at {fmt(now['battery'], ' W')} (+ charging), "
          f"solar {fmt(now['solar'], ' W')}, home {fmt(now['load'], ' W')}; today charged "
          f"{fmt(now['charged_kwh'], ' kWh', 2)}, discharged {fmt(now['discharged_kwh'], ' kWh', 2)}; "
          f"mode {now['mode']}")

    today = datetime.now(tz).date()
    rows = fs.day(today, tz)
    if rows is None:
        sys.exit(f"Today's history failed: {fs.last_error}")
    print(f"\nToday: {len(rows)} five-minute readings")
    for t, soc, batt, solar in rows[-6:]:
        print(f"  {datetime.fromtimestamp(t, tz):%H:%M}  battery {fmt(soc, '%'):>4} {fmt(batt, ' W'):>8}   "
              f"solar {fmt(solar, ' W'):>7}")
    peak = max(rows, key=lambda r: r[3] or 0, default=None)
    if peak and peak[3]:
        print(f"  solar peak today {peak[3]:.0f} W at {datetime.fromtimestamp(peak[0], tz):%H:%M}")

    if "--depth" in sys.argv:
        print("\nHow far back the history goes:")
        for back in (1, 7, 30, 90, 180, 245, 365):
            d = today - timedelta(days=back)
            r = fs.day(d, tz)
            if r is None:
                print(f"  {d}  failed: {fs.last_error}")
                continue
            n_soc = sum(1 for x in r if x[1] is not None)
            n_pv = sum(1 for x in r if x[3] is not None)
            kwh = sum(x[3] or 0 for x in r) * 300 / 3.6e6
            ok = r and all(date.fromtimestamp(0) < datetime.fromtimestamp(x[0], tz).date() == d for x in r[:1])
            print(f"  {d}  {len(r):3} readings (battery {n_soc}, solar {n_pv}), solar {kwh:.1f} kWh"
                  f"{'' if ok or not r else '  ! readings are from another day'}")

    print(f"\nChecked at {datetime.now(tz):%H:%M}.")


if __name__ == "__main__":
    main()
