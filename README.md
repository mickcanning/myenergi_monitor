# myenergi monitor

A small local Flask app for investigating Harvi link problems on a myenergi system.

- **Live values** (`/`): the latest readings for every device on your hub (Eddi, Zappi, Libbi, Harvi), refreshed automatically, with how old each device's last report is.
- **Log report** (`/upload`): upload a CSV written by `monitor.py` and get the report chart and tables: Harvi report age, CT power, silences over a threshold, and "gaps of concern" (long silences while current was flowing, so low harvested power doesn't explain them).
- **`monitor.py`**: a command-line logger that polls the API and writes the CSV the report reads.

The myenergi API used here is the community-documented, unofficial one, so field names and units may differ by firmware.

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

**Unraid**

1. Copy `unraid/myenergi-monitor.xml` to `/boot/config/plugins/dockerMan/templates-user/` on the server
   (e.g. via the `flash` share: `config/plugins/dockerMan/templates-user/`).
2. **Docker > Add Container**, choose the **myenergi-monitor** template, fill in the hub serial, API key
   and (optionally) Harvi names, then **Apply**.
3. Open `http://<unraid-ip>:5001/`.

Data (uploaded logs, the current report, CT choices) is kept in `/mnt/user/appdata/myenergi-monitor`.
The live CT chart history is in memory and starts again when the container restarts.
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

## Record a log

```bash
caffeinate -is python monitor.py --interval 15 --stale 300 --csv overnight.csv   # Ctrl+C to stop
```

`caffeinate -is` stops a Mac sleeping while the logger runs (keep it plugged in; a closed lid still sleeps unless you're in clamshell mode with an external display). Then upload `overnight.csv` on the **Log report** page.

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
| `REFRESH_SECONDS`, `PORT` | Live-page refresh interval (default 15) and port (default 5001) |

## Tests

```bash
pip install pytest
python -m pytest -q
```

Uploaded logs are stored in `instance/uploads/` and deleted after 7 days.
