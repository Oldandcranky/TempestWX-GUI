# =============================================================================
# Tempest Weather Station — shared core
# Version 3.1.0
#
# MIT License
# Copyright (c) 2026  Chris Goodman  &  Claude (Anthropic)
# See tempest_weather.py for the full licence text.
# =============================================================================
#
# Everything that is not a user interface: unit conversions, the derived
# meteorology, local sun and moon maths, the rolling history store, the UDP
# listener and the station state it feeds.
#
# Imported by both front ends:
#   tempest_weather.py  — the tkinter desktop dashboard
#   tempest_server.py   — the LAN web dashboard
#
# Standard library only.
# =============================================================================

import json
import math
import os
import socket
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta

VERSION      = "3.5.0"
DEFAULT_PORT = 50222

_HERE         = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(_HERE, "tempest_settings.json")
HISTORY_FILE  = os.path.join(_HERE, "tempest_history.json")

HISTORY_MAX      = 2880        # 48 h at one sample a minute
HISTORY_MIN_GAP  = 55          # seconds between retained samples
HISTORY_SAVE_SEC = 300         # flush to disk every 5 min
DAYS_MAX         = 36500       # days of daily record kept: a century, 10 MB
DAYS_FILE_NAME   = "tempest_days.json"
BACKUPS_KEPT     = 30          # dated copies of the daily record, one a day
LOW_SPACE_BYTES  = 500 * 1024 * 1024
DAY_FULL_MIN     = 1200        # minutes of coverage that make a day "whole"

STALE_AFTER   = 120            # seconds without a packet → amber
OFFLINE_AFTER = 360            # → red


PRECIP_TYPES = {0: "None", 1: "Rain", 2: "Hail", 3: "Rain + Hail"}
WIND_DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

TEMP_UNITS = ["°F", "°C", "K"]
WIND_UNITS = ["mph", "km/h", "m/s", "kts"]
PRES_UNITS = ["inHg", "hPa", "kPa", "mmHg"]
RAIN_UNITS = ["in", "mm"]
DIST_UNITS = ["mi", "km"]

BEAUFORT = [
    (0.3, "Calm"), (1.6, "Light air"), (3.4, "Light breeze"),
    (5.5, "Gentle breeze"), (8.0, "Moderate breeze"), (10.8, "Fresh breeze"),
    (13.9, "Strong breeze"), (17.2, "Near gale"), (20.8, "Gale"),
    (24.5, "Severe gale"), (28.5, "Storm"), (32.7, "Violent storm"),
]

# ─────────────────────────────────────────────── Is this reading real? ─────
#
# A record is kept for years, so one bad number from the sensor is a bad
# record for years. These are not forecasts of the weather, only the edges of
# what the instrument can mean: outside them the reading is a fault, and it is
# dropped rather than remembered. Pressure is station pressure, so the floor
# has to leave room for a station up a mountain.
BOUNDS = {
    "temp_c": (-60.0, 60.0), "rh": (0.0, 100.0), "pres_mb": (500.0, 1100.0),
    "wind_lull_ms": (0.0, 75.0), "wind_avg_ms": (0.0, 75.0),
    "wind_gust_ms": (0.0, 75.0), "uv": (0.0, 20.0), "solar": (0.0, 1600.0),
    "lux": (0.0, 200000.0),
    "rain_mm": (0.0, 15.0),          # in one minute; the world record is 31
}
# The most a reading may move between two observations a few minutes apart.
# Air does not warm eight degrees in a minute; a sensor that says so is
# glitching. Three in a row, though, and it is the world that changed.
JUMPS = {"temp_c": 8.0, "pres_mb": 10.0, "rh": 40.0}
JUMP_WINDOW_S = 600
JUMP_PERSISTS = 3


def plausible(key, value):
    """True unless `value` is outside what `key`'s sensor can mean."""
    limits = BOUNDS.get(key)
    if limits is None or value is None:
        return True
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and limits[0] <= v <= limits[1]


# ────────────────────────────────────────────────── Unit conversions ───────

def deg_to_compass(deg):
    return "---" if deg is None else WIND_DIRS[round(deg / 22.5) % 16]


def convert_temp(c, unit):
    if c is None:
        return None
    if unit == "°F":
        return round(c * 9 / 5 + 32, 1)
    if unit == "K":
        return round(c + 273.15, 1)
    return round(c, 1)


def temp_delta(delta_c, unit):
    """A temperature *difference* converts without the offset term."""
    if delta_c is None:
        return None
    return round(delta_c * 9 / 5, 1) if unit == "°F" else round(delta_c, 1)


def convert_wind(ms, unit):
    if ms is None:
        return None
    if unit == "km/h":
        return round(ms * 3.6, 1)
    if unit == "mph":
        return round(ms * 2.23694, 1)
    if unit == "kts":
        return round(ms * 1.94384, 1)
    return round(ms, 1)


def convert_pres(mb, unit):
    if mb is None:
        return None
    if unit == "inHg":
        return round(mb * 0.02953, 3)
    if unit == "kPa":
        return round(mb * 0.1, 2)
    if unit == "mmHg":
        return round(mb * 0.750062, 1)
    return round(mb, 1)


def convert_rain(mm, unit):
    if mm is None:
        return None
    return round(mm / 25.4, 2) if unit == "in" else round(mm, 1)


def convert_dist(km, unit):
    if km is None:
        return None
    return round(km * 0.621371, 1) if unit == "mi" else round(km, 1)


def pres_decimals(unit):
    return {"inHg": 3, "kPa": 2}.get(unit, 1)


def rain_decimals(unit):
    return 2 if unit == "in" else 1


def dew_point_c(temp_c, rh):
    """Magnus-Tetens dew point."""
    if temp_c is None or rh is None or rh <= 0:
        return None
    a, b = 17.27, 237.7
    gamma = (a * temp_c / (b + temp_c)) + math.log(rh / 100.0)
    return b * gamma / (a - gamma)


def heat_index_f(tf, rh):
    """Rothfusz heat index; None when the conditions don't apply."""
    if tf is None or rh is None or tf < 80 or rh < 40:
        return None
    return (-42.379 + 2.04901523 * tf + 10.14333127 * rh
            - 0.22475541 * tf * rh - 6.83783e-3 * tf ** 2
            - 5.481717e-2 * rh ** 2 + 1.22874e-3 * tf ** 2 * rh
            + 8.5282e-4 * tf * rh ** 2 - 1.99e-6 * tf ** 2 * rh ** 2)


def wind_chill_f(tf, mph):
    """NWS wind chill; valid at or below 50 °F with wind above 3 mph."""
    if tf is None or mph is None or tf > 50 or mph <= 3:
        return None
    return (35.74 + 0.6215 * tf - 35.75 * mph ** 0.16
            + 0.4275 * tf * mph ** 0.16)


def apparent_temp_c(temp_c, rh, wind_ms):
    """Feels-like in °C, plus the model used ('' when it is just the air)."""
    if temp_c is None:
        return None, ""
    tf = temp_c * 9 / 5 + 32
    hi = heat_index_f(tf, rh)
    if hi is not None:
        return (hi - 32) * 5 / 9, "Heat index"
    wc = wind_chill_f(tf, None if wind_ms is None else wind_ms * 2.23694)
    if wc is not None:
        return (wc - 32) * 5 / 9, "Wind chill"
    return temp_c, ""


def uv_category(uv):
    if uv is None:
        return ""
    for limit, name in ((3, "Low"), (6, "Moderate"), (8, "High"),
                        (11, "Very high")):
        if uv < limit:
            return name
    return "Extreme"


def beaufort(ms):
    """(force number, description) for a wind speed in m/s."""
    if ms is None:
        return None, ""
    for i, (limit, name) in enumerate(BEAUFORT):
        if ms < limit:
            return i, name
    return 12, "Hurricane force"


def comfort_words(temp_c, rh):
    """The two-word mood line under the temperature, e.g. 'WARM · MUGGY'."""
    if temp_c is None:
        return ""
    tf = temp_c * 9 / 5 + 32
    for limit, word in ((10, "Frigid"), (32, "Freezing"), (45, "Cold"),
                        (60, "Chilly"), (72, "Mild"), (82, "Warm"),
                        (95, "Hot")):
        if tf < limit:
            break
    else:
        word = "Sweltering"

    dp = dew_point_c(temp_c, rh)
    if dp is None:
        return word
    dpf = dp * 9 / 5 + 32
    for limit, second in ((35, "Very dry"), (50, "Dry"), (60, "Comfortable"),
                          (65, "Sticky"), (70, "Muggy"), (75, "Oppressive")):
        if dpf < limit:
            break
    else:
        second = "Tropical"
    return "%s · %s" % (word, second)


def pressure_trend_label(delta_mb):
    """Standard barometric tendency wording for a 3-hour change."""
    if delta_mb is None:
        return "", "flat"
    a = abs(delta_mb)
    if a < 0.5:
        return "Steady", "flat"
    word, arrow = ("Rising", "up") if delta_mb > 0 else ("Falling", "down")
    if a >= 3.5:
        return word + " rapidly", arrow
    if a >= 1.5:
        return word, arrow
    return word + " slowly", arrow


def pressure_outlook(delta_mb):
    """The all-caps forecast line under the barometer."""
    if delta_mb is None:
        return "Building a 3-hour baseline"
    if delta_mb <= -3.5:
        return "Storm likely — pressure dropping fast"
    if delta_mb <= -1.5:
        return "Unsettled weather approaching"
    if delta_mb <= -0.5:
        return "Slow decline — watch for cloud"
    if delta_mb < 0.5:
        return "Little change expected"
    if delta_mb < 1.5:
        return "Slowly improving"
    if delta_mb < 3.5:
        return "Clearing and settling"
    return "Strong clearing — brisk wind possible"


SENSOR_FAULTS = [
    (0x000000001, "lightning sensor failed"),
    (0x000000002, "lightning noise"),
    (0x000000004, "lightning disturber"),
    (0x000000008, "pressure sensor failed"),
    (0x000000010, "temperature sensor failed"),
    (0x000000020, "humidity sensor failed"),
    (0x000000040, "wind sensor failed"),
    (0x000000080, "precipitation sensor failed"),
    (0x000000100, "light / UV sensor failed"),
]


def sensor_faults(status):
    """Human-readable faults from a device_status sensor_status bitfield."""
    try:
        bits = int(status)
    except (TypeError, ValueError):
        return []
    return [name for mask, name in SENSOR_FAULTS if bits & mask]


def format_uptime(seconds):
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return None
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        return "%dh %dm" % (s // 3600, (s % 3600) // 60)
    return "%dd %dh" % (s // 86400, (s % 86400) // 3600)


def battery_pct(volts):
    """Rough Tempest state of charge: 2.355 V empty, 2.80 V full."""
    if volts is None:
        return None
    return max(0.0, min(1.0, (volts - 2.355) / (2.80 - 2.355)))


# ─────────────────────────────────────────── Astronomy (no network) ────────

def _sun_event_epochs(lat, lon, day):
    """Sunrise/sunset as unix epochs for `day`, via the standard NOAA
    approximation. Returns (rise, set, kind) where kind is 'normal',
    'up' (midnight sun) or 'down' (polar night)."""
    rad = math.radians
    n = (day.toordinal() - date(2000, 1, 1).toordinal()) + 0.0008
    j_star = n - lon / 360.0
    m = (357.5291 + 0.98560028 * j_star) % 360.0
    c = (1.9148 * math.sin(rad(m)) + 0.0200 * math.sin(rad(2 * m))
         + 0.0003 * math.sin(rad(3 * m)))
    lam = (m + c + 180.0 + 102.9372) % 360.0
    j_transit = (2451545.0 + j_star + 0.0053 * math.sin(rad(m))
                 - 0.0069 * math.sin(rad(2 * lam)))
    decl = math.asin(math.sin(rad(lam)) * math.sin(rad(23.44)))
    try:
        cos_w = ((math.sin(rad(-0.833)) - math.sin(rad(lat)) * math.sin(decl))
                 / (math.cos(rad(lat)) * math.cos(decl)))
    except ZeroDivisionError:
        return None, None, "normal"
    if cos_w < -1:
        return None, None, "up"
    if cos_w > 1:
        return None, None, "down"
    w = math.degrees(math.acos(cos_w))

    def to_epoch(j):
        return (j - 2440587.5) * 86400.0

    return (to_epoch(j_transit - w / 360.0),
            to_epoch(j_transit + w / 360.0), "normal")


def sun_times(lat, lon, when=None):
    """(sunrise, sunset, kind) for the day of `when`, as datetimes on *this
    machine's* clock — right when the PC sits near the station, which is the
    normal case for a hub on the same LAN."""
    when = when or datetime.now()
    rise, sset, kind = _sun_event_epochs(lat, lon, when.date())
    if rise is None:
        return None, None, kind
    return (datetime.fromtimestamp(rise), datetime.fromtimestamp(sset), kind)


MOON_PHASES = [
    (1.0, "New moon"), (6.4, "Waxing crescent"), (8.4, "First quarter"),
    (13.8, "Waxing gibbous"), (15.8, "Full moon"), (21.3, "Waning gibbous"),
    (23.3, "Last quarter"), (28.5, "Waning crescent"), (30.0, "New moon"),
]
SYNODIC = 29.530588853
_NEW_MOON_EPOCH = 947182440.0          # 2000-01-06 18:14 UTC


def moon_state(now=None):
    """(age in days, illuminated fraction, phase name)."""
    now = time.time() if now is None else now
    age = ((now - _NEW_MOON_EPOCH) / 86400.0) % SYNODIC
    illum = (1.0 - math.cos(2 * math.pi * age / SYNODIC)) / 2.0
    for limit, name in MOON_PHASES:
        if age < limit:
            return age, illum, name
    return age, illum, "New moon"


# ──────────────────────────────────────────── History & rain records ───────

class History:
    """Rolling 48 h of samples, thinned to one a minute and saved to disk so
    trends and 24-hour extremes survive a restart — and, beside them, one
    small summary per day that is kept for years.

    The samples answer "what has the last day been like"; they are gone after
    two. The daily record is what is left of a day once it is over: its peak
    gust, its pressure range, how much sun it had, how many strikes. Without
    it the strongest gust the station ever measured is forgotten on the third
    morning.
    """

    KEYS = ("temp_c", "pres_mb", "wind_ms", "gust_ms", "rh", "dir", "battery")

    # What a day's summary may hold, all in canonical units. Rain and the
    # temperature high and low predate this and keep their own tables.
    #   gust, gust_t   peak gust in m/s, and when
    #   pmin, pmax     station pressure, mb
    #   rhmin, rhmax   humidity, %
    #   uv, solar      peak UV index and peak W/m²
    #   sun            solar energy over the day, Wh/m²
    #   strikes        lightning strikes counted
    #   tmean, tn      mean temperature, and the readings behind it
    #   wind, wn       mean wind speed, likewise
    #   mins           minutes of the day actually observed
    DAY_MAXES = ("gust", "pmax", "rhmax", "uv", "solar")
    DAY_MINS = ("pmin", "rhmin")
    DAY_NUMBERS = DAY_MAXES + DAY_MINS + ("gust_t", "sun", "strikes", "tmean",
                                          "tn", "wind", "wn", "mins")
    # Readings that can be struck from the record as not real.
    STRIKABLE = ("hi", "lo", "swing", "rain", "gust", "pmin", "pmax",
                 "strikes", "sun")

    def __init__(self, path=None):
        self.path = path or HISTORY_FILE
        self.samples = deque(maxlen=HISTORY_MAX)
        self.rain_days = {}        # "YYYY-MM-DD" → mm
        self.temp_days = {}        # "YYYY-MM-DD" → {"lo": °C, "hi": °C}
        self.days = {}             # "YYYY-MM-DD" → the day's summary, above
        self.struck = {}           # "YYYY-MM-DD" → readings judged not real
        self.meta = {}             # bookkeeping, e.g. when rain was swept
        self.notices = []          # what storage had to do, for the page
        self.saved_at = None
        self.save_failing_since = None
        self._save_errors = {}     # file name → why it would not save
        self._days_locked = False  # never write over a file we cannot read
        self._backup_day = None
        self._backup_count = None
        self._last_saved = 0.0
        self._last_obs_t = None    # for the minutes between live readings
        self.load()

    # ── persistence ───────────────────────────────────────────────────────

    # The daily record is the part of this worth protecting: years of it, a
    # few hundred bytes a day, and without a WeatherFlow token the only copy.
    # It used to share a file with the 48 hours of samples, rewritten whole
    # every five minutes with nothing flushed; a file that failed to parse
    # was treated as no file, and overwritten with an empty one at the next
    # save; and a save that failed — a full disk — failed in silence. So: its
    # own file, flushed before it replaces the old one; a damaged file is set
    # aside and the record restored from a dated backup; and every failure is
    # kept where the page can show it.

    @property
    def days_path(self):
        return os.path.join(os.path.dirname(self.path), DAYS_FILE_NAME)

    @property
    def backups_dir(self):
        return os.path.join(os.path.dirname(self.path), "backups")

    @staticmethod
    def _read_json(path):
        """(data, problem): problem is None, "missing", or why it failed."""
        # Only an ordinary file can be damaged. Anything else — no file, a
        # directory, /dev/null standing in for "keep nothing" — is simply not
        # a record, and must never be renamed out of the way.
        if not os.path.isfile(path):
            return None, "missing"
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except OSError as e:
            return None, "unreadable (%s)" % (e.strerror or e.__class__.__name__)
        except ValueError:
            return None, "damaged (not valid JSON)"
        if not isinstance(raw, dict):
            return None, "damaged (not a record)"
        return raw, None

    def _set_aside(self, path):
        """Move a file we could not read out of the way, under a name that
        says when. None if even that failed."""
        aside = "%s.damaged-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        try:
            os.replace(path, aside)
            return aside
        except OSError:
            return None

    def _note(self, text):
        self.notices.append(text)
        del self.notices[:-5]
        print("  storage  : " + text, flush=True)

    def _load_tables(self, raw):
        """The daily tables out of a parsed file. True if it held any."""
        found = False
        meta = raw.get("meta")
        if isinstance(meta, dict):
            self.meta.update(meta)
        temps = raw.get("temp_days")
        if isinstance(temps, dict):
            for k, v in temps.items():
                if isinstance(v, dict) and "lo" in v and "hi" in v:
                    try:
                        self.temp_days[str(k)] = {"lo": float(v["lo"]),
                                                  "hi": float(v["hi"])}
                        found = True
                    except (TypeError, ValueError):
                        continue
        days = raw.get("rain_days")
        if isinstance(days, dict):
            for k, v in days.items():
                try:
                    self.rain_days[str(k)] = float(v)
                    found = True
                except (TypeError, ValueError):
                    continue
            self._trim_rain_days()
        summaries = raw.get("days")
        if isinstance(summaries, dict):
            for k, v in summaries.items():
                if not isinstance(v, dict):
                    continue
                rec = {}
                for key in self.DAY_NUMBERS:
                    try:
                        if v.get(key) is not None and math.isfinite(float(v[key])):
                            rec[key] = float(v[key])
                    except (TypeError, ValueError):
                        continue
                if rec:
                    self.days[str(k)] = rec
                    found = True
            self._trim(self.days)
        struck = raw.get("struck")
        if isinstance(struck, dict):
            for k, fields in struck.items():
                if isinstance(fields, list):
                    keep = sorted({str(f) for f in fields if f in self.STRIKABLE})
                    if keep:
                        self.struck[str(k)] = keep
        return found

    def load(self):
        raw, problem = self._read_json(self.path)
        if problem and problem != "missing":
            aside = self._set_aside(self.path)
            self._note("%s was %s; %s" % (
                os.path.basename(self.path), problem,
                "kept as " + os.path.basename(aside) if aside
                else "it could not be moved aside"))
            raw = None
        raw = raw or {}
        cutoff = time.time() - 48 * 3600
        for smp in raw.get("samples", []):
            try:
                if float(smp["t"]) >= cutoff:
                    self.samples.append(smp)
            except (TypeError, ValueError, KeyError):
                continue

        days, problem = self._read_json(self.days_path)
        if days is not None:
            self._load_tables(days)
        elif problem == "missing":
            # Before the daily record had a file of its own it lived in the
            # samples file. Take it from there, once; the next save moves it.
            self._load_tables(raw)
        else:
            aside = self._set_aside(self.days_path)
            if aside is None:
                # Cannot read it and cannot move it. Writing over it is the
                # one thing that would make this worse.
                self._days_locked = True
                self._note("%s is %s and could not be moved aside. It will "
                           "not be written to until that is fixed."
                           % (DAYS_FILE_NAME, problem))
            else:
                self._note("%s was %s; kept as %s" % (
                    DAYS_FILE_NAME, problem, os.path.basename(aside)))
            restored = self._restore_from_backup()
            if restored:
                self._note("The daily record was restored from the backup of "
                           + restored)
            elif self._load_tables(raw):
                self._note("The daily record was restored from the copy in "
                           + os.path.basename(self.path))
            else:
                self._note("No backup of the daily record could be read. "
                           "With a WeatherFlow token it will be rebuilt.")
                self.meta.pop("sweep_keeps", None)     # walk the archive again

    @staticmethod
    def _trim(table):
        if len(table) > DAYS_MAX:
            for key in sorted(table)[:-DAYS_MAX]:
                del table[key]

    # The rain table was capped at 800 days, which is two years and a bit:
    # "all time" would have started forgetting in the station's third summer.
    def _trim_rain_days(self):
        self._trim(self.rain_days)

    def _write_json(self, path, payload):
        """Write a file so that a crash or a power cut leaves either the old
        one or the new one, never half of each: a temporary file, flushed to
        the disk, then swapped in. The failure, if any, is kept."""
        name = os.path.basename(path)
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            try:                              # and the rename itself
                fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass                          # not every filesystem allows it
        except (OSError, ValueError) as e:
            first = name not in self._save_errors
            self._save_errors[name] = "Cannot save %s — %s" % (
                name, getattr(e, "strerror", None) or e)
            if self.save_failing_since is None:
                self.save_failing_since = time.time()
            if first:
                print("  storage  : " + self._save_errors[name], flush=True)
            return False
        if self._save_errors.pop(name, None):
            print("  storage  : saving %s again" % name, flush=True)
        if not self._save_errors:
            self.save_failing_since = None
        return True

    def _days_payload(self):
        return {"version": 1, "saved_at": time.time(),
                "rain_days": self.rain_days, "temp_days": self.temp_days,
                "days": self.days, "struck": self.struck, "meta": self.meta}

    def day_count(self):
        return len(set(self.days) | set(self.rain_days) | set(self.temp_days))

    def save(self, force=False):
        now = time.time()
        if not force and now - self._last_saved < HISTORY_SAVE_SEC:
            return
        if os.path.exists(self.path) and not os.path.isfile(self.path):
            return                     # pointed at /dev/null: keep nothing
        self._last_saved = now
        kept = False
        if not self._days_locked:
            kept = self._write_json(self.days_path, self._days_payload())
        samples = {"samples": list(self.samples)}
        if not kept:
            # The daily record could not go to its own file, so it rides
            # along here as it used to. Two chances are better than none.
            samples.update(rain_days=self.rain_days, temp_days=self.temp_days,
                           days=self.days, struck=self.struck, meta=self.meta)
        if self._write_json(self.path, samples) or kept:
            self.saved_at = now
        if kept:
            self._backup_if_due()

    # ── backups ───────────────────────────────────────────────────────────

    def _backups(self):
        """[(date_iso, path)] of the dated copies, newest first."""
        try:
            names = os.listdir(self.backups_dir)
        except OSError:
            return []
        out = []
        for name in names:
            if name.startswith("tempest_days-") and name.endswith(".json"):
                day = name[len("tempest_days-"):-len(".json")]
                try:
                    date.fromisoformat(day)
                except ValueError:
                    continue
                out.append((day, os.path.join(self.backups_dir, name)))
        return sorted(out, reverse=True)

    def _backup_if_due(self, today=None):
        """One dated copy of the daily record a day; thirty kept, and the
        first of every month kept for good. They are a kilobyte a day."""
        today = (today or date.today()).isoformat()
        if self._backup_day == today:
            return False
        have = self._backups()
        if have and have[0][0] == today:
            self._backup_day = today
            return False
        # A record that has collapsed must not be copied over the month of
        # good ones: thirty days of empty backups would push every real one
        # out. Measure against the newest backup before adding to them.
        count = self.day_count()
        if have and self._backup_count is None:
            prior, _why = self._read_json(have[0][1])
            if prior:
                self._backup_count = len(set(prior.get("days") or {})
                                         | set(prior.get("rain_days") or {})
                                         | set(prior.get("temp_days") or {}))
        if self._backup_count and self._backup_count >= 20 \
                and count < self._backup_count * 0.5:
            self._backup_day = today
            self._note("The daily record has shrunk from %d days to %d, so "
                       "today's backup was skipped and the older ones kept."
                       % (self._backup_count, count))
            return False
        if not count:
            return False
        try:
            os.makedirs(self.backups_dir, exist_ok=True)
        except OSError:
            pass
        path = os.path.join(self.backups_dir, "tempest_days-%s.json" % today)
        if not self._write_json(path, self._days_payload()):
            return False
        self._backup_day = today
        self._backup_count = count
        dailies = [b for b in self._backups() if not b[0].endswith("-01")]
        for _day, old in dailies[BACKUPS_KEPT:]:
            try:
                os.remove(old)
            except OSError:
                pass
        return True

    def _restore_from_backup(self):
        """Load the newest backup that can be read. Its date, or None."""
        for day, path in self._backups():
            raw, problem = self._read_json(path)
            if raw is not None and self._load_tables(raw):
                return day
        return None

    def storage_status(self):
        """What the page needs to say whether the record is safe."""
        backups = self._backups()
        try:
            size = os.path.getsize(self.days_path)
        except OSError:
            size = None
        free = None
        try:
            import shutil
            free = shutil.disk_usage(os.path.dirname(self.path) or ".").free
        except OSError:
            pass
        errors = list(self._save_errors.values())
        low = free is not None and free < LOW_SPACE_BYTES
        if low and not errors:
            errors.append("Only %d MB free where the history is kept"
                          % (free // (1024 * 1024)))
        return {"ok": not errors, "error": " · ".join(errors),
                "failing_since": self.save_failing_since,
                "saved_at": self.saved_at, "notices": list(self.notices),
                "locked": self._days_locked, "days": self.day_count(),
                "days_bytes": size, "free_bytes": free,
                "backups": len(backups),
                "last_backup": backups[0][0] if backups else None}

    # ── writing ───────────────────────────────────────────────────────────

    def add(self, data):
        now = time.time()
        if self.samples and now - self.samples[-1]["t"] < HISTORY_MIN_GAP:
            return
        sample = {"t": now}
        for key in self.KEYS:
            value = data.get(key)
            if value is not None:
                sample[key] = round(float(value), 3)
        self.samples.append(sample)
        self.save()

    def note_temp(self, day_iso, temp_c):
        """Widen a day's recorded high and low as live readings arrive, so
        today's record is current without waiting for the next sweep."""
        if temp_c is None:
            return
        try:
            t = float(temp_c)
        except (TypeError, ValueError):
            return
        day = self.temp_days.get(day_iso)
        if day is None:
            self.temp_days[day_iso] = {"lo": round(t, 2), "hi": round(t, 2)}
            return
        if t < day["lo"]:
            day["lo"] = round(t, 2)
        elif t > day["hi"]:
            day["hi"] = round(t, 2)

    def temp_record(self, scope="year", today=None):
        """Hottest and coldest over a scope, as
        {"hi": (°C, "YYYY-MM-DD"), "lo": (...), "days": n} — or None."""
        today = today or date.today()
        if scope == "month":
            keep = lambda d: d.startswith(today.strftime("%Y-%m-"))
        elif scope == "year":
            keep = lambda d: d.startswith(today.strftime("%Y-"))
        else:
            keep = lambda d: True
        rows = [(d, v) for d, v in self.temp_days.items() if keep(d)]
        his = [r for r in rows if not self.is_struck(r[0], "hi")]
        los = [r for r in rows if not self.is_struck(r[0], "lo")]
        if not his or not los:
            return None
        hi = max(his, key=lambda r: r[1]["hi"])
        lo = min(los, key=lambda r: r[1]["lo"])
        return {"hi": (hi[1]["hi"], hi[0]), "lo": (lo[1]["lo"], lo[0]),
                "days": len(rows)}

    def add_rain(self, day_iso, mm):
        if not mm:
            return
        self.rain_days[day_iso] = self.rain_days.get(day_iso, 0.0) + float(mm)
        self._trim_rain_days()

    # ── the daily record ──────────────────────────────────────────────────

    @classmethod
    def fold(cls, rec, ts, minutes, temp_c=None, pres_mb=None, wind_ms=None,
             gust_ms=None, rh=None, uv=None, solar=None):
        """Fold one reading, standing for `minutes` of the day, into a day's
        summary. The live feed and the backfill both come through here, so a
        day means the same thing whichever of them wrote it."""
        def num(v, key=None):
            try:
                v = float(v)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(v) or (key and not plausible(key, v)):
                return None
            return v

        minutes = max(0.0, num(minutes) or 0.0)
        temp_c, pres_mb = num(temp_c, "temp_c"), num(pres_mb, "pres_mb")
        wind_ms, rh = num(wind_ms, "wind_avg_ms"), num(rh, "rh")
        uv, solar = num(uv, "uv"), num(solar, "solar")
        gust = num(gust_ms, "wind_gust_ms")
        if gust is not None and gust > rec.get("gust", -1.0):
            rec["gust"] = round(gust, 2)
            if ts is not None:
                rec["gust_t"] = float(int(ts))
        for key, value in (("pmin", pres_mb), ("rhmin", rh)):
            value = num(value)
            if value is not None and value < rec.get(key, float("inf")):
                rec[key] = round(value, 2)
        for key, value in (("pmax", pres_mb), ("rhmax", rh), ("uv", uv),
                           ("solar", solar)):
            value = num(value)
            if value is not None and value > rec.get(key, float("-inf")):
                rec[key] = round(value, 2)
        for key, count, value in (("tmean", "tn", temp_c),
                                  ("wind", "wn", wind_ms)):
            value = num(value)
            if value is None:
                continue
            n = rec.get(count, 0.0)
            rec[key] = round((rec.get(key, 0.0) * n + value) / (n + 1.0), 3)
            rec[count] = n + 1.0
        sun = num(solar)
        if sun is not None and minutes:
            rec["sun"] = round(rec.get("sun", 0.0) + sun * minutes / 60.0, 2)
        rec["mins"] = round(rec.get("mins", 0.0) + minutes, 2)
        return rec

    def note_obs(self, day_iso, ts, **reading):
        """A live reading. The minutes it stands for are however long it has
        been since the last one — capped, so a morning the server spent
        switched off is not credited to the first reading after it."""
        ts = float(ts if ts is not None else time.time())
        gap = 60.0 if self._last_obs_t is None else ts - self._last_obs_t
        self._last_obs_t = ts
        minutes = min(max(gap, 0.0), 300.0) / 60.0
        self.fold(self.days.setdefault(day_iso, {}), ts, minutes, **reading)
        self._trim(self.days)

    def note_strikes(self, day_iso, count):
        if not count:
            return
        rec = self.days.setdefault(day_iso, {})
        rec["strikes"] = rec.get("strikes", 0.0) + float(count)

    def merge_day(self, day_iso, other):
        """Fold a summary built elsewhere — the backfill — into ours.

        Extremes are a union: both are the station's own measurements, so the
        higher gust is the day's gust whoever saw it. Totals and means cannot
        be combined that way, so they are taken whole from whichever side
        watched more of the day. Running it twice changes nothing.
        """
        if not other:
            return False
        rec = self.days.get(day_iso)
        before = dict(rec) if rec else None
        if rec is None:
            rec = self.days[day_iso] = {}
        if other.get("gust") is not None and other["gust"] > rec.get("gust", -1.0):
            rec["gust"] = other["gust"]
            if other.get("gust_t") is not None:
                rec["gust_t"] = other["gust_t"]
        for key in self.DAY_MINS:
            if other.get(key) is not None:
                rec[key] = min(rec.get(key, other[key]), other[key])
        for key in self.DAY_MAXES:
            if key != "gust" and other.get(key) is not None:
                rec[key] = max(rec.get(key, other[key]), other[key])
        if other.get("strikes"):
            rec["strikes"] = max(rec.get("strikes", 0.0), other["strikes"])
        if other.get("mins", 0.0) > rec.get("mins", 0.0) + 30.0:
            for key in ("tmean", "tn", "wind", "wn", "sun", "mins"):
                if other.get(key) is not None:
                    rec[key] = other[key]
        self._trim(self.days)
        return rec != before

    def is_struck(self, day_iso, field):
        return field in self.struck.get(day_iso, ())

    def strike(self, day_iso, field, restore=False):
        """Mark one reading on one day as not real — or take the mark off.

        The number is kept, not deleted: the backfill would only put it back,
        and a mark can be lifted if it turns out the gust was real after all.
        A struck reading is left out of records, the almanac and the totals.
        Returns an error string, or None.
        """
        if field not in self.STRIKABLE:
            return "Unknown reading: %s" % field
        try:
            date.fromisoformat(str(day_iso))
        except ValueError:
            return "Not a date: %s" % day_iso
        fields = set(self.struck.get(day_iso, ()))
        if restore:
            fields.discard(field)
        else:
            fields.add(field)
        if fields:
            self.struck[day_iso] = sorted(fields)
        else:
            self.struck.pop(day_iso, None)
        self._records_held = None
        self.save(force=True)
        return None

    def day_rows(self, since=None, until=None, raw=False):
        """Every day anything is known about, oldest first, as one flat dict
        each: the summary, plus the rain and temperature tables' figures.
        Readings struck from the record are left out, unless `raw`."""
        keys = set(self.days) | set(self.rain_days) | set(self.temp_days)
        rows = []
        for day in sorted(keys):
            if (since and day < since) or (until and day > until):
                continue
            row = {"date": day}
            row.update(self.days.get(day) or {})
            t = self.temp_days.get(day)
            if t:
                row["lo"], row["hi"] = t["lo"], t["hi"]
            # A day the station was up and measured nothing is a dry day; a
            # day missing from every table is not a day we know about.
            row["rain"] = self.rain_days.get(day, 0.0)
            struck = self.struck.get(day)
            if struck and raw:
                row["struck"] = list(struck)
            elif struck:
                for field in struck:
                    if field == "rain":
                        row["rain"] = 0.0          # it did not rain, then
                    elif field == "gust":
                        row.pop("gust", None)
                        row.pop("gust_t", None)
                    elif field == "swing":
                        row["no_swing"] = True
                    else:
                        row.pop(field, None)
            rows.append(row)
        return rows

    def csv(self, temp="°C", wind="m/s", pres="hPa", rain="mm"):
        """The whole daily record as CSV text, one row a day, oldest first.

        The way out: years of a station's days belong in a spreadsheet as
        well as on a wall. Every figure is as recorded — a reading struck from
        the record is still here, and named in the last column, because an
        export that quietly dropped numbers could not be checked against
        anything. The header says the unit of every column.
        """
        tag = lambda u: u.replace("°", "").replace("/", "")
        t, w, p, r = tag(temp), tag(wind), tag(pres), tag(rain)
        head = ["date", "high_" + t, "low_" + t, "mean_temp_" + t, "rain_" + r,
                "peak_gust_" + w, "peak_gust_time", "mean_wind_" + w,
                "pressure_min_" + p, "pressure_max_" + p,
                "humidity_min_pct", "humidity_max_pct", "uv_max",
                "solar_peak_wm2", "sun_kwh_m2", "lightning_strikes",
                "minutes_observed", "struck_as_not_real"]
        cell = lambda v, dp=None: "" if v is None else (
            str(v) if dp is None else ("%.*f" % (dp, v)))
        lines = [",".join(head)]
        for row in self.day_rows(raw=True):
            when = ""
            if row.get("gust_t"):
                when = datetime.fromtimestamp(row["gust_t"]).strftime("%H:%M")
            lines.append(",".join([
                row["date"],
                cell(convert_temp(row.get("hi"), temp)),
                cell(convert_temp(row.get("lo"), temp)),
                cell(convert_temp(row.get("tmean"), temp)),
                cell(convert_rain(row.get("rain"), rain), rain_decimals(rain)),
                cell(convert_wind(row.get("gust"), wind)), when,
                cell(convert_wind(row.get("wind"), wind)),
                cell(convert_pres(row.get("pmin"), pres), pres_decimals(pres)),
                cell(convert_pres(row.get("pmax"), pres), pres_decimals(pres)),
                cell(row.get("rhmin")), cell(row.get("rhmax")),
                cell(row.get("uv")), cell(row.get("solar")),
                cell(None if row.get("sun") is None else row["sun"] / 1000.0, 2),
                cell(None if row.get("strikes") is None else int(row["strikes"])),
                cell(None if row.get("mins") is None else int(round(row["mins"]))),
                " ".join(row.get("struck", [])),
            ]))
        return "\r\n".join(lines) + "\r\n"

    # What counts as rain. A Tempest reports dew and drizzle as hundredths of
    # a millimetre; a "wet day" that nobody got wet on spoils a dry streak.
    WET_MM = 0.25

    def station_records(self, today=None):
        """The station's records beyond hot and cold, each as
        {"v": value, "date": "YYYY-MM-DD"} or None, for the year and for the
        whole record: strongest gust, wettest day, lowest and highest
        pressure, most strikes, sunniest day, and the widest swing between a
        day's low and high."""
        today = today or date.today()
        rows = self.day_rows(until=today.isoformat())
        year = today.strftime("%Y-")

        def best(pool, key, pick, floor=None):
            have = [r for r in pool if r.get(key) is not None
                    and (floor is None or r[key] > floor)]
            if not have:
                return None
            r = pick(have, key=lambda row: row[key])
            return {"v": r[key], "date": r["date"]}

        for r in rows:
            if r.get("hi") is not None and r.get("lo") is not None \
                    and not r.get("no_swing"):
                r["swing"] = round(r["hi"] - r["lo"], 2)

        out = {}
        for scope, pool in (("year", [r for r in rows
                                      if r["date"].startswith(year)]),
                            ("all", rows)):
            out[scope] = {
                "gust": best(pool, "gust", max, 0.0),
                "rain": best(pool, "rain", max, self.WET_MM),
                "pmin": best(pool, "pmin", min),
                "pmax": best(pool, "pmax", max),
                "strikes": best(pool, "strikes", max, 0.0),
                "sun": best(pool, "sun", max, 0.0),
                "swing": best(pool, "swing", max),
                "days": len(pool),
            }
        return out

    def station_records_cached(self, ttl=60.0):
        """The same, worked out at most once a minute. Every screen asks for a
        snapshot every two seconds, and a record does not fall that often."""
        now = time.time()
        held = getattr(self, "_records_held", None)
        if held is None or now - held[0] > ttl or held[1] != date.today():
            held = (now, date.today(), self.station_records())
            self._records_held = held
        return held[2]

    def almanac(self, today=None, warm_c=26.67, hot_c=32.22, frost_c=0.0,
                plot_days=366):
        """Where today stands in the station's own record.

        Everything here is arithmetic on the daily tables, given the date, so
        it can be tested against a made-up year. Normals are not applied
        here: the page has them, and the unit the reader thinks in.
        """
        today = today or date.today()
        iso = today.isoformat()
        rows = self.day_rows(until=iso)
        by_day = {r["date"]: r for r in rows}
        this = by_day.get(iso, {"date": iso})
        past = [r for r in rows if r["date"] < iso]           # oldest first

        def ago(day):
            return (today - date.fromisoformat(day)).days

        # "The warmest since…": walk back until a day beat today. With
        # nothing beating it, today is the warmest the record holds.
        def since(key, beats):
            if this.get(key) is None:
                return None
            span = 0
            for r in reversed(past):
                if r.get(key) is None:
                    continue
                if beats(r[key], this[key]):
                    return {"date": r["date"], "days": ago(r["date"]),
                            "record": False}
                span += 1
            return {"date": None, "days": span, "record": True} if span else None

        # Streaks end at today. A dry streak survives a dry today; a day with
        # no entry at all breaks either, since nothing is known about it.
        wet = lambda r: r.get("rain", 0.0) >= self.WET_MM
        dry_run = wet_run = 0
        cursor = today
        while cursor.isoformat() in by_day and not wet(by_day[cursor.isoformat()]):
            dry_run += 1
            cursor -= timedelta(days=1)
        cursor = today
        while cursor.isoformat() in by_day and wet(by_day[cursor.isoformat()]):
            wet_run += 1
            cursor -= timedelta(days=1)
        last_wet = next((r for r in reversed(rows) if wet(r)), None)

        # The cold season runs July to June, so an October frost and the
        # April one that follows belong to the same winter.
        season_year = today.year if today.month >= 7 else today.year - 1
        season_from = "%d-07-01" % season_year
        year_from = "%d-01-01" % today.year
        frosty = lambda r: r.get("lo") is not None and r["lo"] <= frost_c
        in_season = [r for r in rows if r["date"] >= season_from]
        in_year = [r for r in rows if r["date"] >= year_from]
        frosts = [r for r in in_season if frosty(r)]
        spring = [r for r in in_year if frosty(r) and r["date"][5:7] <= "06"]
        warms = [r for r in in_year if r.get("hi") is not None
                 and r["hi"] >= warm_c]
        hots = [r for r in in_year if r.get("hi") is not None
                and r["hi"] >= hot_c]
        mark = lambda r: None if r is None else {
            "date": r["date"], "days": ago(r["date"]),
            "lo": r.get("lo"), "hi": r.get("hi")}

        # A year ago today, or the nearest the calendar allows on 29 February.
        try:
            then = today.replace(year=today.year - 1)
        except ValueError:
            then = today - timedelta(days=365)
        year_ago = by_day.get(then.isoformat())

        months = {}
        for r in rows:
            m = months.setdefault(r["date"][:7], {
                "month": r["date"][:7], "days": 0, "rain": 0.0, "wet_days": 0,
                "his": [], "los": [], "gust": None, "hi": None, "lo": None,
                "strikes": 0.0})
            m["days"] += 1
            m["rain"] += r.get("rain", 0.0)
            m["wet_days"] += 1 if wet(r) else 0
            m["strikes"] += r.get("strikes", 0.0)
            if r.get("hi") is not None:
                m["his"].append(r["hi"])
                m["hi"] = r["hi"] if m["hi"] is None else max(m["hi"], r["hi"])
            if r.get("lo") is not None:
                m["los"].append(r["lo"])
                m["lo"] = r["lo"] if m["lo"] is None else min(m["lo"], r["lo"])
            if r.get("gust") is not None:
                m["gust"] = (r["gust"] if m["gust"] is None
                             else max(m["gust"], r["gust"]))
        table = []
        for key in sorted(months)[-12:]:
            m = months[key]
            his, los = m.pop("his"), m.pop("los")
            m["hi_avg"] = round(sum(his) / len(his), 2) if his else None
            m["lo_avg"] = round(sum(los) / len(los), 2) if los else None
            m["rain"] = round(m["rain"], 2)
            table.append(m)

        plot_from = (today - timedelta(days=plot_days - 1)).isoformat()
        return {
            "date": iso,
            "from": rows[0]["date"] if rows else None,
            "days": len(rows),
            "today": this,
            "year_ago": year_ago,
            "warmest_since": since("hi", lambda a, b: a >= b),
            "coldest_since": since("lo", lambda a, b: a <= b),
            "gustiest_since": since("gust", lambda a, b: a >= b),
            "dry_days": dry_run,
            "wet_days": wet_run,
            "last_rain": None if last_wet is None else {
                "date": last_wet["date"], "days": ago(last_wet["date"]),
                "mm": last_wet["rain"]},
            "first_frost": mark(frosts[0] if frosts else None),
            "last_frost": mark(spring[-1] if spring else None),
            "frost_days": len(frosts),
            "last_warm": mark(warms[-1] if warms else None),
            "warm_days": len(warms),
            "hot_days": len(hots),
            "thresholds": {"warm_c": warm_c, "hot_c": hot_c,
                           "frost_c": frost_c},
            "months": table,
            # For the year's picture: [date, low, high, rain, gust] per day.
            "plot": [[r["date"], r.get("lo"), r.get("hi"),
                      round(r.get("rain", 0.0), 2), r.get("gust")]
                     for r in rows if r["date"] >= plot_from],
            "records": self.station_records(today),
        }

    # ── reading ───────────────────────────────────────────────────────────

    def series(self, key, hours=24, points=120):
        """(timestamp, value) pairs over the window, downsampled to `points`."""
        cutoff = time.time() - hours * 3600
        rows = [(s["t"], s[key]) for s in self.samples
                if s["t"] >= cutoff and key in s]
        if len(rows) <= points:
            return rows
        step = len(rows) / float(points)
        return [rows[min(len(rows) - 1, int(i * step))] for i in range(points)]

    def extremes(self, key, hours=24):
        """((min_value, min_time), (max_value, max_time)) or (None, None)."""
        rows = self.series(key, hours, points=10 ** 6)
        if not rows:
            return None, None
        lo = min(rows, key=lambda r: r[1])
        hi = max(rows, key=lambda r: r[1])
        return (lo[1], lo[0]), (hi[1], hi[0])

    def value_at(self, key, hours_ago, tolerance=1800):
        """Closest sample to N hours ago, or None when the window isn't
        covered yet — so a trend is never invented from a short record."""
        target = time.time() - hours_ago * 3600
        best, best_gap = None, None
        for s in self.samples:
            if key not in s:
                continue
            gap = abs(s["t"] - target)
            if best_gap is None or gap < best_gap:
                best, best_gap = s[key], gap
        return None if best is None or best_gap > tolerance else best

    def wind_run_m(self, hours=24):
        """Distance of air that has passed the station, in metres."""
        rows = self.series("wind_ms", hours, points=10 ** 6)
        if len(rows) < 2:
            return None
        total = 0.0
        for (t0, v0), (t1, v1) in zip(rows, rows[1:]):
            dt = t1 - t0
            if 0 < dt <= 900:                 # ignore gaps from downtime
                total += (v0 + v1) / 2.0 * dt
        return total

    def range_of(self, key, hours=1):
        """(min, max) of `key` over the window, or (None, None)."""
        rows = self.series(key, hours, points=10 ** 6)
        if not rows:
            return None, None
        vals = [v for _t, v in rows]
        return min(vals), max(vals)

    def mean_direction(self, hours=1):
        """Vector-mean wind direction in degrees, or None."""
        cutoff = time.time() - hours * 3600
        rows = [s for s in self.samples
                if s["t"] >= cutoff and "wind_ms" in s and "dir" in s]
        if not rows:
            return None
        sx = sy = 0.0
        for s in rows:
            rad = math.radians(s["dir"])
            sx += s["wind_ms"] * math.sin(rad)
            sy += s["wind_ms"] * math.cos(rad)
        if sx == 0 and sy == 0:
            return None
        return math.degrees(math.atan2(sx, sy)) % 360.0

    def steadiness(self, hours=3):
        """Vector mean over scalar mean, as a percentage — how constant the
        wind direction has been."""
        cutoff = time.time() - hours * 3600
        rows = [s for s in self.samples
                if s["t"] >= cutoff and "wind_ms" in s and "dir" in s]
        if len(rows) < 3:
            return None
        sx = sy = scalar = 0.0
        for s in rows:
            spd = s["wind_ms"]
            rad = math.radians(s["dir"])
            sx += spd * math.sin(rad)
            sy += spd * math.cos(rad)
            scalar += spd
        if scalar <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * math.hypot(sx, sy) / scalar))

    def rain_total(self, days=1, today=None):
        """Rain over the last `days` calendar days, ending today."""
        today = today or date.today()
        total = 0.0
        for i in range(days):
            key = (today - timedelta(days=i)).isoformat()
            if not self.is_struck(key, "rain"):
                total += self.rain_days.get(key, 0.0)
        return total

    def rain_month(self, today=None):
        today = today or date.today()
        prefix = today.strftime("%Y-%m-")
        return sum(v for k, v in self.rain_days.items()
                   if k.startswith(prefix) and not self.is_struck(k, "rain"))

    def rain_year(self, today=None):
        today = today or date.today()
        prefix = today.strftime("%Y-")
        return sum(v for k, v in self.rain_days.items()
                   if k.startswith(prefix) and not self.is_struck(k, "rain"))


# ─────────────────────────────────────────────────────────── Demo model ────

def demo_weather(epoch):
    """A plausible day of weather for `epoch`: warmest mid-afternoon, coolest
    before dawn, sun following the clock.  Used by --demo for both the seeded
    history and the live packets, so the two agree."""
    when = datetime.fromtimestamp(epoch)
    hour = when.hour + when.minute / 60.0 + when.second / 3600.0
    day = when.timetuple().tm_yday
    # Peak temperature around 15:00, minimum around 05:00.
    diurnal = math.sin(2 * math.pi * (hour - 9.0) / 24.0)
    # A slow multi-day swing on top, so day-over-day comparisons show motion.
    drift = 2.4 * math.sin(epoch / 86400.0 * 1.1)
    temp = 21.0 + 5.5 * diurnal + drift
    rh = max(28.0, min(96.0, 62.0 - 16.0 * diurnal - 3.0 * drift))
    pres = 1018.4 + 2.4 * math.sin(2 * math.pi * (hour + day * 7) / 37.0) \
        + 1.6 * math.sin(epoch / 86400.0 * 0.8)
    wind = max(0.0, 1.6 + 1.1 * math.sin(2 * math.pi * (hour - 6) / 14.0))
    # Daylight-driven sun channels: zero at night, peaking at solar noon.
    solar_f = max(0.0, math.sin(math.pi * (hour - 6.0) / 12.0))
    uv = round(6.2 * solar_f ** 1.6, 2)
    solar = 900.0 * solar_f
    return {
        "temp_c": temp, "rh": rh, "pres_mb": pres,
        "wind_ms": wind, "gust_ms": wind + 1.4, "dir": (126 + 34 * diurnal) % 360,
        "uv": uv, "solar": solar, "lux": solar * 120.0,
        "rain_mm": 0.02 if 3.0 < ((hour + day) % 19.0) < 3.4 else 0.0,
    }


# ────────────────────────────────────────────────────── Station state ──────

class StationState:
    """Everything the hub has told us, plus the values derived from it.

    `handle` is called from whichever thread owns the socket; `snapshot` is
    called from HTTP worker threads, so both take the lock.
    """

    def __init__(self, history=None, demo=False, port=DEFAULT_PORT):
        self.history = history if history is not None else History()
        self.demo = demo
        self.port = port
        self.lock = threading.RLock()

        self.data = {}
        self.last_rapid_wind = {}
        self.strike_events = []          # [{ts, dist_km, energy}]
        self.strikes_today = 0
        self._strikes_heard = 0          # evt_strike since the last obs
        self._strikes_carried = 0        # heard, but not yet in any obs
        self._accepted = {}              # key → (last believed value, when)
        self._jumping = {}               # key → readings in a row that jumped
        self.rejected_today = 0
        self.last_rejected = None
        self.last_precip_time = None
        self.device_status = {}     # firmware, uptime, signal, sensor health
        self.hub_status = {}
        self.serial = ""            # the station, e.g. ST-00012345
        self.hub_serial = ""        # the hub it reports through, HB-...
        self.last_packet = None
        self.last_packet_type = ""
        self.day = date.today().isoformat()
        self.restored_at = None

    # ── persistence of the day's running counts ───────────────────────────

    # Observations live only in memory, so a restart used to blank most of
    # the dashboard until the next obs_st — up to a minute. These round-trip
    # the last reading through disk. `last_packet` is deliberately NOT
    # restored: freshness must reflect real packets, not what we reloaded.
    MAX_RESTORE_AGE = 3600

    def dump_state(self):
        with self.lock:
            return {
                "day": self.day,
                "strikes_today": self.strikes_today,
                "serial": self.serial,
                "hub_serial": self.hub_serial,
                "obs": dict(self.data),
                "rapid": dict(self.last_rapid_wind),
                "strike_events": list(self.strike_events)[-50:],
                "last_precip_time": self.last_precip_time,
                "saved_at": self.last_packet,
            }

    def load_state(self, saved):
        """Restore the last observation when it is recent enough to still be
        worth showing. True if anything came back."""
        if not isinstance(saved, dict):
            return False
        self.load_day(saved.get("day"), saved.get("strikes_today", 0))
        try:
            saved_at = float(saved.get("saved_at") or 0)
        except (TypeError, ValueError):
            return False
        if not saved_at or time.time() - saved_at > self.MAX_RESTORE_AGE:
            return False
        with self.lock:
            if isinstance(saved.get("obs"), dict):
                self.data.update(saved["obs"])
            if isinstance(saved.get("rapid"), dict):
                self.last_rapid_wind = dict(saved["rapid"])
            if isinstance(saved.get("strike_events"), list):
                self.strike_events = [e for e in saved["strike_events"]
                                      if isinstance(e, dict) and "ts" in e]
            if saved.get("serial"):
                self.serial = str(saved["serial"])
            if saved.get("hub_serial"):
                self.hub_serial = str(saved["hub_serial"])
            self.last_precip_time = saved.get("last_precip_time")
            self.restored_at = saved_at
        return True

    def load_day(self, saved_day, strikes):
        if saved_day == self.day:
            try:
                self.strikes_today = int(strikes)
            except (TypeError, ValueError):
                pass

    def roll_day(self):
        """Reset the daily counters when the local date changes."""
        today = date.today().isoformat()
        if today == self.day:
            return False
        with self.lock:
            self.day = today
            self.strikes_today = 0
            self.rejected_today = 0
        return True

    # ── ingest ────────────────────────────────────────────────────────────

    @staticmethod
    def _first_obs(msg):
        obs = msg.get("obs")
        if isinstance(obs, list) and obs and isinstance(obs[0], list):
            return obs[0]
        return []

    def handle(self, msg, addr=None):
        """Fold one hub packet into the state. True if it was one we know."""
        if not isinstance(msg, dict):
            return False
        with self.lock:
            return self._handle_locked(msg, addr)

    def _handle_locked(self, msg, addr):
        mtype = msg.get("type")
        # The hub and the station both broadcast a serial_number, and the
        # hub's own status messages carry the hub's. Taking whichever arrived
        # last made the display flip between ST- and HB- every minute, so
        # keep them apart: the station's serial identifies the station.
        sn = msg.get("serial_number")
        hub_sn = msg.get("hub_sn")
        if hub_sn:
            self.hub_serial = str(hub_sn)
        if sn:
            if mtype == "hub_status":
                self.hub_serial = str(sn)
            else:
                self.serial = str(sn)
        elif not self.serial and addr:
            self.serial = str(addr[0])

        known = True
        if mtype == "obs_st":
            obs = self._first_obs(msg)
            if len(obs) >= 17:
                seen = self._screen({
                    "wind_lull_ms": obs[1], "wind_avg_ms": obs[2],
                    "wind_gust_ms": obs[3], "wind_dir": obs[4],
                    "pres_mb": obs[6], "temp_c": obs[7], "rh": obs[8],
                    "lux": obs[9], "uv": obs[10], "solar": obs[11],
                    "rain_mm": obs[12], "precip_type": obs[13],
                    "strike_dist_km": obs[14], "strike_count": obs[15],
                    "battery": obs[16],
                })
                self.data.update(seen)
                self._accumulate(seen.get("rain_mm"), seen.get("strike_count"))
                self._record_history()

        elif mtype == "obs_air":
            obs = self._first_obs(msg)
            if len(obs) >= 7:
                seen = self._screen({
                    "pres_mb": obs[1], "temp_c": obs[2], "rh": obs[3],
                    "strike_count": obs[4], "strike_dist_km": obs[5],
                    "battery": obs[6],
                })
                self.data.update(seen)
                self._accumulate(None, seen.get("strike_count"))
                self._record_history()

        elif mtype == "obs_sky":
            obs = self._first_obs(msg)
            if len(obs) >= 13:
                seen = self._screen({
                    "lux": obs[1], "uv": obs[2], "rain_mm": obs[3],
                    "wind_lull_ms": obs[4], "wind_avg_ms": obs[5],
                    "wind_gust_ms": obs[6], "wind_dir": obs[7],
                    "battery": obs[8], "solar": obs[10], "precip_type": obs[12],
                })
                self.data.update(seen)
                self._accumulate(seen.get("rain_mm"), None)
                self._record_history()

        elif mtype == "rapid_wind":
            ob = msg.get("ob") or []
            if len(ob) >= 3:
                self.last_rapid_wind = {"speed_ms": ob[1], "dir_deg": ob[2],
                                        "ts": ob[0]}
                self.data.setdefault("wind_avg_ms", ob[1])
                if self.data.get("wind_dir") is None:
                    self.data["wind_dir"] = ob[2]

        elif mtype == "evt_strike":
            evt = msg.get("evt") or []
            if len(evt) >= 3:
                self.strike_events.append({"ts": evt[0], "dist_km": evt[1],
                                           "energy": evt[2]})
                del self.strike_events[:-200]
                self._count_strikes(1)
                self._strikes_heard += 1

        elif mtype == "evt_precip":
            evt = msg.get("evt") or []
            if evt:
                self.last_precip_time = evt[0]

        elif mtype == "device_status":
            if msg.get("voltage") is not None:
                self.data["battery"] = msg["voltage"]
            self.device_status = {
                "voltage": msg.get("voltage"),
                "firmware": msg.get("firmware_revision"),
                "uptime": msg.get("uptime"),
                "rssi": msg.get("rssi"),
                "hub_rssi": msg.get("hub_rssi"),
                "sensor_status": msg.get("sensor_status"),
                "ts": msg.get("timestamp") or time.time(),
            }

        elif mtype == "hub_status":
            radio = msg.get("radio_stats") or []
            self.hub_status = {
                "firmware": msg.get("firmware_revision"),
                "uptime": msg.get("uptime"),
                "rssi": msg.get("rssi"),
                "reset_flags": msg.get("reset_flags"),
                "reboots": radio[1] if len(radio) > 1 else None,
                "radio_status": radio[3] if len(radio) > 3 else None,
                "ts": msg.get("timestamp") or time.time(),
            }
        else:
            known = False

        if known:
            self.last_packet = time.time()
            self.last_packet_type = mtype or ""
        return known

    def _screen(self, reading):
        """An observation with its impossible values taken out.

        Two tests. Outside what the sensor can mean at all — a temperature of
        300, a negative wind — is a fault. And a jump no real air makes in a
        minute is a glitch, unless the next readings agree with it, in which
        case the first was the truth and we were the ones wrong: after three
        in a row it is believed. What is dropped is simply absent, so the card
        keeps the last good value rather than showing a bad one.
        """
        now = time.time()
        out = {}
        for key, value in reading.items():
            if value is None:
                continue
            if not plausible(key, value):
                self._reject(key, value, "outside what the sensor can read")
                continue
            limit = JUMPS.get(key)
            last = self._accepted.get(key)
            if limit is not None and last is not None \
                    and now - last[1] <= JUMP_WINDOW_S \
                    and abs(float(value) - last[0]) > limit:
                run = self._jumping.get(key, 0) + 1
                if run < JUMP_PERSISTS:
                    self._jumping[key] = run
                    self._reject(key, value, "jumped %.1f from %.1f"
                                 % (float(value) - last[0], last[0]))
                    continue
            self._jumping.pop(key, None)
            if key in JUMPS:
                self._accepted[key] = (float(value), now)
            out[key] = value
        return out

    def _reject(self, key, value, why):
        self.rejected_today += 1
        self.last_rejected = {"key": key, "value": value, "why": why,
                              "ts": time.time()}
        print("  sensor   : dropped %s=%s (%s)" % (key, value, why), flush=True)

    def _count_strikes(self, n):
        self.strikes_today += n
        self.history.note_strikes(date.today().isoformat(), n)

    def _accumulate(self, rain_mm_last_min, strike_count):
        """Rain and strikes from an observation.

        A strike reaches us twice: at once as an evt_strike, and again inside
        the next observation's count for the minute. Adding both made every
        storm twice as violent as it was. The observation's count is the one
        WeatherFlow's own record keeps, so it is the authority — but waiting
        for it would hold the card a minute behind the flash. So events count
        as they arrive, and the observation only adds what they missed.

        A strike on the edge of the minute can be heard before one
        observation and counted in the next, so an event nobody has claimed
        is carried forward once, and then let go.
        """
        try:
            if rain_mm_last_min:
                self.history.add_rain(date.today().isoformat(),
                                      float(rain_mm_last_min))
            if strike_count is not None:
                count = max(0, int(strike_count))
                from_carried = min(count, self._strikes_carried)
                from_heard = min(count - from_carried, self._strikes_heard)
                self._strikes_carried = self._strikes_heard - from_heard
                self._strikes_heard = 0
                missed = count - from_carried - from_heard
                if missed > 0:
                    self._count_strikes(missed)
        except (TypeError, ValueError):
            pass

    def _record_history(self):
        d = self.data
        today = date.today().isoformat()
        self.history.note_temp(today, d.get("temp_c"))
        self.history.note_obs(
            today, time.time(), temp_c=d.get("temp_c"),
            pres_mb=d.get("pres_mb"), wind_ms=d.get("wind_avg_ms"),
            gust_ms=d.get("wind_gust_ms"), rh=d.get("rh"), uv=d.get("uv"),
            solar=d.get("solar"))
        self.history.add({
            "temp_c":  d.get("temp_c"),
            "pres_mb": d.get("pres_mb"),
            "wind_ms": d.get("wind_avg_ms"),
            "gust_ms": d.get("wind_gust_ms"),
            "rh":      d.get("rh"),
            "dir":     d.get("wind_dir"),
            "battery": d.get("battery"),
        })

    # ── read side ─────────────────────────────────────────────────────────

    def health(self):
        """'waiting' | 'live' | 'stale' | 'offline'."""
        if self.last_packet is None:
            return "waiting"
        age = time.time() - self.last_packet
        if age > OFFLINE_AFTER:
            return "offline"
        if age > STALE_AFTER:
            return "stale"
        return "live"

    def snapshot(self, lat=None, lon=None, series_points=90):
        """A complete, JSON-serialisable view in canonical units (°C, mb, m/s,
        mm, km). Formatting and unit choice belong to the front end."""
        with self.lock:
            d = dict(self.data)
            rapid = dict(self.last_rapid_wind)
            events = list(self.strike_events)
            strikes_today = self.strikes_today
            serial = self.serial
            hub_serial = self.hub_serial
            dev = dict(self.device_status)
            hub = dict(self.hub_status)
            last_packet = self.last_packet
            last_type = self.last_packet_type
            health = self.health()
            hist = self.history
            station_records = hist.station_records_cached()

        now = time.time()
        temp_c, rh = d.get("temp_c"), d.get("rh")
        wind_ms, mb = d.get("wind_avg_ms"), d.get("pres_mb")

        feels_c, feels_model = apparent_temp_c(temp_c, rh, wind_ms)
        t_lo, t_hi = hist.extremes("temp_c")
        p_lo, p_hi = hist.extremes("pres_mb")
        _g_lo, g_hi = hist.extremes("gust_ms")

        t_24 = hist.value_at("temp_c", 24, tolerance=3600)
        bat_24 = hist.value_at("battery", 24, tolerance=3600)
        t_1 = hist.value_at("temp_c", 1, tolerance=900)
        p_1 = hist.value_at("pres_mb", 1, tolerance=900)
        p_3 = hist.value_at("pres_mb", 3)
        d3 = None if mb is None or p_3 is None else mb - p_3
        trend_word, trend_arrow = pressure_trend_label(d3)
        force, force_name = beaufort(wind_ms)

        nearest = min((e["dist_km"] for e in events
                       if e.get("dist_km") is not None), default=None)
        pair = lambda x: None if x is None else {"v": x[0], "t": x[1]}

        sun = {"kind": "unset", "rise": None, "set": None,
               "next_label": None, "next_s": None}
        if lat is not None and lon is not None:
            rise, sset, kind = sun_times(lat, lon)
            sun["kind"] = kind
            if rise and sset:
                sun["rise"], sun["set"] = rise.timestamp(), sset.timestamp()
                now_dt = datetime.now()
                if now_dt < rise:
                    sun["next_label"], nxt = "Sunrise in", rise
                elif now_dt < sset:
                    sun["next_label"], nxt = "Sunset in", sset
                else:
                    nxt = sun_times(lat, lon, now_dt + timedelta(days=1))[0]
                    sun["next_label"] = "Sunrise in"
                if nxt:
                    sun["next_s"] = max(0, (nxt - datetime.now()).total_seconds())
        age, illum, moon_name = moon_state(now)
        rose, moon_set, next_rise = moon_window_cached(lat, lon)
        new_moon, full_moon = next_moon_phases(now)
        moon_alt = (moon_altitude(lat, lon, now)
                    if lat is not None and lon is not None else None)
        w_lo, w_hi = hist.range_of("wind_ms", 1)
        avg_ms = None
        rows = hist.series("wind_ms", 1, points=10 ** 6)
        if rows:
            avg_ms = sum(v for _t, v in rows) / len(rows)

        return {
            "version": VERSION,
            "now": now,
            "serial": serial,
            "hub_serial": hub_serial,
            "demo": self.demo,
            "port": self.port,
            "health": health,
            "age_s": None if last_packet is None else now - last_packet,
            "restored_at": self.restored_at,
            "last_type": last_type,
            "obs": d,
            "rapid": rapid,
            "derived": {
                "feels_c": feels_c,
                "feels_model": feels_model,
                "dew_c": dew_point_c(temp_c, rh),
                "comfort": comfort_words(temp_c, rh),
                "temp_min": pair(t_lo), "temp_max": pair(t_hi),
                "temp_24h_delta_c": (None if temp_c is None or t_24 is None
                                     else temp_c - t_24),
                "temp_1h_delta_c": (None if temp_c is None or t_1 is None
                                    else temp_c - t_1),
                "pres_min": pair(p_lo), "pres_max": pair(p_hi),
                "pres_1h_delta_mb": (None if mb is None or p_1 is None
                                     else mb - p_1),
                "pres_3h_delta_mb": d3,
                "pres_trend": trend_word, "pres_arrow": trend_arrow,
                "pres_outlook": pressure_outlook(d3),
                "beaufort": force, "beaufort_name": force_name,
                "gust_max": pair(g_hi),
                "wind_run_m": hist.wind_run_m(24),
                "steadiness_pct": hist.steadiness(3),
                "steadiness_today_pct": hist.steadiness(24),
                "wind_avg_1h_ms": avg_ms,
                "wind_dir_1h": hist.mean_direction(1),
                "wind_min_1h_ms": w_lo,
                "wind_max_1h_ms": w_hi,
                "rain_rate_mm_h": (d.get("rain_mm") or 0.0) * 60.0,
                "rain_today_mm": hist.rain_total(1),
                "rain_yday_mm": hist.rain_days.get(
                    (date.today() - timedelta(days=1)).isoformat(), 0.0),
                "rain_month_mm": hist.rain_month(),
                "rain_year_mm": hist.rain_year(),
                # The earliest day the rain record actually covers. A fresh
                # install has days, not years, and the Rainfall card says so
                # rather than letting a week of rain pose as a year of it.
                "rain_from": min(hist.rain_days) if hist.rain_days else None,
                "uv_category": uv_category(d.get("uv")),
                "strikes_today": strikes_today,
                "strike_nearest_km": nearest,
                "strike_last": events[-1] if events else None,
                "strikes_1h": sum(1 for e in events if now - e["ts"] <= 3600),
                "strikes_3h": sum(1 for e in events
                                  if now - e["ts"] <= 3 * 3600),
            },
            "sun": sun,
            "hardware": {
                "station": dict(dev),
                "hub": dict(hub),
                "battery_v": d.get("battery"),
                "battery_pct": battery_pct(d.get("battery")),
                "battery_24h_delta": (
                    None if d.get("battery") is None or bat_24 is None
                    else d.get("battery") - bat_24),
                "faults": sensor_faults(dev.get("sensor_status")),
                "station_uptime": format_uptime(dev.get("uptime")),
                "hub_uptime": format_uptime(hub.get("uptime")),
                "report_interval_s": d.get("report_interval"),
                "rejected_today": self.rejected_today,
                "last_rejected": self.last_rejected,
            },
            "storage": hist.storage_status(),
            "records": {
                "month": hist.temp_record("month"),
                "year": hist.temp_record("year"),
                "all": hist.temp_record("all"),
                "station": station_records,
            },
            "moon": {
                "age_days": age, "illum": illum, "name": moon_name,
                "waxing": age < SYNODIC / 2.0,
                "altitude": moon_alt,
                "up": None if moon_alt is None else moon_alt > 0,
                "rose": rose, "set": moon_set, "next_rise": next_rise,
                "new": new_moon, "full": full_moon,
            },
            "series": {
                "temp_c": hist.series("temp_c", 24, series_points),
                "pres_mb": hist.series("pres_mb", 24, series_points),
                "wind_ms": hist.series("wind_ms", 24, series_points),
            },
        }


# ──────────────────────────────────────────────────────── Packet sources ───

class UdpListener(threading.Thread):
    """Receives hub broadcasts and hands each decoded packet to `on_message`.

    `error` is set (and the thread exits) if the port cannot be bound — most
    often because something else already holds it, or because a container was
    started without host networking.
    """

    def __init__(self, port, on_message, stop_event=None):
        threading.Thread.__init__(self, daemon=True)
        self.port = port
        self.on_message = on_message
        # The shared event stops the whole application. `closed` stops only
        # this listener, so shutting one down — or having one die — never
        # takes the forecast, backfill and housekeeping threads with it.
        self.stop_event = stop_event or threading.Event()
        self.closed = threading.Event()
        self.error = ""
        self.packets = 0
        self._sock = None

    @property
    def _done(self):
        return self.stop_event.is_set() or self.closed.is_set()

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", self.port))
            sock.settimeout(1.0)
            self._sock = sock
        except OSError as e:
            self.error = "Cannot bind UDP :%d — %s" % (self.port, e)
            return

        while not self._done:
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(msg, dict):
                self.packets += 1
                self.on_message(msg, addr)
        self.close()

    def close(self):
        """Stop just this listener. The application's own stop event is left
        alone, so callers can replace a listener without shutting anything
        else down."""
        self.closed.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass


class DemoSource(threading.Thread):
    """Synthesises hub packets so the dashboard runs with no hub present."""

    def __init__(self, on_message, stop_event=None, interval=2.0):
        threading.Thread.__init__(self, daemon=True)
        self.on_message = on_message
        self.stop_event = stop_event or threading.Event()
        self.closed = threading.Event()
        self.interval = interval

    def close(self):
        self.closed.set()

    def run(self):
        tick = 0
        while not (self.stop_event.is_set() or self.closed.is_set()):
            now = time.time()
            m = demo_weather(now)
            wind = max(0.0, m["wind_ms"] + 0.5 * math.sin(now / 6.0))
            gust = max(wind, m["gust_ms"] + 0.4 * abs(math.sin(now / 11.0)))
            self.on_message({"type": "rapid_wind",
                             "serial_number": "ST-DEMO0001",
                             "ob": [int(now), round(wind, 1),
                                    int(m["dir"])]}, ("demo", 0))
            if tick % 5 == 0:
                self.on_message({
                    "type": "obs_st", "serial_number": "ST-DEMO0001",
                    "obs": [[int(now), round(max(0.0, wind - 0.8), 1),
                             round(wind, 1), round(gust, 1), int(m["dir"]), 3,
                             round(m["pres_mb"], 2), round(m["temp_c"], 1),
                             round(m["rh"], 1), round(m["lux"]),
                             round(m["uv"], 2), round(m["solar"]),
                             m["rain_mm"], 1 if m["rain_mm"] else 0,
                             0, 0, 2.68, 60]],
                }, ("demo", 0))
            if tick and tick % 60 == 0:
                self.on_message({"type": "evt_strike",
                                 "serial_number": "ST-DEMO0001",
                                 "evt": [int(now), 8, 4200]}, ("demo", 0))
            tick += 1
            self.stop_event.wait(self.interval)


def seed_demo_history(history, hours=24, step_s=480):
    """Fill history with a plausible day so trends have shape immediately."""
    if history.samples:
        return
    now = time.time()
    count = int(hours * 3600 / step_s)
    for i in range(count):
        ts = now - (count - i) * float(step_s)
        m = demo_weather(ts)
        history.samples.append({
            "t": ts,
            "temp_c": round(m["temp_c"], 2),
            "pres_mb": round(m["pres_mb"], 2),
            "wind_ms": round(m["wind_ms"], 2),
            "gust_ms": round(m["gust_ms"], 2),
            "rh": round(m["rh"], 1),
            "dir": round(m["dir"], 1),
        })
    today = date.today()
    history.rain_days.setdefault((today - timedelta(days=1)).isoformat(), 3.0)
    history.rain_days.setdefault((today - timedelta(days=12)).isoformat(), 1.3)
    seed_demo_days(history, today)


def seed_demo_days(history, today=None, days=400):
    """A made-up year of daily record, so the demo's almanac has a past.

    Only into an empty record: pointed at a real data directory by mistake,
    the demo must not write a year of fiction over a station's history. The
    same date always gets the same weather, so a screenshot taken twice
    matches itself.
    """
    import random
    if len(history.temp_days) > 5 or len(history.days) > 5:
        return 0
    today = today or date.today()
    for back in range(days, 0, -1):
        day = today - timedelta(days=back)
        rnd = random.Random(day.toordinal())
        season = math.cos(2 * math.pi * (day.timetuple().tm_yday - 201) / 365.25)
        mean = 9.5 + 14.5 * season + rnd.gauss(0, 3.2)
        spread = 5.0 + 2.0 * rnd.random()
        iso = day.isoformat()
        history.temp_days.setdefault(iso, {"lo": round(mean - spread, 2),
                                           "hi": round(mean + spread, 2)})
        stormy = rnd.random() < 0.30
        if stormy:
            history.rain_days.setdefault(
                iso, round(rnd.expovariate(1 / 6.0) + 0.3, 2))
        summer = max(0.0, season)
        gust = 4.0 + rnd.expovariate(1 / 3.0) + (5.0 if stormy else 0.0)
        mid = 1001.0 + rnd.gauss(0, 6.0) - (5.0 if stormy else 0.0)
        history.days.setdefault(iso, {
            "gust": round(gust, 2),
            "gust_t": float(int(time.mktime(day.timetuple())) + 15 * 3600),
            "pmin": round(mid - 2.5, 2), "pmax": round(mid + 2.5, 2),
            "rhmin": 38.0, "rhmax": 92.0,
            "uv": round(1.0 + 8.0 * summer * (0.4 if stormy else 1.0), 2),
            "solar": round(320 + 620 * (season + 1) / 2, 2),
            "sun": round((1500 + 5200 * (season + 1) / 2)
                         * (0.35 if stormy else 1.0), 2),
            "strikes": float(int(rnd.expovariate(1 / 40.0)))
                       if stormy and summer > 0.4 and rnd.random() < 0.5 else 0.0,
            "tmean": round(mean, 3), "tn": 1440.0,
            "wind": round(gust / 2.6, 3), "wn": 1440.0, "mins": 1440.0,
        })
    return days


# ────────────────────────────────────────────── Moon position & phases ─────
#
# Low-precision lunar theory (Meeus, "Astronomical Algorithms", ch. 47 & 49).
# Good to a few arc-minutes, which puts rise and set times within a couple of
# minutes — plenty for a dashboard, and it needs no network.

_J2000 = 2451545.0


def _julian_day(epoch):
    return epoch / 86400.0 + 2440587.5


def _moon_ecliptic(jd):
    """(ecliptic longitude, latitude) of the Moon in degrees."""
    t = (jd - _J2000) / 36525.0
    rad = math.radians
    lp = 218.3164477 + 481267.88123421 * t          # mean longitude
    m = 357.5291092 + 35999.0502909 * t             # sun's mean anomaly
    mp = 134.9633964 + 477198.8675055 * t           # moon's mean anomaly
    d = 297.8501921 + 445267.1114034 * t            # mean elongation
    f = 93.2720950 + 483202.0175233 * t             # argument of latitude

    lon = (lp
           + 6.288774 * math.sin(rad(mp))
           + 1.274027 * math.sin(rad(2 * d - mp))
           + 0.658314 * math.sin(rad(2 * d))
           + 0.213618 * math.sin(rad(2 * mp))
           - 0.185116 * math.sin(rad(m))
           - 0.114332 * math.sin(rad(2 * f))
           + 0.058793 * math.sin(rad(2 * d - 2 * mp))
           + 0.057066 * math.sin(rad(2 * d - m - mp))
           + 0.053322 * math.sin(rad(2 * d + mp))
           + 0.045758 * math.sin(rad(2 * d - m))
           - 0.040923 * math.sin(rad(m - mp))
           - 0.034720 * math.sin(rad(d))
           - 0.030383 * math.sin(rad(m + mp)))
    lat = (5.128122 * math.sin(rad(f))
           + 0.280602 * math.sin(rad(mp + f))
           + 0.277693 * math.sin(rad(mp - f))
           + 0.173237 * math.sin(rad(2 * d - f))
           + 0.055413 * math.sin(rad(2 * d - mp + f))
           + 0.046271 * math.sin(rad(2 * d - mp - f))
           + 0.032573 * math.sin(rad(2 * d + f)))
    return lon % 360.0, lat


def _ecliptic_to_equatorial(lon, lat, jd):
    """(right ascension, declination) in degrees."""
    rad = math.radians
    t = (jd - _J2000) / 36525.0
    eps = 23.439291 - 0.0130042 * t
    sl, cl = math.sin(rad(lon)), math.cos(rad(lon))
    sb, cb = math.sin(rad(lat)), math.cos(rad(lat))
    se, ce = math.sin(rad(eps)), math.cos(rad(eps))
    ra = math.degrees(math.atan2(sl * ce - (sb / cb) * se, cl)) % 360.0
    dec = math.degrees(math.asin(sb * ce + cb * se * sl))
    return ra, dec


def _gmst_deg(jd):
    """Greenwich mean sidereal time in degrees."""
    return (280.46061837 + 360.98564736629 * (jd - _J2000)) % 360.0


def moon_altitude(lat, lon, epoch):
    """The Moon's altitude above the horizon, in degrees."""
    jd = _julian_day(epoch)
    ra, dec = _ecliptic_to_equatorial(*_moon_ecliptic(jd), jd=jd)
    h = math.radians((_gmst_deg(jd) + lon - ra) % 360.0)
    rl, rd = math.radians(lat), math.radians(dec)
    return math.degrees(math.asin(
        math.sin(rl) * math.sin(rd) + math.cos(rl) * math.cos(rd) * math.cos(h)))


# Standard altitude for moonrise/set: refraction plus the Moon's semidiameter
# and parallax roughly cancel to +0.125°.
_MOON_H0 = 0.125


def moon_rise_set(lat, lon, epoch=None, span_h=26):
    """Next rise and set as unix epochs, found by sampling the altitude curve
    and interpolating the crossings. Either may be None near the poles."""
    if lat is None or lon is None:
        return None, None
    start = time.time() if epoch is None else epoch
    step = 300.0                                  # 5-minute sampling
    prev_t = start
    prev_a = moon_altitude(lat, lon, start) - _MOON_H0
    rise = set_ = None
    steps = int(span_h * 3600 / step)
    for i in range(1, steps + 1):
        t = start + i * step
        a = moon_altitude(lat, lon, t) - _MOON_H0
        if prev_a <= 0 < a and rise is None:
            rise = prev_t + step * (-prev_a) / (a - prev_a)
        elif prev_a > 0 >= a and set_ is None:
            set_ = prev_t + step * prev_a / (prev_a - a)
        if rise is not None and set_ is not None:
            break
        prev_t, prev_a = t, a
    return rise, set_


def moon_last_rise(lat, lon, epoch=None):
    """The most recent rise at or before `epoch` — what the Moon is 'on' now."""
    if lat is None or lon is None:
        return None
    now = time.time() if epoch is None else epoch
    rise, _ = moon_rise_set(lat, lon, now - 26 * 3600, span_h=52)
    if rise is None:
        return None
    # walk forward to the last rise that is still in the past
    last = None
    t = rise
    while t <= now:
        last = t
        nxt, _ = moon_rise_set(lat, lon, t + 3600, span_h=30)
        if nxt is None or nxt <= t:
            break
        t = nxt
    return last


def moon_window(lat, lon, epoch=None):
    """The Moon's current cycle as (last rise, the set after it, next rise) —
    the three times the reference dashboard labels ROSE / SET / RISES."""
    if lat is None or lon is None:
        return None, None, None
    now = time.time() if epoch is None else epoch
    last = moon_last_rise(lat, lon, now)
    this_set = None
    if last is not None:
        _r, this_set = moon_rise_set(lat, lon, last + 60, span_h=30)
    next_rise, next_set = moon_rise_set(lat, lon, now, span_h=30)
    if this_set is None:
        this_set = next_set
    return last, this_set, next_rise


_moon_cache = {}


def moon_window_cached(lat, lon, ttl=600):
    """moon_window(), recomputed at most every `ttl` seconds."""
    if lat is None or lon is None:
        return None, None, None
    now = time.time()
    key = (round(lat, 3), round(lon, 3))
    hit = _moon_cache.get(key)
    if hit and now - hit[0] < ttl and (hit[1][2] is None or now < hit[1][2]):
        return hit[1]
    value = moon_window(lat, lon, now)
    _moon_cache[key] = (now, value)
    return value


def _phase_event(k, full):
    """Julian Ephemeris Day of the new (full=False) or full moon nearest k."""
    rad = math.radians
    if full:
        k += 0.5
    t = k / 1236.85
    jde = (2451550.09766 + 29.530588861 * k
           + 0.00015437 * t ** 2 - 0.000000150 * t ** 3
           + 0.00000000073 * t ** 4)
    e = 1 - 0.002516 * t - 0.0000074 * t ** 2
    m = 2.5534 + 29.10535670 * k - 0.0000014 * t ** 2
    mp = 201.5643 + 385.81693528 * k + 0.0107582 * t ** 2
    f = 160.7108 + 390.67050284 * k - 0.0016118 * t ** 2
    om = 124.7746 - 1.56375588 * k + 0.0020672 * t ** 2
    a1 = 299.77 + 0.107408 * k
    if full:
        corr = (-0.40614 * math.sin(rad(mp)) + 0.17302 * e * math.sin(rad(m))
                + 0.01614 * math.sin(rad(2 * mp)) + 0.01043 * math.sin(rad(2 * f))
                + 0.00734 * e * math.sin(rad(mp - m))
                - 0.00515 * e * math.sin(rad(mp + m))
                + 0.00209 * e * e * math.sin(rad(2 * m))
                - 0.00111 * math.sin(rad(mp - 2 * f))
                - 0.00057 * math.sin(rad(mp + 2 * f)))
    else:
        corr = (-0.40720 * math.sin(rad(mp)) + 0.17241 * e * math.sin(rad(m))
                + 0.01608 * math.sin(rad(2 * mp)) + 0.01039 * math.sin(rad(2 * f))
                + 0.00739 * e * math.sin(rad(mp - m))
                - 0.00514 * e * math.sin(rad(mp + m))
                + 0.00208 * e * e * math.sin(rad(2 * m))
                - 0.00111 * math.sin(rad(mp - 2 * f))
                - 0.00057 * math.sin(rad(mp + 2 * f)))
    corr += (0.000325 * math.sin(rad(a1)) - 0.000165 * math.sin(rad(om)))
    return jde + corr


def next_moon_phases(epoch=None):
    """(next new moon, next full moon) as unix epochs."""
    now = time.time() if epoch is None else epoch
    jd_now = _julian_day(now)
    # k counts lunations from 2000-01-06; start a little behind and walk on.
    k0 = math.floor((jd_now - 2451550.09766) / 29.530588861) - 1
    to_epoch = lambda jde: (jde - 2440587.5) * 86400.0
    new = full = None
    for i in range(4):
        if new is None:
            e = to_epoch(_phase_event(k0 + i, False))
            if e > now:
                new = e
        if full is None:
            e = to_epoch(_phase_event(k0 + i, True))
            if e > now:
                full = e
    return new, full
