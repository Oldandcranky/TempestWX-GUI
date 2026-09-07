# Tempest Weather Dashboard

A self-hosted weather dashboard for a [WeatherFlow Tempest](https://weatherflow.com/tempest-weather-system/)
station. It listens for the hub's UDP broadcasts on your own network — no cloud
account, no API key, no `pip install` — and serves six cards as a web page you
can open from anything, or leave up on a TV.

Built to run on a NAS. It is running on a Synology DS723+.

**Author:** Michael Walker VA3MW &nbsp;·&nbsp; Built with [Claude](https://claude.ai) (Anthropic)

![Python](https://img.shields.io/badge/Python-3.8%2B-blue) ![Version](https://img.shields.io/badge/Version-3.2.0-orange) ![License](https://img.shields.io/badge/License-MIT-green) ![Platform](https://img.shields.io/badge/Platform-Synology%20%7C%20Linux%20%7C%20macOS%20%7C%20Windows-lightgrey) ![Dependencies](https://img.shields.io/badge/Dependencies-none-brightgreen)

---

## Files

```
tempest_core.py      # measurements, meteorology, sun & moon, history, UDP listener
tempest_server.py    # the web server and the Open-Meteo forecast fetcher
web/index.html       # the dashboard — one file, no build step, no CDN
web/fonts/           # Weather Icons (SIL OFL 1.1), vendored
Dockerfile           # for Synology Container Manager
docker-compose.yml
```

---

---

## Quick start

```bash
python3 tempest_server.py --lat 42.1681 --lon -88.4281
```

Open `http://<that-host>:8444/` from anything on your network — the server
prints the URLs it can be reached on. Add `?tv` for the big-screen layout:
larger type, no cursor, no controls.

To see it before a hub is involved:

```bash
python3 tempest_server.py --demo
```

Demo mode synthesises a plausible day — warmest mid-afternoon, sun following
the clock — so the whole dashboard has something to show with no hardware.

---

## Hosting it on a Synology NAS

This is the setup the project is aimed at: the NAS runs the dashboard around
the clock, and a TV or tablet just points a browser at it.

### The one thing that will bite you

The Tempest hub **broadcasts** over UDP. Broadcasts do not cross Docker's
bridge network, and they do not cross subnets or VLANs. So:

- the container **must** use `network_mode: host`, and
- the NAS **must** be on the same subnet/VLAN as the hub.

A bridged container starts cleanly and then sits at "no hub seen yet" forever.
If that happens, this is why. The page shows a red banner when nothing has
arrived, so the failure is visible rather than silent.

### Deploying over SSH (what this repo was actually deployed with)

Enable **Control Panel → Terminal & SNMP → SSH**, then, from your workstation:

```bash
# 1. authorise your key (you will be asked for the DSM password once)
ssh-copy-id -i ~/.ssh/id_ed25519.pub YOURUSER@YOUR-NAS
ssh YOURUSER@YOUR-NAS 'chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys; chmod 755 ~'

# 2. ship the files. DSM restricts SFTP, so stream a tar over ssh
#    rather than using scp
tar czf - tempest_core.py tempest_server.py web Dockerfile \
    docker-compose.yml .dockerignore \
  | ssh YOURUSER@YOUR-NAS 'mkdir -p /volume1/docker/tempest && \
      tar xzf - -C /volume1/docker/tempest'

# 3. tell the container which host user owns ./data
ssh YOURUSER@YOUR-NAS 'cd /volume1/docker/tempest && mkdir -p data && \
  printf "PUID=%s\nPGID=%s\n" "$(id -u)" "$(id -g)" > .env'

# 4. build and start
ssh YOURUSER@YOUR-NAS 'cd /volume1/docker/tempest && \
  /usr/local/bin/docker compose up -d --build'
```

Your DSM account must be in the **administrators** group (for SSH) and the
**docker** group (so `docker` works without sudo — Container Manager adds
administrators to it). Check with `id`.

Three DSM quirks that will trip you up:

- `/tmp` and SFTP are locked down, hence the `tar | ssh` above instead of `scp`.
- `docker` is not on the default `PATH` for a non-login shell. Use the full path
  `/usr/local/bin/docker`.
- The bind-mounted `data/` directory belongs to your DSM user, so the container
  has to run as that uid — that is what `.env` is for. Without it the container
  starts and then fails to write its history.

### Container Manager UI instead

1. Copy the folder to `/volume1/docker/tempest` (File Station is fine).
2. Edit `docker-compose.yml` — set `TEMPEST_LAT`, `TEMPEST_LON`, `TZ`, and the
   port. Create a `.env` next to it with your `PUID`/`PGID`.
3. **Container Manager → Project → Create**, point it at that folder, build.
4. If DSM's firewall is on, allow inbound TCP on your port and UDP 50222 in
   **Control Panel → Security → Firewall**.
5. Open `http://<nas-ip>:8444/`.

### Without Docker

Install the **Python 3** package from Package Center, then add a
**Control Panel → Task Scheduler → Triggered Task → User-defined script**, set
to run at boot as `root` (binding UDP 50222 needs it on some DSM versions):

```bash
cd /volume1/docker/tempest && /usr/local/bin/python3 tempest_server.py   --lat 45.4215 --lon -75.6972 --http-port 8444 --data-dir /volume1/docker/tempest/data
```

### Putting it on the TV

Any TV browser that can open a URL will do. Use the `?tv` layout and, if your
TV or streaming stick supports it, disable the screen saver for that app. On a
spare tablet or Fire tablet, a kiosk-browser app pointed at
`http://<nas-ip>:8444/?tv` works well.

> **No authentication.** Anyone who can reach the port can see your weather.
> That is fine on a home LAN — do not port-forward it to the internet.

---

## Server options

Every flag has an environment-variable equivalent, which is what
`docker-compose.yml` uses.

| Flag | Env | Default | Meaning |
|---|---|---|---|
| `--host` | `TEMPEST_HOST` | `0.0.0.0` | Address to bind the web server to |
| `--http-port` | `TEMPEST_HTTP_PORT` | `8444` | Web server port |
| `--udp-port` | `TEMPEST_UDP_PORT` | `50222` | Hub broadcast port |
| `--lat` / `--lon` | `TEMPEST_LAT` / `TEMPEST_LON` | unset | Station location, for sun times |
| `--name` | `TEMPEST_NAME` | hub serial | Label in the footer |
| `--data-dir` | `TEMPEST_DATA_DIR` | next to the script | Where history is written |
| `--demo` | `TEMPEST_DEMO` | off | Synthetic weather, no hub |
| `--temp-unit` etc. | `TEMPEST_TEMP_UNIT` etc. | °F, mph, inHg, in, mi | Starting units |
| `--verbose` | — | off | Log every HTTP request |

Units are a *starting* choice: each viewer can click any big number to cycle
units, and the browser remembers their preference. `t` toggles light/dark, `f`
toggles the TV layout.

### Endpoints

| Path | Returns |
|---|---|
| `/` | The dashboard |
| `/api/state` | The whole state as JSON, in canonical units (°C, mb, m/s, mm, km) |
| `/api/wind` | Just the live wind — 99 bytes, polled often so the compass stays smooth |
| `/healthz` | `{"ok": true, "health": "live"}` — handy for DSM or uptime checks |

`/api/state` is a stable, documented shape — if you want to feed this into Home
Assistant, Grafana, or a script of your own, poll that.

---

## Features

- **Live UDP data** — receives broadcasts on your local network (port 50222).
  No cloud account, no API key, no `pip install`.
- **Six cards, drawn on a canvas** — each one is a rounded panel with a hero
  number, inset stat rows, and a plain-English summary line:

  | Card | Shows |
  |---|---|
  | **Temperature** | Current temp, 24-hour min/max with the times they happened, 24-hour change, hourly trend, feels-like, humidity, dew point, and a comfort read ("WARM · MUGGY") |
  | **Wind** | Average and gust with the day's peak, a live rapid-wind hero, a linear compass ribbon centred on the current bearing, Beaufort force, 24-hour wind run and 3-hour steadiness |
  | **Pressure** | Half-dial gauge scaled to the 24-hour range, hourly rate of change, barometric tendency, 24-hour min/max/span, and an outlook line |
  | **Rainfall** | Current rate, today and yesterday, month and year to date |
  | **Sun & sky** | Sunrise, sunset, time until the next one, a daylight arc with the sun's position, UV index with WHO band, brightness, solar radiation, and moon phase |
  | **Lightning** | Strikes today, nearest strike, last strike time and distance, last hour and last 3 hours |

- **Connection health** — a dot on every card and in the footer turns green when
  packets are flowing, amber after 2 minutes of silence, red after 6.
- **Click to cycle units** — click any hero number:
  - Temperature: °F → °C → K
  - Wind: mph → km/h → m/s → kts
  - Pressure: inHg → hPa → kPa → mmHg
  - Rain: in → mm
  Distance (mi / km) is in the settings menu, along with everything else.
- **Trend history** — a rolling 48 hours is kept on disk, so the min/max,
  trends and totals are populated the moment you relaunch.
- **Sun times computed locally** — set your latitude and longitude once
  (☰ → *Set location*). The maths runs on your machine; nothing is sent
  anywhere.
- **Light and dark themes** — press `t`.
- **Mini bar** — press `m` to collapse to a 360×46 always-on-top strip showing
  temperature, wind, rain and strikes. Drag it by the grip; click it to expand.
- **Persistent settings** — units, theme, location, window position and the
  day's totals are saved to `tempest_settings.json`.
- **Desktop launcher** — one click creates a `.lnk` (Windows), `.command`
  (macOS) or `.desktop` (Linux) shortcut.

---

## Requirements

- Python 3.8 or later
- **Standard library only** — nothing to install
- A Tempest hub on the same LAN subnet as whatever runs this
- No GUI toolkit, so it runs happily headless on a NAS or server
- Optional: the [Inter](https://rsms.me/inter/) font. Without it the dashboard
  falls back to the platform UI font.

---

## Installation

```bash
git clone https://github.com/Oldandcranky/TempestWX-GUI.git
cd TempestWX-GUI
python3 tempest_server.py --demo
```

### Keyboard and mouse

| Action | Effect |
|---|---|
| click a big number | Cycle that measurement's units (remembered per browser) |
| `t` | Toggle light / dark |
| `f` | Toggle the TV layout |

---

## How it works

The hub broadcasts JSON over **UDP port 50222** to every device on the local
network. The app binds that port on a background thread and hands each packet to
the Tk thread via `after()`, so all drawing stays on the main thread.

| Message type | Description |
|---|---|
| `obs_st` | Full Tempest observation — every sensor, ~1 min interval |
| `obs_air` | AIR module — temperature, humidity, pressure, lightning |
| `obs_sky` | SKY module — wind, rain, UV, solar radiation |
| `rapid_wind` | Wind speed and direction, ~3 s interval |
| `evt_strike` | Lightning strike — distance and energy |
| `evt_precip` | Rain start event |
| `device_status` / `hub_status` | Battery voltage |

Full protocol reference: [Tempest UDP Broadcast API](https://apidocs.tempestwx.com/reference/tempest-udp-broadcast)

### A note on rain and strike totals

`obs_st` reports rain accumulated over the *previous minute*, not a running
total, and a strike count for the reporting interval. The app accumulates those
itself into per-day records in `tempest_history.json`. So "month" and "year"
totals mean *since you started running this app*, not since the station was
installed.

---

## Files it writes

All live in `--data-dir` and are safe to delete — they get recreated.

| File | Contents |
|---|---|
| `tempest_history.json` | 48 hours of samples, and per-day rainfall |
| `tempest_server_state.json` | Today's strike count and the last observation, so a restart comes back populated |
| `tempest_forecast.json` | The last forecast, so a restart does not re-hit the free API |

Each viewer's unit and theme choices live in that browser's `localStorage`, not
on the server, so the TV and your phone can differ.

---

## Changelog

### v3.2.1
- **Weather map.** The map button in the footer (or `w`) hides the cards and
  shows live radar for the station's location, animated with the native
  [View Transitions API](https://developer.mozilla.org/docs/Web/API/View_Transitions_API)
  — no library. `Esc` closes it. The embed is only created the first time it
  is opened, and it is the one part of the dashboard that needs internet on
  the *viewing* device; everything else is served by the NAS.
- **Smoother compass.** The 193-tick strip is built once and reused instead of
  being rebuilt on every two-second render, which was the real source of the
  stutter. The spring was softened so the glide spans the gap between the
  hub's ~3 s wind packets, gust-to-gust jitter is damped before it reaches the
  dial, the strip composites on the GPU, and the speed readout eases too.
- **Cards can no longer clip their own text.** Type is now capped by card
  width as well as height, the gauge and sun arc may shrink, and a fit pass
  measures each card after layout and trims the type until the content
  genuinely clears — so a platform whose fonts run taller cannot slice a
  caption in half.

### v3.2.0
- **Forecast card replaces the lightning card**, fed by
  [Open-Meteo](https://open-meteo.com/) — free, no account, no key. It shows
  the current condition, today's high/low and precipitation chance, and a
  three-day strip; ten days are fetched so a fuller view can use them later.
  This is the **only** outbound network call the dashboard makes, it is
  optional (`--no-forecast`), it fails soft, and it sends nothing but a coarse
  latitude and longitude.
- The forecast is cached to disk and reused for its lifetime, so restarting the
  container does not cost the free API a request. A failed fetch now retries
  after 15 s and backs off, instead of blanking the card for two minutes.
- **The desktop tkinter app has been removed.** This is a hosted dashboard now;
  `tempest_core.py` and `tempest_server.py` are the whole thing.
- **The wind compass moves properly.** Graduations every 5.625° (four per
  compass point) instead of 11.25°, a critically damped spring instead of a
  linear ease, and a 99-byte `/api/wind` endpoint polled every 0.9 s so the
  dial tracks the station rather than the snapshot poll.

### v3.1.3
- The last observation is now saved beside the history and restored at
  startup, so a container restart or NAS reboot no longer blanks temperature,
  pressure and UV for up to a minute. Readings older than an hour are ignored,
  and `last_packet` is never restored — the freshness dot still reflects real
  packets only.

### v3.1.2
- **Weather Icons** vendored locally (SIL OFL 1.1) and used for the card
  headers, sunrise/sunset, moonrise/moonset, Beaufort force, and the Moon —
  the font carries the full 28-glyph lunation, so the phase shown is picked
  from the Moon's real age rather than approximated.
- **The wind compass now slides.** The tick strip spans three full turns and
  eases under a fixed centre marker, taking the shortest way around north
  (350° → 10° moves +20°, not -340°).
- Card type now scales with card height (container queries), matching the
  reference's scale — previously it scaled with viewport width and read far
  too small.
- Fixed: the Sun was pinned to the left edge of its arc (a 0–1 fraction was
  divided by 100 a second time); the footer blew up because it sits outside
  every card, where container units have no container.
- The server serves vendored assets from `web/` under an allow-list of file
  types, confined to that directory.

### v3.1.1
- **Astronomy card** now matches the reference: a Moon arc alongside the Sun
  arc, moonrise / moonset / next rise, a Moon glyph drawn from the real
  illuminated fraction, and the next new and full Moon dates. All computed
  locally (Meeus low-precision lunar theory) — still no network call.
- **Wind card** gained a tree that leans and sways with the measured wind, plus
  the hourly average direction, the hourly range, and steadiness over both
  today and the last 3 hours.
- Rainfall gained a rate dial in its header and a droplet in the RAW chip; the
  UV index gained its coloured band chip; the barometer gained quarter ticks.
- Units are no longer printed next to a "—" placeholder.

### v3.1.0
- **New: a web dashboard** — `tempest_server.py` serves the same six cards as a
  page on your LAN, for hosting on a NAS and putting on a TV. `?tv` gives a
  big-screen layout; `/api/state` exposes everything as JSON.
- **New: Docker packaging** for Synology Container Manager, with the host
  networking requirement documented (UDP broadcasts don't cross a bridge).
- Split the shared logic into `tempest_core.py`, so the desktop app and the web
  server compute identical numbers from one implementation.
- Viewer-local units and theme in the browser; per-device, not per-server.

### v3.0.0
- **Rebuilt as a six-card dashboard.** Every card is drawn on a tkinter
  `Canvas`: rounded panels, inset stat rows, a compass ribbon, a pressure
  half-dial, and a daylight arc. Type sizes scale with card height, so the
  layout holds from 1000 px wide up to a full screen.
- Visual design follows the [Halcyon WX](https://halcyonwx.com) dashboard's
  layout and colour system.
- **New data**: 24-hour min/max with timestamps, hourly and 24-hour trends,
  barometric tendency and outlook, wind run, wind steadiness, daily/monthly/
  yearly rainfall, per-strike lightning history, brightness (lux), comfort
  summary.
- **New: sun & moon**, computed locally from your latitude and longitude — no
  network call.
- **New: `--demo` mode**, `--port`, `--version`.
- Connection health indicator; stale and offline states are now visible.
- 48-hour history persisted to disk.
- Rain now has its own unit setting (it used to follow the temperature unit).
- Feels-like now applies wind chill as well as heat index.
- Temperature *differences* now convert correctly (no more adding 32 to a delta).
- Light theme; always-on-top; settings menu; keyboard shortcuts.
- Clean shutdown — the UDP socket and listener thread are closed on exit.

### v1.2.0
- Cross-platform: Windows, macOS, and Debian/Ubuntu Linux
- Bottom whitespace fixed; window position restored on launch
- Version bump to 1.2.0

### v1.1.0
- Compact layout; value and unit inline; Tempest Map hotlink

### v1.0.0
- Initial release

---

## Credits

**Windy** — the radar map embed, used under their free embed. It loads only
when you open the map. <https://www.windy.com/>


**Open-Meteo** — the forecast feed. Free for non-commercial use, no API key;
weather data licensed CC BY 4.0. <https://open-meteo.com/>


**Weather Icons** by Erik Flowers — the thermometer, barometer, wind, raindrop,
lightning, sunrise/sunset, moonrise/moonset, Beaufort-force and 28-phase Moon
glyphs. Licensed under the SIL Open Font License 1.1; the font is vendored
unmodified in `web/fonts/` with its licence in
`web/fonts/LICENSE-weather-icons.txt`, so the dashboard needs no CDN on a
private network. <https://github.com/erikflowers/weather-icons>


Visual design adapted from the [Halcyon WX](https://halcyonwx.com) dashboard —
its card layout, type scale and colour tokens. Halcyon is a separate product;
this project is not affiliated with it.

## License

MIT — see the licence header at the top of `tempest_core.py`.
