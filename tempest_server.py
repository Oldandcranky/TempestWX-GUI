#!/usr/bin/env python3
# =============================================================================
# Tempest Weather Station — LAN web dashboard
# Version 3.1.0
#
# MIT License
# Copyright (c) 2026  Chris Goodman  &  Claude (Anthropic)
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
import difflib
import hashlib
import json
import math
import os
import re
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


def ui_fingerprint():
    """A short digest of the page this server serves.

    A dashboard left open polls for data but never re-fetches itself, so it
    keeps running whatever HTML and CSS it loaded with — a rebuilt server can
    sit behind a wall display showing a stale interface indefinitely. The page
    watches this value and reloads when it changes.

    VERSION will not do: it moves on releases, not on every rebuild, and the
    rebuild nobody remembered to bump is exactly the one that strands a wall
    display. Reading the file is right because the page is baked into the
    image — a changed page means a restarted server means a fresh digest.
    """
    try:
        with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return ""          # no page to serve; the UI has bigger problems


UI_ID = ui_fingerprint()


def twelve(text, what):
    """Twelve numbers, January first, or None. Complains rather than guessing."""
    parts = [p.strip() for p in (text or "").split(",") if p.strip()]
    if not parts:
        return None
    if len(parts) != 12:
        print("Ignoring %s: got %d values, need 12" % (what, len(parts)),
              file=sys.stderr)
        return None
    try:
        return [float(p) for p in parts]
    except ValueError:
        print("Ignoring %s: twelve numbers expected, January first" % what,
              file=sys.stderr)
        return None


def rain_monthly(monthly, annual):
    """Twelve normal monthly rainfalls in inches, January first, or None.

    Rain is not spread evenly through a year — May here is more than twice
    February — so the card's pace mark is only honest against the shape.
    A plain annual figure still works and is spread evenly, which is the
    same straight line as before and better than nothing.
    """
    got = twelve(monthly, "--rain-monthly")
    if got:
        return got
    return [annual / 12.0] * 12 if annual else None


def temp_normal(highs, lows):
    """Normal monthly highs and lows as {"hi": [...], "lo": [...]} in °C.

    Given in Fahrenheit, like the rain normals are given in inches, because
    that is how NOAA publishes them and this is who asks for them. Both are
    needed: one line alone would be the daily mean, and a day's temperature
    crosses its own mean twice before breakfast.
    """
    hi = twelve(highs, "--temp-normal-high")
    lo = twelve(lows, "--temp-normal-low")
    if not hi or not lo:
        return None
    f2c = lambda f: (f - 32.0) * 5.0 / 9.0
    return {"hi": [f2c(v) for v in hi], "lo": [f2c(v) for v in lo]}

# Icons the page links to. Listed one by one so the static route stays a
# closed set rather than anything that happens to sit in web/.
ICON_PATHS = ("/favicon.svg", "/favicon.ico", "/apple-touch-icon.png",
              "/icon-192.png", "/icon-512.png", "/manifest.webmanifest")

# The only file types the static route will hand out.
STATIC_TYPES = {
    ".woff2": "font/woff2",
    ".woff":  "font/woff",
    ".css":   "text/css; charset=utf-8",
    ".js":    "application/javascript; charset=utf-8",
    ".svg":   "image/svg+xml",
    ".png":   "image/png",
    ".webmanifest": "application/manifest+json",
    ".ico":   "image/x-icon",
    ".txt":   "text/plain; charset=utf-8",
}

# The hub only ever broadcasts; nothing here talks back to it.
POLL_HINT_MS = 2000


class PollingFetcher(threading.Thread):
    """One external service, fetched on a schedule, in a daemon thread.

    Five of these existed with the same loop written out five times. What
    they share is not boilerplate — it is the contract the dashboard reads:

      on success   replace the data and clear the error
      on failure   keep the data and set the error

    so "available, with an error set" always means *what you are looking at
    is old*, which is the amber dot on a card. A sixth copy of that, written
    slightly differently, would be a card that lies about its own freshness.

    A subclass must supply `fetch_once`. It may override:

      store          keep the result somewhere other than self.data
      after_success  work to do outside the lock, such as writing a cache
      describe       the card's wording for a particular failure
      backoff        a different retry policy
      snapshot       a different shape for the card
    """

    REFRESH = 900             # seconds between successful fetches
    RETRY = 60                # first delay after a failure
    LABEL = "Data"            # how a card names this source when it fails

    def __init__(self, stop_event):
        threading.Thread.__init__(self, daemon=True)
        self.stop_event = stop_event
        self.lock = threading.Lock()
        self.data = None
        self.error = ""

    def fetch_once(self):
        raise NotImplementedError

    def store(self, fresh):
        """Called holding the lock. Keep it short."""
        self.data = fresh

    def after_success(self, fresh):
        """Called without the lock, so this is where slow work belongs."""

    def describe(self, exc):
        return "%s unavailable (%s)" % (self.LABEL, exc.__class__.__name__)

    def backoff(self, fails):
        """Double the retry each time, four times, then hold. Capped at the
        refresh interval: there is no sense retrying more slowly than the
        thing would have refreshed anyway."""
        return min(self.REFRESH, self.RETRY * (2 ** min(fails - 1, 3)))

    def long_history(self, days=None):
        """Several days of results, for the Internet page. Held for five
        minutes: the page is opened by hand, and a tracker that tests hourly
        has nothing new to say in between."""
        days = int(days or self.HISTORY_DAYS)
        now = time.time()
        with self.history_lock:
            got = self.history_cache
        if got and got[0] == days and now - got[2] < self.HISTORY_CACHE:
            return got[1]
        url = (self.base + "/api/v1/results?"
               + urllib.parse.urlencode({"page[size]": self.HISTORY_ROWS,
                                         "sort": "-created_at"}))
        req = urllib.request.Request(
            url, headers={"User-Agent": "tempest-dashboard/" + core.VERSION,
                          "Accept": "application/json",
                          "Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = json.loads(r.read().decode("utf-8"))
        out = self.window(raw, days, now)
        with self.history_lock:
            self.history_cache = (days, out, now)
        return out

    @staticmethod
    def span(hours):
        """How long a window is, in words. Past a couple of days "168h" is
        arithmetic the reader should not have to do."""
        h = round(hours or 0) or 24
        return "%dd" % round(h / 24.0) if h >= 48 else "%dh" % h

    @classmethod
    def window(cls, raw, days, now=None):
        """Every result within the last `days`, oldest first, with the per-test
        detail the card has no room for: jitter, the server that answered, and
        the link to the test's own page on speedtest.net."""
        now = time.time() if now is None else now
        floor = now - days * 86400.0
        rnd = lambda v, dp=1: None if v is None else round(v, dp)
        pts = []
        for r in raw.get("data") or []:
            at = cls._epoch(r.get("created_at"))
            if at is None or at < floor:
                continue
            d = r.get("data") or {}
            idle, load = cls._latency(r)
            pts.append({
                "at": at,
                "ok": cls._ok(r),
                "tool": cls._tool_error(r),
                "down": rnd(cls._mbps(r.get("download_bits"), r.get("download"))),
                "up": rnd(cls._mbps(r.get("upload_bits"), r.get("upload"))),
                "ping": rnd(idle),
                "loaded": rnd(load),
                "jitter": rnd(cls._ms((d.get("ping") or {}).get("jitter")), 2),
                "loss": rnd(cls._loss(r), 2),
                "dropped": cls._dropped(r) or None,
                "server": ((d.get("server") or {}).get("name") or "") or None,
                "url": ((d.get("result") or {}).get("url") or "") or None,
            })
        pts.sort(key=lambda p: p["at"])
        return {"available": True, "error": "", "days": days,
                "fetched_at": now, "points": pts}

    def run(self):
        fails = 0
        down_since = None
        while not self.stop_event.is_set():
            try:
                fresh = self.fetch_once()
                with self.lock:
                    self.store(fresh)
                    self.error = ""
                self.after_success(fresh)
                if fails:
                    core.log(self.LABEL.lower(), "back after %d failure%s, %s"
                             % (fails, "" if fails == 1 else "s",
                                core.format_uptime(time.time() - down_since) or "a moment"),
                             "notice")
                fails = 0
                down_since = None
                wait = self.REFRESH
            except Exception as e:          # never let the thread die
                with self.lock:
                    self.error = self.describe(e)
                fails += 1
                if fails == 1:
                    # The first failure, not every retry: a flapping source
                    # must not fill the log. The recovery line says how many.
                    down_since = time.time()
                    code = getattr(e, "code", None)
                    core.log(self.LABEL.lower(), "%s — %s%s" % (
                        self.error, e.__class__.__name__,
                        " HTTP %s" % code if code else ""), "warning")
                wait = self.backoff(fails)
            self.stop_event.wait(wait)

    def snapshot(self):
        with self.lock:
            if self.data is None:
                return {"available": False, "error": self.error}
            out = dict(self.data)
            out["available"] = True
            out["error"] = self.error
            return out


class ForecastFetcher(PollingFetcher):
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

    LABEL = "Forecast"

    def __init__(self, lat, lon, stop_event, days=10, cache_path=None):
        PollingFetcher.__init__(self, stop_event)
        self.lat, self.lon, self.days = lat, lon, days
        self.cache_path = cache_path
        self._load_cache()

    def after_success(self, fresh):
        self._save_cache(fresh)

    def describe(self, exc):
        # A network failure names its reason where it has one — "timed out"
        # reads better on a card than "URLError". Anything else is a bug
        # rather than the weather service being unreachable, and is worded
        # so the two can be told apart.
        if isinstance(exc, (urllib.error.URLError, OSError, ValueError,
                            TimeoutError)):
            return "Forecast unavailable (%s)" % (
                getattr(exc, "reason", None) or exc.__class__.__name__)
        return "Forecast error (%s)" % exc.__class__.__name__

    def backoff(self, fails):
        """Deliberately not the shared policy. A forecast that is merely slow
        the first time should not leave the card blank for two minutes, so
        this starts at fifteen seconds and climbs to RETRY, where the others
        start at RETRY and climb to REFRESH."""
        return min(self.RETRY, 15 * (2 ** (fails - 1)))

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
                      "precipitation_probability_max,sunrise,sunset,"
                      "snowfall_sum,apparent_temperature_min"),
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
                # Snow, which the station cannot measure. Centimetres; the
                # page shows inches or centimetres as the reader has chosen.
                "snow_cm": pick("snowfall_sum"),
                "feels_min_c": pick("apparent_temperature_min"),
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
        # A cache restored at startup is already the answer; wait out what is
        # left of its life before asking again. Then the shared loop.
        with self.lock:
            warm = self.data
        if warm:
            left = self.REFRESH - (time.time() - warm["fetched_at"])
            if left > 0 and self.stop_event.wait(left):
                return
        PollingFetcher.run(self)

    def snapshot(self):
        with self.lock:
            if self.data is None:
                return {"available": False, "error": self.error}
            out = dict(self.data)
            out["available"] = True
            out["error"] = self.error
            out["age_s"] = time.time() - out["fetched_at"]
            return out


class AirQualityFetcher(PollingFetcher):
    """Air quality from Open-Meteo. Free, no account, no key, and it does
    cover North America — unlike their pollen, which is a European model."""

    ENDPOINT = "https://air-quality-api.open-meteo.com/v1/air-quality"
    REFRESH = 1800
    RETRY = 120
    FIELDS = ["us_aqi", "pm2_5", "pm10", "ozone", "nitrogen_dioxide",
              "sulphur_dioxide", "carbon_monoxide", "dust"]

    LABEL = "Air quality"

    def __init__(self, lat, lon, stop_event):
        PollingFetcher.__init__(self, stop_event)
        self.lat, self.lon = lat, lon

    def fetch_once(self):
        url = self.ENDPOINT + "?" + urllib.parse.urlencode({
            "latitude": "%.4f" % self.lat, "longitude": "%.4f" % self.lon,
            "current": ",".join(self.FIELDS), "timezone": "auto",
        })
        req = urllib.request.Request(
            url, headers={"User-Agent": "tempest-dashboard/" + core.VERSION})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = json.loads(r.read().decode("utf-8"))
        cur = raw.get("current") or {}
        out = {"fetched_at": time.time(), "source": "Open-Meteo"}
        for f in self.FIELDS:
            out[f] = cur.get(f)
        return out



# Statuspage's component states, mildest first. Anything a page invents later
# ranks as a minor problem rather than as fine.
STATUS_RANK = {"operational": 0, "under_maintenance": 1, "degraded_performance": 2,
               "partial_outage": 3, "major_outage": 4}
# What each state is worth to a syslog server that mails on severity: a service
# that is slow is news; one that is down, or something unheard of, is a warning.
STATUS_LEVEL = {"under_maintenance": "notice", "degraded_performance": "notice"}
# How many failing components a reading or an incident names. Enough for a
# whole service: ChatGPT's group has fifteen. It was six, and the page's
# "+3 more" then under-counted an incident that touched all of them.
PARTS_KEPT = 20


def _iso_epoch(text):
    """ISO 8601 to epoch seconds, or None. A trailing Z and fractional seconds
    of any length are taken, which Python 3.8's fromisoformat does not."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return datetime.fromisoformat(
            re.sub(r"\.\d+", "", text.strip().replace("Z", "+00:00"), count=1)).timestamp()
    except ValueError:
        return None


class StatusFeed(PollingFetcher):
    """A public status page read as JSON, and turned by `parse` into a word.
    The address can be replaced from the settings, in case a page moves."""

    REFRESH = 120
    RETRY = 60
    ENDPOINT = ""
    NAME = ""

    def __init__(self, stop_event, url=None, record=None):
        PollingFetcher.__init__(self, stop_event)
        self.url = url or self.ENDPOINT
        self.record = record          # told of every reading, for the reliability page
        self.note = ""                # why the last refinement failed, for the log
        self._not_ok_since = None
        self._record_failed = False

    def after_success(self, fresh):
        """Outside the lock, and outside the fetch's own error handling: a
        record that could not be kept is not the status page being down."""
        if not self.record:
            return
        try:
            self.record(fresh)
        except Exception as e:
            if not self._record_failed:
                self._record_failed = True
                core.log(self.LABEL.lower(), "could not record the reading — %s" %
                         e.__class__.__name__, "warning")

    def store(self, fresh):
        """Say so when the service changes state, as the alerts do: not on every
        poll, and nothing at all while it stays fine. A service found already
        down at start is news; one found fine is not."""
        was = self.data or {}
        old, new = was.get("status", "operational"), fresh["status"]
        tag = self.LABEL.lower()
        words = lambda st: st.replace("_", " ")
        if new != old:
            if new == "operational":
                core.log(tag, "back to operational after %s" % (
                    core.format_uptime(time.time() - (self._not_ok_since or time.time())) or "a moment"), "notice")
                self._not_ok_since = None
            else:
                if old == "operational":
                    self._not_ok_since = time.time()
                core.log(tag, "%s → %s" % (words(old), words(new)),
                         STATUS_LEVEL.get(new, "warning"))
        # Which of the page's components were counted, when that can change.
        via = fresh.get("via")
        if via and via != was.get("via", "group"):
            core.log(tag, "components chosen from %s%s" % (
                VIA_WORDS[via], " (%s)" % self.note if self.note and via == "names" else ""),
                "notice" if via == "group" else "warning")
        PollingFetcher.store(self, fresh)

    @staticmethod
    def _read(url, accept):
        req = urllib.request.Request(
            url, headers={"User-Agent": "tempest-dashboard/" + core.VERSION, "Accept": accept})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8")

    @staticmethod
    def get(url):
        return json.loads(StatusFeed._read(url, "application/json"))

    @staticmethod
    def get_text(url):
        return StatusFeed._read(url, "text/html")

    def fetch_once(self):
        return self.parse(self.get(self.url))

    def snapshot(self):
        """With how often the page is read, so the card's heartbeat knows how
        long a beat takes to cross it without keeping its own copy."""
        out = PollingFetcher.snapshot(self)
        out["every"] = self.REFRESH
        return out

    def describe(self, exc):
        if isinstance(exc, ValueError):
            return "%s: %s" % (self.LABEL, exc)
        return PollingFetcher.describe(self, exc)


class StatuspageFetcher(StatusFeed):
    """Whether one AI service is up, from its public status page.

    Anthropic and OpenAI both serve Statuspage's keyless JSON. It is polled
    rather than subscribed to: a webhook would need the status page to reach
    this server, and this one lives on a LAN address.

    The result is one word: the worst state among the components that count.
    """

    PARTS = None          # the names of the components that count; None is all

    @classmethod
    def parse(cls, raw, now=None, ids=None):
        # Component groups are headings, not things that can be down.
        real = [c for c in raw.get("components") or [] if not c.get("group")]
        # By id when the page's own grouping is to hand: names can repeat.
        # Otherwise, or if no id matched, by name.
        comps = [c for c in real if c.get("id") in ids] if ids else []
        via = "group" if comps else None
        if not comps:
            comps = [c for c in real if cls.PARTS is None or c.get("name") in cls.PARTS]
            via = None if cls.PARTS is None else "names"
        if not comps:
            # Not "operational": a renamed component must not read as fine.
            raise ValueError("none of its components were found")
        worst = max((c.get("status") or "operational" for c in comps),
                    key=lambda st: STATUS_RANK.get(st, 2))
        parts = sorted({c.get("name") or "" for c in comps
                        if (c.get("status") or "operational") != "operational"})
        return {"fetched_at": time.time() if now is None else now, "status": worst,
                "via": via, "parts": parts[:PARTS_KEPT]}



class ClaudeStatusFetcher(StatuspageFetcher):
    ENDPOINT = "https://status.claude.com/api/v2/summary.json"
    HISTORY = "https://status.claude.com/api/v2/incidents.json"
    LABEL = "Claude status"
    NAME = "Claude"
    IMPACT = {"minor": "degraded_performance", "major": "partial_outage",
              "critical": "major_outage"}

    @classmethod
    def parse_history(cls, raw):
        """The past incidents from Statuspage's list: (events, the earliest
        moment the list is complete from). An event is when one began and
        ended, how bad it got, and which components it touched. Claude counts
        every component, so every incident on its page is its own."""
        incidents = raw.get("incidents") or []
        starts = [_iso_epoch(i.get("started_at") or i.get("created_at")) for i in incidents]
        starts = [t for t in starts if t is not None]
        if not starts:
            raise ValueError("its history has no incidents to go on")
        events = []
        for i in incidents:
            start = _iso_epoch(i.get("started_at") or i.get("created_at"))
            end = _iso_epoch(i.get("resolved_at"))         # one still open is the live record's
            if start is None or end is None or end <= start:
                continue
            seen = [ac.get("new_status") for u in i.get("incident_updates") or []
                    for ac in u.get("affected_components") or []
                    if ac.get("new_status") not in (None, "operational")]
            worst = (max(seen, key=lambda st: STATUS_RANK.get(st, 2)) if seen
                     else cls.IMPACT.get(i.get("impact")))
            if worst is None:                              # an impact of "none" is a notice
                continue
            events.append({"start": start, "end": end, "worst": worst,
                           "parts": sorted({c.get("name") or ""
                                            for c in i.get("components") or []})[:PARTS_KEPT]})
        return events, min(starts)

    def history(self):
        return self.parse_history(self.get(self.HISTORY))


class ChatGptStatusFetcher(StatuspageFetcher):
    """OpenAI's summary lists ChatGPT flat, beside its API and Codex, and two
    of them are both called "Login". Which belong to ChatGPT is what the
    status page's own layout says, which it serves separately, so that is read
    for the ids. If it cannot be, the names below are the fallback, and both
    Logins then count. If neither finds anything this fails loudly, as "none of
    its components were found", rather than going green."""

    ENDPOINT = "https://status.openai.com/api/v2/summary.json"
    PAGE = "https://status.openai.com/"
    LAYOUT = "https://status.openai.com/proxy/status.openai.com"
    GROUP = "ChatGPT"
    LABEL = "ChatGPT status"
    NAME = "ChatGPT"
    PARTS = frozenset({"Conversations", "Login", "ChatGPT Work", "Codex in ChatGPT Desktop",
                       "Compliance API", "Search", "File uploads", "Voice mode", "GPTs",
                       "Image Generation", "Deep Research", "Agent", "ChatGPT Atlas",
                       "Sites", "Connectors/Apps"})

    @classmethod
    def group_parts(cls, layout):
        """{component id: name} for what the page lists under ChatGPT, or None."""
        items = ((layout.get("summary") or {}).get("structure") or {}).get("items") or []
        for item in items:
            group = item.get("group") or {}
            if group.get("name") == cls.GROUP:
                return {c.get("component_id"): c.get("name") or ""
                        for c in group.get("components") or []} or None
        return None

    @classmethod
    def group_ids(cls, layout):
        """The component ids the page lists under ChatGPT, or None."""
        return set(cls.group_parts(layout) or ()) or None

    def fetch_once(self):
        raw = self.get(self.url)
        try:
            ids = self.group_ids(self.get(self.LAYOUT))
            self.note = ""
        except Exception as e:
            ids = None        # only a refinement: the names still answer
            self.note = e.__class__.__name__
        return self.parse(raw, ids=ids)

    # OpenAI's incident list says nothing of components, so it cannot say which
    # incidents were ChatGPT's. The page's own bars can: they are drawn from a
    # list of impacts, one per component, embedded in the page's data.
    STATUSES = {"full_outage": "major_outage"}

    @staticmethod
    def _flight(html):
        """The page's own data. Next.js streams it as JSON strings in script
        tags, split across many, and one value can straddle two."""
        return "".join(json.loads(m.group(1)) for m in re.finditer(
            r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', html))

    @classmethod
    def parse_page_history(cls, html, parts, now=None):
        """ChatGPT's past incidents from the status page itself: (events, the
        earliest moment they are complete from). Each impact on the page names
        a component, when it began and ended, and how bad; the ones on the
        components in `parts` (id: name), joined per incident where they touch
        or overlap, are the events. The page draws its bars from that list, so
        it is complete from its oldest impact, on any component, and not
        further back than the window the page says it shows."""
        now = time.time() if now is None else now
        flight = cls._flight(html)
        tail = flight.find('"component_uptimes"')
        head = flight.rfind('"component_impacts":', 0, tail) if tail >= 0 else -1
        if head < 0:
            raise ValueError("the page holds no component history")
        head += len('"component_impacts":')
        try:
            impacts, end = json.JSONDecoder().raw_decode(flight, head)
        except ValueError:
            raise ValueError("the page's component history could not be read")
        # The page-level list is the one that sits beside the uptime figures;
        # an incident's own list is not the whole record.
        if not isinstance(impacts, list) or not flight[end:].startswith(',"component_uptimes"'):
            raise ValueError("the page's component history is not where it was")
        if not impacts:
            raise ValueError("the page's component history is empty")
        oldest = [t for t in (_iso_epoch(i.get("start_at")) for i in impacts) if t is not None]
        if not oldest:
            raise ValueError("the page's component history has no dates")
        m = re.search(r'"history_window_days":(\d+)', flight)
        by_incident = {}
        for imp in impacts:
            cid, t0 = imp.get("component_id"), _iso_epoch(imp.get("start_at"))
            if cid not in parts or t0 is None:
                continue
            t1 = _iso_epoch(imp.get("end_at"))              # "$undefined" while it goes on
            by_incident.setdefault(imp.get("status_page_incident_id") or imp.get("id"), []).append(
                (t0, now if t1 is None else t1,
                 cls.STATUSES.get(imp.get("status"), imp.get("status")), parts[cid]))
        worse = lambda a, b: max((a, b), key=lambda st: STATUS_RANK.get(st, 2))
        events = []
        for spans in by_incident.values():
            spans.sort(key=lambda x: x[0])
            run = None
            for t0, t1, st, name in spans:
                if run and t0 <= run["end"]:
                    run["end"], run["worst"] = max(run["end"], t1), worse(run["worst"], st)
                    run["parts"].add(name)
                else:
                    run = {"start": t0, "end": t1, "worst": st, "parts": {name}}
                    events.append(run)
        events = [dict(e, parts=sorted(e["parts"])[:PARTS_KEPT]) for e in events
                  if e["end"] > e["start"] and e["worst"] != "operational"]
        events.sort(key=lambda e: e["start"])
        return events, max(min(oldest), now - (int(m.group(1)) if m else 90) * 86400)

    def history(self):
        parts = self.group_parts(self.get(self.LAYOUT))
        if not parts:
            raise ValueError("the page's own layout does not say which components are ChatGPT's")
        return self.parse_page_history(self.get_text(self.PAGE), parts)


class GeminiStatusFetcher(StatusFeed):
    """The Gemini app, from Google's Workspace status dashboard.

    That feed is a list of incidents, each naming the products it touches.
    There is no standing "operational": Gemini is fine unless an incident on
    it is still open, and an incident is over once its latest update says
    AVAILABLE. An outage is red; anything else still open, a disruption or
    a notice, is amber.
    """

    ENDPOINT = "https://www.google.com/appsstatus/dashboard/incidents.json"
    PRODUCT = "npdyhgECDJ6tB66MxXyo"
    LABEL = "Gemini status"
    NAME = "Gemini"

    @classmethod
    def parse_history(cls, raw):
        """The past incidents from the same feed: (events, the earliest moment
        it is complete from). The feed lists every Workspace product's, so the
        earliest date is taken across all of them, and only Gemini's that are
        over count."""
        if not isinstance(raw, list):
            raise ValueError("unexpected response")
        begins = [t for t in (_iso_epoch(i.get("begin")) for i in raw) if t is not None]
        if not begins:
            raise ValueError("its history has no incidents to go on")
        events = []
        for i in raw:
            if not any(p.get("id") == cls.PRODUCT for p in i.get("affected_products") or []):
                continue
            if (i.get("most_recent_update") or {}).get("status") != "AVAILABLE":
                continue
            start, end = _iso_epoch(i.get("begin")), _iso_epoch(i.get("end"))
            if start is None or end is None or end <= start:
                continue
            events.append({"start": start, "end": end, "parts": [],
                           "worst": "major_outage" if i.get("status_impact") == "SERVICE_OUTAGE"
                           else "degraded_performance"})
        return events, min(begins)

    def history(self):
        return self.parse_history(self.get(self.url))

    @classmethod
    def parse(cls, raw, now=None):
        if not isinstance(raw, list):
            raise ValueError("unexpected response")
        worst = "operational"
        for i in raw:
            if not any(p.get("id") == cls.PRODUCT for p in i.get("affected_products") or []):
                continue
            last = i.get("most_recent_update") or {}
            if last.get("status") == "AVAILABLE":
                continue
            level = "major_outage" if last.get("status") == "SERVICE_OUTAGE" \
                else "degraded_performance"
            if STATUS_RANK[level] > STATUS_RANK[worst]:
                worst = level
        return {"fetched_at": time.time() if now is None else now, "status": worst,
                "parts": []}


# How a service's components were chosen, for Check now to say.
VIA_WORDS = {"group": "the page's own group",
             "names": "the fixed list of names: the page's grouping was not usable"}

AI_SERVICES = {"claude": ClaudeStatusFetcher, "chatgpt": ChatGptStatusFetcher,
               "gemini": GeminiStatusFetcher}


class AiHistory:
    """How often each AI service has been degraded or down, as its own status
    page reported it.

    That is not the same as whether it worked for you: a page is updated late,
    and a blip too short to post never appears on it. What is kept is what this
    dashboard saw, every couple of minutes, and nothing is assumed about the
    time it did not see. Between two sightings closer than GAP the time counts
    towards what the first one said; a longer silence, a deploy or the page
    being unreachable, counts towards nothing, so it cannot flatter the score.

    Two things are kept per service. The days: seconds seen operational,
    degraded and down, per local date. And the events: when it left
    operational, how bad it got, which components, and when it came back, with
    the ends flagged approximate when a silence hid them. An event still open
    at a restart is carried over.

    It is written the way the daily record is, through the History's own
    writer, so a save that fails is kept where the banner and /healthz read it,
    and a file that will not parse is set aside and never treated as empty.
    """

    FILE = "ai_status_log.json"
    GAP = 6 * 60              # seconds; past this between sightings, the time is not counted
    SAVE_EVERY = 5 * 60       # a change is saved at once, a quiet stretch this often
    DAYS_KEPT = 400
    EVENTS_KEPT = 300
    WINDOWS = (7, 30, 90)

    def __init__(self, path, history):
        self.path = path
        self.history = history
        self.lock = threading.Lock()
        self.since = None
        self.services = {}
        self.locked = False       # never write over a file we could not read or move
        self._saved = 0.0
        self.load()

    # ── persistence ───────────────────────────────────────────────────────
    @staticmethod
    def _clean_service(d):
        days = {}
        for day, row in (d.get("days") or {}).items():
            days[str(day)] = {k: float(row.get(k, 0)) for k in ("ok", "deg", "out")}
        events = []
        for e in d.get("events") or []:
            ev = {"start": float(e["start"]),
                  "end": None if e.get("end") is None else float(e["end"]),
                  "worst": str(e["worst"]),
                  "parts": [str(p) for p in e.get("parts") or []][:PARTS_KEPT],
                  "approx_start": bool(e.get("approx_start")),
                  "approx_end": bool(e.get("approx_end"))}
            if e.get("src"):
                ev["src"] = str(e["src"])
            events.append(ev)
        last = d.get("last")
        if last is not None:
            last = {"at": float(last["at"]), "status": str(last["status"])}
        bf = d.get("backfill")
        if bf is not None:
            bf = {"at": float(bf["at"]), "from": str(bf["from"]), "events": int(bf["events"]),
                  "keeps": int(bf.get("keeps", 1))}
        return {"days": days, "events": events, "last": last, "backfill": bf}

    @staticmethod
    def _blank():
        return {"days": {}, "events": [], "last": None, "backfill": None}

    def load(self):
        raw, problem = self.history._read_json(self.path)
        if problem == "missing":
            return
        if problem is None:
            try:
                since = raw.get("since")
                services = {k: self._clean_service(v)
                            for k, v in (raw.get("services") or {}).items() if k in AI_SERVICES}
                self.since = None if since is None else float(since)
                self.services = services
                return
            except (KeyError, TypeError, ValueError, AttributeError):
                problem = "damaged (not a record)"
        aside = self.history._set_aside(self.path)
        self.locked = aside is None
        self.history._note("%s is %s; %s" % (
            self.FILE, problem,
            "kept aside as %s and starting again" % os.path.basename(aside) if aside
            else "left where it is, and nothing will be written over it"))

    def _save(self, force):
        now = time.time()
        if self.locked or (not force and now - self._saved < self.SAVE_EVERY):
            return
        self._saved = now
        for s in self.services.values():
            for day in sorted(s["days"])[:-self.DAYS_KEPT]:
                del s["days"][day]
            del s["events"][:-self.EVENTS_KEPT]
        self.history._write_json(self.path, {"version": 1, "saved_at": now,
                                             "since": self.since, "services": self.services})

    # ── recording ─────────────────────────────────────────────────────────
    @staticmethod
    def _bucket(status):
        if status == "operational":
            return "ok"
        return "out" if STATUS_RANK.get(status, 2) >= 3 else "deg"

    @staticmethod
    def _credit(s, t0, t1, bucket):
        """Add the time from t0 to t1 to its local days, split at midnight."""
        while t0 < t1:
            d = datetime.fromtimestamp(t0)
            nxt = time.mktime((d.date() + timedelta(days=1)).timetuple())
            end = min(t1, nxt)
            row = s["days"].setdefault(d.strftime("%Y-%m-%d"), {"ok": 0.0, "deg": 0.0, "out": 0.0})
            row[bucket] += end - t0
            t0 = end

    def observe(self, key, fresh):
        """One reading of one service: {"fetched_at", "status", "parts"}."""
        t, status = fresh["fetched_at"], fresh["status"]
        parts = list(fresh.get("parts") or [])
        with self.lock:
            s = self.services.setdefault(key, self._blank())
            last = s["last"]
            # A first sighting, a long silence, or a clock that went backwards.
            silent = last is None or not 0 <= t - last["at"] <= self.GAP
            if not silent:
                self._credit(s, last["at"], t, self._bucket(last["status"]))
            if self.since is None:
                self.since = t
            open_ = s["events"][-1] if s["events"] and s["events"][-1]["end"] is None else None
            changed = False
            if status != "operational":
                if open_ is None:
                    s["events"].append({"start": t, "end": None, "worst": status,
                                        "parts": parts[:PARTS_KEPT],
                                        "approx_start": silent, "approx_end": False})
                    changed = True
                else:
                    if STATUS_RANK.get(status, 2) > STATUS_RANK.get(open_["worst"], 2):
                        open_["worst"] = status
                        changed = True
                    for p in parts:
                        if p not in open_["parts"] and len(open_["parts"]) < PARTS_KEPT:
                            open_["parts"].append(p)
                            changed = True
            elif open_ is not None:
                open_["end"], open_["approx_end"] = t, silent
                changed = True
            s["last"] = {"at": t, "status": status}
            self._save(changed)

    # ── history ───────────────────────────────────────────────────────────
    @staticmethod
    def _day_seconds(day):
        d = datetime.strptime(day, "%Y-%m-%d").date()
        return time.mktime((d + timedelta(days=1)).timetuple()) - time.mktime(d.timetuple())

    # Raise this when what a backfill keeps changes, as Backfill.SWEEP_KEEPS is
    # for the WeatherFlow sweep: a history filled the old way is filled again.
    # 2: all of an incident's components, not the first six.
    BACKFILL_KEEPS = 2

    def backfilled(self, key):
        with self.lock:
            bf = (self.services.get(key) or {}).get("backfill")
            return bool(bf) and bf.get("keeps") == self.BACKFILL_KEEPS

    def backfill(self, key, events, covered_from, now=None):
        """Fill in the days before the live record began, from the service's
        own list of past incidents. `covered_from` is when that list is
        complete from: days before it are left alone, since a day with no
        incident on a list that does not reach it proves nothing.

        Only days before the first live day are written, and they are set, not
        added to, so doing this twice, or after a crash half way, changes
        nothing. Overlapping incidents count once, at the worse state. False
        when the live record has not begun, to be tried again."""
        with self.lock:
            if self.since is None:
                return False
            s = self.services.setdefault(key, self._blank())
            first = date.fromtimestamp(covered_from)
            first_live = date.fromtimestamp(self.since)
            floor = time.mktime(first.timetuple())
            ceiling = time.mktime(first_live.timetuple())     # where the live days begin
            spans = [(max(e["start"], floor), min(e["end"], ceiling), self._bucket(e["worst"]))
                     for e in events]
            spans = [x for x in spans if x[1] > x[0]]
            tmp = {"days": {}}
            edges = sorted({t for a, b, _ in spans for t in (a, b)})
            for a, b in zip(edges, edges[1:]):
                kinds = [k for s0, s1, k in spans if s0 <= a and b <= s1]
                if kinds:
                    self._credit(tmp, a, b, "out" if "out" in kinds else "deg")
            day = first
            while day < first_live:
                k = day.strftime("%Y-%m-%d")
                row = tmp["days"].get(k, {"ok": 0.0, "deg": 0.0, "out": 0.0})
                row["ok"] = max(0.0, self._day_seconds(k) - row["deg"] - row["out"])
                s["days"][k] = row
                day += timedelta(days=1)
            # History covers whole days. An incident that ran on into the live
            # days is the live record's; only its part before them is counted.
            past = [dict(e, approx_start=False, approx_end=False, src="history")
                    for e in events if e["end"] <= ceiling]
            s["events"] = sorted(past + [e for e in s["events"] if e.get("src") != "history"],
                                 key=lambda e: e["start"])
            s["backfill"] = {"at": time.time() if now is None else now,
                             "from": first.isoformat(), "events": len(past),
                             "keeps": self.BACKFILL_KEEPS}
            self._save(True)
            return True

    # ── reporting ─────────────────────────────────────────────────────────
    def report(self, now=None, watching=()):
        """Per service: each of the last 90 days, the windows summed, and the
        recent incidents. The date arithmetic is here, where it is tested."""
        now = time.time() if now is None else now
        today = date.fromtimestamp(now)
        keys = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(max(self.WINDOWS) - 1, -1, -1)]
        out = {"available": False, "error": "Nothing recorded yet", "since": None,
               "covered_from": None, "watched_days": 0, "services": {}}
        with self.lock:
            if self.since is not None:
                # As far back as any service has anything: its history's first
                # day, or the first live one.
                begins = [date.fromtimestamp(self.since)] + [
                    date.fromisoformat(v["backfill"]["from"]) for v in self.services.values()
                    if v.get("backfill")]
                first = min(begins)
                out.update(available=True, error="", since=self.since,
                           covered_from=first.isoformat(),
                           watched_days=max(1, (today - first).days + 1))
            for key, cls in AI_SERVICES.items():
                s = self.services.get(key)
                if s is None and key not in watching:
                    continue
                s = s or self._blank()
                rows = [dict(date=k, **s["days"].get(k, {"ok": 0.0, "deg": 0.0, "out": 0.0}))
                        for k in keys]
                windows = {}
                for w in self.WINDOWS:
                    tot = {b: sum(r[b] for r in rows[-w:]) for b in ("ok", "deg", "out")}
                    t0 = time.mktime(datetime.strptime(keys[-w], "%Y-%m-%d").timetuple())
                    began = [e for e in s["events"] if e["start"] >= t0]
                    tot.update(observed=tot["ok"] + tot["deg"] + tot["out"], days=w,
                               deg_events=sum(1 for e in began if self._bucket(e["worst"]) == "deg"),
                               out_events=sum(1 for e in began if self._bucket(e["worst"]) == "out"))
                    windows[str(w)] = tot
                out["services"][key] = {
                    "name": cls.NAME, "watching": key in watching,
                    "backfill": s.get("backfill"),
                    "status": (s["last"] or {}).get("status"),
                    "days": rows, "windows": windows,
                    "incidents": [dict(e, open=e["end"] is None) for e in s["events"][-25:][::-1]]}
        return out


class PollenFetcher(PollingFetcher):
    """Tree, grass and weed pollen from the Google Pollen API.

    No free keyless source covers North America — Open-Meteo's pollen model
    is European, and the one keyless US endpoint is undocumented and gated.
    So this needs a key. It is optional: without one the thread never starts
    and the card says what is missing. The key is handled like the
    WeatherFlow token — stored server-side, never returned to a browser.
    """

    ENDPOINT = "https://pollen.googleapis.com/v1/forecast:lookup"
    REFRESH = 3 * 3600        # pollen moves slowly; be frugal with quota
    RETRY = 900
    TYPES = {"TREE": "tree", "GRASS": "grass", "WEED": "weed"}

    LABEL = "Pollen"

    def __init__(self, lat, lon, key, stop_event):
        PollingFetcher.__init__(self, stop_event)
        self.lat, self.lon, self.key = lat, lon, key

    def describe(self, exc):
        # A rejected key and an exhausted quota are worth saying plainly:
        # both are things the owner can act on, and neither is transient.
        if isinstance(exc, urllib.error.HTTPError):
            if exc.code in (400, 401, 403):
                return ("Pollen key rejected — check it is valid "
                        "and the Pollen API is enabled")
            if exc.code == 429:
                return "Pollen quota exceeded"
            return "Pollen unavailable (HTTP %s)" % exc.code
        return PollingFetcher.describe(self, exc)

    @staticmethod
    def _index(info):
        """(value, category) from an indexInfo block, tolerating absence."""
        idx = (info or {}).get("indexInfo") or {}
        val = idx.get("value")
        try:
            val = None if val is None else float(val)
        except (TypeError, ValueError):
            val = None
        return val, idx.get("category") or ""

    def fetch_once(self):
        url = self.ENDPOINT + "?" + urllib.parse.urlencode({
            "key": self.key,
            "location.latitude": "%.4f" % self.lat,
            "location.longitude": "%.4f" % self.lon,
            "days": "1",
            "languageCode": "en",
        })
        req = urllib.request.Request(
            url, headers={"User-Agent": "tempest-dashboard/" + core.VERSION,
                          "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return self.parse(json.loads(r.read().decode("utf-8")))

    @classmethod
    def parse(cls, raw):
        """Pull the response apart. Split out from the request so it can be
        exercised against a real payload — several fields are simply absent
        when a pollen type is out of season, rather than present and zero."""
        days = raw.get("dailyInfo") or []
        if not days:
            raise ValueError("no dailyInfo in response")
        day = days[0]

        out = {"fetched_at": time.time(), "source": "Google Pollen",
               "region": raw.get("regionCode") or "",
               "tree": None, "grass": None, "weed": None,
               "categories": {}, "plants": []}
        for info in (day.get("pollenTypeInfo") or []):
            key = cls.TYPES.get(info.get("code"))
            if not key:
                continue
            val, cat = cls._index(info)
            out[key] = val
            if cat:
                out["categories"][key] = cat

        # Which plants are actually in season, worst first — more useful
        # than three abstract indices on their own.
        plants = []
        for info in (day.get("plantInfo") or []):
            if not info.get("inSeason"):
                continue
            val, cat = cls._index(info)
            if val is None:
                continue
            plants.append({"name": info.get("displayName") or info.get("code"),
                           "value": val, "category": cat})
        plants.sort(key=lambda p: p["value"], reverse=True)
        out["plants"] = plants[:3]
        return out

    def snapshot(self):
        with self.lock:
            if self.data is None:
                return {"available": False, "error": self.error}
            out = dict(self.data)
            out["available"] = True
            out["error"] = self.error
            return out


class SpeedtestFetcher(PollingFetcher):
    """Internet health, from a self-hosted Speedtest Tracker on the LAN.

    Speedtest Tracker already stores the history, so this pulls the last
    day of results in one request rather than keeping a second copy: the
    newest entry is the card's headline, and the rest of the window is
    what "any problems lately?" is answered from.

    The interesting number is not the speed. A connection that has gone
    bad usually still tests fast; what changes is latency *under load*
    (bufferbloat) and packet loss. Both are in the payload already.

    The token is handled like the WeatherFlow token — server-side only,
    never returned to a browser.
    """

    REFRESH = 300             # tests run hourly; this is just staleness
    RETRY = 60
    # A week, not a day: one bad afternoon says little, and a week says
    # whether the line is getting worse. 200 rows covers seven days of
    # hourly tests with room to spare.
    WINDOW = 200              # results to pull
    WINDOW_H = 168.0          # and how far back those results may reach
    DOWN_AFTER = 3            # consecutive failures before the line is "down"

    # What counts as a problem worth putting on a wall display.
    LOSS_PCT = 1.0            # packet loss above this is not noise
    BLOAT_MS = 100.0          # added latency under load
    # What the tool can report and mean. The Ookla CLI once recorded an upload
    # latency of 3,251,667,954 ms — thirty-seven days — with a jitter of 2.9
    # million, on a test that moved 816 Mbps: a broken measurement, not a
    # slow line, and it set the week's "worst" and stretched the chart to
    # match. No latency the line could have is over ten seconds. And its
    # packet-loss probe, a separate UDP stream, said 86.6% on a test that
    # moved 754 Mbps: TCP cannot do that through real loss on that scale
    # (a few Mbps per flow at 20%), so past LOSS_ABSURD_PCT the figure is
    # believed only when the test itself was slow enough to be consistent.
    LATENCY_MAX_MS = 10000.0
    LOSS_ABSURD_PCT = 10.0
    LOSS_CREDIBLE_BELOW_MBPS = 100.0
    JITTER_MS = 30.0
    PLAN_FRAC = 0.5           # this fraction of the advertised rate

    # The Internet page's week, fetched when someone opens it rather than
    # every two seconds: 400 rows covers a fortnight of hourly tests, and the
    # answer is held for five minutes so reopening the page is free.
    HISTORY_DAYS = 7
    HISTORY_ROWS = 400
    HISTORY_CACHE = 300

    def __init__(self, base_url, token, stop_event,
                 plan_down=0.0, plan_up=0.0):
        PollingFetcher.__init__(self, stop_event)
        self.base = (base_url or "").rstrip("/")
        self.token = token
        self.plan_down = plan_down or 0.0
        self.plan_up = plan_up or 0.0
        self.history_lock = threading.Lock()
        self.history_cache = None        # (days, payload, fetched_at)

    def describe(self, exc):
        # A token without the right ability is a setup mistake, not an
        # outage, and saying so saves a round of guessing.
        if isinstance(exc, urllib.error.HTTPError):
            if exc.code in (401, 403):
                return ("Speedtest token rejected — it needs "
                        "the results:read ability")
            if exc.code == 406:
                return "Speedtest Tracker refused the request"
            return "Speedtest unavailable (HTTP %s)" % exc.code
        return PollingFetcher.describe(self, exc)

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _epoch(text):
        """ISO 8601 to epoch seconds. Python 3.8's fromisoformat will not
        take a trailing Z, and the API sends one."""
        if not isinstance(text, str) or not text:
            return None
        t = text.strip().replace("Z", "+00:00")
        if "." in t:                       # trim fractional seconds
            head, _, tail = t.partition(".")
            keep = ""
            for ch in tail:
                if not ch.isdigit():
                    keep = tail[tail.index(ch):]
                    break
            t = head + keep
        try:
            return datetime.fromisoformat(t).timestamp()
        except ValueError:
            return None

    @staticmethod
    def _num(v):
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _mbps(cls, bits, bytes_per_s):
        """Prefer the bits field; fall back to bytes/s x 8."""
        v = cls._num(bits)
        if v is not None and v > 0:
            return v / 1e6
        v = cls._num(bytes_per_s)
        return None if v is None else v * 8 / 1e6

    @classmethod
    def _trim(cls, rows):
        """Drop anything reaching further back than WINDOW_H before the newest
        result. A fixed number of rows is asked for, so a gap in the history
        would otherwise stretch "the last day" to weeks. Rows whose timestamp
        will not parse are kept: a bad date is no reason to discard a result."""
        stamped = [(r, cls._epoch(r.get("created_at"))) for r in rows]
        newest = max((s for _, s in stamped if s), default=None)
        if newest is None:
            return list(rows)
        floor = newest - cls.WINDOW_H * 3600
        return [r for r, s in stamped if s is None or s >= floor]

    @staticmethod
    def _tool_error(row):
        """A test that never ran: the Ookla CLI itself fell over ("Cannot read
        from socket", "An unexpected error occurred…") and the tracker stored
        its log line instead of a result. That says nothing about the line —
        a week of them scattered across every hour is the tool being flaky,
        not an outage. An outage shows as a run of them, which the
        consecutive-failures rule still catches, because they are still not
        successes."""
        if str(row.get("status") or "").lower() == "completed":
            return False
        d = row.get("data") or {}
        return isinstance(d, dict) and d.get("type") == "log"

    @staticmethod
    def _ok(row):
        status = str(row.get("status") or "").lower()
        return status in ("", "completed") and row.get("download") is not None

    @classmethod
    def _latency(cls, row):
        """Idle ping and the worst latency while the line is saturated, in ms.
        The gap between the two is the bufferbloat, which is what actually
        makes a connection feel broken."""
        d = row.get("data") or {}
        idle = cls._ms((d.get("ping") or {}).get("latency"))
        if idle is None:
            idle = cls._ms(row.get("ping"))
        loaded = None
        for leg in ("download", "upload"):
            iqm = cls._ms(((d.get(leg) or {}).get("latency") or {}).get("iqm"))
            if iqm is not None and (loaded is None or iqm > loaded):
                loaded = iqm
        return idle, loaded

    @classmethod
    def _ms(cls, v):
        """A latency or jitter in ms, or None when the tool's figure is not
        one the line could produce. A leg that overflowed is simply absent;
        the other leg still counts."""
        v = cls._num(v)
        return None if v is None or v < 0 or v > cls.LATENCY_MAX_MS else v

    @classmethod
    def _loss(cls, row):
        """Packet loss in percent, or None when the probe's figure cannot be
        believed: outside 0–100, or heavy loss on a test that was fast enough
        to prove the packets were getting through."""
        d = row.get("data") or {}
        loss = cls._num(d.get("packetLoss"))
        if loss is None or loss < 0 or loss > 100:
            return None
        if loss > cls.LOSS_ABSURD_PCT:
            down = cls._mbps(row.get("download_bits"), row.get("download"))
            if down is not None and down >= cls.LOSS_CREDIBLE_BELOW_MBPS:
                return None
        return loss

    @classmethod
    def _dropped(cls, row):
        """Which of a test's figures were thrown out as the tool's mistake,
        so the page can say so rather than quietly showing a dash."""
        d = row.get("data") or {}
        out = []
        raw_loss = cls._num(d.get("packetLoss"))
        if raw_loss is not None and cls._loss(row) is None:
            out.append("loss")
        for leg in ("download", "upload"):
            raw = cls._num(((d.get(leg) or {}).get("latency") or {}).get("iqm"))
            if raw is not None and cls._ms(raw) is None:
                out.append(leg + " latency")
        if cls._num((d.get("ping") or {}).get("jitter")) is not None \
                and cls._ms((d.get("ping") or {}).get("jitter")) is None:
            out.append("jitter")
        return out

    # -- fetch ------------------------------------------------------------
    def fetch_once(self):
        url = (self.base + "/api/v1/results?"
               # JSON:API paging: page[size], not per_page or per.page, both
               # of which the tracker ignores — it was returning its default
               # 25 and happening to be enough.
               + urllib.parse.urlencode({"page[size]": self.WINDOW,
                                         "sort": "-created_at"}))
        req = urllib.request.Request(
            url, headers={"User-Agent": "tempest-dashboard/" + core.VERSION,
                          "Accept": "application/json",
                          "Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.loads(r.read().decode("utf-8"))
        return self.parse(raw, self.plan_down, self.plan_up)

    @classmethod
    def parse(cls, raw, plan_down=0.0, plan_up=0.0):
        rows = raw.get("data")
        if not isinstance(rows, list):
            raise ValueError("no result list in response")
        rows = cls._trim(rows)

        out = {"fetched_at": time.time(), "tests": len(rows),
               "failures": 0, "issues": [], "status": "unknown",
               "plan_down": plan_down or None, "plan_up": plan_up or None}

        # Tests that ran and failed, as distinct from tests that never ran.
        failures = [r for r in rows if not cls._ok(r) and not cls._tool_error(r)]
        out["skipped"] = sum(1 for r in rows if cls._tool_error(r))
        good = [r for r in rows if cls._ok(r)]
        out["failures"] = len(failures)

        if good:
            downs = [cls._mbps(r.get("download_bits"), r.get("download"))
                     for r in good]
            ups = [cls._mbps(r.get("upload_bits"), r.get("upload"))
                   for r in good]
            downs = [v for v in downs if v is not None]
            ups = [v for v in ups if v is not None]
            out["avg_down"] = sum(downs) / len(downs) if downs else None
            out["avg_up"] = sum(ups) / len(ups) if ups else None

        stamps = [cls._epoch(r.get("created_at")) for r in rows]
        stamps = [s for s in stamps if s]
        out["window_h"] = ((max(stamps) - min(stamps)) / 3600.0
                           if len(stamps) > 1 else 0.0)

        # The per-test series behind the card's detail view. Every row the
        # window holds is already in hand, so this costs no extra request —
        # and failed tests are kept, because a gap in the line is the whole
        # point of looking. Oldest first, so it plots left to right.
        def rnd(v, dp=1):
            return None if v is None else round(v, dp)

        history = []
        for r in reversed(rows):
            idle, load = cls._latency(r)
            history.append({
                "at": cls._epoch(r.get("created_at")),
                "ok": cls._ok(r),
                "tool": cls._tool_error(r),
                "ping": rnd(idle),
                "loaded": rnd(load),
                "loss": rnd(cls._loss(r), 2),
                "down": rnd(cls._mbps(r.get("download_bits"), r.get("download"))),
                "up": rnd(cls._mbps(r.get("upload_bits"), r.get("upload")))})
        out["history"] = history

        latest = good[0] if good else None
        if latest is None:
            out["status"] = "down"
            out["issues"].append("No successful test in the window")
            return out

        d = latest.get("data") or {}
        ping = d.get("ping") or {}
        out["at"] = cls._epoch(latest.get("created_at"))
        out["down"] = cls._mbps(latest.get("download_bits"),
                                latest.get("download"))
        out["up"] = cls._mbps(latest.get("upload_bits"), latest.get("upload"))
        out["ping"], loaded = cls._latency(latest)
        out["jitter"] = cls._ms(ping.get("jitter"))
        out["loss"] = cls._loss(latest)
        out["dropped"] = cls._dropped(latest)
        out["isp"] = d.get("isp") or ""
        out["server"] = ((d.get("server") or {}).get("name") or "")
        out["healthy"] = latest.get("healthy")

        bloat = (None if loaded is None or out["ping"] is None
                 else loaded - out["ping"])
        out["bloat"] = bloat

        # -- what is wrong, in the order it matters -----------------------
        issues = out["issues"]
        # One failed test is common enough — a server hiccup, a restart — that
        # calling the line down for it would cry wolf on a wall display. Three
        # in a row is a pattern. A single failure still shows as an issue, so
        # the card reads degraded rather than silent.
        recent = rows[:cls.DOWN_AFTER]
        if len(recent) == cls.DOWN_AFTER and not any(cls._ok(r) for r in recent):
            out["status"] = "down"
            issues.append("Last %d tests failed" % cls.DOWN_AFTER)
        elif rows and not cls._ok(rows[0]) and not cls._tool_error(rows[0]):
            issues.append("Most recent test failed")
        if out["loss"] is not None and out["loss"] > cls.LOSS_PCT:
            issues.append("Packet loss %.1f%%" % out["loss"])
        if bloat is not None and bloat > cls.BLOAT_MS:
            issues.append("Latency +%d ms under load" % round(bloat))
        if out["jitter"] is not None and out["jitter"] > cls.JITTER_MS:
            issues.append("Jitter %d ms" % round(out["jitter"]))
        if plan_down and out["down"] is not None \
                and out["down"] < plan_down * cls.PLAN_FRAC:
            issues.append("Download %d%% of plan"
                          % round(out["down"] / plan_down * 100))
        if plan_up and out["up"] is not None \
                and out["up"] < plan_up * cls.PLAN_FRAC:
            issues.append("Upload %d%% of plan"
                          % round(out["up"] / plan_up * 100))
        if out["failures"]:
            issues.append("%d failed test%s in %s"
                          % (out["failures"],
                             "" if out["failures"] == 1 else "s",
                             cls.span(out["window_h"])))
        if out["healthy"] is False:
            issues.append("Below your threshold")

        if out["status"] != "down":
            out["status"] = "degraded" if issues else "good"
        return out

    def snapshot(self):
        with self.lock:
            if self.data is None:
                return {"available": False, "error": self.error}
            out = dict(self.data)
            out["available"] = True
            out["error"] = self.error
            return out


class ObservationFetcher(PollingFetcher):
    """What is actually falling right now, from the nearest National Weather
    Service station.

    The Tempest cannot see snow. Its rain sensor feels drops strike the top of
    the unit, and snowflakes land too softly to register; its own report knows
    only rain and hail. So snow — and freezing rain, and sleet — has to come
    from somewhere that watches for it, and the nearest airport's automated
    station does, keylessly. It can be twenty miles away, which is why the
    page only believes it when the Tempest's own thermometer agrees it is cold
    enough.

    Two stations, not one: the nearest two by real distance (the API lists
    them in an order of its own). Here that is DuPage to the south-east and
    DeKalb to the west, with Huntley between them, so snow arriving from the
    west shows at DeKalb first. If either reports snow, it is snowing; if one
    station is down, the other carries on. They are looked up once and kept,
    unless --obs-stations names them.
    """

    POINTS = "https://api.weather.gov/points/%.4f,%.4f"
    LATEST = "https://api.weather.gov/stations/%s/observations/latest"
    REFRESH = 600
    RETRY = 60
    LABEL = "Observations"

    # METAR present-weather codes, most worth drawing first: when an airport
    # reports rain and snow together, the card shows the snow.
    KINDS = (("FZRA", "freezing_rain"), ("FZDZ", "freezing_rain"),
             ("SN", "snow"), ("SG", "snow"), ("PL", "sleet"),
             ("GR", "hail"), ("GS", "hail"), ("RA", "rain"), ("DZ", "rain"))

    COUNT = 2                 # stations watched
    STALE = 2 * 3600          # a report older than this says nothing about now
    # What each kind is worth drawing, and how hard, for choosing between
    # two stations' reports.
    NOTABLE = {"freezing_rain": 5, "snow": 4, "sleet": 3, "hail": 2, "rain": 1, "none": 0}
    HARD = {"heavy": 2, "moderate": 1, "light": 0, None: 0}

    def __init__(self, lat, lon, stop_event, pinned=()):
        PollingFetcher.__init__(self, stop_event)
        self.lat, self.lon = lat, lon
        self.pinned = [p.strip().upper() for p in pinned if p and p.strip()]
        self.stations = None      # [(id, name, miles)], nearest first

    def _get(self, url):
        req = urllib.request.Request(url, headers={
            "User-Agent": "tempest-dashboard/%s (self-hosted station display)"
                          % core.VERSION,
            "Accept": "application/geo+json",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def miles(self, lat, lon):
        r = 3958.8
        p1, p2 = math.radians(self.lat), math.radians(lat)
        a = (math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2)
             * math.sin(math.radians(lon - self.lon) / 2) ** 2)
        return 2 * r * math.asin(math.sqrt(a))

    def find_stations(self):
        point = self._get(self.POINTS % (self.lat, self.lon))
        listing = self._get(point["properties"]["observationStations"])
        found = []
        for f in listing.get("features") or []:
            p = f.get("properties") or {}
            lon, lat = (f.get("geometry") or {}).get("coordinates") or (None, None)
            if p.get("stationIdentifier") and lat is not None:
                found.append((p["stationIdentifier"], p.get("name") or "",
                              self.miles(lat, lon)))
        found.sort(key=lambda s: s[2])
        if self.pinned:
            named = {s[0]: s for s in found}
            return [named.get(sid, (sid, sid, None)) for sid in self.pinned]
        return found[:self.COUNT]

    def fetch_once(self):
        if not self.stations:
            self.stations = self.find_stations()
        reports, failures = [], []
        for sid, name, miles in self.stations:
            try:
                r = self.parse(self._get(self.LATEST % sid))
            except Exception as e:          # one station down is not an outage
                failures.append(e)
                continue
            r.update(station=sid, station_name=self.short_name(name),
                     miles=None if miles is None else round(miles, 1))
            reports.append(r)
        if not reports:
            raise failures[0] if failures else ValueError("no stations")
        return self.merge(reports, time.time())

    @classmethod
    def merge(cls, reports, now):
        """One answer from several stations. Among reports from the last two
        hours, the most notable thing falling wins — snow over rain, heavy over
        light, and the nearer station on a tie — because if either station
        sees snow, it is snowing somewhere between them. With nothing recent,
        the newest report is returned as it is, and the page, seeing its age,
        turns to the forecast instead."""
        fresh = [r for r in reports
                 if r.get("observed_at") and now - r["observed_at"] < cls.STALE]
        if fresh:
            best = max(fresh, key=lambda r: (cls.NOTABLE.get(r["kind"], 0),
                                             cls.HARD.get(r.get("intensity"), 0),
                                             -(r.get("miles") or 0)))
        else:
            best = max(reports, key=lambda r: r.get("observed_at") or 0)
        out = dict(best)
        out["blowing"] = any(r.get("blowing") for r in fresh)
        out["stations"] = [{k: r.get(k) for k in ("station", "station_name", "miles",
                                                  "kind", "intensity", "observed_at")}
                           for r in reports]
        return out

    @staticmethod
    def short_name(name):
        """'Chicago / West Chicago, Dupage Airport' -> 'Dupage Airport', and
        'De Kalb Taylor Municipal Airport' -> 'De Kalb Taylor Airport'. The
        part after the last comma is the place; the rest is the city it is
        filed under. The civic words go too: a caption has little room."""
        short = (name or "").rsplit(",", 1)[-1].strip() or name or ""
        for word in (" Municipal", " Regional", " International", " County"):
            short = short.replace(word, "")
        return short

    @classmethod
    def parse(cls, raw):
        """Reduce one observation to what the Rainfall card draws: the most
        notable thing falling, how hard, whether snow is blowing, and when."""
        p = (raw or {}).get("properties") or {}
        best, blowing = None, False
        for item in p.get("presentWeather") or []:
            code = str((item or {}).get("rawString") or "").upper().strip()
            if not code or code.startswith("VC"):
                continue            # "in the vicinity" is not at the station
            if "BLSN" in code:
                blowing = True      # lifted off the ground, not falling
                code = code.replace("BLSN", "")
            for rank, (token, kind) in enumerate(cls.KINDS):
                if token in code:
                    intensity = ("heavy" if code.startswith("+") else
                                 "light" if code.startswith("-") else "moderate")
                    if best is None or rank < best[0]:
                        best = (rank, kind, intensity)
                    break
        observed = None
        ts = p.get("timestamp")
        if isinstance(ts, str) and ts:
            try:
                observed = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                observed = None
        return {
            "kind": best[1] if best else "none",
            "intensity": best[2] if best else None,
            "blowing": blowing,
            "text": p.get("textDescription") or "",
            "observed_at": observed,
        }


class NwsForecastFetcher(PollingFetcher):
    """The National Weather Service's written forecast for the station's own
    grid square — "Tonight: showers and thunderstorms before 1am…" — from the
    same keyless API the alerts use.

    The ten-day outlook is numbers and icons; this is the sentence a person
    wants. It is the forecaster's own words for this place rather than a
    global model's output, and it reads across a room.
    """

    POINTS = "https://api.weather.gov/points/%.4f,%.4f"
    REFRESH = 1800
    RETRY = 120
    LABEL = "NWS forecast"
    KEEP = 6                  # periods to carry: three days, day and night

    def __init__(self, lat, lon, stop_event):
        PollingFetcher.__init__(self, stop_event)
        self.lat, self.lon = lat, lon
        self.url = None
        self.office = ""

    def _get(self, url):
        req = urllib.request.Request(url, headers={
            "User-Agent": "tempest-dashboard/%s (self-hosted station display)"
                          % core.VERSION,
            "Accept": "application/geo+json",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def fetch_once(self):
        if not self.url:
            p = self._get(self.POINTS % (self.lat, self.lon))["properties"]
            self.url = p["forecast"]
            self.office = p.get("cwa") or ""
        out = self.parse(self._get(self.url))
        out["office"] = self.office
        return out

    @classmethod
    def parse(cls, raw):
        p = (raw or {}).get("properties") or {}
        periods = []
        for q in (p.get("periods") or [])[:cls.KEEP]:
            if not isinstance(q, dict) or not q.get("name"):
                continue
            pop = (q.get("probabilityOfPrecipitation") or {}).get("value")
            periods.append({
                "name": q["name"],
                "short": q.get("shortForecast") or "",
                "detail": q.get("detailedForecast") or "",
                "temp_f": q.get("temperature"),
                "day": bool(q.get("isDaytime")),
                "start": cls._epoch(q.get("startTime")),
                "pop": pop,
            })
        return {"periods": periods, "updated": cls._epoch(p.get("updateTime"))}

    @staticmethod
    def _epoch(text):
        if not isinstance(text, str) or not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None


class AlertsFetcher(PollingFetcher):
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

    LABEL = "Alerts"

    def __init__(self, lat, lon, stop_event):
        PollingFetcher.__init__(self, stop_event)
        self.lat, self.lon = lat, lon
        self.alerts = []
        self.fetched_at = None

    # Syslog severities for NWS ones: a tornado warning is critical, an
    # advisory is a notice. Log Center can mail on the former alone.
    SYSLOG_LEVEL = {"Extreme": "critical", "Severe": "error",
                    "Moderate": "warning", "Minor": "notice", "Unknown": "notice"}

    def store(self, fresh):
        # No alerts is a perfectly good answer, so an empty list must still
        # count as having been asked — hence fetched_at rather than truthiness.
        before = {a.get("id"): a for a in self.alerts if a.get("id")}
        after = {a.get("id"): a for a in fresh if a.get("id")}
        for aid, a in after.items():
            if aid not in before:
                core.log("alert", "%s (%s) until %s%s" % (
                    a["event"], a["severity"], (a.get("ends") or a.get("expires") or "?")[:16],
                    " · issued %d times" % a["copies"] if a.get("copies", 1) > 1 else ""),
                    self.SYSLOG_LEVEL.get(a["severity"], "notice"))
        for aid, a in before.items():
            if aid not in after:
                core.log("alert", "%s ended" % a["event"], "notice")
        self.alerts = fresh
        self.fetched_at = time.time()

    def fetch_once(self):
        url = "%s?point=%.4f,%.4f" % (self.ENDPOINT, self.lat, self.lon)
        req = urllib.request.Request(url, headers={
            "User-Agent": "tempest-dashboard/%s (self-hosted station display)"
                          % core.VERSION,
            "Accept": "application/geo+json",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            return self.parse(json.loads(r.read().decode("utf-8")))

    @classmethod
    def parse(cls, raw, now=None):
        """Reduce the GeoJSON to what the banner needs, worst first. Split
        from the request so it can be run against real alerts rather than
        waiting for weather."""
        out = []
        for feat in (raw.get("features") or []):
            p = feat.get("properties") if isinstance(feat, dict) else None
            if not isinstance(p, dict):
                continue
            event = (p.get("event") or "").strip()
            headline = (p.get("headline") or "").strip()
            if not event and not headline:
                continue  # nothing to render; skip rather than show a blank banner
            out.append({
                "id": p.get("id") or feat.get("id") or "",
                "event": event or "Weather alert",
                "severity": p.get("severity") or "Unknown",
                "urgency": p.get("urgency") or "",
                "headline": headline,
                # "ends" is when the hazard is over; "expires" only when this
                # message lapses. The banner shows ends, falling back to expires.
                "onset": p.get("onset"), "expires": p.get("expires"),
                "ends": p.get("ends"), "sent": p.get("sent"),
                "sender": p.get("senderName") or "",
                # What the statement actually says. The banner shows the
                # event and the end time; this is behind a tap.
                "description": cls._prose(p.get("description")),
                "instruction": cls._prose(p.get("instruction")),
                "areas": cls._areas(p.get("areaDesc")),
                "copies": 1,
                "rank": cls.RANK.get(p.get("severity") or "Unknown", 0),
            })
        out = cls._fold(out)
        out = [a for a in out if cls._live(a, now)]
        out.sort(key=lambda a: (a["rank"], a.get("sent") or ""), reverse=True)
        return out

    @staticmethod
    def _live(alert, now=None):
        """False once the alert's end — or, with no end, its expiry — has
        passed. The feed sometimes lists a statement a while after."""
        when = alert.get("ends") or alert.get("expires")
        if not when:
            return True
        try:
            end = datetime.fromisoformat(str(when))
        except ValueError:
            return True
        if end.tzinfo is None:
            return True
        now = now if now is not None else time.time()
        return end.timestamp() > now

    @staticmethod
    def _prose(text):
        """NWS text is wrapped at sixty-odd columns with a blank line between
        paragraphs. Unwrap the lines; keep the paragraphs."""
        if not text:
            return ""
        paras = []
        for para in str(text).replace("\r", "").split("\n\n"):
            words = " ".join(line.strip() for line in para.split("\n"))
            words = " ".join(words.split())
            if words:
                paras.append(words)
        return "\n\n".join(paras)

    @staticmethod
    def _areas(text):
        return [a.strip() for a in str(text or "").split(";") if a.strip()]

    @classmethod
    def _fold(cls, alerts):
        """One banner for one statement.

        The office issued a fog statement at 7:04, then again at 7:08 with a
        typo fixed, and never withdrew the first; both were active and the
        wall showed two identical banners. Alerts for the same event from
        the same office ending at the same time, whose text says the same
        thing, are one alert: the newest is kept and it says how many times
        it was issued. Two different statements ending together stay two.

        A reissue can also move the end: a Hydrologic Outlook sent at 1:00
        ending at 3:00 was sent again at 1:02 ending at 4 AM, the first never
        withdrawn and neither naming the other. So a different end still
        folds when the areas are the same and the text says the same thing.
        """
        kept = []
        for a in alerts:
            twin = None
            for k in kept:
                if (k["event"], k["sender"]) != (a["event"], a["sender"]):
                    continue
                if (k["ends"] or k["expires"]) != (a["ends"] or a["expires"]) and not (
                        a["description"] and k["description"]
                        and set(a["areas"]) == set(k["areas"])):
                    continue
                if a["description"] and k["description"]:
                    alike = difflib.SequenceMatcher(
                        None, a["description"], k["description"]).ratio()
                    if alike < 0.8:
                        continue
                twin = k
                break
            if twin is None:
                kept.append(a)
                continue
            twin["copies"] += 1
            if (a.get("sent") or "") > (twin.get("sent") or ""):
                copies = twin["copies"]
                twin.clear(); twin.update(a); twin["copies"] = copies
        return kept

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

    # What the sweep keeps of each day. Raise it when the sweep learns to
    # keep something new, and the archive is walked again to collect it.
    SWEEP_KEEPS = 2

    @staticmethod
    def bucket_minutes(rows):
        """How many minutes each row of a window stands for.

        WeatherFlow answers a window of more than a day with buckets — five
        minutes for a few days, longer beyond — so a row is not always a
        minute. The smallest gap between neighbours is the bucket; a hole in
        the record is a larger gap and does not confuse it.
        """
        stamps = []
        for obs in rows:
            try:
                stamps.append(float(obs[0]))
            except (TypeError, ValueError, IndexError):
                continue
        stamps.sort()
        gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b - a >= 30]
        if not gaps:
            return 1.0
        return max(1.0, min(180.0, min(gaps) / 60.0))

    @staticmethod
    def fold_obs(summary, obs, minutes):
        """One row of WeatherFlow's device record into a day's summary."""
        at = lambda i: obs[i] if len(obs) > i else None
        try:
            ts = float(obs[0])
        except (TypeError, ValueError):
            return
        core.History.fold(summary, ts, minutes, temp_c=at(7), pres_mb=at(6),
                          wind_ms=at(2), gust_ms=at(3), rh=at(8), uv=at(10),
                          solar=at(11))
        try:
            if at(15):
                summary["strikes"] = summary.get("strikes", 0.0) + float(at(15))
        except (TypeError, ValueError):
            pass

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
        # The sweep used to keep rain and temperature only. A record swept
        # before it learned to keep the rest of the day — gusts, pressure,
        # strikes, sun — has to be walked again from the top, once, or those
        # would only ever start from the day this version was installed.
        if self.history.meta.get("sweep_keeps") != self.SWEEP_KEEPS:
            already = reached = None
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
        summaries = {}
        requests = 0
        empty_runs = 0
        cut_short = False
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
                cut_short = True
                break
            end = min(cursor + timedelta(days=chunk_days), today + timedelta(days=1))
            try:
                raw = self._get(self.DEVICE_OBS % device, {
                    "time_start": int(time.mktime(cursor.timetuple())),
                    "time_end": int(time.mktime(end.timetuple())),
                })
            except Exception:
                continue                   # a gap is better than giving up
            step = self.bucket_minutes(raw.get("obs") or [])
            for obs in (raw.get("obs") or []):
                if not isinstance(obs, list) or len(obs) < 13:
                    continue
                try:
                    day = datetime.fromtimestamp(float(obs[0])).date().isoformat()
                except (TypeError, ValueError, OSError):
                    continue
                self.fold_obs(summaries.setdefault(day, {}), obs, step)
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
        # Today is merged too. Its extremes are a union, so the live record
        # loses nothing, and a day the server was restarted halfway through
        # gets back the gust it was not running to see.
        filled = 0
        with self.state.lock:
            for day, summary in summaries.items():
                if self.history.merge_day(day, summary):
                    filled += 1
        self.history.meta[done_key] = time.time()
        self.history.meta[from_key] = reached_back.isoformat()
        if not cut_short:
            self.history.meta["sweep_keeps"] = self.SWEEP_KEEPS
        self.history.save(force=True)
        core.log("backfill", "swept back to %s in %d requests — %d rain days, "
                 "%d temperature days, %d daily summaries"
                 % (reached_back.isoformat(), requests, added, warm, filled))
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
                core.log("backfill", "merged %d observations from WeatherFlow" % added)
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
              "forecast", "lightning", "radar", "records", "air", "pollen",
              "internet", "hardware", "claude"]


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
        self.pollen_key = defaults.get("pollen_key") or ""
        self.speedtest_token = defaults.get("speedtest_token") or ""
        # Whether each AI service is watched, and where from; an empty address
        # is the built-in one.
        self.ai_status = {k: {"enabled": True, "url": ""} for k in AI_SERVICES}
        self.syslog = {"enabled": False, "host": "127.0.0.1", "port": 514,
                       "proto": "udp", "obs": False}
        # The AI card's pulse, from one of the owner's own ECG recordings
        # (core.parse_ecg): beat timings and a beat's shape, nothing that
        # says whose. None until one is loaded from the settings page.
        self.heart = None
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
            if isinstance(saved.get("pollen_key"), str) and saved["pollen_key"]:
                self.pollen_key = saved["pollen_key"]
            if isinstance(saved.get("speedtest_token"), str) \
                    and saved["speedtest_token"]:
                self.speedtest_token = saved["speedtest_token"]
            if isinstance(saved.get("ai_status"), dict):
                try:
                    self.ai_status = self.clean_ai(saved["ai_status"], self.ai_status)
                except ValueError:
                    pass
            got = saved.get("syslog")
            if isinstance(got, dict):
                self.syslog.update(self.clean_syslog(got, self.syslog))
            self.heart = self.clean_heart(saved.get("heart"))

    @staticmethod
    def clean_heart(raw):
        """A stored heart profile, or None if it does not hold together."""
        if not isinstance(raw, dict):
            return None
        rr, shape = raw.get("rr_ms"), raw.get("shape")
        if not (isinstance(rr, list) and 8 <= len(rr) <= 400
                and all(isinstance(v, (int, float)) and 200 <= v <= 3000 for v in rr)):
            return None
        if not (isinstance(shape, list) and len(shape) == core.ECG_POINTS
                and all(isinstance(v, (int, float)) and -2 <= v <= 2 for v in shape)):
            return None
        early = [g for g in (raw.get("early") or []) if isinstance(g, int) and 0 <= g < len(rr)]
        return {"enabled": raw.get("enabled", True) is not False,
                "recorded": str(raw.get("recorded") or "")[:10],
                "classification": str(raw.get("classification") or "")[:40],
                "bpm": int(raw.get("bpm") or round(60000 * len(rr) / sum(rr))),
                "rr_ms": [int(v) for v in rr], "early": early,
                "shape": [float(v) for v in shape]}

    def set_heart(self, profile):
        with self.lock:
            self.heart = self.clean_heart(dict(profile, enabled=True))
        self.save()
        return self.heart is not None

    def heart_for_page(self):
        """What the dashboard draws the pulse from, when it is switched on."""
        with self.lock:
            h = self.heart
            if not h or not h["enabled"]:
                return None
            return {k: h[k] for k in ("recorded", "rr_ms", "early", "shape")}

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"slots": self.slots, "token": self.token,
                           "pollen_key": self.pollen_key,
                           "speedtest_token": self.speedtest_token,
                           "ai_status": self.ai_status,
                           "syslog": self.syslog,
                           "heart": self.heart},
                          f, indent=2)
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

    @staticmethod
    def clean_ai(raw, current):
        """An ai_status block from a browser, checked service by service."""
        out = {}
        for key in AI_SERVICES:
            got = raw.get(key) or {}
            if not isinstance(got, dict):
                raise ValueError("%s must be an object" % key)
            cur = current[key]
            enabled = bool(got["enabled"]) if "enabled" in got else cur["enabled"]
            url = got.get("url", cur["url"])
            if not isinstance(url, str):
                raise ValueError("the address must be text")
            url = url.strip()
            if url:
                parts = urllib.parse.urlparse(url)
                if (len(url) > 300 or parts.scheme not in ("http", "https")
                        or not parts.netloc or any(c.isspace() for c in url)):
                    raise ValueError("that does not look like a web address")
            out[key] = {"enabled": enabled, "url": url}
        return out

    def public(self):
        """Everything a browser may see — note the token itself is absent."""
        with self.lock:
            return {"slots": list(self.slots),
                    "cards": CARD_NAMES,
                    "token_set": bool(self.token),
                    "pollen_key_set": bool(self.pollen_key),
                    "speedtest_token_set": bool(self.speedtest_token),
                    "ai_status": {k: dict(v) for k, v in self.ai_status.items()},
                    "ai_defaults": {k: c.ENDPOINT for k, c in AI_SERVICES.items()},
                    "syslog": dict(self.syslog),
                    "heart": ({k: self.heart[k] for k in ("enabled", "recorded", "classification", "bpm")}
                              | {"beats": len(self.heart["rr_ms"]) + 1, "early": len(self.heart["early"])}
                              if self.heart else None)}

    @staticmethod
    def clean_syslog(raw, current):
        """A syslog block from a browser, checked field by field."""
        out = dict(current)
        if "enabled" in raw:
            out["enabled"] = bool(raw["enabled"])
        if "obs" in raw:
            out["obs"] = bool(raw["obs"])
        host = raw.get("host")
        if isinstance(host, str) and host.strip():
            host = host.strip()
            if len(host) > 253 or any(c.isspace() for c in host):
                raise ValueError("that host name looks wrong")
            out["host"] = host
        if "port" in raw:
            try:
                port = int(raw["port"])
            except (TypeError, ValueError):
                raise ValueError("the port must be a number")
            if not 1 <= port <= 65535:
                raise ValueError("the port must be between 1 and 65535")
            out["port"] = port
        proto = raw.get("proto")
        if proto is not None:
            if proto not in ("udp", "tcp"):
                raise ValueError("the protocol is udp or tcp")
            out["proto"] = proto
        return out

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
            for field, label in (("pollen_key", "pollen_key"),
                                 ("speedtest_token", "speedtest_token")):
                if field not in patch:
                    continue
                val = patch.get(field)
                if val is None or (isinstance(val, str) and not val.strip()):
                    if getattr(self, field):
                        setattr(self, field, "")
                        changed.append(label + "_cleared")
                elif isinstance(val, str):
                    val = val.strip()
                    if len(val) > 200:
                        return [], "that key looks too long"
                    if val != getattr(self, field):
                        setattr(self, field, val)
                        changed.append(label + "_set")
                else:
                    return [], "key must be text"
            if "ai_status" in patch:
                if not isinstance(patch["ai_status"], dict):
                    return [], "ai_status must be an object"
                try:
                    fresh = self.clean_ai(patch["ai_status"], self.ai_status)
                except ValueError as e:
                    return [], str(e)
                if fresh != self.ai_status:
                    self.ai_status = fresh
                    changed.append("ai_status")
            if "syslog" in patch:
                if not isinstance(patch["syslog"], dict):
                    return [], "syslog must be an object"
                try:
                    fresh = self.clean_syslog(patch["syslog"], self.syslog)
                except ValueError as e:
                    return [], str(e)
                if fresh != self.syslog:
                    self.syslog = fresh
                    changed.append("syslog")
            if "heart" in patch:
                # Switched on or off, or taken away; loading one is /api/heart.
                got = patch["heart"]
                if got is None:
                    if self.heart:
                        self.heart = None
                        changed.append("heart_cleared")
                elif isinstance(got, dict) and isinstance(got.get("enabled"), bool):
                    if not self.heart:
                        return [], "no heartbeat has been loaded"
                    if got["enabled"] != self.heart["enabled"]:
                        self.heart["enabled"] = got["enabled"]
                        changed.append("heart_" + ("on" if got["enabled"] else "off"))
                else:
                    return [], "heart must be {\"enabled\": true|false} or null"
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
        self.rain_monthly = rain_monthly(args.rain_monthly,
                                         args.rain_normal)
        self.temp_normal = temp_normal(args.temp_normal_high,
                                       args.temp_normal_low)
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

        self._source_restarts = 0
        self._source_restart_at = 0

        self.forecast = None
        if args.forecast and args.lat is not None and args.lon is not None:
            self.forecast = ForecastFetcher(
                args.lat, args.lon, self.stop,
                cache_path=os.path.join(args.data_dir, "tempest_forecast.json"))
            self.forecast.start()
        self.backfill = None
        self.start_backfill()

        self.air = None
        if args.lat is not None and args.lon is not None:
            self.air = AirQualityFetcher(args.lat, args.lon, self.stop)
            self.air.start()

        self.pollen = None
        self.start_pollen()

        self.speedtest = None
        self.start_speedtest()

        self.ai_history = AiHistory(os.path.join(args.data_dir, AiHistory.FILE), self.history)
        self.ai = {}
        self.apply_ai_status(quiet=True)
        threading.Thread(target=self._backfill_ai, daemon=True).start()

        self.apply_syslog()

        self.alerts = None
        if args.alerts and args.lat is not None and args.lon is not None:
            self.alerts = AlertsFetcher(args.lat, args.lon, self.stop)
            self.alerts.start()

        # The forecaster's own words for this grid square, for the outlook.
        self.nws = None
        if args.nws and args.lat is not None and args.lon is not None:
            self.nws = NwsForecastFetcher(args.lat, args.lon, self.stop)
            self.nws.start()

        # What is falling at the nearest airport, for snow the Tempest cannot see.
        self.observations = None
        if args.observations and args.lat is not None and args.lon is not None:
            self.observations = ObservationFetcher(
                args.lat, args.lon, self.stop,
                pinned=(args.obs_stations or "").split(","))
            self.observations.start()
        threading.Thread(target=self._housekeeping, daemon=True).start()

    def start_pollen(self):
        """Start the pollen fetcher once a key exists. Called at boot and
        again when a key is saved, so it does not need a restart."""
        if self.pollen is not None and self.pollen.is_alive():
            return False
        key = self.config.pollen_key
        if not key or self.args.lat is None or self.args.lon is None:
            return False
        self.pollen = PollenFetcher(self.args.lat, self.args.lon, key, self.stop)
        self.pollen.start()
        return True

    def start_speedtest(self):
        """Start the Speedtest Tracker fetcher once a token exists. Called at
        boot and again when a token is saved, so it needs no restart."""
        if self.speedtest is not None and self.speedtest.is_alive():
            return False
        token = self.config.speedtest_token
        if not token or not self.args.speedtest_url:
            return False
        self.speedtest = SpeedtestFetcher(self.args.speedtest_url, token,
                                          self.stop,
                                          plan_down=self.args.plan_down,
                                          plan_up=self.args.plan_up)
        self.speedtest.start()
        return True

    def apply_ai_status(self, quiet=False):
        """Start, stop or repoint the AI status fetchers to match the settings.
        Called at boot and when they are saved, so it needs no restart. A
        thread cannot be pointed elsewhere, so a changed address is a new one.
        `quiet` is the boot: nothing is being switched, so nothing is said,
        except that a service reading from somewhere else always is."""
        for key, cls in AI_SERVICES.items():
            want = self.config.ai_status[key]
            url = want["url"] or cls.ENDPOINT
            f = self.ai.get(key)
            was_running = f is not None
            if f is not None and (not want["enabled"] or f.url != url):
                f.stop_event.set()
                del self.ai[key]
                f = None
                if not want["enabled"]:
                    core.log("ai status", "%s switched off" % cls.NAME, "notice")
            if f is None and want["enabled"]:
                said = []
                if not was_running and not quiet:
                    said.append("switched on")
                if url != cls.ENDPOINT:
                    said.append("reading from %s" % url)
                elif was_running:
                    said.append("reading from the built-in address")
                if said:
                    core.log("ai status", "%s %s" % (cls.NAME, ", ".join(said)), "notice")
                self.ai[key] = cls(threading.Event(), url,
                                   lambda fresh, key=key: self.ai_history.observe(key, fresh))
                self.ai[key].start()

    def apply_syslog(self):
        """Point the log at the syslog server the settings name, or at none."""
        cfg = self.config.syslog
        old = core.LOG.syslog
        if cfg.get("enabled"):
            same = old is not None and (old.host, old.port, old.proto) == \
                (cfg["host"], int(cfg["port"]), cfg["proto"])
            if same:
                return
            core.LOG.syslog = core.Syslog(cfg["host"], cfg["port"], cfg["proto"])
            core.log("syslog", "sending to %s:%s over %s" % (cfg["host"], cfg["port"], cfg["proto"]))
        else:
            core.LOG.syslog = None
            if old is not None:
                core.log("syslog", "switched off")
        if old is not None:
            old.close()

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
            core.log("restored", "last observation from %s" % when)

    def _save(self):
        try:
            tmp = self.settings_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state.dump_state(), f)
            os.replace(tmp, self.settings_path)
        except OSError:
            pass

    def _housekeeping(self):
        last_health, quiet_since = None, None
        last_winter, froze_on = None, None
        next_obs_line = 0.0
        while not self.stop.wait(30.0):
            self.state.roll_day()
            self.history.save()
            self._save()
            self._watch_source()
            # The hub going quiet and coming back, once each, not every tick.
            health = self.state.health()
            if health != last_health and last_health is not None:
                if health in ("stale", "offline"):
                    if quiet_since is None:
                        quiet_since = time.time()
                    core.log("hub", "no packets for %s" % core.format_uptime(
                        self.state.snapshot_age() or 0), "warning" if health == "stale" else "error")
                elif health == "live" and quiet_since is not None:
                    core.log("hub", "packets again after %s" % core.format_uptime(
                        time.time() - quiet_since), "notice")
                    quiet_since = None
            last_health = health
            # Winter's firsts, once a day each: the frost line lighting, and
            # the temperature going below freezing.
            try:
                snap = self.state.snapshot(self.args.lat, self.args.lon, series_points=2)
                fc = self.forecast.snapshot() if self.forecast else {}
                winter = core.winter_outlook(fc.get("days") or []) if fc.get("available") else None
                key = (winter["kind"], winter["date"]) if winter else None
                if key and key != last_winter:
                    core.log("winter", "%s %s, low %.1f °C" % (
                        winter["kind"], winter["when"], winter["low_c"]), "notice")
                last_winter = key
                since = snap["derived"].get("freezing_since")
                today = date.today().isoformat()
                if since and froze_on != today:
                    froze_on = today
                    core.log("winter", "below freezing since %s" %
                             time.strftime("%H:%M", time.localtime(since)), "notice")
                # The minute's observation for a syslog store, when asked.
                if self.config.syslog.get("obs") and core.LOG.syslog and time.time() >= next_obs_line:
                    next_obs_line = time.time() + 60
                    o = snap["obs"]
                    core.log("obs", " ".join("%s=%s" % (k, o[k]) for k in
                             ("temp_c", "rh", "pres_mb", "wind_avg_ms", "wind_gust_ms",
                              "wind_dir", "rain_mm", "uv", "solar", "battery") if o.get(k) is not None))
            except Exception as e:
                core.log("housekeeping", "%s: %s" % (e.__class__.__name__, e), "warning")

    def _watch_source(self):
        """Restart the packet source if its thread has died.

        The listener exits on an unrecoverable socket error. Nothing else
        would notice: the server keeps serving, the page keeps polling, and
        the data quietly stops — the worst kind of failure for something
        nobody is watching. Retries are spaced out so a genuinely unusable
        port does not spin.
        """
        if self.stop.is_set() or self.source is None:
            return
        if self.source.is_alive():
            self._source_failed_at = None
            return
        now = time.time()
        last = getattr(self, "_source_restart_at", 0)
        if now - last < 60:
            return
        self._source_restart_at = now
        self._source_restarts = getattr(self, "_source_restarts", 0) + 1
        err = getattr(self.source, "error", "") or "thread exited"
        core.log("watchdog", "packet source stopped (%s) — restart #%d"
                 % (err, self._source_restarts), "error")
        try:
            if self.args.demo:
                self.source = core.DemoSource(self.state.handle, self.stop)
            else:
                self.source = core.UdpListener(self.args.udp_port,
                                               self.state.handle, self.stop)
            self.source.start()
        except Exception as e:
            core.log("watchdog", "restart failed (%s)" % e.__class__.__name__, "error")

    # What changes slowly is served slowly. The 24-hour series and the
    # Internet card's week of tests were 28 KB of a 35 KB snapshot sent every
    # two seconds to every screen — 1.5 GB a day each — and they change once
    # a minute and once an hour. They ride a second endpoint the page asks
    # for every thirty seconds.
    SLOW = ("series",)
    SLOW_INTERNET = ("history",)

    @classmethod
    def split_slow(cls, snap):
        """Take the slow-changing parts out of a snapshot. Returns them."""
        slow = {"series": snap.pop("series", None), "internet_history": None,
                "nws": snap.pop("nws", None)}
        net = snap.get("internet")
        if isinstance(net, dict):
            slow["internet_history"] = net.pop("history", None)
        return slow

    def snapshot(self):
        snap = self.state.snapshot(self.args.lat, self.args.lon)
        snap["source_error"] = getattr(self.source, "error", "") or ""
        snap["source_restarts"] = self._source_restarts
        snap["source_alive"] = bool(self.source and self.source.is_alive())
        snap["uptime_s"] = time.time() - self.started
        snap["poll_ms"] = POLL_HINT_MS
        snap["rain_monthly_in"] = self.rain_monthly
        snap["temp_normal_c"] = self.temp_normal
        snap["ui"] = UI_ID
        snap["units"] = {"temp": self.args.temp_unit, "wind": self.args.wind_unit,
                         "pres": self.args.pres_unit, "rain": self.args.rain_unit,
                         "dist": self.args.dist_unit}
        snap["station_name"] = self.args.name or snap.get("serial") or ""
        snap["lat"], snap["lon"] = self.args.lat, self.args.lon
        snap["air"] = (self.air.snapshot() if self.air
                       else {"available": False, "error": "Set --lat and --lon"})
        snap["pollen"] = (self.pollen.snapshot() if self.pollen
                          else {"available": False,
                                "error": "Add a pollen key in settings"})
        snap["internet"] = (self.speedtest.snapshot() if self.speedtest
                            else {"available": False,
                                  "error": "Add a Speedtest token in settings"})
        # Where the tracker's own page lives, so the card can offer a way in.
        # It is a LAN address and no secret; the token stays server-side.
        snap["internet"]["url"] = self.args.speedtest_url or ""
        snap["ai_status"] = {k: f.snapshot() for k, f in list(self.ai.items())}
        snap["heart"] = self.config.heart_for_page()
        snap["nws"] =(self.nws.snapshot() if self.nws
                       else {"available": False, "error": ""})
        snap["precip_obs"] = (self.observations.snapshot() if self.observations
                              else {"available": False, "error": ""})
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
        # Worked out now rather than when the forecast was fetched, because
        # "tonight" changes at midnight and the forecast is held for an hour.
        if snap["forecast"].get("available"):
            snap["forecast"]["winter"] = core.winter_outlook(
                snap["forecast"].get("days") or [])
        return snap

    def _backfill_ai(self):
        """Once per service, fill the days before the live record began from
        its own list of past incidents. It waits for the live record to begin,
        tries again if the page cannot be read (the first failure is logged,
        not every try), and does not ask a service that is switched off."""
        due, failed = {}, set()
        while not self.stop.is_set():
            for key, cls in AI_SERVICES.items():
                f = self.ai.get(key)
                if f is None or self.ai_history.backfilled(key) or time.time() < due.get(key, 0):
                    continue
                try:
                    events, covered = f.history()
                    if not self.ai_history.backfill(key, events, covered):
                        due[key] = time.time() + 30              # the live record has not begun
                        continue
                except Exception as e:
                    due[key] = time.time() + 1800
                    if key not in failed:
                        failed.add(key)
                        core.log("ai status", "%s history unavailable: %s" % (
                            cls.NAME, e if isinstance(e, ValueError) else e.__class__.__name__),
                            "warning")
                    continue
                failed.discard(key)
                core.log("ai status", "%s history: %d incident%s, back to %s" % (
                    cls.NAME, len(events), "" if len(events) == 1 else "s",
                    date.fromtimestamp(covered).isoformat()), "notice")
            self.stop.wait(30)

    def reliability(self):
        """How often each AI service has been degraded or down, for its page."""
        return self.ai_history.report(watching=set(self.ai))

    def almanac(self, **thresholds):
        """Where today stands in the station's record, for the Almanac page.
        Under the state's lock, since the listener writes to the same tables
        and a dictionary that grows at midnight cannot be walked meanwhile."""
        with self.state.lock:
            body = self.history.almanac(**thresholds)
        body["available"] = bool(body.get("days"))
        body["backfill"] = (self.backfill.snapshot()["status"]
                            if self.backfill else "off")
        return body

    def server_config(self):
        """What this server was started with, for the settings page to show.

        None of it can be changed from a browser — it is read once, at start,
        from the environment — but until now finding out what was set meant
        an SSH session and a look at the compose file. Each row names the
        variable that sets it. No secrets: those are not here to show.
        """
        a = self.args
        months = lambda v: "twelve monthly figures" if v else ""
        rows = [
            ("Station name", a.name or "", "TEMPEST_NAME"),
            ("Location", "" if a.lat is None or a.lon is None
                else "%.2f, %.2f" % (a.lat, a.lon), "TEMPEST_LAT / TEMPEST_LON"),
            ("Units a new browser starts in", " · ".join(
                [a.temp_unit, a.wind_unit, a.pres_unit, a.rain_unit, a.dist_unit]),
             "TEMPEST_TEMP_UNIT and its kin"),
            ("Normal rainfall", months(self.rain_monthly), "TEMPEST_RAIN_MONTHLY"),
            ("Normal highs and lows", months(self.temp_normal),
             "TEMPEST_TEMP_NORMAL_HIGH / _LOW"),
            ("Internet plan", "" if not (a.plan_down or a.plan_up)
                else "%g down · %g up Mbps" % (a.plan_down or 0, a.plan_up or 0),
             "TEMPEST_PLAN_DOWN / _UP"),
            ("Speedtest Tracker", a.speedtest_url or "", "TEMPEST_SPEEDTEST_URL"),
            ("Observation stations", self._obs_station_names()
             or a.obs_stations or "nearest two, chosen by distance",
             "TEMPEST_OBS_STATIONS"),
            ("Hub broadcasts on", "UDP %s" % a.udp_port, "TEMPEST_UDP_PORT"),
            ("History kept in", a.data_dir, "TEMPEST_DATA_DIR"),
        ]
        off = [name for name, on in (("forecast", a.forecast), ("alerts", a.alerts),
                                     ("NWS forecast", a.nws),
                                     ("observations", a.observations)) if not on]
        if off:
            rows.append(("Switched off", ", ".join(off), "TEMPEST_NO_…"))
        return [{"what": w, "value": v, "env": e} for w, v, e in rows]

    RECORD_LABELS = (("hi", "Hottest"), ("lo", "Coldest"),
                     ("gust", "Strongest gust"), ("rain", "Wettest day"),
                     ("pmin", "Lowest pressure"), ("pmax", "Highest pressure"),
                     ("strikes", "Most strikes"), ("sun", "Sunniest day"),
                     ("swing", "Widest swing"))

    def _obs_station_names(self):
        st = getattr(self.observations, "stations", None)
        if not st:
            return ""
        return ", ".join("%s (%.0f mi)" % (name or sid, miles) for sid, name, miles in st)

    # ── Check now: one fetch, on demand, for the settings page ────────────
    CHECKABLE = ("hub", "wf", "forecast", "nws", "obs", "air", "pollen", "internet",
                 "ai")

    def check(self, source):
        """Fetch one source right now and say what happened: whether it
        worked, how long it took, and one line on what came back — or the
        failure in full. A success replaces the source's data, so the card
        refreshes too; a failure changes nothing the schedule holds.

        Nothing here echoes a URL or a response body: the WeatherFlow token
        travels in the URL, and a body can carry it back.
        """
        started = time.time()
        out = {"source": source, "at": started, "ok": False, "summary": "", "error": ""}
        if source not in self.CHECKABLE:
            out["error"] = "Unknown source"
            return out
        try:
            if source == "hub":
                seen = self.state.packets_in(60)
                out["ok"] = seen["total"] > 0
                if seen["total"]:
                    kinds = ", ".join("%s %d" % (k, n) for k, n in
                                      sorted(seen["by_type"].items(), key=lambda kv: -kv[1]))
                    out["summary"] = ("%d packets in the last minute (%s)"
                                      % (seen["total"], kinds)
                                      + (" from " + ", ".join(seen["from"]) if seen["from"] else ""))
                else:
                    out["error"] = ("Nothing heard on UDP %d in the last minute. The hub "
                                    "broadcasts; this host must share its subnet, and a "
                                    "container must use host networking." % self.args.udp_port)
            elif source == "wf":
                if not self.backfill:
                    out["error"] = "No WeatherFlow token is saved"
                else:
                    device = self.backfill._find_device()
                    out["ok"] = device is not None
                    out["summary"] = ("Token accepted; Tempest device %s found" % device
                                      if device is not None else "")
                    if device is None:
                        out["error"] = "Token accepted, but no Tempest device on the account"
            elif source == "ai":
                parts, failed = [], False
                for f in list(self.ai.values()):
                    try:
                        fresh = f.fetch_once()
                    except Exception as e:
                        failed = True
                        parts.append("%s: %s" % (f.NAME, f.describe(e)))
                        continue
                    with f.lock:
                        f.store(fresh); f.error = ""
                    f.after_success(fresh)
                    via = VIA_WORDS.get(fresh.get("via"))
                    parts.append("%s %s%s" % (f.NAME, fresh["status"].replace("_", " "),
                                             " (from %s)" % via if via else ""))
                out["ok"] = bool(parts) and not failed
                out["summary"] = " · ".join(parts)
                if failed:
                    out["error"] = out["summary"]
                elif not parts:
                    out["error"] = "All three services are switched off"
            elif source == "nws":
                parts = []
                for name, f in (("alerts", self.alerts), ("forecast text", self.nws)):
                    if f is None:
                        continue
                    fresh = f.fetch_once()
                    with f.lock:
                        f.store(fresh); f.error = ""
                    f.after_success(fresh)
                    if name == "alerts":
                        parts.append("%d active alert%s" % (len(fresh), "" if len(fresh) == 1 else "s"))
                    else:
                        parts.append("%d forecast periods from %s" % (
                            len(fresh.get("periods") or []), fresh.get("office") or "NWS"))
                out["ok"] = bool(parts)
                out["summary"] = " · ".join(parts) or ""
                if not parts:
                    out["error"] = "Alerts and the NWS forecast are switched off"
            else:
                f = {"forecast": self.forecast, "obs": self.observations,
                     "air": self.air, "pollen": self.pollen,
                     "internet": self.speedtest}[source]
                if f is None:
                    out["error"] = {"pollen": "No pollen key is saved",
                                    "internet": "No Speedtest token is saved"}.get(
                        source, "This source is switched off")
                else:
                    fresh = f.fetch_once()
                    with f.lock:
                        f.store(fresh); f.error = ""
                    f.after_success(fresh)
                    out["ok"] = True
                    out["summary"] = self._describe_fetch(source, fresh)
        except Exception as e:
            code = getattr(e, "code", None)
            out["error"] = "%s%s" % (
                (self._describer(source)(e) if self._describer(source) else
                 "%s (%s)" % (e.__class__.__name__, e)),
                " · HTTP %s" % code if code else "")
        out["took_ms"] = int((time.time() - started) * 1000)
        return out

    def _describer(self, source):
        f = {"forecast": self.forecast, "obs": self.observations, "air": self.air,
             "pollen": self.pollen, "internet": self.speedtest, "nws": self.nws,
             "wf": self.backfill}.get(source)
        return getattr(f, "describe", None)

    @staticmethod
    def _describe_fetch(source, fresh):
        fresh = fresh or {}
        if source == "forecast":
            cur = fresh.get("current") or {}
            return "%d days; now %s °C" % (len(fresh.get("days") or []), cur.get("temp_c"))
        if source == "obs":
            names = [st.get("station_name") or st.get("station")
                     for st in (fresh.get("stations") or [])]
            return "%s reporting: %s" % (", ".join(names) or "no station",
                                         fresh.get("kind") or "nothing falling")
        if source == "air":
            return "US AQI %s" % fresh.get("us_aqi")
        if source == "pollen":
            return "tree %s · grass %s · weed %s" % (
                fresh.get("tree"), fresh.get("grass"), fresh.get("weed"))
        if source == "internet":
            return "%d tests in the window; latest %s Mbps down, %s" % (
                fresh.get("tests") or 0,
                None if fresh.get("down") is None else round(fresh["down"]),
                fresh.get("status") or "")
        return "fetched"

    def record(self):
        """The all-time records, each with the day it was set, so that one
        which was never real can be struck; what has been struck already; and
        the state of the file all of it lives in."""
        with self.state.lock:
            temps = self.history.temp_record("all") or {}
            station = self.history.station_records()["all"]
            struck = [{"date": d, "field": f}
                      for d, fields in sorted(self.history.struck.items())
                      for f in fields]
            storage = self.history.storage_status()
        names = dict(self.RECORD_LABELS)
        rows = []
        for field, label in self.RECORD_LABELS:
            if field in ("hi", "lo"):
                got = temps.get(field)
                got = None if not got else {"v": got[0], "date": got[1]}
            else:
                got = station.get(field)
            if got:
                rows.append({"field": field, "label": label,
                             "v": got["v"], "date": got["date"]})
        for item in struck:
            item["label"] = names.get(item["field"], item["field"])
        return {"records": rows, "struck": struck, "storage": storage}

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
        if path not in ("/api/config", "/api/record", "/api/check", "/api/log", "/api/heart"):
            self._send(404, "Not found\n", "text/plain; charset=utf-8")
            return
        # There is no login on this dashboard, so require a header a plain
        # cross-site form cannot set. That blocks another page on the network
        # from quietly reconfiguring this one.
        # sendBeacon cannot carry the header, and a page's error report is
        # the one write worth accepting without it: it changes nothing, the
        # Origin check below still holds, and it is capped per address.
        if self.headers.get("X-Tempest-Config") != "1" and path != "/api/log":
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
        # An Apple Watch ECG export is about 120 KB; everything else is small.
        if length <= 0 or length > (600_000 if path == "/api/heart" else 8192):
            self._send(400, "Bad request body\n", "text/plain; charset=utf-8")
            return
        try:
            patch = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, "Malformed JSON\n", "text/plain; charset=utf-8")
            return

        dash = self.server.dashboard
        who = self.client_address[0] if self.client_address else "?"
        if path == "/api/log":
            # The page's own troubles — a script error, a watchdog reload — or
            # a test line from the settings page. Capped and rate-limited: a
            # page in a loop must not fill the log by itself.
            body = patch if isinstance(patch, dict) else {}
            if body.get("test"):
                core.log("syslog", "test line from the settings page (%s)" % who, "notice")
                out = core.LOG.syslog_status()
                out["ok"] = True
                self._send(200, json.dumps(out), "application/json; charset=utf-8")
                return
            kind = str(body.get("kind") or "error")[:20]
            text = " ".join(str(body.get("text") or "").split())[:300]
            if not text:
                self._send(400, json.dumps({"ok": False, "error": "Nothing to log"}),
                           "application/json; charset=utf-8")
                return
            if self.server.page_log_allowed(who):
                core.log("page", "%s %s: %s" % (who, kind, text),
                         "warning" if kind == "error" else "notice")
            self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8")
            return
        if path == "/api/heart":
            # {"csv": <an ECG export>}. Read here and kept as beat timings and
            # a shape; the file itself, and the name in its header, are not.
            text = (patch if isinstance(patch, dict) else {}).get("csv")
            try:
                if not isinstance(text, str):
                    raise ValueError("send the ECG file's text as csv")
                profile = core.parse_ecg(text)
            except ValueError as e:
                self._send(400, json.dumps({"ok": False, "error": str(e)}),
                           "application/json; charset=utf-8")
                return
            dash.config.set_heart(profile)
            core.log("settings", "heartbeat loaded: an ECG from %s, %d beats (%s)" % (
                profile["recorded"] or "an unknown date", len(profile["rr_ms"]) + 1, who), "notice")
            body = dash.config.public()
            body["ok"] = True
            self._send(200, json.dumps(body), "application/json; charset=utf-8")
            return
        if path == "/api/check":
            # A fetch on demand costs the outside service a call — Google
            # bills for pollen — so it sits behind the same header as a write.
            source = str((patch if isinstance(patch, dict) else {}).get("source") or "")
            body = dash.check(source)
            self._send(200 if body["ok"] or body.get("error") else 400,
                       json.dumps(body, allow_nan=False, default=str),
                       "application/json; charset=utf-8")
            return
        if path == "/api/record":
            # {"strike": {"date": ..., "field": ...}} or {"restore": {...}}
            if not isinstance(patch, dict):
                patch = {}
            what = patch.get("strike") or patch.get("restore")
            if not isinstance(what, dict):
                self._send(400, json.dumps({"ok": False, "error": "Nothing to do"}),
                           "application/json; charset=utf-8")
                return
            with dash.state.lock:
                err = dash.history.strike(str(what.get("date") or ""),
                                          str(what.get("field") or ""),
                                          restore="restore" in patch)
            if not err:
                core.log("record", "%s %s on %s (%s)" % (
                    "restored" if "restore" in patch else "struck",
                    what.get("field"), what.get("date"), who), "notice")
            body = dash.record()
            body["ok"], body["error"] = not err, err or ""
            self._send(400 if err else 200, json.dumps(body, allow_nan=False),
                       "application/json; charset=utf-8")
            return
        changed, err = dash.config.apply(patch)
        if err:
            self._send(400, json.dumps({"ok": False, "error": err}),
                       "application/json; charset=utf-8")
            return
        if changed:
            core.log("settings", "%s (%s)" % (", ".join(changed), who), "notice")
        if "syslog" in changed:
            dash.apply_syslog()
        if "token_set" in changed:
            dash.start_backfill()
        if "pollen_key_set" in changed:
            dash.start_pollen()
        if "speedtest_token_set" in changed:
            dash.start_speedtest()
        if "ai_status" in changed:
            dash.apply_ai_status()
        body = dash.config.public()
        body["ok"] = True
        body["changed"] = changed
        self._send(200, json.dumps(body), "application/json; charset=utf-8")

    @staticmethod
    def _days(query):
        """?days=N from the Internet page, held to something sensible."""
        try:
            n = int(urllib.parse.parse_qs(query or "").get("days", ["7"])[0])
        except (TypeError, ValueError):
            return 7
        return max(1, min(31, n))

    @staticmethod
    def _units(query, args):
        """?temp=&wind=&pres=&rain= for the CSV, each checked against the
        units the dashboard knows; anything else falls back to the server's
        own. The page passes its reader's choices, which live in the browser."""
        got = urllib.parse.parse_qs(query or "")
        out = {}
        for name, known, fallback in (
                ("temp", core.TEMP_UNITS, args.temp_unit),
                ("wind", core.WIND_UNITS, args.wind_unit),
                ("pres", core.PRES_UNITS, args.pres_unit),
                ("rain", core.RAIN_UNITS, args.rain_unit)):
            asked = got.get(name, [""])[0]
            # "F" and "C" as well as "°F": a degree sign in a URL is a nuisance.
            asked = {"F": "°F", "C": "°C"}.get(asked, asked)
            out[name] = asked if asked in known else (
                fallback if fallback in known else known[0])
        return out

    @staticmethod
    def _thresholds(query):
        """?warm=&hot=&frost= in °C, for the almanac's "last warm day" and its
        kin. The page sends them because the round number depends on the unit
        its reader thinks in — 80 °F is not a round number in Celsius — and
        that choice lives in the browser. Anything unreadable or absurd falls
        back to the default rather than failing the page."""
        got = urllib.parse.parse_qs(query or "")
        out = {}
        for name, key in (("warm", "warm_c"), ("hot", "hot_c"),
                          ("frost", "frost_c")):
            try:
                value = float(got.get(name, [""])[0])
            except (TypeError, ValueError):
                continue
            if -60.0 <= value <= 60.0:
                out[key] = value
        return out

    def do_GET(self):
        raw, _, query = self.path.partition("?")
        path = raw.rstrip("/") or "/"
        try:
            if raw.startswith("/testall/"):
                # The page fetches api/state relative to itself, so from
                # /testall/ it would ask for /testall/api/state.
                self.send_response(301)
                self.send_header("Location", "/testall" + ("?" + query if query else ""))
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path in ("/", "/index.html", "/testall"):
                # /testall is the same page; it loops through every preview.
                self._serve_file("index.html", "text/html; charset=utf-8")
            elif path == "/api/state":
                snap = self.server.dashboard.snapshot()
                Dashboard.split_slow(snap)
                body = json.dumps(snap, allow_nan=False, default=str)
                self._send(200, body, "application/json; charset=utf-8")
            elif path == "/api/series":
                # The slow half of the snapshot: the 24-hour series and the
                # Internet card's history. Polled every thirty seconds.
                slow = Dashboard.split_slow(self.server.dashboard.snapshot())
                body = json.dumps(slow, allow_nan=False, default=str)
                self._send(200, body, "application/json; charset=utf-8")
            elif path == "/api/internet":
                # Fetched only when the Internet page is opened, not with
                # every snapshot: a week of results is far too much to send
                # twice a second.
                dash = self.server.dashboard
                if not dash.speedtest:
                    body = {"available": False,
                            "error": "Add a Speedtest token in settings"}
                else:
                    try:
                        body = dash.speedtest.long_history(self._days(query))
                    except Exception as e:
                        body = {"available": False,
                                "error": dash.speedtest.describe(e)}
                self._send(200, json.dumps(body), "application/json; charset=utf-8")
            elif path == "/api/reliability":
                # Fetched when the reliability page is opened, like the almanac.
                self._send(200, json.dumps(self.server.dashboard.reliability(),
                                           allow_nan=False),
                           "application/json; charset=utf-8")
            elif path == "/api/almanac":
                # Fetched when the Almanac page is opened: a year of daily
                # rows is no more use in the two-second snapshot than a week
                # of speedtests was.
                dash = self.server.dashboard
                body = dash.almanac(**self._thresholds(query))
                self._send(200, json.dumps(body, allow_nan=False),
                           "application/json; charset=utf-8")
            elif path == "/api/log":
                # The last few hundred lines, for the settings page: what the
                # container's log has, without an SSH session to read it.
                body = {"lines": core.LOG.recent(300),
                        "syslog": core.LOG.syslog_status()}
                self._send(200, json.dumps(body, allow_nan=False, default=str),
                           "application/json; charset=utf-8")
            elif path == "/api/record":
                # The records as they stand, what has been struck from them,
                # and whether the file they live in is safe. For settings.
                self._send(200, json.dumps(self.server.dashboard.record(),
                                           allow_nan=False),
                           "application/json; charset=utf-8")
            elif path == "/api/days.csv":
                dash = self.server.dashboard
                with dash.state.lock:
                    text = dash.history.csv(**self._units(query, dash.args))
                name = "tempest-daily-record-%s.csv" % date.today().isoformat()
                self._send(200, text, "text/csv; charset=utf-8", extra={
                    "Content-Disposition": 'attachment; filename="%s"' % name})
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
                cfg["server"] = self.server.dashboard.server_config()
                self._send(200, json.dumps(cfg),
                           "application/json; charset=utf-8")
            elif path == "/healthz":
                state = self.server.dashboard.state
                # "ok" still means the server is up, which is what the
                # container's healthcheck asks: restarting it would not free
                # a full disk. "saving" is the honest answer to the other
                # question, for anything that wants to alert on it.
                storage = self.server.dashboard.history.storage_status()
                self._send(200, json.dumps({"ok": True,
                                            "health": state.health(),
                                            "saving": storage["ok"],
                                            "storage_error": storage["error"],
                                            "version": core.VERSION}),
                           "application/json")
            elif path.startswith("/fonts/") or path in ICON_PATHS:
                self._serve_static(path)
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
    p.add_argument("--speedtest-url",
                   default=env_default("TEMPEST_SPEEDTEST_URL",
                                       "http://127.0.0.1:8080"),
                   help="base URL of your Speedtest Tracker instance. The "
                        "token itself is set on the settings page, not here")
    p.add_argument("--plan-down", type=float,
                   default=env_default("TEMPEST_PLAN_DOWN", 0.0, float),
                   help="advertised download rate in Mbps, so the card can "
                        "say what fraction of it you are getting")
    p.add_argument("--plan-up", type=float,
                   default=env_default("TEMPEST_PLAN_UP", 0.0, float),
                   help="advertised upload rate in Mbps")
    p.add_argument("--rain-normal", type=float,
                   default=env_default("TEMPEST_RAIN_NORMAL", 0.0, float),
                   help="a normal year's rainfall where you are, in inches, "
                        "so the card can fill against it. NOAA publishes "
                        "these; without one the card just shows the total")
    p.add_argument("--rain-monthly",
                   default=env_default("TEMPEST_RAIN_MONTHLY", ""),
                   help="twelve normal monthly rainfalls in inches, January "
                        "first, comma separated. More accurate than "
                        "--rain-normal, which it overrides: rain is not "
                        "spread evenly through a year, and the card's pace "
                        "mark is only honest if it knows the shape")
    p.add_argument("--temp-normal-high",
                   default=env_default("TEMPEST_TEMP_NORMAL_HIGH", ""),
                   help="twelve normal monthly high temperatures in "
                        "Fahrenheit, January first, comma separated")
    p.add_argument("--temp-normal-low",
                   default=env_default("TEMPEST_TEMP_NORMAL_LOW", ""),
                   help="twelve normal monthly low temperatures in "
                        "Fahrenheit. Both are needed: the Temperature card "
                        "draws them as a band behind its trace, and one line "
                        "alone would be the daily mean, which every day "
                        "crosses twice")
    p.add_argument("--no-nws", dest="nws", action="store_false",
                   default=not env_default("TEMPEST_NO_NWS", ""),
                   help="do not fetch the National Weather Service's written "
                        "forecast for the outlook page")
    p.add_argument("--no-observations", dest="observations", action="store_false",
                   default=not env_default("TEMPEST_NO_OBSERVATIONS", ""),
                   help="do not ask the nearest NWS station what is falling "
                        "(snow and freezing rain, which the Tempest cannot see)")
    p.add_argument("--obs-stations", default=env_default("TEMPEST_OBS_STATIONS", ""),
                   help="NWS stations to ask what is falling, comma-separated "
                        "(e.g. KDPA,KDKB); unset picks the two nearest")
    p.add_argument("--no-alerts", dest="alerts", action="store_false",
                   default=not env_default("TEMPEST_NO_ALERTS", ""),
                   help="do not fetch National Weather Service alerts")
    p.add_argument("--slots", default=env_default(
                       "TEMPEST_SLOTS",
                       "temperature,wind,pressure,rainfall,astronomy,forecast"),
                   help="comma-separated cards, in order. Choose from: "
                        + ", ".join(CARD_NAMES) + ", blank")
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

    known = set(CARD_NAMES) | {"blank"}
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
    # Five is the standard library's default backlog: the sixth connection
    # arriving while the others are still being accepted gets a reset, not a
    # wait. The test suite opens a dozen pages at once, and a house with a
    # television, two phones and a laptop all reloading after a deploy is
    # not far off that.
    ThreadingHTTPServer.request_queue_size = 64
    httpd = ThreadingHTTPServer((args.host, args.http_port), Handler)
    page_log_seen = {}
    page_log_lock = threading.Lock()

    def page_log_allowed(who, limit=10, per=60.0):
        """At most `limit` page reports a minute from one address."""
        now = time.time()
        with page_log_lock:
            times = [t for t in page_log_seen.get(who, []) if now - t < per]
            if len(times) >= limit:
                page_log_seen[who] = times
                return False
            times.append(now)
            page_log_seen[who] = times
            return True
    httpd.page_log_allowed = page_log_allowed
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
