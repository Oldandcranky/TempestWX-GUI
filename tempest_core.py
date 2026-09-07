# =============================================================================
# Tempest Weather Station — shared core
# Version 3.1.0
#
# MIT License
# Copyright (c) 2026  Michael Walker VA3MW  &  Claude (Anthropic)
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

VERSION      = "3.1.0"
DEFAULT_PORT = 50222

_HERE         = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(_HERE, "tempest_settings.json")
HISTORY_FILE  = os.path.join(_HERE, "tempest_history.json")

HISTORY_MAX      = 2880        # 48 h at one sample a minute
HISTORY_MIN_GAP  = 55          # seconds between retained samples
HISTORY_SAVE_SEC = 300         # flush to disk every 5 min

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
    trends and 24-hour extremes survive a restart."""

    KEYS = ("temp_c", "pres_mb", "wind_ms", "gust_ms", "rh", "dir")

    def __init__(self, path=None):
        self.path = path or HISTORY_FILE
        self.samples = deque(maxlen=HISTORY_MAX)
        self.rain_days = {}        # "YYYY-MM-DD" → mm
        self.temp_days = {}        # "YYYY-MM-DD" → {"lo": °C, "hi": °C}
        self.meta = {}             # bookkeeping, e.g. when rain was swept
        self._last_saved = 0.0
        self.load()

    # ── persistence ───────────────────────────────────────────────────────

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return
        cutoff = time.time() - 48 * 3600
        for s in raw.get("samples", []):
            try:
                if float(s["t"]) >= cutoff:
                    self.samples.append(s)
            except (TypeError, ValueError, KeyError):
                continue
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
                    except (TypeError, ValueError):
                        continue
        days = raw.get("rain_days")
        if isinstance(days, dict):
            for k, v in days.items():
                try:
                    self.rain_days[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
            self._trim_rain_days()

    def _trim_rain_days(self):
        if len(self.rain_days) > 800:
            for key in sorted(self.rain_days)[:-800]:
                del self.rain_days[key]

    def save(self, force=False):
        now = time.time()
        if not force and now - self._last_saved < HISTORY_SAVE_SEC:
            return
        self._last_saved = now
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"samples": list(self.samples),
                           "rain_days": self.rain_days,
                           "temp_days": self.temp_days,
                           "meta": self.meta}, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

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
        if not rows:
            return None
        hi = max(rows, key=lambda r: r[1]["hi"])
        lo = min(rows, key=lambda r: r[1]["lo"])
        return {"hi": (hi[1]["hi"], hi[0]), "lo": (lo[1]["lo"], lo[0]),
                "days": len(rows)}

    def add_rain(self, day_iso, mm):
        if not mm:
            return
        self.rain_days[day_iso] = self.rain_days.get(day_iso, 0.0) + float(mm)
        self._trim_rain_days()

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
            total += self.rain_days.get(key, 0.0)
        return total

    def rain_month(self, today=None):
        today = today or date.today()
        prefix = today.strftime("%Y-%m-")
        return sum(v for k, v in self.rain_days.items() if k.startswith(prefix))

    def rain_year(self, today=None):
        today = today or date.today()
        prefix = today.strftime("%Y-")
        return sum(v for k, v in self.rain_days.items() if k.startswith(prefix))


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
        self.last_precip_time = None
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
                self.data.update({
                    "wind_lull_ms": obs[1], "wind_avg_ms": obs[2],
                    "wind_gust_ms": obs[3], "wind_dir": obs[4],
                    "pres_mb": obs[6], "temp_c": obs[7], "rh": obs[8],
                    "lux": obs[9], "uv": obs[10], "solar": obs[11],
                    "rain_mm": obs[12], "precip_type": obs[13],
                    "strike_dist_km": obs[14], "strike_count": obs[15],
                    "battery": obs[16],
                })
                self._accumulate(obs[12], obs[15])
                self._record_history()

        elif mtype == "obs_air":
            obs = self._first_obs(msg)
            if len(obs) >= 7:
                self.data.update({
                    "pres_mb": obs[1], "temp_c": obs[2], "rh": obs[3],
                    "strike_count": obs[4], "strike_dist_km": obs[5],
                    "battery": obs[6],
                })
                self._accumulate(None, obs[4])
                self._record_history()

        elif mtype == "obs_sky":
            obs = self._first_obs(msg)
            if len(obs) >= 13:
                self.data.update({
                    "lux": obs[1], "uv": obs[2], "rain_mm": obs[3],
                    "wind_lull_ms": obs[4], "wind_avg_ms": obs[5],
                    "wind_gust_ms": obs[6], "wind_dir": obs[7],
                    "battery": obs[8], "solar": obs[10], "precip_type": obs[12],
                })
                self._accumulate(obs[3], None)
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
                self.strikes_today += 1

        elif mtype == "evt_precip":
            evt = msg.get("evt") or []
            if evt:
                self.last_precip_time = evt[0]

        elif mtype in ("device_status", "hub_status"):
            if msg.get("voltage") is not None:
                self.data["battery"] = msg["voltage"]
        else:
            known = False

        if known:
            self.last_packet = time.time()
            self.last_packet_type = mtype or ""
        return known

    def _accumulate(self, rain_mm_last_min, strike_count):
        try:
            if rain_mm_last_min:
                self.history.add_rain(date.today().isoformat(),
                                      float(rain_mm_last_min))
            if strike_count:
                self.strikes_today += int(strike_count)
        except (TypeError, ValueError):
            pass

    def _record_history(self):
        d = self.data
        self.history.note_temp(date.today().isoformat(), d.get("temp_c"))
        self.history.add({
            "temp_c":  d.get("temp_c"),
            "pres_mb": d.get("pres_mb"),
            "wind_ms": d.get("wind_avg_ms"),
            "gust_ms": d.get("wind_gust_ms"),
            "rh":      d.get("rh"),
            "dir":     d.get("wind_dir"),
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
            last_packet = self.last_packet
            last_type = self.last_packet_type
            health = self.health()
            hist = self.history

        now = time.time()
        temp_c, rh = d.get("temp_c"), d.get("rh")
        wind_ms, mb = d.get("wind_avg_ms"), d.get("pres_mb")

        feels_c, feels_model = apparent_temp_c(temp_c, rh, wind_ms)
        t_lo, t_hi = hist.extremes("temp_c")
        p_lo, p_hi = hist.extremes("pres_mb")
        _g_lo, g_hi = hist.extremes("gust_ms")

        t_24 = hist.value_at("temp_c", 24, tolerance=3600)
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
                "uv_category": uv_category(d.get("uv")),
                "strikes_today": strikes_today,
                "strike_nearest_km": nearest,
                "strike_last": events[-1] if events else None,
                "strikes_1h": sum(1 for e in events if now - e["ts"] <= 3600),
                "strikes_3h": sum(1 for e in events
                                  if now - e["ts"] <= 3 * 3600),
            },
            "sun": sun,
            "records": {
                "month": hist.temp_record("month"),
                "year": hist.temp_record("year"),
                "all": hist.temp_record("all"),
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
