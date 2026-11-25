# myenergi monitor

A small local Flask app for investigating Harvi link problems on a myenergi system.

- **Overview** (`/`, the start page): a simple power-flow picture of the system right now. Solar (and battery) and the grid sit at the top, home, hot water (Eddi) and car (Zappi) at the bottom. Arrows show which way power is flowing, and the centre shows how much of what you're using is self-powered. Grid and solar come from the CTs named Grid and Generation (or Generation & Battery). Hot water and car come from each device's diverted power. Home is estimated as grid + solar − hot water − car. Below the picture, each device is listed with its CT readings.
- **Energy** (`/energy`, also opened by tapping Home on the Overview): how much energy the home has used over a day (per half hour), a week or month (per day) or a year (per month). The top panel, **Energy usage**, shows where the energy went in each half hour, day or month, stacked: home, hot water (Eddi), battery charging, car (Zappi) and exported. Each use only appears while it is happening, and the legend gives its total for the period. The **Home energy source** panel below shows the home's use for each half hour, day or month, with each bar split into energy from solar & battery and energy from the grid. The grid's share at any moment is the grid import divided by everything being powered then (home, hot water, car and battery charging). When grid power isn't known, the energy counts as grid, so the self-powered figure is never overstated. Figures are a best estimate, built as follows:
  - **Grid import and export, hot water and car:** taken from the hub's own minute-by-minute history (`cgi-jday`). This is measured continuously, so short spikes between our 15-second polls still count, and so do times the app wasn't running. The recorder fetches this history every 10 minutes and stores it. It also backfills the hub's history from before recording began, back to `HISTORY_FROM` (default a year), 10 days per fetch, newest first. That is about 4 hours per 240 days. The hub keeps this history for well over a year. The Car tab uses those days. The Energy tab still starts on the first recorded day, because its home figures need our solar & battery samples.
  - **Solar & battery:** the Harvis keep no history, so this part comes from our samples. Each minute uses the average of its samples, and gaps of up to 30 minutes are filled by interpolating between the readings on either side.
  - **Uncertainty:** each figure carries an approximate 95% range for the sampled part. It's shown as shading around the running total, whiskers on the bars, and a ± column in the table.
  - **Sampled-only figures:** the table also shows the old sampled-only figure for comparison.
  - **Very recent minutes:** these fall back to sampled values until the next history sync.
  - **Grid import panel:** the page also shows **grid import and export**, measured by the hub, so the totals match the myenergi app. It splits the import by where it went: **used by the home**, **charged the battery** (e.g. off-peak overnight charging), **heated the hot water** and **charged the car**. When the hot water and car both draw power in the same half hour, their grid share is split in proportion to what each used. Import at times with no solar & battery reading (before recording started, or long polling gaps) is shown as *not attributed*. Home use is lower than the import whenever some of the imported energy is still in the battery. Step back and forward through periods, hover or tap for exact figures, or open the table view. Periods follow local time, taken from the `TZ` setting (default Europe/London). Unraid sets `TZ` for every container automatically. Recording before this feature existed is converted using the current CT names.
- **Car** (`/car`, also opened by tapping the car on the Overview): car charging per day for a month or a year. Charging sessions are found in the Zappi's minute-by-minute history from the hub: a run of minutes drawing at least 100 W, with pauses of up to 30 minutes kept in the same session. A session that runs past midnight counts on the day it started. Each session lists its time, kWh, how much came from the grid, what it cost and which tariff rates it used. Only grid energy is costed: while solar or the battery also supplies power, the car's grid share is the import divided by everything being powered then. The chart shows each day's cost stacked by tariff rate, with a rolling 30-day average cost per day. The average divides by the days recorded, fewer at the start of recording, so days without charging bring it down. Without a tariff the page shows the kWh charged instead. For days before recording began there are no solar & battery samples. On those days the car is costed for as much of each minute's import as it could have taken, the smaller of its own power and the import. That is an upper bound, and charging purely from surplus solar still costs nothing.
- **Tariff and import costs** (Settings → Tariff): enter your currency and any number of rates. Each rate has a name, a local time range and a price per kWh, e.g. Day 08:00–23:00 at 0.295 and Night 23:00–08:00 at 0.085. Prices are in whole currency units, so 29.5p is 0.295.
  - **Overlaps:** ranges can run past midnight and can overlap. Where they overlap, the cheapest rate applies, so a Nightboost rate from 02:00–04:00 beats Night.
  - **Full coverage:** together the rates must cover all 24 hours. Saving checks this and lists any missing times.
  - **Costs on the Energy tab:** the grid import card shows what the import cost, split by rate, and a **Cost of grid import** chart shows the cost per slot. Costs are worked out per minute from the hub's measured import, and only grid import is costed.
  - **Changing the tariff** recalculates every period.
- **Hot water tank temperature (optional, EPH Ember):** if your heating system is an EPH Controls Ember with a hot water probe, set `EMBER_EMAIL` and `EMBER_PASSWORD` (your Ember app login). The Overview's Hot water node then shows the tank temperature. The recorder stores the Ember zone temperatures every 5 minutes, and the Energy tab adds a **Hot water tank** chart. This uses [pyephember](https://pypi.org/project/pyephember/), an unofficial client for the Ember cloud API, and only reads data. If Ember can't be reached, the temperature simply isn't shown. The Eddi's own temperature field is only shown once it has been seen to change, because without a probe it reports a fixed value.
- **Live values** (`/live`): the latest readings for every device on your hub (Eddi, Zappi, Libbi, Harvi), refreshed automatically, with how old each device's last report is.
- **Recording**: while the app runs it polls the API every 15 s in the background and stores every reading in `instance/readings.db`. The raw readings (every device's full status, about 1.9 MB a day) feed Live values and Harvi health and are kept for `RETENTION_DAYS` (default 180). Everything else is kept indefinitely for the Energy and Car history: power flows, the hub's minute history, temperatures and signal metrics, about 0.3 MB a day or roughly 1 GB per 10 years. In total that is about 340 MB of raw readings plus about 100 MB per year of history.
- **Harvi health** (`/report`): how reliably the Harvis report. The **Signal quality** section at the top is for judging changes such as a new antenna:
  - **Reports per powered minute** for each Harvi, the headline figure. It only counts time when the Harvi had at least 500 W through its clamp, since a Harvi powers itself from that current. For the grid Harvi this is judged from the hub's own measurements. For other Harvis, a silence longer than 15 minutes is taken as no current.
  - **Reporting gaps:** the typical and 95th-percentile gaps between reports, and silences of 120 s or more per powered hour.
  - **Daily trend chart**, with your changes marked on it.
  - **Change log:** note what you changed and when. Each entry gets a before/after comparison over up to a week either side, using hourly averages with an approximate 95% range.
  - **Interference check:** compares reporting while the immersion heater or car charger is on against when both are off.

  Below that is the original report. Pick a start and end date within the recorded data (up to 31 days at a time) and get the report chart and tables: Harvi report age, CT power, silences over a threshold, and "gaps of concern" (long silences while current was flowing, so low harvested power doesn't explain them). You can still upload a CSV from `monitor.py` instead.
- **`monitor.py`**: optional command-line logger for recording somewhere the app isn't running; its CSV can be uploaded to the report.

The myenergi API used here is the community-documented, unofficial one, so field names and units may differ by firmware.
- **Huawei battery & solar (optional, FusionSolar):** with a Huawei inverter and LUNA2000 battery, set `FUSIONSOLAR_USER`, `FUSIONSOLAR_PASSWORD` and `FUSIONSOLAR_SUBDOMAIN` (your FusionSolar owner login, and the start of the web address after you log in, e.g. `uni004eu5`). The Overview then splits the Solar & battery node into **Solar** and **Battery**. Solar is FusionSolar's latest 5-minute reading. The battery is the rest of the Harvi's live solar & battery reading, so the two always add up to what the Harvi measures and the battery responds live; at night it is exactly the Harvi reading. A ring around the Battery node shows its state of charge. If FusionSolar's newest reading is over 20 minutes old, the Overview goes back to the single combined node. The recorder stores FusionSolar's 5-minute battery state of charge, battery power and solar power, and backfills them to `HISTORY_FROM`, a few days every 5 minutes. FusionSolar publishes each reading about 5 minutes late, so these are never live. This reads the FusionSolar website with [fusion_solar_py](https://github.com/jgriss/FusionSolarPy), an unofficial client, and only reads data. The login is saved as `fusionsolar-session.json` next to the database and reused, because FusionSolar asks for a captcha after too many logins; a failed login waits 30 minutes before trying again. `scripts/fusionsolar-probe.py` checks the settings from your `.env`.

## Setup

```bash
cd ~/repos/myenergi_monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env
```

In `.env`, set your **hub serial number** and an **API key** (myaccount.myenergi.com > Products > Advanced > Generate new API key). Generating a new key cancels the old one. The key is only available in the account the devices were registered with.

## Run the app

```bash
python app.py               # http://127.0.0.1:5001
```

Run like this, it listens on localhost only. It shows your energy data and uses your API key, so don't expose it to the internet (see Docker below for LAN use).

## Run in Docker / on Unraid

Every push to `main` runs the tests, builds the image, smoke-tests it and publishes it to
`ghcr.io/mickcanning/myenergi_monitor:latest` (see `.github/workflows/docker.yml`).

**Test the image locally before pushing**

Needs Docker on the Mac (e.g. `brew install --cask orbstack`, then open OrbStack once).

```bash
scripts/docker-test.sh          # build, run the tests inside the image, smoke-test a container (same checks as CI)
scripts/docker-test.sh run      # ...then run it with your .env at http://localhost:5001 (Ctrl+C stops it)
PLATFORM=linux/amd64 scripts/docker-test.sh   # build the exact x86 image Unraid runs (slower on Apple silicon)
```

To try changes against your real history, load a snapshot of the Unraid database into the local test volume
(`scripts/load-snapshot.sh`; its header has the one-line snapshot command to run on Unraid). Copy the snapshot
to `instance/trial.db`, run the script, then `scripts/docker-test.sh run`. Run it again with a newer snapshot
to refresh. The previous local database is kept as `readings.db.bak` in the volume.

Data from `run` is kept in the Docker volume `myenergi-test-data` (`docker volume rm myenergi-test-data` clears it).

**Unraid**

1. Copy `unraid/myenergi-monitor.xml` to `/boot/config/plugins/dockerMan/templates-user/` on the server
   (e.g. via the `flash` share: `config/plugins/dockerMan/templates-user/`).
2. **Docker > Add Container**, choose the **myenergi-monitor** template, fill in the hub serial, API key
   and (optionally) Harvi names, then **Apply**.
3. Open `http://<unraid-ip>:5001/`.

Recorded readings (`readings.db`), uploaded logs, CT choices and the sign-in key are kept in
`/mnt/user/appdata/myenergi-monitor`, so they survive restarts and updates. Recording runs whenever the
container is up; restarts leave a gap in the data for the time it was down.
To update: **Docker > myenergi-monitor > Force update** (or let the Auto Update plugin do it).

**Any Docker host**

```bash
docker run -d --name myenergi-monitor --restart unless-stopped -p 5001:5001 \
  -v /path/to/appdata:/app/instance \
  -e MYENERGI_HUB_SERIAL=... -e MYENERGI_API_KEY=... -e HARVI_NAMES="serial=Grid,serial=Solar" \
  ghcr.io/mickcanning/myenergi_monitor:latest
```

The container listens on all interfaces so other devices on your LAN can reach it. Set `APP_PASSWORD` to
require a login. Over plain HTTP the password crosses your network unencrypted, so for anything beyond your
LAN put it behind HTTPS (below) or a VPN such as Tailscale.

### HTTPS through nginx

Let your existing nginx handle HTTPS and pass requests to the container over plain HTTP. Set
`BEHIND_PROXY=1` on the container (the Unraid template does) so the app sees the original `https` scheme.

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name energy.example.com;               # your domain

    ssl_certificate     /path/to/fullchain.pem;  # e.g. from Let's Encrypt
    ssl_certificate_key /path/to/privkey.pem;

    # Optional extra layer on top of APP_PASSWORD: only allow your LAN (adjust to your subnet).
    allow 192.168.0.0/16;
    deny  all;

    client_max_body_size 25m;                     # log uploads (nginx's default 1 MB would reject them)

    location / {
        proxy_pass http://UNRAID_IP:5001;         # or http://myenergi-monitor:5001 on a shared Docker network
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;
        proxy_set_header X-Forwarded-Port  $server_port;
        proxy_read_timeout 90s;                   # report charts can take a few seconds to draw
    }
}
```

Nginx Proxy Manager: add a Proxy Host (scheme `http`, forward to the Unraid IP and port 5001), request an
SSL certificate with Force SSL, attach an Access List that only allows your LAN, and under Advanced add
`client_max_body_size 25m;`.

## Record a log elsewhere (optional)

The app records on its own while it runs, so this is only for logging from another machine.


```bash
caffeinate -is python monitor.py --interval 15 --stale 300 --csv overnight.csv   # Ctrl+C to stop
```

`caffeinate -is` stops a Mac sleeping while the logger runs (keep it plugged in; a closed lid still sleeps unless you're in clamshell mode with an external display). Then upload `overnight.csv` on the **Harvi health** page.

## Reading the report

- The Harvi runs on power harvested from its CT clamp, so it transmits less often when little current flows. Long gaps with near-zero CT readings are usually normal.
- A long gap while several hundred watts or more flowed in the CT is more suspicious: that points at the radio link (distance, obstructions, channel, interference).
- If the Eddi's own report age stays low, the Eddi is online, so the gap isn't an internet or cloud problem.
- Report ages come from the cloud, which updates roughly every 20 s, so differences of a few seconds mean little.
- The "threshold line" is whatever you set. The point at which a Harvi gap causes "Grid CT Lost" isn't documented by myenergi that I could find, so treat any number as a guess until you've confirmed it.
- The power line holds the last reading between reports, so it stays flat while a Harvi is silent.

## Settings (`.env`)

| Variable | Meaning |
|---|---|
| `MYENERGI_HUB_SERIAL`, `MYENERGI_API_KEY` | API credentials (required for live values) |
| `HARVI_NAMES` | Friendly names, `serial=name,serial=name` |
| `GRID_HARVI_SNO` | Default grid Harvi in reports (otherwise a name containing "grid", otherwise the first) |
| `APP_PASSWORD` | If set, the app asks for this password (sessions last 30 days; changing it signs everyone out) |
| `BEHIND_PROXY` | `1` when served through a reverse proxy such as nginx (trusts its `X-Forwarded-*` headers) |
| `RECORD`, `POLL_SECONDS`, `RETENTION_DAYS` | Background recording on/off (default on), poll interval (default 15 s) and how many days of raw readings to keep (default 180; everything else is kept indefinitely) |
| `HISTORY_FROM` | How far back to backfill the hub's minute-by-minute history: a date (`2026-03-24`, e.g. when the car charger was installed) or a number of days (default 365) |
| `FUSIONSOLAR_USER`, `FUSIONSOLAR_PASSWORD`, `FUSIONSOLAR_SUBDOMAIN` | Optional Huawei FusionSolar login and web address subdomain (default `region01eu5`) for battery state of charge and solar |
| `REFRESH_SECONDS`, `PORT` | Live-page refresh interval (default 15) and port (default 5001) |

## Tests

```bash
pip install pytest
python -m pytest -q
```

Uploaded logs are stored in `instance/uploads/` and deleted after 7 days.
