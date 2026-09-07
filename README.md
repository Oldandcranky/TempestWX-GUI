# Tempest Weather Dashboard

A self-hosted dashboard for a [WeatherFlow Tempest](https://weatherflow.com/tempest-weather-system/)
station. It listens for the hub's UDP broadcasts on your own network and
serves a web page you can open from anything — or leave up on a TV.

No account, no API key and no `pip install` for the station data itself. It is
Python standard library throughout, and the page has no build step and pulls
nothing from a CDN.

Built to run on a NAS. It runs on a Synology DS723+.

**Author:** Michael Walker VA3MW &nbsp;·&nbsp; Built with [Claude](https://claude.ai) (Anthropic)

![Python](https://img.shields.io/badge/Python-3.8%2B-blue) ![Version](https://img.shields.io/badge/Version-3.3.0-orange) ![License](https://img.shields.io/badge/License-MIT-green) ![Dependencies](https://img.shields.io/badge/Dependencies-none-brightgreen)

---

## Quick start

```bash
python3 tempest_server.py --lat 42.1681 --lon -88.4281
```

Open `http://<that-host>:8444/`. The server prints the addresses it can be
reached on when it starts.

No hub to hand? `--demo` synthesises a plausible day — warmest mid-afternoon,
sun following the clock — so the whole dashboard has something to show.

```
tempest_core.py      measurements, meteorology, sun and moon, history, UDP listener
tempest_server.py    the web server, and the forecast, alert and backfill fetchers
web/index.html       the dashboard — one file, no build step, no CDN
web/fonts/           Weather Icons (SIL OFL 1.1), vendored
Dockerfile           for Synology Container Manager
docker-compose.yml
```

---

## What is on it

Nine cards. Pick which ones you want and drag them into the order you like —
the grid rearranges itself for however many you choose.

| Card | Shows |
|---|---|
| **Temperature** | Now, 24-hour min and max with the times they happened, 24-hour change, hourly trend, feels-like, humidity, dew point, and a comfort read |
| **Wind** | Average and gust with the day's peak, a live rapid-wind hero, a compass strip that slides under a fixed marker, Beaufort force, 24-hour wind run and steadiness |
| **Pressure** | A dial scaled to the 24-hour range, hourly rate of change, barometric tendency, min/max/span, and an outlook line |
| **Rainfall** | Current rate, today and yesterday, month and year to date |
| **Astronomy** | Sunrise, sunset, time to the next, a daylight arc with the sun on it, UV with its WHO band, brightness, solar radiation, moonrise, moonset, phase and the next new and full moon |
| **Forecast** | Current conditions, today's high, low and precipitation chance, and a three-day strip |
| **Records** | Hottest and coldest with dates, for the month, the year and all time, over a band showing the station's whole range |
| **Lightning** | Strikes today, nearest, last strike, last hour and last three hours |
| **Radar** | Live radar for your location |

Three more pages, each behind an icon in the footer:

- **Ten-day outlook** (`d`) — the full forecast, each day's range drawn against
  the ten-day span so a warm spell or a cold snap reads at a glance.
- **Radar** (`w`) — full-screen weather map.
- **Settings** (`s`) — choose and reorder cards, and set the optional
  WeatherFlow token.

Switching between them uses the browser's native View Transitions, so there is
no animation library involved.

---

## Hosting it on a Synology NAS

### The one thing that will bite you

The hub **broadcasts** over UDP. Broadcasts do not cross Docker's bridge
network, and they do not cross subnets or VLANs. So the container must use
`network_mode: host`, and the NAS must be on the same subnet as the hub.

A bridged container starts cleanly and then sits at "no hub seen yet" forever.
The page says so after a minute rather than leaving you guessing.

### Deploying over SSH

Enable **Control Panel → Terminal & SNMP → SSH**, then:

```bash
# 1. authorise your key (you are asked for the DSM password once)
ssh-copy-id -i ~/.ssh/id_ed25519.pub YOURUSER@YOUR-NAS
ssh YOURUSER@YOUR-NAS 'chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys; chmod 755 ~'

# 2. ship the files. DSM restricts SFTP, so stream a tar over ssh
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

Your DSM account needs to be in **administrators** (for SSH) and **docker** (so
`docker` works without sudo — Container Manager adds administrators to it).
Check with `id`.

Three DSM quirks worth knowing:

- `/tmp` and SFTP are locked down, hence `tar | ssh` rather than `scp`.
- `docker` is not on the `PATH` of a non-login shell; use `/usr/local/bin/docker`.
- The bind-mounted `data/` belongs to your DSM user, so the container has to
  run as that uid. That is what `.env` is for — without it the container starts
  and then cannot write its history.

### Container Manager instead

Copy the folder to `/volume1/docker/tempest`, edit `docker-compose.yml`, create
the `.env`, then **Container Manager → Project → Create**. If DSM's firewall is
on, allow inbound TCP on your port and UDP 50222.

---

## Configuring it

Most things can be set two ways: on the server, or from the settings page. The
settings page wins, because it is written to `tempest_config.json` at runtime.

| Flag | Environment | Default | Meaning |
|---|---|---|---|
| `--host` | `TEMPEST_HOST` | `0.0.0.0` | Address to bind to |
| `--http-port` | `TEMPEST_HTTP_PORT` | `8444` | Web server port |
| `--udp-port` | `TEMPEST_UDP_PORT` | `50222` | Hub broadcast port |
| `--lat` / `--lon` | `TEMPEST_LAT` / `TEMPEST_LON` | unset | Station location, for sun, moon, forecast, alerts and radar |
| `--name` | `TEMPEST_NAME` | station serial | Label in the footer |
| `--slots` | `TEMPEST_SLOTS` | six cards | Which cards, in order |
| `--wf-token` | `TEMPEST_WF_TOKEN` | unset | WeatherFlow token for history backfill |
| `--data-dir` | `TEMPEST_DATA_DIR` | beside the script | Where history is written |
| `--demo` | `TEMPEST_DEMO` | off | Synthetic weather, no hub |
| `--no-forecast` | `TEMPEST_NO_FORECAST` | on | Disable the forecast fetch |
| `--no-alerts` | `TEMPEST_NO_ALERTS` | on | Disable weather alerts |

> Prefer the settings page for the token. `docker-compose.yml` is committed to
> git; `tempest_config.json` is gitignored and written `0600`.

### Units and keys

Units are a *starting* choice — click any big number to cycle it, and that
browser remembers. Temperature, wind, pressure, rain and distance are all
independent.

| Key | Action |
|---|---|
| `d` | Ten-day outlook |
| `w` | Radar |
| `s` | Settings |
| `t` | Light / dark |
| `f` | TV layout |
| `Esc` | Back to the cards |

---

## Where the data comes from

**Your hub, over UDP.** Everything on the Temperature, Wind, Pressure,
Rainfall and Lightning cards is measured by your own station. Nothing leaves
your network for any of it.

**Computed locally.** Sunrise, sunset, moonrise, moonset, moon phase and the
next new and full moon are worked out on the machine from your latitude and
longitude — no network call. Dew point, heat index, wind chill, Beaufort
force, wind run and barometric tendency likewise.

**Open-Meteo**, for the forecast. Free, no account, no key. It is cached to
disk and reused, so restarting costs nothing, and `--no-forecast` turns it off.

**The US National Weather Service**, for active alerts. No key. Outside NWS
coverage it simply returns nothing.

**WeatherFlow**, optionally, to backfill history. The hub only broadcasts what
is happening now, so without this the 24-hour figures and the monthly and
yearly totals only count from when the dashboard started. Give it a personal
access token and it fills in your station's real record — daily rainfall and
daily highs and lows, back to the day the station was installed.

### Endpoints

| Path | Returns |
|---|---|
| `/api/state` | Everything, in canonical units (°C, mb, m/s, mm, km) |
| `/api/wind` | Just the live wind — a few dozen bytes, polled often |
| `/api/config` | The card layout, and whether a token is set (never the token) |
| `/healthz` | `{"ok": true, "health": "live"}` |

`/api/state` is a stable shape. If you want to feed this into Home Assistant,
Grafana or a script of your own, poll that.

---

## Staying up

This is meant to sit on a screen nobody touches, so it looks after itself.

- If the hub listener thread dies, the server notices within thirty seconds
  and rebuilds it.
- If the page loses contact for four minutes it reloads, and it refreshes once
  a day when healthy. Reloads are rate-limited across page loads, so a server
  that is genuinely down cannot cause a loop.
- The last observation is saved and restored, so a restart comes back
  populated rather than blank for a minute. Freshness still reflects real
  packets only — the dot never lies about how old the data is.
- The container is `restart: unless-stopped` with a healthcheck.

## On a TV

Add `?tv` for the big-screen layout: larger type throughout, no cursor, no
controls. `f` toggles it.

The whole page drifts through a few pixels on a slow cycle, and TV mode pulls
peak brightness back slightly. Both reduce the risk of burning a static layout
into an OLED. It reduces the risk rather than removing it — a sleep schedule on
the TV itself still does more.

---

## Requirements

- Python 3.8 or later, standard library only
- A Tempest hub on the same LAN subnet
- No GUI toolkit, so it runs headless on a NAS or server
- Optional: the [Inter](https://rsms.me/inter/) font; without it the page falls
  back to the platform UI font

## Files it writes

All live in `--data-dir` and are safe to delete — they are rebuilt.

| File | Contents |
|---|---|
| `tempest_history.json` | 48 hours of samples, plus per-day rainfall and per-day temperature extremes |
| `tempest_config.json` | Card layout and the WeatherFlow token (`0600`) |
| `tempest_server_state.json` | Today's strike count and the last observation |
| `tempest_forecast.json` | The last forecast, so a restart does not re-hit the API |

Each viewer's unit and theme choices live in that browser, not on the server,
so the TV and your phone can differ.

## Security

There is **no authentication**. Anyone who can reach the port can view the
dashboard and change the card layout. Keep it on your own network; do not
forward the port to the internet.

The WeatherFlow token is write-only: stored `0600` and never included in any
response, so a browser can set or clear it but never read it back. Settings
writes require a header that a cross-site form cannot set, and the Origin is
checked against the Host.

---

## Changelog

### v3.3.0
- Watchdogs on both sides: the server rebuilds the hub listener if its thread
  dies, and the page reloads after sustained loss of contact or a day of
  uptime. Fixes a listener that, on dying, also stopped the forecast, backfill
  and housekeeping threads.
- Records card, and the history sweep now walks back to the station's install
  date rather than 1 January.
- No limit on how many cards you display; the grid arranges itself.
- The station serial no longer flips to the hub's when a hub status message
  arrives.



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
