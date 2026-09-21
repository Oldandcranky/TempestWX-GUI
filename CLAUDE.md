# Working on this repo

`README.md` explains what the dashboard is and how the pieces fit. This file
is the operational half: the things a session would otherwise get wrong, or
have to rediscover by breaking something.

## Deploying

`./deploy.sh` ships this checkout to the NAS and rebuilds the container.

**It has to run from a machine on the LAN with an authorised SSH key — the
owner's Mac.** A Claude Code session in Anthropic's cloud has no route to
`10.0.0.101`, no `ssh` binary and an empty `~/.ssh`; private addresses are in
`no_proxy`, so they are attempted directly and fail. Do not offer to deploy.
Give the command as a paste-able block instead:

```bash
cd ~/Documents/GitHub/tempest-weather-monitor
git pull && ./deploy.sh
```

It **ships the working tree**, not `main`, so `git pull` first is what keeps
the wall display and the repo in step.

It never touches `docker-compose.yml`, `.env` or `data/` on the NAS. Those
hold local config, tokens and history. Anything that needs a new environment
variable therefore needs a one-off hand edit of the compose file there, and
saying so is part of finishing the change.

Extraction overwrites; it does not sync. A file deleted from the repo lives
on at `/volume1/docker/tempest` until someone removes it.

## This station

| | |
|---|---|
| NAS | `synologyadmin@10.0.0.101`, DS723+, app at `/volume1/docker/tempest` |
| Dashboard | `http://10.0.0.101:8444/` (`?tv` for the TV layout) |
| Location | 42.1681, −88.4281 — northern Illinois, `America/Chicago` |
| Hub | Tempest `ST-00209255`, UDP broadcast on 50222 |
| Installed | **May 2026.** The rain record starts `2026-05-23` and cannot be backfilled further — the station did not exist before it. |

DSM quirks that have already cost time: `/tmp` and SFTP are locked down, so
files go over as `tar | ssh`; `docker` is not on a non-login shell's `PATH`,
so it is `/usr/local/bin/docker`; and the image chowns `/app` to uid 10001
while compose runs the container as the DSM user, so shipped files must be
world-readable or the server reports `Missing index.html`.

## Config that is not in this repo

`docker-compose.yml` here is a template. The NAS's copy also sets, and the
repo's does not:

    TEMPEST_PLAN_DOWN / TEMPEST_PLAN_UP     plan rates for the Internet gauges
    TEMPEST_RAIN_MONTHLY                    twelve normal monthly rainfalls, inches
    TEMPEST_TEMP_NORMAL_HIGH / _LOW         twelve normal highs and lows, Fahrenheit

Secrets — the WeatherFlow token, the Google Pollen key, the Speedtest Tracker
token — are entered on the dashboard's settings page and stored server-side in
`data/tempest_config.json`. They are never sent to a browser and never belong
in the compose file.

The rain and temperature normals are NOAA's 1991-2020 monthly normals for
**Crystal Lake 4NW (USC00112048)**, 6.6 miles from the station, fetched with
`tools/normals.py` on 2026-09-19. Re-run it if the station moves; do not
guess them.

## Working on the page

`web/index.html` is the whole front end: no build step, no CDN, fonts vendored
in `web/fonts/`. Edit it directly.

- **`render()` calls `grid.replaceChildren()` every two seconds.** Any UI state
  that must survive — a flipped card, a cached measurement — lives in a
  module-level variable, never in the DOM.
- **The `--fit` pass** (`fitCards()`) measures each card and shrinks its type
  until the content fits. It checks *both* axes; it did not always, and a word
  running off the side went unnoticed until a screenshot caught it.
- **`.card` has `container-type: size`**, which flattens 3-D transform contexts
  in some browsers. A card flip built on `rotateY` + `backface-visibility`
  worked headless and painted both faces on the real display. The flip is 2-D
  now. Be wary of anything needing a preserved 3-D context.
- **Give a card's spare room to flex**, do not reason about it. The Pollen face
  overlapped its own caption because the space was calculated rather than
  handed over. The fix, used since by the Temperature trace, is a flex child
  with a `max-height`: it takes what is going spare and overlap is impossible.
- **`.dockerignore` patterns match the whole path from the context root**, so
  `._*` catches the top level only and `**/._*` is needed for nested files.

## The daily record

`History.days` holds one summary per day — gust, pressure range, strikes,
sunshine — beside the older `rain_days` and `temp_days`. The live feed and
the WeatherFlow sweep both write it through `History.fold`, and
`merge_day` reconciles them; it must stay idempotent, because the sweep
redoes the last week every run.

**If the sweep learns to keep a new field, raise `Backfill.SWEEP_KEEPS`.**
That is what makes an already-swept archive get walked again; without it the
new field only ever starts from the day it was deployed.

The almanac's date arithmetic lives in `History.almanac`, takes `today=`, and
is unit-tested against made-up years. Keep it there rather than in the page.
The page supplies only the normals and the reader's round numbers.

## Tests

`tests/run.sh` — unit tests plus a headless-browser geometry pass. Run it
before pushing anything that touches the page or the card logic; it takes
about twenty seconds and it has already caught a real overflow.

`tests/state.json` is a captured dashboard state, deliberately hostile: the
longest ISP name, the widest pollen word, every card switched on. The
geometry pass renders against it rather than against live demo data, whose
numbers drift with the clock and made the suite answer differently on
identical code. If you add a card or a field, recapture it.

## Testing the page by hand

There is a headless Chromium here, and using it is the difference between
shipping a bug and catching one. Two of the three UI bugs that reached the
wall display would have been caught by measuring instead of reasoning.

```js
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const b = await chromium.launch({ args: ['--no-sandbox'] });
```

Run the real server alongside it — `python3 tempest_server.py --demo` — and:

- **Intercept `/api/state`** with `page.route()` to inject a state rather than
  trying to make a fetcher fail. It exercises the real render loop.
- **Seed `tempest_history.json`** to fake a record: rain days, temperature
  days, sample series. That is how a year of rainfall or a fresh install gets
  tested.
- **Use `page.$eval(sel, el => el.click())`.** The two-second rebuild detaches
  elements constantly, so `page.click()` and `element.screenshot()` fail with
  "element is not stable". Screenshot with `page.screenshot({clip})` instead.
- **Measure at several viewports** — 414, 1024, 1280, 1920, 1920×720, 2560 —
  and assert `scrollHeight - clientHeight` *and* `scrollWidth - clientWidth`
  are zero on `.card-body`, plus `--fit` where it matters.
- **`getBoundingClientRect().width`**, not `scrollWidth`, for inline elements.

Do not kill the demo server with `pkill -f "tempest_server.py --demo"` — the
pattern matches the shell's own command line and kills the session. Use:

```bash
ps -eo pid,args | awk '$3 ~ /tempest_server.py/ && $4=="--demo" {print $1}' | xargs -r kill
```

## Git

History is linear and has no merge commits anywhere. **Merge pull requests
with rebase.** Commit messages explain why, not what; the diff covers what.

Every change gets a bullet at the top of the current version's section in the
README changelog.

## Verifying a deploy

The server reports `sha256(web/index.html)[:12]` as `ui` in `/api/state`, and
`deploy.sh` compares it against the same digest computed locally before it
calls a deploy finished. Its `verify what landed` step hashes the file *on the
NAS* before building, which separates "the files never arrived" from "the
container did not restart" — two failures that otherwise look identical.

Every run writes a transcript to `deploy-logs/` (gitignored, last twenty
kept). On failure the log also gets a diagnostic sweep of the NAS. That file
is the thing to ask for when a deploy goes wrong.

The page reloads itself when `ui` changes, so the wall display picks up a
deploy without being touched.
