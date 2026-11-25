# TRMNL 7.5" OG DIY Kit — self-hosted BYOS (LaraPaper) + custom weather

Self-hosted server for a TRMNL 7.5" (OG) DIY Kit, running as LaraPaper in a
Proxmox LXC, with a custom weather screen that blends Open-Meteo and the
US National Weather Service instead of depending on a third-party relay.

```
┌─────────────┐      WiFi       ┌───────────────────────────────┐
│ TRMNL device │ ───────────────▶│ LXC "terminus-trmnl-server"    │
│ (e-paper)    │   GET /api/...  │  ┌───────────┐  ┌────────────┐ │
└─────────────┘◀─────────────────│  │ app        │  │ weather    │ │
   image on       image           │  │ (LaraPaper)│◀─│ (blend.py) │ │
   screen         response        │  │ :4567      │  │ :8081      │ │
                                   │  └───────────┘  └─────┬──────┘ │
                                   └──────────────────────┼────────┘
                                                            │
                                              ┌─────────────┴─────────────┐
                                              │  Open-Meteo API (always)  │
                                              │  NWS api.weather.gov      │
                                              │  (blended in when up)     │
                                              └────────────────────────────┘
```

## What's in this repo

| Path | What it is |
|---|---|
| `docker-compose.yml` | LaraPaper + the weather-blending sidecar |
| `.env.example` | Template for the secrets/config `docker-compose.yml` reads |
| `weather/blend.py` | Fetches Open-Meteo + NWS, blends them, serves the result on :8081 |
| `recipes/weather.liquid` | The LaraPaper recipe template (weather screen + battery badge) |

Nothing here is TRMNL/LaraPaper-account-specific secrets other than the
`APP_KEY`, which you generate fresh (see below) — nothing to redact.

## 1. Host: Proxmox LXC

- Debian 12/13, **unprivileged**, with **Nesting** (and ideally **keyctl**)
  enabled under *Options → Features* so Docker runs inside it.
- 2 vCPU / 2–4 GB RAM / 16–20 GB disk is comfortable for LaraPaper + the
  weather sidecar.
- Give it a static LAN IP or a DHCP reservation — the device and the
  recipe both need to reach it by a fixed address.
- Proxmox host should be on **PVE 9.1+**. Earlier 8.x/9.0 releases have a
  known bug where Docker-in-LXC fails with:
  ```
  OCI runtime create failed ... open sysctl net.ipv4.ip_unprivileged_port_start
  file: reopen fd 8: permission denied
  ```
  Upgrading the host to 9.1+ fixes it outright. If you can't upgrade yet,
  add to `/etc/pve/lxc/<CTID>.conf` on the **host** and restart the
  container:
  ```
  lxc.apparmor.profile: unconfined
  lxc.mount.entry: /dev/null sys/module/apparmor/parameters/enabled none bind 0 0
  ```

Inside the LXC, install Docker:
```bash
apt update && apt install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sh
docker compose version   # sanity check
```

## 2. Deploy LaraPaper + the weather sidecar

```bash
git clone <this-repo-url> ~/larapaper   # or just copy these files there
cd ~/larapaper
cp .env.example .env
```

Edit `.env`:
```bash
echo "APP_KEY=base64:$(openssl rand -base64 32)" > .env
echo "APP_URL=http://<this-LXC-LAN-IP>:4567" >> .env
```

Start everything:
```bash
docker compose up -d
docker compose ps        # both app + weather should show healthy/running
```

Browse to `http://<LXC-IP>:4567` and register. **The first account
registered gets full access** — further signup stays closed unless you
set `MULTIUSER_REGISTRATION_ENABLED=1`.

### Why there's a `weather` sidecar at all

The straightforward approach — pointing a recipe straight at
`api.weather.gov` — breaks: NWS's hourly endpoint returns ~156 hours with
no way to request fewer, and LaraPaper's Liquid renderer rejects a payload
that large (`Context too large for external liquid renderer`).

`weather/blend.py` solves this by running as its own tiny container that:
1. Fetches Open-Meteo (small, always works, has feels-like/gusts/icons)
   and NWS (forecaster-corrected, no feels-like/gusts) every 30 minutes.
2. Averages temperature/rain-chance/wind-speed for the hours both cover.
3. Writes one small JSON file shaped like Open-Meteo's own response, so
   the Liquid template only ever needs to know one field layout.
4. Falls back to Open-Meteo alone if NWS fails, and keeps serving the
   last good file if *both* fail — the screen goes stale, never blank.
5. Retries every 5 minutes after a failure instead of waiting the full
   30-minute cycle.

It's exposed on **:8081** and reachable from the `app` container at
`http://weather:8081/weather.json` (Docker's built-in service-name DNS,
since both are in the same compose file).

## 3. Pair the physical device

1. The DIY kit's board (XIAO ESP32-S3) usually ships with TRMNL firmware
   already on it — no flashing needed for a genuinely new board. If it's
   been test-flashed/used before and won't broadcast a setup AP, hold its
   boot-equivalent button (KEY1 on this board) for ~7–8 seconds to force
   it back into pairing mode.
2. Power the board (USB-C from a real wall charger, not a USB hub —
   ESP32 WiFi draws current spikes a weak/hub-limited source can brown
   out on).
3. Connect your phone to the **TRMNL** WiFi network it broadcasts, open
   the captive portal (or browse to `http://4.3.2.1`), enter your home
   WiFi credentials.
4. On the same screen, choose **Custom Server** and enter
   `http://<LXC-IP>:4567`.
5. In LaraPaper, turn on **Permit Auto-Join** (header bar) *before* this
   step if the device doesn't appear on its own — it only works while
   there's a single registered user. Turn it back off once the device
   shows up under **Devices**.
6. **Turn off the device's own OTA firmware updates** in LaraPaper if it's
   already on firmware newer than what LaraPaper has cached — otherwise
   it can get stuck trying to push a downgrade, which most ESP32
   bootloaders reject (shows as a vague/blank "Firmware update failed"
   with nothing useful in the on-device log).

## 4. Add the weather recipe

In LaraPaper: **Plugins & Recipes → Markup** (or wherever "new recipe"
lives) → paste `recipes/weather.liquid` into the **Full** layout.

Recipe settings (these are **not** in the template file — set them in
LaraPaper's recipe editor):

| Field | Value |
|---|---|
| Method | `GET` |
| Polling URL | `http://weather:8081/weather.json` (fallback: `http://<LXC-IP>:8081/weather.json`) |
| Headers | none needed |
| Refresh | 30–60 minutes |

Then add the recipe to the device's playlist, and set the device's own
**Refresh Rate** (separate setting, in seconds) to however often you
want the physical screen to redraw — e.g. `3600` for hourly. Keep the
recipe's refresh ≤ the device's refresh rate, or the screen can show data
that's stale by roughly the sum of both intervals.

### Debugging a stale/stuck screen

Check each link in the chain, source-first:

```bash
# 1. Is the blender's file fresh?
docker compose logs --tail 10 weather
curl -s http://localhost:8081/weather.json | grep -o '"generated_at": *"[^"]*"'

# 2. Does LaraPaper see fresh data when forced to re-fetch?
#    (use the recipe's refresh/update-now action, then re-check its preview)

# 3. When did the device last check in?
#    (Devices page in the LaraPaper UI shows last check-in time)
```

A `handshake operation timed out` error in the weather container's logs
pointing at Open-Meteo is usually transient on Open-Meteo's end — confirm
with `curl` from a second machine/network before assuming your own LXC
or router is at fault. If only large HTTPS responses stall while small
ones succeed, suspect an MTU mismatch between the LXC's `eth0` and the
Proxmox host's bridge.

## 5. Recipe behavior notes

- **Battery badge** (top-right) reads `trmnl.device.percent_charged`,
  which both TRMNL's cloud and LaraPaper populate from the device's last
  check-in — it's a snapshot, not a live reading, and will sit near 100%
  whenever the device is on USB power (it's being charged).
- **Chart line** is drawn as straight segments with ±10° of padding above
  and below the actual values, specifically so a nearly-flat forecast
  reads as a nearly-flat line instead of looking artificially wavy.
- **Wind arrows** show the compass direction the wind is blowing *from*
  (standard meteorological convention), fed from
  `hourly.wind_direction_10m`.
- Fields with no NWS equivalent (feels-like, gusts, the weather icon)
  always come from Open-Meteo, even during hours where NWS data was
  successfully blended in for temperature/rain/wind-speed.

## 6. Keeping it running across reboots

- **LXC starts at Proxmox boot:** `pct set <CTID> --onboot 1` (or GUI:
  container → *Options → Start at boot*).
- **Docker starts at LXC boot:** `systemctl enable docker` (the
  `get.docker.com` install script usually does this already).
- **Containers restart with Docker:** already handled by
  `restart: unless-stopped` in `docker-compose.yml`.
- If this LXC depends on a Pi-hole container for DNS, give Pi-hole an
  earlier boot order than this one (*Options → Start/Shutdown order*) so
  outbound fetches on first boot don't fail before DNS is up — harmless
  either way since both the blender and LaraPaper retry on failure.

## Known limitations / things not carried over from the original design

- No severe-weather alerts (NWS has these; this build doesn't surface
  them — would need a separate `/alerts` poll).
- "Feels like" and "gusts" during any hour where only NWS data was
  available would be approximate (shifted from Open-Meteo's value) —
  in practice this only matters if Open-Meteo itself is down, since the
  blend always starts from Open-Meteo's numbers.
- The recipe assumes a 6-tile hourly strip (now + 5 hours) — if the
  device's refresh rate is set much longer than ~2 hours, several tiles
  will show times that have already passed by the time the screen next
  updates.
