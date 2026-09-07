#!/usr/bin/env python3
# =============================================================================
# Tempest Weather Station — LAN web dashboard
# Version 3.1.0
#
# MIT License
# Copyright (c) 2026  Michael Walker VA3MW  &  Claude (Anthropic)
# See tempest_weather.py for the full licence text.
# =============================================================================
#
# Listens for the Tempest hub's UDP broadcasts and serves the same six-card
# dashboard as a web page, so it can be opened from any device on the LAN or
# left up on a TV.
#
#   python3 tempest_server.py --lat 45.4215 --lon -75.6972
#   python3 tempest_server.py --demo            # no hub needed
#
# Then open  http://<this-host>:8444/   (add ?tv for the big-screen layout).
#
# NOTE: there is no authentication. Keep it on your own network — do not
# forward the port to the internet.
#
# Standard library only. Requires tempest_core.py alongside it.
# =============================================================================

import argparse
import json
import os
import signal
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tempest_core as core

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")

# The only file types the static route will hand out.
STATIC_TYPES = {
    ".woff2": "font/woff2",
    ".woff":  "font/woff",
    ".css":   "text/css; charset=utf-8",
    ".js":    "application/javascript; charset=utf-8",
    ".svg":   "image/svg+xml",
    ".png":   "image/png",
    ".txt":   "text/plain; charset=utf-8",
}

# The hub only ever broadcasts; nothing here talks back to it.
POLL_HINT_MS = 2000


class ForecastFetcher(threading.Thread):
    """Ten-day forecast from Open-Meteo.

    This is the one thing the hub cannot provide — a barometer cannot predict
    Thursday — so it is the only outbound network call the dashboard makes.
    It is optional (needs --lat/--lon, disabled by --no-forecast), it fails
    soft, and nothing about your station is sent: just a coarse latitude and
    longitude, to a free service that needs no account or key.

    Open-Meteo data is CC BY 4.0 — https://open-meteo.com/
    """

    ENDPOINT = "https://api.open-meteo.com/v1/forecast"
    REFRESH = 1800          # half an hour; the feed updates far slower
    RETRY = 120             # after a failure

    def __init__(self, lat, lon, stop_event, days=10, cache_path=None):
        threading.Thread.__init__(self, daemon=True)
        self.lat, self.lon, self.days = lat, lon, days
        self.stop_event = stop_event
        self.cache_path = cache_path
        self.lock = threading.Lock()
        self.data = None
        self.error = ""
        self._load_cache()

    # A restart should not cost Open-Meteo a request. The feed changes far
    # more slowly than this container restarts, so a recent cache is reused
    # and the next fetch is deferred until it actually goes stale.
    def _load_cache(self):
        if not self.cache_path:
            return
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, ValueError):
            return
        try:
            age = time.time() - float(cached.get("fetched_at") or 0)
        except (TypeError, ValueError):
            return
        if 0 <= age < self.REFRESH and cached.get("days"):
            self.data = cached

    def _save_cache(self, fresh):
        if not self.cache_path:
            return
        try:
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(fresh, f)
            os.replace(tmp, self.cache_path)
        except OSError:
            pass

    def _url(self):
        return self.ENDPOINT + "?" + urllib.parse.urlencode({
            "latitude": "%.4f" % self.lat,
            "longitude": "%.4f" % self.lon,
            "current": ("temperature_2m,relative_humidity_2m,"
                        "apparent_temperature,weather_code,wind_speed_10m,"
                        "wind_direction_10m,is_day"),
            "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                      "precipitation_probability_max,sunrise,sunset"),
            "timezone": "auto",
            "forecast_days": str(self.days),
            "wind_speed_unit": "ms",
            "temperature_unit": "celsius",
            "precipitation_unit": "mm",
        })

    def fetch_once(self):
        req = urllib.request.Request(
            self._url(), headers={"User-Agent": "tempest-dashboard/" + core.VERSION})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = json.loads(r.read().decode("utf-8"))

        cur = raw.get("current") or {}
        daily = raw.get("daily") or {}
        dates = daily.get("time") or []
        days = []
        for i, day in enumerate(dates):
            pick = lambda k: (daily.get(k) or [None] * len(dates))[i]
            days.append({
                "date": day,
                "code": pick("weather_code"),
                "tmax_c": pick("temperature_2m_max"),
                "tmin_c": pick("temperature_2m_min"),
                "pop": pick("precipitation_probability_max"),
            })
        return {
            "fetched_at": time.time(),
            "source": "Open-Meteo",
            "current": {
                "temp_c": cur.get("temperature_2m"),
                "rh": cur.get("relative_humidity_2m"),
                "feels_c": cur.get("apparent_temperature"),
                "code": cur.get("weather_code"),
                "wind_ms": cur.get("wind_speed_10m"),
                "wind_dir": cur.get("wind_direction_10m"),
                "is_day": bool(cur.get("is_day", 1)),
            },
            "days": days,
        }

    def run(self):
        # If the cache is still warm, wait out its remaining life first.
        with self.lock:
            warm = self.data
        if warm:
            left = self.REFRESH - (time.time() - warm["fetched_at"])
            if left > 0 and self.stop_event.wait(left):
                return

        fails = 0
        while not self.stop_event.is_set():
            try:
                fresh = self.fetch_once()
                with self.lock:
                    self.data, self.error = fresh, ""
                self._save_cache(fresh)
                fails = 0
                wait = self.REFRESH
            except (urllib.error.URLError, OSError, ValueError,
                    TimeoutError) as e:
                with self.lock:
                    self.error = "Forecast unavailable (%s)" % (
                        getattr(e, "reason", None) or e.__class__.__name__)
                fails += 1
                wait = self._backoff(fails)
            except Exception as e:                      # never kill the thread
                with self.lock:
                    self.error = "Forecast error (%s)" % e.__class__.__name__
                fails += 1
                wait = self._backoff(fails)
            self.stop_event.wait(wait)

    def _backoff(self, fails):
        """Come back quickly after the first failure, then back off — a slow
        first attempt should not blank the card for two minutes."""
        return min(self.RETRY, 15 * (2 ** (fails - 1)))

    def snapshot(self):
        with self.lock:
            if self.data is None:
                return {"available": False, "error": self.error}
            out = dict(self.data)
            out["available"] = True
            out["error"] = self.error
            out["age_s"] = time.time() - out["fetched_at"]
            return out


class AlertsFetcher(threading.Thread):
    """Active weather alerts for the station's location, from the US National
    Weather Service. No key and no account — the NWS only asks that clients
    identify themselves in the User-Agent, which we do.

    Outside NWS coverage the endpoint simply returns nothing, so this is a
    no-op rather than an error.
    """

    ENDPOINT = "https://api.weather.gov/alerts/active"
    REFRESH = 300
    RETRY = 60
    # Most to least serious; the dashboard shows the worst one active.
    RANK = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1, "Unknown": 0}

    def __init__(self, lat, lon, stop_event):
        threading.Thread.__init__(self, daemon=True)
        self.lat, self.lon = lat, lon
        self.stop_event = stop_event
        self.lock = threading.Lock()
        self.alerts = []
        self.error = ""
        self.fetched_at = None

    def fetch_once(self):
        url = "%s?point=%.4f,%.4f" % (self.ENDPOINT, self.lat, self.lon)
        req = urllib.request.Request(url, headers={
            "User-Agent": "tempest-dashboard/%s (self-hosted station display)"
                          % core.VERSION,
            "Accept": "application/geo+json",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = json.loads(r.read().decode("utf-8"))

        out = []
        for feat in (raw.get("features") or []):
            p = feat.get("properties") or {}
            out.append({
                "event": p.get("event") or "Weather alert",
                "severity": p.get("severity") or "Unknown",
                "urgency": p.get("urgency") or "",
                "headline": (p.get("headline") or "").strip(),
                "onset": p.get("onset"), "expires": p.get("expires"),
                "sender": p.get("senderName") or "",
                "rank": self.RANK.get(p.get("severity") or "Unknown", 0),
            })
        out.sort(key=lambda a: a["rank"], reverse=True)
        return out

    def run(self):
        fails = 0
        while not self.stop_event.is_set():
            try:
                fresh = self.fetch_once()
                with self.lock:
                    self.alerts, self.error = fresh, ""
                    self.fetched_at = time.time()
                fails = 0
                wait = self.REFRESH
            except Exception as e:
                with self.lock:
                    self.error = "Alerts unavailable (%s)" % e.__class__.__name__
                fails += 1
                wait = min(self.REFRESH, self.RETRY * (2 ** min(fails - 1, 3)))
            self.stop_event.wait(wait)

    def snapshot(self):
        with self.lock:
            return {"alerts": list(self.alerts), "error": self.error,
                    "checked": self.fetched_at is not None}


class Backfill(threading.Thread):
    """Fill the history from WeatherFlow's own record.

    The hub only broadcasts what is happening now, so a fresh install has no
    past: 24-hour highs and lows, and month/year rainfall, only count from the
    moment the dashboard started. Given a personal access token this pulls the
    station's real record and merges it in.

    The token is read from the environment and used only against WeatherFlow.
    Without one this does nothing at all.
    """

    STATIONS = "https://swd.weatherflow.com/swd/rest/stations"
    DEVICE_OBS = "https://swd.weatherflow.com/swd/rest/observations/device/%s"
    REFRESH = 6 * 3600
    RETRY = 600

    def __init__(self, token, history, state, stop_event, hours=48):
        threading.Thread.__init__(self, daemon=True)
        self.token = token
        self.history = history
        self.state = state
        self.stop_event = stop_event
        self.hours = hours
        self.lock = threading.Lock()
        self.status = "waiting"
        self.added = 0
        self.error = ""

    def _get(self, url, params):
        params = dict(params)
        params["token"] = self.token
        req = urllib.request.Request(
            url + "?" + urllib.parse.urlencode(params),
            headers={"User-Agent": "tempest-dashboard/" + core.VERSION})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def _find_device(self):
        """The Tempest ('ST') device id, preferring the serial we hear on the
        wire so a multi-station account picks the right one."""
        raw = self._get(self.STATIONS, {})
        want = (self.state.serial or "").strip()
        fallback = None
        for st in (raw.get("stations") or []):
            for dev in (st.get("devices") or []):
                if dev.get("device_type") != "ST":
                    continue
                did = dev.get("device_id")
                if did is None:
                    continue
                if want and str(dev.get("serial_number") or "") == want:
                    return did
                if fallback is None:
                    fallback = did
        return fallback

    def _merge(self, rows):
        """Fold device observations into history, without disturbing anything
        we already recorded live."""
        if not rows:
            return 0
        have = set()
        for smp in self.history.samples:
            have.add(int(smp["t"] // 60))
        cutoff = time.time() - self.hours * 3600
        rain_by_day = {}
        merged = []
        for obs in rows:
            if not isinstance(obs, list) or len(obs) < 17:
                continue
            try:
                ts = float(obs[0])
            except (TypeError, ValueError):
                continue
            if ts < cutoff or ts > time.time() + 300:
                continue
            if obs[12]:
                day = datetime.fromtimestamp(ts).date().isoformat()
                try:
                    rain_by_day[day] = rain_by_day.get(day, 0.0) + float(obs[12])
                except (TypeError, ValueError):
                    pass
            slot = int(ts // 60)
            if slot in have:
                continue
            have.add(slot)
            smp = {"t": ts}
            for key, idx in (("temp_c", 7), ("pres_mb", 6), ("wind_ms", 2),
                             ("gust_ms", 3), ("rh", 8), ("dir", 4)):
                val = obs[idx]
                if val is not None:
                    try:
                        smp[key] = round(float(val), 3)
                    except (TypeError, ValueError):
                        pass
            merged.append(smp)

        if not merged:
            return 0
        combined = list(self.history.samples) + merged
        combined.sort(key=lambda x: x["t"])
        self.history.samples.clear()
        for smp in combined[-self.history.samples.maxlen:]:
            self.history.samples.append(smp)
        # Only fill rainfall days we have nothing for; live totals win.
        for day, mm in rain_by_day.items():
            if day not in self.history.rain_days:
                self.history.rain_days[day] = round(mm, 3)
        self.history.save(force=True)
        return len(merged)

    def sweep_daily(self, device, days=4000, chunk_days=4,
                    max_requests=500):
        """Walk back through the year building daily rain totals and daily
        temperature highs and lows.

        The 48-hour sample merge cannot fill month- or year-to-date figures,
        nor say anything about the hottest day in June. This asks for older
        windows a few days at a time and keeps only the per-day summary,
        which is all those figures need — no point holding a year of minute
        data in memory.
        """
        done_key = "rain_swept_at"
        from_key = "swept_from"
        already = self.history.meta.get(done_key)
        reached = self.history.meta.get(from_key)
        if already and time.time() - already < 20 * 3600 and reached:
            return 0                       # swept recently; nothing to do

        today = date.today()
        # Walk back from wherever we last reached, rather than a fixed year,
        # and keep going until the station stops answering — that is its
        # install date. A run is capped so a first sweep cannot hammer the
        # API; the next run picks up where this one stopped.
        try:
            oldest = date.fromisoformat(reached) if reached else today
        except (TypeError, ValueError):
            oldest = today
        oldest = min(oldest, today - timedelta(days=7))   # always redo the tail
        floor = today - timedelta(days=days)
        totals = {}
        temps = {}
        requests = 0
        empty_runs = 0
        # forwards over the recent tail, then backwards into the archive
        windows = []
        cursor = oldest
        while cursor <= today:
            windows.append(cursor)
            cursor = cursor + timedelta(days=chunk_days)
        back = oldest
        while back > floor and len(windows) < max_requests:
            back = back - timedelta(days=chunk_days)
            windows.append(back)

        reached_back = oldest
        for cursor in windows:
            if self.stop_event.is_set() or requests >= max_requests:
                break
            end = min(cursor + timedelta(days=chunk_days), today + timedelta(days=1))
            try:
                raw = self._get(self.DEVICE_OBS % device, {
                    "time_start": int(time.mktime(cursor.timetuple())),
                    "time_end": int(time.mktime(end.timetuple())),
                })
            except Exception:
                continue                   # a gap is better than giving up
            for obs in (raw.get("obs") or []):
                if not isinstance(obs, list) or len(obs) < 13:
                    continue
                try:
                    day = datetime.fromtimestamp(float(obs[0])).date().isoformat()
                except (TypeError, ValueError, OSError):
                    continue
                if obs[12]:
                    try:
                        totals[day] = totals.get(day, 0.0) + float(obs[12])
                    except (TypeError, ValueError):
                        pass
                if len(obs) > 7 and obs[7] is not None:
                    try:
                        t = float(obs[7])
                    except (TypeError, ValueError):
                        continue
                    cur = temps.get(day)
                    if cur is None:
                        temps[day] = {"lo": t, "hi": t}
                    else:
                        if t < cur["lo"]: cur["lo"] = t
                        if t > cur["hi"]: cur["hi"] = t
            requests += 1
            got = len(raw.get("obs") or [])
            if cursor < oldest:            # only the backwards half can end
                if got:
                    empty_runs = 0
                    reached_back = min(reached_back, cursor)
                else:
                    empty_runs += 1
                    if empty_runs >= 4:
                        break              # before the station existed
            self.stop_event.wait(0.4)      # be gentle with the API

        added = warm = 0
        today_iso = today.isoformat()
        for day, mm in totals.items():
            # never overwrite today, which we are still measuring live
            if day == today_iso or day in self.history.rain_days:
                continue
            self.history.rain_days[day] = round(mm, 3)
            added += 1
        for day, mm in temps.items():
            if day == today_iso or day in self.history.temp_days:
                continue
            self.history.temp_days[day] = {"lo": round(mm["lo"], 2),
                                           "hi": round(mm["hi"], 2)}
            warm += 1
        self.history.meta[done_key] = time.time()
        self.history.meta[from_key] = reached_back.isoformat()
        self.history.save(force=True)
        print("  backfill : swept back to %s in %d requests — %d rain days, "
              "%d temperature days" % (reached_back.isoformat(), requests,
                                       added, warm))
        sys.stdout.flush()
        return added

    def run(self):
        while not self.stop_event.is_set():
            try:
                device = self._find_device()
                if device is None:
                    with self.lock:
                        self.status, self.error = "no Tempest device found", ""
                    self.stop_event.wait(self.REFRESH)
                    continue
                now = int(time.time())
                raw = self._get(self.DEVICE_OBS % device,
                                {"time_start": now - self.hours * 3600,
                                 "time_end": now})
                added = self._merge(raw.get("obs") or [])
                with self.lock:
                    self.added += added
                    self.status = "ok"
                    self.error = ""
                print("  backfill : merged %d observations from WeatherFlow"
                      % added)
                sys.stdout.flush()
                self.sweep_daily(device)
                wait = self.REFRESH
            except Exception as e:
                with self.lock:
                    self.status = "failed"
                    self.error = "Backfill failed (%s)" % e.__class__.__name__
                wait = self.RETRY
            self.stop_event.wait(wait)

    def snapshot(self):
        with self.lock:
            return {"status": self.status, "added": self.added,
                    "error": self.error}


CARD_NAMES = ["temperature", "wind", "pressure", "rainfall", "astronomy",
              "forecast", "lightning", "radar", "records"]


class Config:
    """Settings the dashboard can change about itself, shared by every viewer.

    The WeatherFlow token lives here but is deliberately write-only: it is
    never included in anything the server hands back, so a browser can set or
    clear it but never read it.
    """

    # As many cards as there are; the grid arranges itself to suit.
    MAX_SLOTS = len(CARD_NAMES)

    def __init__(self, path, defaults):
        self.path = path
        self.lock = threading.Lock()
        self.slots = list(defaults.get("slots") or [])
        self.token = defaults.get("token") or ""
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        slots = self.clean_slots(saved.get("slots"))
        with self.lock:
            if slots:
                self.slots = slots
            if isinstance(saved.get("token"), str) and saved["token"]:
                self.token = saved["token"]

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"slots": self.slots, "token": self.token}, f, indent=2)
            os.chmod(tmp, 0o600)          # it holds a token
            os.replace(tmp, self.path)
        except OSError:
            pass

    @classmethod
    def clean_slots(cls, raw):
        if not isinstance(raw, list):
            return []
        out = []
        for name in raw:
            if isinstance(name, str):
                name = name.strip().lower()
                if name in CARD_NAMES and name not in out:
                    out.append(name)
        return out[:cls.MAX_SLOTS]

    def public(self):
        """Everything a browser may see — note the token itself is absent."""
        with self.lock:
            return {"slots": list(self.slots),
                    "cards": CARD_NAMES,
                    "token_set": bool(self.token)}

    def apply(self, patch):
        """Returns (changed_fields, error)."""
        if not isinstance(patch, dict):
            return [], "expected an object"
        changed = []
        with self.lock:
            if "slots" in patch:
                slots = self.clean_slots(patch.get("slots"))
                if not slots:
                    return [], "at least one valid card is required"
                if slots != self.slots:
                    self.slots = slots
                    changed.append("slots")
            if "token" in patch:
                tok = patch.get("token")
                if tok is None or (isinstance(tok, str) and not tok.strip()):
                    if self.token:
                        self.token = ""
                        changed.append("token_cleared")
                elif isinstance(tok, str):
                    tok = tok.strip()
                    if len(tok) > 200:
                        return [], "that token looks too long"
                    if tok != self.token:
                        self.token = tok
                        changed.append("token_set")
                else:
                    return [], "token must be text"
        if changed:
            self.save()
        return changed, ""


class Dashboard:
    """Owns the station state, the packet source and the on-disk history."""

    def __init__(self, args):
        self.args = args
        self.started = time.time()
        self.stop = threading.Event()
        history_path = os.path.join(args.data_dir, "tempest_history.json")
        self.history = core.History(history_path)
        self.state = core.StationState(history=self.history, demo=args.demo,
                                       port=args.udp_port)
        self.settings_path = os.path.join(args.data_dir,
                                          "tempest_server_state.json")
        self.config = Config(os.path.join(args.data_dir, "tempest_config.json"),
                             {"slots": args.slots, "token": args.wf_token})
        self._load()

        if args.demo:
            core.seed_demo_history(self.history)
            self.source = core.DemoSource(self.state.handle, self.stop)
        else:
            self.source = core.UdpListener(args.udp_port, self.state.handle,
                                           self.stop)
        self.source.start()

        self.forecast = None
        if args.forecast and args.lat is not None and args.lon is not None:
            self.forecast = ForecastFetcher(
                args.lat, args.lon, self.stop,
                cache_path=os.path.join(args.data_dir, "tempest_forecast.json"))
            self.forecast.start()
        self.backfill = None
        self.start_backfill()

        self.alerts = None
        if args.alerts and args.lat is not None and args.lon is not None:
            self.alerts = AlertsFetcher(args.lat, args.lon, self.stop)
            self.alerts.start()
        threading.Thread(target=self._housekeeping, daemon=True).start()

    def start_backfill(self):
        """Start the backfill thread if a token is configured and it is not
        already running. Called at boot and again when a token is saved."""
        if self.backfill is not None and self.backfill.is_alive():
            return False
        token = self.config.token
        if not token:
            return False
        self.backfill = Backfill(token, self.history, self.state, self.stop)
        self.backfill.start()
        return True

    # ── the day's counters survive a restart ──────────────────────────────

    def _load(self):
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        if self.state.load_state(saved):
            when = datetime.fromtimestamp(saved["saved_at"]).strftime("%H:%M:%S")
            print("  restored : last observation from %s" % when)

    def _save(self):
        try:
            tmp = self.settings_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state.dump_state(), f)
            os.replace(tmp, self.settings_path)
        except OSError:
            pass

    def _housekeeping(self):
        while not self.stop.wait(30.0):
            self.state.roll_day()
            self.history.save()
            self._save()

    def snapshot(self):
        snap = self.state.snapshot(self.args.lat, self.args.lon)
        snap["source_error"] = getattr(self.source, "error", "") or ""
        snap["uptime_s"] = time.time() - self.started
        snap["poll_ms"] = POLL_HINT_MS
        snap["units"] = {"temp": self.args.temp_unit, "wind": self.args.wind_unit,
                         "pres": self.args.pres_unit, "rain": self.args.rain_unit,
                         "dist": self.args.dist_unit}
        snap["station_name"] = self.args.name or snap.get("serial") or ""
        snap["lat"], snap["lon"] = self.args.lat, self.args.lon
        snap["alerts"] = (self.alerts.snapshot() if self.alerts
                          else {"alerts": [], "error": "", "checked": False})
        snap["slots"] = self.config.slots
        snap["backfill"] = (self.backfill.snapshot() if self.backfill
                            else {"status": "off", "added": 0, "error": ""})
        snap["forecast"] = (self.forecast.snapshot() if self.forecast
                            else {"available": False,
                                  "error": "Forecast off"
                                           if not self.args.forecast
                                           else "Set --lat and --lon"})
        return snap

    def shutdown(self):
        self.stop.set()
        if hasattr(self.source, "close"):
            self.source.close()
        self.history.save(force=True)
        self._save()


class Handler(BaseHTTPRequestHandler):
    server_version = "TempestDashboard/" + core.VERSION
    dashboard = None            # set on the server instance below

    # ── plumbing ──────────────────────────────────────────────────────────

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype, cache="no-store", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ── routes ────────────────────────────────────────────────────────────

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/api/config":
            self._send(404, "Not found\n", "text/plain; charset=utf-8")
            return
        # There is no login on this dashboard, so require a header a plain
        # cross-site form cannot set. That blocks another page on the network
        # from quietly reconfiguring this one.
        if self.headers.get("X-Tempest-Config") != "1":
            self._send(403, "Missing X-Tempest-Config header\n",
                       "text/plain; charset=utf-8")
            return
        origin = self.headers.get("Origin")
        if origin:
            try:
                host = urllib.parse.urlparse(origin).netloc
            except ValueError:
                host = ""
            if host and host != self.headers.get("Host"):
                self._send(403, "Cross-origin write refused\n",
                           "text/plain; charset=utf-8")
                return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 8192:
            self._send(400, "Bad request body\n", "text/plain; charset=utf-8")
            return
        try:
            patch = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, "Malformed JSON\n", "text/plain; charset=utf-8")
            return

        dash = self.server.dashboard
        changed, err = dash.config.apply(patch)
        if err:
            self._send(400, json.dumps({"ok": False, "error": err}),
                       "application/json; charset=utf-8")
            return
        if "token_set" in changed:
            dash.start_backfill()
        body = dash.config.public()
        body["ok"] = True
        body["changed"] = changed
        self._send(200, json.dumps(body), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path in ("/", "/index.html"):
                self._serve_file("index.html", "text/html; charset=utf-8")
            elif path == "/api/state":
                body = json.dumps(self.server.dashboard.snapshot(),
                                  allow_nan=False, default=str)
                self._send(200, body, "application/json; charset=utf-8")
            elif path == "/api/wind":
                # Tiny payload, so the compass can be polled far more often
                # than the full snapshot without wasting bandwidth.
                st = self.server.dashboard.state
                with st.lock:
                    rapid = dict(st.last_rapid_wind)
                    avg = st.data.get("wind_avg_ms")
                    adir = st.data.get("wind_dir")
                    gust = st.data.get("wind_gust_ms")
                body = json.dumps({
                    "t": time.time(),
                    "dir": rapid.get("dir_deg", adir),
                    "ms": rapid.get("speed_ms", avg),
                    "avg_ms": avg, "gust_ms": gust,
                    "health": st.health(),
                })
                self._send(200, body, "application/json; charset=utf-8")
            elif path == "/api/config":
                cfg = self.server.dashboard.config.public()
                cfg["backfill"] = self.server.dashboard.snapshot()["backfill"]
                self._send(200, json.dumps(cfg),
                           "application/json; charset=utf-8")
            elif path == "/healthz":
                state = self.server.dashboard.state
                self._send(200, json.dumps({"ok": True,
                                            "health": state.health(),
                                            "version": core.VERSION}),
                           "application/json")
            elif path.startswith("/fonts/"):
                self._serve_static(path)
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._send(404, "Not found\n", "text/plain; charset=utf-8")
        except BrokenPipeError:
            pass                      # a TV browser closing the tab mid-poll
        except ConnectionResetError:
            pass

    def _serve_static(self, path):
        """Serve a vendored asset from web/. The path comes from the URL, so
        it is checked against the allow-listed types and confined to WEB_DIR
        before anything is opened."""
        if ".." in path or "%" in path or "\\" in path:
            self._send(404, "Not found\n", "text/plain; charset=utf-8")
            return
        ctype = STATIC_TYPES.get(os.path.splitext(path)[1].lower())
        if ctype is None:
            self._send(404, "Not found\n", "text/plain; charset=utf-8")
            return
        root = os.path.realpath(WEB_DIR)
        full = os.path.realpath(os.path.join(root, path.lstrip("/")))
        if full != root and not full.startswith(root + os.sep):
            self._send(404, "Not found\n", "text/plain; charset=utf-8")
            return
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            self._send(404, "Not found\n", "text/plain; charset=utf-8")
            return
        self._send(200, body, ctype, cache="public, max-age=604800")

    def _serve_file(self, name, ctype):
        # `name` is a fixed literal from the routing table above, never
        # anything the client supplied.
        full = os.path.join(WEB_DIR, name)
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            self._send(500, "Missing %s — expected it next to the server in "
                            "web/\n" % name, "text/plain; charset=utf-8")
            return
        self._send(200, body, ctype)


def local_ips():
    """Best-effort list of addresses this host can be reached on."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))            # TEST-NET-1: never sends
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def env_default(key, fallback=None, cast=str):
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return fallback
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return fallback


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Serve the Tempest dashboard on your LAN.")
    p.add_argument("--host", default=env_default("TEMPEST_HOST", "0.0.0.0"),
                   help="address to bind the web server to (default 0.0.0.0)")
    p.add_argument("--http-port", type=int,
                   default=env_default("TEMPEST_HTTP_PORT", 8444, int),
                   help="web server port (default 8444)")
    p.add_argument("--udp-port", type=int,
                   default=env_default("TEMPEST_UDP_PORT",
                                       core.DEFAULT_PORT, int),
                   help="hub broadcast port (default 50222)")
    p.add_argument("--lat", type=float, default=env_default("TEMPEST_LAT",
                                                            None, float),
                   help="station latitude, for sunrise and sunset")
    p.add_argument("--lon", type=float, default=env_default("TEMPEST_LON",
                                                            None, float),
                   help="station longitude")
    p.add_argument("--name", default=env_default("TEMPEST_NAME", ""),
                   help="label shown in the footer (default: hub serial)")
    p.add_argument("--data-dir", default=env_default("TEMPEST_DATA_DIR", HERE),
                   help="where history and counters are written")
    p.add_argument("--demo", action="store_true",
                   default=bool(env_default("TEMPEST_DEMO", "")),
                   help="synthesise weather instead of listening for a hub")
    p.add_argument("--wf-token", default=env_default("TEMPEST_WF_TOKEN", ""),
                   help="WeatherFlow personal access token. Optional; only "
                        "used to backfill history so 24-hour and monthly "
                        "figures are real from day one. Get one at "
                        "tempestwx.com/settings/tokens")
    p.add_argument("--no-alerts", dest="alerts", action="store_false",
                   default=not env_default("TEMPEST_NO_ALERTS", ""),
                   help="do not fetch National Weather Service alerts")
    p.add_argument("--slots", default=env_default(
                       "TEMPEST_SLOTS",
                       "temperature,wind,pressure,rainfall,astronomy,forecast"),
                   help="comma-separated cards, in order. Choose from: "
                        "temperature, wind, pressure, rainfall, astronomy, "
                        "forecast, lightning, radar, records, blank")
    p.add_argument("--no-forecast", dest="forecast", action="store_false",
                   default=not env_default("TEMPEST_NO_FORECAST", ""),
                   help="do not fetch the Open-Meteo forecast (no outbound "
                        "network calls at all)")
    p.add_argument("--verbose", action="store_true",
                   help="log every HTTP request")
    for name, choices, default in (
            ("temp", core.TEMP_UNITS, "°F"), ("wind", core.WIND_UNITS, "mph"),
            ("pres", core.PRES_UNITS, "inHg"), ("rain", core.RAIN_UNITS, "in"),
            ("dist", core.DIST_UNITS, "mi")):
        p.add_argument("--%s-unit" % name, choices=choices,
                       default=env_default("TEMPEST_%s_UNIT" % name.upper(),
                                           default),
                       help="default %s unit (viewers can change it)" % name)
    p.add_argument("--version", action="version",
                   version="Tempest dashboard " + core.VERSION)
    args = p.parse_args(argv[1:])
    if (args.lat is None) != (args.lon is None):
        p.error("--lat and --lon must be given together")

    known = {"temperature", "wind", "pressure", "rainfall", "astronomy",
             "forecast", "lightning", "radar", "records", "blank"}
    slots = [x.strip().lower() for x in (args.slots or "").split(",")
             if x.strip()]
    bad = [x for x in slots if x not in known]
    if bad:
        p.error("unknown card(s) in --slots: %s. Choose from: %s"
                % (", ".join(bad), ", ".join(sorted(known))))
    args.slots = slots or ["temperature", "wind", "pressure",
                           "rainfall", "astronomy", "forecast"]
    return args


def main(argv):
    args = parse_args(argv)
    try:
        os.makedirs(args.data_dir, exist_ok=True)
    except OSError as e:
        print("Cannot use --data-dir %s: %s" % (args.data_dir, e),
              file=sys.stderr)
        return 2

    dashboard = Dashboard(args)
    httpd = ThreadingHTTPServer((args.host, args.http_port), Handler)
    httpd.daemon_threads = True
    httpd.dashboard = dashboard
    httpd.verbose = args.verbose

    print("Tempest dashboard %s" % core.VERSION)
    if args.demo:
        print("  source   : demo mode (synthetic weather, no hub)")
    else:
        print("  source   : UDP :%d  (hub broadcasts)" % args.udp_port)
    if args.lat is None:
        print("  sun times: off — pass --lat and --lon to enable")
    else:
        print("  location : %.4f, %.4f" % (args.lat, args.lon))
    print("  data dir : %s" % args.data_dir)
    print("  cards    : %s  (editable in the dashboard's settings)"
          % ", ".join(dashboard.config.slots))
    if args.alerts and args.lat is not None:
        print("  alerts   : National Weather Service (no key needed)")
    print("  backfill : %s" % ("WeatherFlow history"
                               if dashboard.config.token
                               else "off (add a token in settings)"))
    if args.forecast and args.lat is not None:
        print("  forecast : Open-Meteo (the only outbound call; --no-forecast "
              "disables)")
    else:
        print("  forecast : off")
    for ip in local_ips() or [args.host]:
        print("  open     : http://%s:%d/       (add ?tv for the TV layout)"
              % (ip, args.http_port))
    print("  no authentication — keep this on your own network")
    sys.stdout.flush()

    def bye(_signum=None, _frame=None):
        print("\nstopping…")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, bye)
        except (ValueError, OSError):
            pass

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        dashboard.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
