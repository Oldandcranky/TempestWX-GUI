#!/usr/bin/env python3
"""Unit tests for the logic that decides what a card says.

Standard library only, like the rest of the project. Run them with
tests/run.sh, or directly:

    python3 -m unittest discover -s tests -v

What is worth a test here is anything with a threshold or a branch — the
code that turns numbers into a word on the wall. The maths that merely
converts units is left alone; it is one line and its own documentation.
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempest_core as core
import tempest_server as server


def result(down_mbps=500.0, up_mbps=100.0, idle=10.0, loaded=None,
           loss=0.0, ok=True, at="2026-09-14T12:00:00Z"):
    """One Speedtest Tracker row, in the shape the API actually returns."""
    return {
        "created_at": at,
        "status": "completed" if ok else "failed",
        "download": (down_mbps * 1e6 / 8) if ok else None,
        "upload": up_mbps * 1e6 / 8,
        "data": {
            "ping": {"latency": idle, "jitter": 2.0},
            "packetLoss": loss,
            "isp": "Comcast Cable",
            "download": {"latency": {"iqm": loaded if loaded else idle}},
            "upload": {"latency": {"iqm": loaded if loaded else idle}},
        },
    }


def rows(*results):
    return {"data": list(results)}


class SpeedtestVerdicts(unittest.TestCase):
    """SpeedtestFetcher.parse turns a day of results into a status word and a
    list of issues. These are the thresholds the Internet card reads."""

    def parse(self, raw, plan_down=0.0, plan_up=0.0):
        return server.SpeedtestFetcher.parse(raw, plan_down, plan_up)

    def test_a_clean_run_is_good_with_nothing_to_say(self):
        out = self.parse(rows(result()))
        self.assertEqual(out["status"], "good")
        self.assertEqual(out["issues"], [])

    def test_the_thresholds_are_the_ones_the_card_was_written_against(self):
        # Pinned deliberately. The behaviour tests below use these numbers as
        # literals rather than reading them back off the class, because a test
        # that asks the code what the answer should be cannot fail. Changing
        # one of these is a decision, and it should break this line first.
        f = server.SpeedtestFetcher
        self.assertEqual(f.DOWN_AFTER, 3)
        self.assertEqual(f.LOSS_PCT, 1.0)
        self.assertEqual(f.BLOAT_MS, 100.0)
        self.assertEqual(f.JITTER_MS, 30.0)
        self.assertEqual(f.PLAN_FRAC, 0.5)

    def test_anything_to_say_makes_it_degraded(self):
        # The status word is derived, not set: any issue short of down
        # demotes "good" to "degraded".
        out = self.parse(rows(result(loss=2.0)))
        self.assertEqual(out["status"], "degraded")
        self.assertTrue(out["issues"])

    def test_no_results_at_all_is_down(self):
        out = self.parse(rows())
        self.assertEqual(out["status"], "down")

    def test_one_failure_is_an_issue_but_not_down(self):
        # A single failed test is a server hiccup often enough that calling
        # the line down for it would cry wolf on a wall display.
        out = self.parse(rows(result(ok=False), result(), result()))
        self.assertNotEqual(out["status"], "down")
        self.assertTrue(any("recent test failed" in i for i in out["issues"]))

    def test_three_consecutive_failures_is_down(self):
        out = self.parse(rows(result(ok=False), result(ok=False),
                              result(ok=False), result()))
        self.assertEqual(out["status"], "down")

    def test_two_failures_is_still_not_down(self):
        # The boundary, explicitly: DOWN_AFTER is three, so two is not it.
        out = self.parse(rows(result(ok=False), result(ok=False), result()))
        self.assertNotEqual(out["status"], "down")

    def test_packet_loss_above_the_threshold_is_named(self):
        out = self.parse(rows(result(loss=1.5)))
        self.assertTrue(any("Packet loss" in i for i in out["issues"]))

    def test_packet_loss_below_the_threshold_is_noise(self):
        out = self.parse(rows(result(loss=0.5)))
        self.assertFalse(any("Packet loss" in i for i in out["issues"]))

    def test_bufferbloat_is_loaded_latency_minus_idle(self):
        out = self.parse(rows(result(idle=10.0, loaded=180.0)))
        self.assertAlmostEqual(out["bloat"], 170.0, places=3)
        self.assertEqual(out["ping"], 10.0)

    def test_bufferbloat_over_the_threshold_is_named(self):
        out = self.parse(rows(result(idle=10.0, loaded=170.0)))   # 160ms of bloat
        self.assertTrue(out["issues"], "expected an issue for 160ms of bloat")

    def test_well_under_the_plan_is_named(self):
        # Half the advertised rate is the line; 250 of 1000 is well under it.
        out = self.parse(rows(result(down_mbps=250.0)), plan_down=1000)
        self.assertTrue(any("%" in i for i in out["issues"]))

    def test_close_to_the_plan_says_nothing(self):
        out = self.parse(rows(result(down_mbps=900.0)), plan_down=1000)
        self.assertEqual(out["issues"], [])

    def test_history_is_oldest_first_and_carries_every_row(self):
        # The card's plot reads left to right, so the server hands it back
        # reversed from the API's newest-first order.
        out = self.parse(rows(
            result(at="2026-09-14T12:00:00Z"),
            result(at="2026-09-14T11:00:00Z"),
            result(at="2026-09-14T10:00:00Z")))
        stamps = [h["at"] for h in out["history"]]
        self.assertEqual(len(stamps), 3)
        self.assertEqual(stamps, sorted(stamps))

    def test_history_survives_a_line_that_is_down(self):
        # The early return for "no good results" must not skip the history,
        # or the card loses its plot exactly when the plot matters most.
        out = self.parse(rows(result(ok=False), result(ok=False)))
        self.assertEqual(out["status"], "down")
        self.assertEqual(len(out["history"]), 2)

    def test_download_prefers_the_bits_field_over_bytes(self):
        row = result()
        row["download_bits"] = 700e6          # 700 Mbps
        row["download"] = 1.0                 # nonsense in the bytes field
        out = self.parse(rows(row))
        self.assertAlmostEqual(out["down"], 700.0, places=3)


class TwelveNumberConfig(unittest.TestCase):
    """The rain and temperature normals are twelve numbers in an environment
    variable. Bad input must be refused loudly rather than half-read."""

    def test_twelve_numbers_parse(self):
        self.assertEqual(server.twelve("1,2,3,4,5,6,7,8,9,10,11,12", "x"),
                         [float(n) for n in range(1, 13)])

    def test_the_wrong_count_is_refused(self):
        self.assertIsNone(server.twelve("1,2,3", "x"))

    def test_words_are_refused(self):
        self.assertIsNone(server.twelve("a,b,c,d,e,f,g,h,i,j,k,l", "x"))

    def test_empty_is_simply_absent(self):
        self.assertIsNone(server.twelve("", "x"))

    def test_an_annual_rain_figure_is_spread_evenly(self):
        got = server.rain_monthly("", 36.0)
        self.assertEqual(len(got), 12)
        self.assertAlmostEqual(sum(got), 36.0, places=6)
        self.assertEqual(len(set(got)), 1)

    def test_monthly_rain_overrides_the_annual_figure(self):
        got = server.rain_monthly("1,2,3,4,5,6,7,8,9,10,11,12", 36.0)
        self.assertEqual(got[0], 1.0)

    def test_neither_rain_figure_means_no_gauge(self):
        self.assertIsNone(server.rain_monthly("", 0))

    def test_temperature_normals_convert_to_celsius(self):
        got = server.temp_normal("32,32,32,32,32,32,32,32,32,32,32,32",
                                 "212,212,212,212,212,212,212,212,212,212,212,212")
        self.assertAlmostEqual(got["hi"][0], 0.0, places=6)
        self.assertAlmostEqual(got["lo"][0], 100.0, places=6)

    def test_temperature_normals_need_both_halves(self):
        twelve = ",".join(["50"] * 12)
        self.assertIsNone(server.temp_normal(twelve, ""))
        self.assertIsNone(server.temp_normal("", twelve))


class RainTotals(unittest.TestCase):
    """The Rainfall card's figures come straight off these."""

    def setUp(self):
        self.h = core.History(path=os.devnull)
        self.today = datetime.date(2026, 9, 14)
        self.h.rain_days = {
            "2025-12-31": 5.0,          # last year, must not count
            "2026-01-01": 10.0,
            "2026-08-31": 20.0,
            "2026-09-01": 30.0,
            "2026-09-13": 40.0,
            "2026-09-14": 50.0,
        }

    def test_year_counts_only_this_year(self):
        self.assertAlmostEqual(self.h.rain_year(self.today), 150.0)

    def test_month_counts_only_this_month(self):
        self.assertAlmostEqual(self.h.rain_month(self.today), 120.0)

    def test_one_day_is_today(self):
        self.assertAlmostEqual(self.h.rain_total(1, self.today), 50.0)

    def test_two_days_reaches_back_one(self):
        self.assertAlmostEqual(self.h.rain_total(2, self.today), 90.0)

    def test_adding_rain_accumulates_within_a_day(self):
        h = core.History(path=os.devnull)
        h.add_rain("2026-09-14", 1.5)
        h.add_rain("2026-09-14", 2.5)
        self.assertAlmostEqual(h.rain_days["2026-09-14"], 4.0)


class Poller(server.PollingFetcher):
    """A PollingFetcher that fetches from a script instead of the internet."""

    REFRESH = 0.001
    RETRY = 0.001
    LABEL = "Recorder"

    def __init__(self, stop, script):
        server.PollingFetcher.__init__(self, stop)
        self.script = list(script)
        self.saved = []

    def fetch_once(self):
        step = self.script.pop(0)
        if not self.script:
            self.stop_event.set()          # last step: end the loop
        if isinstance(step, Exception):
            raise step
        return step

    def after_success(self, fresh):
        self.saved.append(fresh)


class FetcherContract(unittest.TestCase):
    """The loop every fetcher now shares. What the card dots read is
    `available` and `error` together, so these two must stay exactly true:
    a success clears the error, and a failure keeps the data."""

    def drive(self, *script):
        import threading
        p = Poller(threading.Event(), script)
        p.run()                             # synchronous; REFRESH is 1ms
        return p

    def test_a_success_stores_the_data(self):
        p = self.drive({"n": 1})
        self.assertEqual(p.data, {"n": 1})
        self.assertEqual(p.error, "")

    def test_a_failure_keeps_the_last_good_data(self):
        # This is the whole contract. A card showing yesterday's reading must
        # still have the reading; only the dot changes.
        p = self.drive({"n": 1}, RuntimeError("boom"))
        self.assertEqual(p.data, {"n": 1})
        self.assertIn("Recorder unavailable", p.error)

    def test_a_later_success_clears_the_error(self):
        p = self.drive({"n": 1}, RuntimeError("boom"), {"n": 2})
        self.assertEqual(p.data, {"n": 2})
        self.assertEqual(p.error, "")

    def test_after_success_runs_only_on_success(self):
        p = self.drive({"n": 1}, RuntimeError("boom"), {"n": 2})
        self.assertEqual(p.saved, [{"n": 1}, {"n": 2}])

    def test_nothing_fetched_yet_is_unavailable(self):
        p = self.drive(RuntimeError("boom"))
        snap = p.snapshot()
        self.assertFalse(snap["available"])
        self.assertIn("Recorder unavailable", snap["error"])

    def test_data_with_an_error_is_available_and_says_so(self):
        p = self.drive({"n": 1}, RuntimeError("boom"))
        snap = p.snapshot()
        self.assertTrue(snap["available"])
        self.assertTrue(snap["error"])
        self.assertEqual(snap["n"], 1)

    def test_the_thread_survives_anything_thrown_at_it(self):
        # A fetcher that dies takes its card's freshness with it, silently.
        p = self.drive(KeyboardInterrupt(), {"n": 1})
        self.assertEqual(p.data, {"n": 1})

    def test_backoff_doubles_then_holds(self):
        import threading
        p = Poller(threading.Event(), [{"n": 1}])
        p.REFRESH, p.RETRY = 900, 60
        self.assertEqual([p.backoff(n) for n in range(1, 7)],
                         [60, 120, 240, 480, 480, 480])

    def test_backoff_never_waits_longer_than_a_refresh(self):
        import threading
        p = Poller(threading.Event(), [{"n": 1}])
        p.REFRESH, p.RETRY = 300, 120
        self.assertEqual(max(p.backoff(n) for n in range(1, 9)), 300)


class FetcherWording(unittest.TestCase):
    """Each fetcher words its own failures. These are the messages that end
    up on a card, so they are worth pinning."""

    @staticmethod
    def http(code):
        import urllib.error
        return urllib.error.HTTPError("http://x", code, "nope", None, None)

    def test_pollen_names_a_rejected_key(self):
        f = server.PollenFetcher(0, 0, "k", None)
        self.assertIn("key rejected", f.describe(self.http(403)))

    def test_pollen_names_an_exhausted_quota(self):
        f = server.PollenFetcher(0, 0, "k", None)
        self.assertIn("quota", f.describe(self.http(429)))

    def test_pollen_falls_back_to_the_shared_wording(self):
        f = server.PollenFetcher(0, 0, "k", None)
        self.assertEqual(f.describe(RuntimeError()),
                         "Pollen unavailable (RuntimeError)")

    def test_speedtest_names_a_token_without_the_right_ability(self):
        f = server.SpeedtestFetcher("http://x", "t", None)
        self.assertIn("results:read", f.describe(self.http(401)))

    def test_forecast_prefers_the_reason_a_network_error_carries(self):
        import urllib.error
        f = server.ForecastFetcher(0, 0, None)
        self.assertEqual(f.describe(urllib.error.URLError("timed out")),
                         "Forecast unavailable (timed out)")

    def test_forecast_tells_a_bug_apart_from_an_outage(self):
        f = server.ForecastFetcher(0, 0, None)
        self.assertEqual(f.describe(KeyError()), "Forecast error (KeyError)")

    def test_the_forecast_retries_sooner_than_the_others(self):
        # Deliberately a different policy: a slow first attempt should not
        # leave the card blank for two minutes.
        fc = server.ForecastFetcher(0, 0, None)
        air = server.AirQualityFetcher(0, 0, None)
        self.assertLess(fc.backoff(1), air.backoff(1))
        self.assertEqual(fc.backoff(1), 15)


class AlertTimes(unittest.TestCase):
    """The banner says when an alert stops, which needs the hazard's end kept
    alongside the message's expiry — they differ, most of all for warnings."""

    def one(self, **props):
        base = {"event": "Flood Watch", "severity": "Moderate",
                "senderName": "NWS Chicago IL",
                "expires": "2026-09-20T07:00:00-05:00"}
        base.update(props)
        return server.AlertsFetcher.parse({"features": [{"properties": base}]})[0]

    def test_keeps_the_hazard_end(self):
        a = self.one(ends="2026-09-20T13:00:00-05:00")
        self.assertEqual(a["ends"], "2026-09-20T13:00:00-05:00")
        self.assertEqual(a["expires"], "2026-09-20T07:00:00-05:00")

    def test_no_end_is_none_not_missing(self):
        self.assertIn("ends", self.one())
        self.assertIsNone(self.one()["ends"])

    def test_keeps_the_office(self):
        self.assertEqual(self.one()["sender"], "NWS Chicago IL")


class WhatIsFalling(unittest.TestCase):
    """The nearest NWS station's present weather, reduced to what the Rainfall
    card draws. The shapes are the API's own: presentWeather items carry the
    METAR code in rawString."""

    def obs(self, *codes, ts="2026-12-19T19:05:00+00:00"):
        return server.ObservationFetcher.parse({"properties": {
            "timestamp": ts, "textDescription": "x",
            "presentWeather": [{"rawString": c} for c in codes]}})

    def test_intensity_comes_from_the_prefix(self):
        self.assertEqual(self.obs("-SN")["intensity"], "light")
        self.assertEqual(self.obs("SN")["intensity"], "moderate")
        self.assertEqual(self.obs("+SN")["intensity"], "heavy")

    def test_snow_outranks_rain_when_both_fall(self):
        self.assertEqual(self.obs("-RA", "SN")["kind"], "snow")
        self.assertEqual(self.obs("RASN")["kind"], "snow")

    def test_freezing_rain_outranks_everything(self):
        self.assertEqual(self.obs("-SN", "FZRA")["kind"], "freezing_rain")
        self.assertEqual(self.obs("-FZDZ")["kind"], "freezing_rain")

    def test_sleet_hail_and_showers(self):
        self.assertEqual(self.obs("PL")["kind"], "sleet")
        self.assertEqual(self.obs("GS")["kind"], "hail")
        self.assertEqual(self.obs("-SHSN")["kind"], "snow")
        self.assertEqual(self.obs("TSRA")["kind"], "rain")

    def test_blowing_snow_is_not_falling_snow(self):
        o = self.obs("BLSN")
        self.assertEqual(o["kind"], "none")
        self.assertTrue(o["blowing"])
        self.assertEqual(self.obs("-SN", "BLSN")["kind"], "snow")

    def test_vicinity_is_not_here(self):
        self.assertEqual(self.obs("VCSH")["kind"], "none")

    def test_nothing_falling(self):
        o = self.obs()
        self.assertEqual((o["kind"], o["intensity"], o["blowing"]), ("none", None, False))
        self.assertEqual(server.ObservationFetcher.parse({})["kind"], "none")

    def test_timestamp_and_bad_timestamp(self):
        self.assertAlmostEqual(self.obs("SN")["observed_at"],
            datetime.datetime(2026, 12, 19, 19, 5, tzinfo=datetime.timezone.utc).timestamp())
        self.assertIsNone(self.obs("SN", ts="yesterday")["observed_at"])

    def test_short_station_name(self):
        f = server.ObservationFetcher.short_name
        self.assertEqual(f("Chicago / West Chicago, Dupage Airport"), "Dupage Airport")
        self.assertEqual(f("De Kalb Taylor Municipal Airport"), "De Kalb Taylor Airport")
        self.assertEqual(f("KDPA"), "KDPA")


class ToolErrors(unittest.TestCase):
    """A test the Ookla CLI never ran is not a failed test of the line."""

    def rows(self, *specs):
        out = []
        for i, kind in enumerate(specs):
            at = "2026-09-19T%02d:00:00Z" % (23 - i)
            if kind == "ok":
                out.append(result(at=at))
            elif kind == "tool":
                out.append({"created_at": at, "status": "failed", "download": None,
                            "data": {"type": "log", "level": "error",
                                     "message": "Error: [0] Cannot read from socket: "}})
            else:
                out.append(result(ok=False, at=at))
        return {"data": out}

    def parse(self, *specs):
        return server.SpeedtestFetcher.parse(self.rows(*specs))

    def test_a_tool_error_is_not_a_failure(self):
        p = self.parse("tool", "ok", "ok", "ok")
        self.assertEqual((p["failures"], p["skipped"]), (0, 1))
        self.assertEqual(p["status"], "good")
        self.assertNotIn("Most recent test failed", p["issues"])

    def test_a_real_failure_still_is(self):
        p = self.parse("fail", "ok", "ok", "ok")
        self.assertEqual((p["failures"], p["skipped"]), (1, 0))
        self.assertIn("Most recent test failed", p["issues"])

    def test_three_tool_errors_in_a_row_is_still_down(self):
        # An outage stops the CLI too; a run of them is not the tool being flaky.
        self.assertEqual(self.parse("tool", "tool", "tool", "ok")["status"], "down")

    def test_history_marks_them(self):
        h = self.parse("tool", "fail", "ok")["history"]
        self.assertEqual([(x["ok"], x["tool"]) for x in h],
                         [(True, False), (False, False), (False, True)])


class TwoStations(unittest.TestCase):
    """DuPage and DeKalb, merged into one answer for the card."""

    NOW = 1_800_000_000

    def rep(self, sid, kind, intensity=None, age=600, miles=20.0, blowing=False):
        return {"station": sid, "station_name": sid, "miles": miles, "kind": kind,
                "intensity": intensity, "blowing": blowing,
                "observed_at": None if age is None else self.NOW - age}

    def merge(self, *reps):
        return server.ObservationFetcher.merge(list(reps), self.NOW)

    def test_either_seeing_snow_means_snow(self):
        m = self.merge(self.rep("KDPA", "none"), self.rep("KDKB", "snow", "light", miles=21.6))
        self.assertEqual((m["kind"], m["station"]), ("snow", "KDKB"))

    def test_heavier_wins_between_two_of_the_same(self):
        m = self.merge(self.rep("KDPA", "snow", "light"), self.rep("KDKB", "snow", "heavy", miles=21.6))
        self.assertEqual(m["intensity"], "heavy")

    def test_nearer_wins_a_tie(self):
        m = self.merge(self.rep("KDKB", "rain", "light", miles=21.6), self.rep("KDPA", "rain", "light", miles=20.9))
        self.assertEqual(m["station"], "KDPA")

    def test_old_snow_is_not_snow_now(self):
        m = self.merge(self.rep("KDPA", "none"), self.rep("KDKB", "snow", "heavy", age=3 * 3600))
        self.assertEqual(m["kind"], "none")

    def test_nothing_recent_hands_back_the_newest(self):
        m = self.merge(self.rep("KDPA", "snow", age=5 * 3600), self.rep("KDKB", "rain", age=3 * 3600))
        self.assertEqual(m["station"], "KDKB")        # the page sees its age and uses the forecast

    def test_blowing_from_either(self):
        m = self.merge(self.rep("KDPA", "none", blowing=True), self.rep("KDKB", "snow", "light"))
        self.assertTrue(m["blowing"])

    def test_both_listed_for_the_record(self):
        m = self.merge(self.rep("KDPA", "none"), self.rep("KDKB", "rain"))
        self.assertEqual([s["station"] for s in m["stations"]], ["KDPA", "KDKB"])

    def test_one_station_down_is_not_an_outage(self):
        f = server.ObservationFetcher(42.1681, -88.4281, None)
        f.stations = [("KDPA", "Dupage Airport", 20.9), ("KDKB", "De Kalb Taylor Municipal Airport", 21.6)]
        def fake(url):
            if "KDPA" in url:
                raise OSError("down")
            return {"properties": {"timestamp": "2026-12-19T19:05:00+00:00",
                                   "presentWeather": [{"rawString": "-SN"}]}}
        f._get = fake
        m = f.fetch_once()
        self.assertEqual((m["station"], m["kind"]), ("KDKB", "snow"))
        self.assertEqual(m["station_name"], "De Kalb Taylor Airport")

    def test_both_down_is_an_error(self):
        f = server.ObservationFetcher(42.1681, -88.4281, None)
        f.stations = [("KDPA", "x", 1.0), ("KDKB", "y", 2.0)]
        def down(url):
            raise OSError("down")
        f._get = down
        with self.assertRaises(OSError):
            f.fetch_once()

    def test_nearest_two_by_real_distance(self):
        f = server.ObservationFetcher(42.1681, -88.4281, None)
        # the API's own order, which is not distance: O'Hare first
        listing = {"features": [
            {"properties": {"stationIdentifier": "KORD", "name": "O'Hare"},
             "geometry": {"coordinates": [-87.9335, 41.9602]}},
            {"properties": {"stationIdentifier": "KDKB", "name": "DeKalb"},
             "geometry": {"coordinates": [-88.7295, 41.9335]}},
            {"properties": {"stationIdentifier": "KDPA", "name": "DuPage"},
             "geometry": {"coordinates": [-88.2481, 41.9078]}}]}
        f._get = lambda url: ({"properties": {"observationStations": "list"}}
                              if "points" in url else listing)
        self.assertEqual([s[0] for s in f.find_stations()], ["KDPA", "KDKB"])
        f.pinned = ["KDKB"]
        self.assertEqual([s[0] for s in f.find_stations()], ["KDKB"])


class NormalsTool(unittest.TestCase):
    """tools/normals.py reshapes NCEI's rows into the compose lines."""

    def rows(self, months=range(1, 13)):
        return [{"DATE": "%02d" % m, "MLY-TMAX-NORMAL": str(30 + m), "MLY-TMIN-NORMAL": str(10 + m),
                 "MLY-PRCP-NORMAL": "%.2f" % (1 + m / 10)} for m in months]

    def setUp(self):
        import importlib.util, os
        spec = importlib.util.spec_from_file_location("normals", os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "normals.py"))
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)

    def test_reshape_is_in_month_order_whatever_the_rows_order(self):
        hi, lo, pr = self.tool.reshape(list(reversed(self.rows())))
        self.assertEqual(hi[0], 31.0); self.assertEqual(hi[11], 42.0)
        self.assertEqual(lo[5], 16.0); self.assertAlmostEqual(pr[11], 2.2)

    def test_a_missing_month_is_an_error_not_a_zero(self):
        with self.assertRaises(ValueError):
            self.tool.reshape(self.rows(range(1, 12)))

    def test_compose_lines_are_what_the_server_parses(self):
        hi, lo, pr = self.tool.reshape(self.rows())
        h, l, r = self.tool.compose_lines(hi, lo, pr)
        val = lambda line: line.split('"')[1]
        self.assertEqual(len(server.twelve(val(h), "x")), 12)
        self.assertEqual(server.twelve(val(r), "x")[0], 1.1)


class SlowHalf(unittest.TestCase):
    """The 24-hour series and the Internet history leave the two-second
    snapshot for the thirty-second one."""

    def test_split_takes_both_and_leaves_the_rest(self):
        snap = {"obs": {"temp_c": 1}, "series": {"temp_c": [1, 2]},
                "internet": {"status": "good", "history": [{"at": 1}]}}
        slow = server.Dashboard.split_slow(snap)
        self.assertEqual(slow, {"series": {"temp_c": [1, 2]}, "internet_history": [{"at": 1}], "nws": None})
        self.assertNotIn("series", snap)
        self.assertNotIn("history", snap["internet"])
        self.assertEqual(snap["internet"]["status"], "good")

    def test_split_copes_with_an_unavailable_internet_card(self):
        snap = {"internet": {"available": False, "error": "x"}}
        slow = server.Dashboard.split_slow(snap)
        self.assertIsNone(slow["internet_history"])
        self.assertIsNone(slow["series"])


class NwsWords(unittest.TestCase):
    """The written forecast, reduced to the periods the outlook shows."""

    def raw(self, n=8):
        return {"properties": {"updateTime": "2026-09-20T02:51:23+00:00", "periods": [
            {"name": "P%d" % i, "shortForecast": "s", "detailedForecast": "d%d" % i,
             "temperature": 60 + i, "isDaytime": i % 2 == 0,
             "startTime": "2026-09-20T%02d:00:00-05:00" % i,
             "probabilityOfPrecipitation": {"value": None if i == 1 else i * 10}}
            for i in range(n)]}}

    def test_keeps_six_periods_in_order(self):
        out = server.NwsForecastFetcher.parse(self.raw())
        self.assertEqual([p["name"] for p in out["periods"]], ["P0", "P1", "P2", "P3", "P4", "P5"])
        self.assertEqual(out["periods"][1]["pop"], None)
        self.assertEqual(out["periods"][2]["pop"], 20)
        self.assertTrue(out["periods"][0]["day"])

    def test_times_and_empty(self):
        out = server.NwsForecastFetcher.parse(self.raw(2))
        self.assertAlmostEqual(out["updated"],
            datetime.datetime(2026, 9, 20, 2, 51, 23, tzinfo=datetime.timezone.utc).timestamp())
        self.assertIsNotNone(out["periods"][0]["start"])
        self.assertEqual(server.NwsForecastFetcher.parse({})["periods"], [])
        self.assertEqual(server.NwsForecastFetcher.parse({"properties": {"periods": [{"x": 1}]}})["periods"], [])


class StationHealth(unittest.TestCase):
    """The dot on every hub-fed card is this one function."""

    def setUp(self):
        self.st = core.StationState()

    def test_no_packet_ever_is_waiting(self):
        self.st.last_packet = None
        self.assertEqual(self.st.health(), "waiting")

    def test_a_recent_packet_is_live(self):
        self.st.last_packet = __import__("time").time()
        self.assertEqual(self.st.health(), "live")

    def test_past_the_stale_mark_is_stale(self):
        now = __import__("time").time()
        self.st.last_packet = now - (core.STALE_AFTER + 1)
        self.assertEqual(self.st.health(), "stale")

    def test_past_the_offline_mark_is_offline(self):
        now = __import__("time").time()
        self.st.last_packet = now - (core.OFFLINE_AFTER + 1)
        self.assertEqual(self.st.health(), "offline")


def blank_history():
    """A History that writes to a throwaway directory, not into the repo."""
    import tempfile
    return core.History(os.path.join(tempfile.mkdtemp(), "history.json"))


def obs_st(strikes=0, gust=5.0, temp=20.0, pres=1000.0, solar=600):
    return {"type": "obs_st", "serial_number": "ST-TEST",
            "obs": [[0, 1.0, 2.0, gust, 180, 3, pres, temp, 50, 40000, 4.0,
                     solar, 0.0, 0, 8, strikes, 2.6, 1]]}


STRIKE = {"type": "evt_strike", "serial_number": "ST-TEST",
          "evt": [0, 8, 4200]}


class StrikeCounting(unittest.TestCase):
    """A strike arrives twice — as an event, then inside the next
    observation's count. Both were added, so every storm counted double."""

    def setUp(self):
        self.st = core.StationState(history=blank_history())

    def test_an_event_and_its_observation_are_one_strike(self):
        self.st.handle(STRIKE)
        self.st.handle(obs_st(strikes=1))
        self.assertEqual(self.st.strikes_today, 1)

    def test_a_lost_event_is_made_up_from_the_observation(self):
        self.st.handle(STRIKE)
        self.st.handle(obs_st(strikes=3))
        self.assertEqual(self.st.strikes_today, 3)

    def test_it_counts_at_once_not_a_minute_later(self):
        self.st.handle(STRIKE)
        self.assertEqual(self.st.strikes_today, 1)

    def test_a_strike_on_the_edge_of_the_minute(self):
        # Heard before one observation, counted in the next.
        self.st.handle(STRIKE)
        self.st.handle(obs_st(strikes=0))
        self.st.handle(obs_st(strikes=1))
        self.assertEqual(self.st.strikes_today, 1)

    def test_an_unclaimed_event_is_let_go_after_one_observation(self):
        self.st.handle(STRIKE)
        self.st.handle(obs_st(strikes=0))
        self.st.handle(obs_st(strikes=0))
        self.st.handle(obs_st(strikes=1))
        self.assertEqual(self.st.strikes_today, 2)

    def test_the_day_keeps_the_same_count(self):
        self.st.handle(STRIKE)
        self.st.handle(obs_st(strikes=2))
        today = datetime.date.today().isoformat()
        self.assertEqual(self.st.history.days[today]["strikes"], 2)


class DailyRecord(unittest.TestCase):
    """What is left of a day once its samples have aged out."""

    def test_live_readings_build_the_day(self):
        st = core.StationState(history=blank_history())
        st.handle(obs_st(gust=5.0, pres=1002.0, temp=18.0))
        st.handle(obs_st(gust=14.0, pres=998.0, temp=24.0))
        st.handle(obs_st(gust=7.0, pres=1000.0, temp=21.0))
        day = st.history.days[datetime.date.today().isoformat()]
        self.assertEqual(day["gust"], 14.0)
        self.assertEqual((day["pmin"], day["pmax"]), (998.0, 1002.0))
        self.assertAlmostEqual(day["tmean"], 21.0)

    def test_a_long_silence_is_not_credited_to_the_next_reading(self):
        h = blank_history()
        h.note_obs("2026-07-01", 1000.0, solar=600)
        h.note_obs("2026-07-01", 1000.0 + 6 * 3600, solar=600)
        # one minute for the first, five at most for the second
        self.assertEqual(h.days["2026-07-01"]["mins"], 6.0)
        self.assertAlmostEqual(h.days["2026-07-01"]["sun"], 60.0)

    def test_it_survives_a_restart(self):
        h = blank_history()
        h.note_obs("2026-07-01", 1000.0, gust_ms=12.5, pres_mb=1001.0)
        h.note_strikes("2026-07-01", 4)
        h.save(force=True)
        again = core.History(h.path)
        self.assertEqual(again.days["2026-07-01"]["gust"], 12.5)
        self.assertEqual(again.days["2026-07-01"]["strikes"], 4)

    def test_a_damaged_entry_is_dropped_not_fatal(self):
        import json
        h = blank_history()
        with open(h.path, "w") as f:
            json.dump({"days": {"2026-07-01": {"gust": "windy"},
                                "2026-07-02": "nonsense",
                                "2026-07-03": {"gust": 9.0}}}, f)
        self.assertEqual(list(core.History(h.path).days), ["2026-07-03"])

    def test_rain_is_no_longer_forgotten_after_800_days(self):
        h = blank_history()
        start = datetime.date(2020, 1, 1)
        for i in range(1500):
            h.add_rain((start + datetime.timedelta(days=i)).isoformat(), 1.0)
        self.assertEqual(len(h.rain_days), 1500)


class MergingTheBackfill(unittest.TestCase):
    """WeatherFlow's record folded into ours, without losing what we saw."""

    def setUp(self):
        self.h = blank_history()

    def test_an_empty_day_is_filled(self):
        self.assertTrue(self.h.merge_day("2026-06-01", {"gust": 9.0, "mins": 1440.0}))
        self.assertEqual(self.h.days["2026-06-01"]["gust"], 9.0)

    def test_extremes_are_a_union(self):
        self.h.days["2026-06-01"] = {"gust": 12.0, "gust_t": 5.0, "pmin": 990.0,
                                     "pmax": 1001.0, "mins": 1440.0}
        self.h.merge_day("2026-06-01", {"gust": 9.0, "gust_t": 7.0, "pmin": 988.0,
                                        "pmax": 1000.0, "mins": 1440.0})
        day = self.h.days["2026-06-01"]
        self.assertEqual((day["gust"], day["gust_t"]), (12.0, 5.0))
        self.assertEqual((day["pmin"], day["pmax"]), (988.0, 1001.0))

    def test_totals_come_from_whoever_watched_more_of_the_day(self):
        # We were restarted at noon: six hours seen, and WeatherFlow saw all.
        self.h.days["2026-06-01"] = {"sun": 900.0, "tmean": 25.0, "mins": 360.0}
        self.h.merge_day("2026-06-01", {"sun": 5200.0, "tmean": 19.0, "mins": 1440.0})
        self.assertEqual(self.h.days["2026-06-01"]["sun"], 5200.0)
        self.assertEqual(self.h.days["2026-06-01"]["tmean"], 19.0)

    def test_a_day_we_saw_whole_keeps_its_own_totals(self):
        self.h.days["2026-06-01"] = {"sun": 5100.0, "mins": 1438.0}
        self.h.merge_day("2026-06-01", {"sun": 5200.0, "mins": 1440.0})
        self.assertEqual(self.h.days["2026-06-01"]["sun"], 5100.0)

    def test_twice_is_the_same_as_once(self):
        wf = {"gust": 9.0, "pmin": 990.0, "strikes": 3.0, "sun": 100.0, "mins": 1440.0}
        self.h.merge_day("2026-06-01", dict(wf))
        once = dict(self.h.days["2026-06-01"])
        self.assertFalse(self.h.merge_day("2026-06-01", dict(wf)))
        self.assertEqual(self.h.days["2026-06-01"], once)

    def test_a_window_of_five_minute_buckets_is_read_as_such(self):
        rows = [[t] for t in (0, 300, 600, 4200, 4500)]      # with a hole in it
        self.assertEqual(server.Backfill.bucket_minutes(rows), 5.0)
        self.assertEqual(server.Backfill.bucket_minutes([[0]]), 1.0)

    def test_a_weatherflow_row_lands_in_the_right_fields(self):
        row = [1750000000, 0.5, 2.0, 11.0, 200, 3, 995.5, 27.0, 60, 50000,
               7.5, 800, 0.0, 0, 5, 2, 2.6, 5]
        day = {}
        server.Backfill.fold_obs(day, row, 5.0)
        self.assertEqual((day["gust"], day["pmin"], day["uv"], day["strikes"]),
                         (11.0, 995.5, 7.5, 2.0))
        self.assertAlmostEqual(day["sun"], 800 * 5 / 60.0, places=1)
        self.assertEqual(day["mins"], 5.0)


class Almanac(unittest.TestCase):
    """Where today stands in the record. All of it is date arithmetic, which
    is exactly the kind that is wrong by one."""

    TODAY = datetime.date(2026, 9, 21)

    def setUp(self):
        self.h = blank_history()

    def day(self, back, lo=10.0, hi=20.0, rain=0.0, **summary):
        iso = (self.TODAY - datetime.timedelta(days=back)).isoformat()
        self.h.temp_days[iso] = {"lo": lo, "hi": hi}
        if rain:
            self.h.rain_days[iso] = rain
        if summary:
            self.h.days[iso] = dict(summary)
        return iso

    def almanac(self, **kw):
        return self.h.almanac(today=self.TODAY, **kw)

    def test_a_dry_streak_counts_today_and_stops_at_the_rain(self):
        for back in range(0, 4):
            self.day(back)
        self.day(4, rain=6.0)
        self.day(5)
        a = self.almanac()
        self.assertEqual((a["dry_days"], a["wet_days"]), (4, 0))
        self.assertEqual(a["last_rain"]["days"], 4)

    def test_dew_does_not_end_a_dry_streak(self):
        self.day(0); self.day(1, rain=0.05); self.day(2)
        self.assertEqual(self.almanac()["dry_days"], 3)

    def test_a_day_nothing_is_known_about_ends_a_streak(self):
        self.day(0); self.day(1); self.day(3)
        self.assertEqual(self.almanac()["dry_days"], 2)

    def test_warmest_since(self):
        self.day(0, hi=31.0)
        self.day(1, hi=25.0); self.day(2, hi=30.9); self.day(3, hi=33.0)
        got = self.almanac()["warmest_since"]
        self.assertEqual((got["days"], got["record"]), (3, False))

    def test_warmest_the_record_holds(self):
        self.day(0, hi=35.0); self.day(1, hi=25.0); self.day(2, hi=30.0)
        got = self.almanac()["warmest_since"]
        self.assertEqual((got["date"], got["days"], got["record"]), (None, 2, True))

    def test_first_frost_belongs_to_the_winter_not_the_year(self):
        self.day(0)
        self.day(160, lo=-3.0)                       # 14 April: last spring's
        a = self.almanac()
        self.assertIsNone(a["first_frost"])
        self.assertEqual(a["last_frost"]["days"], 160)
        self.day(2, lo=-0.5)
        a = self.almanac()
        self.assertEqual((a["first_frost"]["days"], a["frost_days"]), (2, 1))

    def test_the_warm_threshold_is_the_readers(self):
        self.day(0, hi=24.0); self.day(3, hi=26.0); self.day(9, hi=28.0)
        self.assertEqual(self.almanac()["last_warm"]["days"], 9)
        self.assertEqual(self.almanac(warm_c=25.0)["last_warm"]["days"], 3)

    def test_a_year_ago(self):
        self.day(0)
        self.assertIsNone(self.almanac()["year_ago"])
        iso = self.day(365, hi=17.5)
        self.assertEqual(iso, "2025-09-21")
        self.assertEqual(self.almanac()["year_ago"]["hi"], 17.5)

    def test_months_average_the_days_they_have(self):
        self.day(0, lo=10, hi=20, rain=5.0)
        self.day(1, lo=12, hi=24)
        self.day(30, lo=15, hi=30, rain=2.0, gust=14.0)
        months = {m["month"]: m for m in self.almanac()["months"]}
        self.assertEqual(months["2026-09"]["hi_avg"], 22.0)
        self.assertEqual(months["2026-09"]["wet_days"], 1)
        self.assertEqual(months["2026-08"]["gust"], 14.0)

    def test_records_name_the_day(self):
        self.day(0, gust=6.0, pmin=1001.0)
        windy = self.day(40, gust=21.5, pmin=982.0, strikes=55.0)
        wet = self.day(90, rain=48.0)
        last_year = self.day(300, gust=30.0)
        rec = self.h.station_records(today=self.TODAY)
        self.assertEqual(rec["year"]["gust"], {"v": 21.5, "date": windy})
        self.assertEqual(rec["all"]["gust"]["date"], last_year)
        self.assertEqual(rec["year"]["rain"]["date"], wet)
        self.assertEqual(rec["year"]["pmin"]["v"], 982.0)
        self.assertEqual(rec["year"]["strikes"]["v"], 55.0)

    def test_nothing_recorded_is_none_not_zero(self):
        self.day(0)
        rec = self.h.station_records(today=self.TODAY)["year"]
        self.assertIsNone(rec["gust"])
        self.assertIsNone(rec["rain"])
        self.assertIsNone(rec["strikes"])

    def test_an_empty_record_does_not_raise(self):
        a = self.almanac()
        self.assertEqual((a["days"], a["dry_days"], a["plot"]), (0, 0, []))

    def test_the_demo_year_stays_out_of_a_real_record(self):
        for back in range(10):
            self.day(back)
        self.assertEqual(core.seed_demo_days(self.h, self.TODAY), 0)
        self.assertEqual(len(self.h.temp_days), 10)


class KeepingTheRecordSafe(unittest.TestCase):
    """The daily record is years of data in one small file. These are the ways
    it could be lost without anyone noticing, each one closed."""

    def filled(self):
        h = blank_history()
        for i in range(40):
            day = (datetime.date(2026, 6, 1) + datetime.timedelta(days=i)).isoformat()
            h.temp_days[day] = {"lo": 10.0, "hi": 20.0 + i / 10.0}
            h.days[day] = {"gust": 5.0 + i / 10.0, "mins": 1440.0}
        return h

    def test_it_has_a_file_of_its_own(self):
        import json
        h = self.filled()
        h.save(force=True)
        self.assertEqual(list(json.load(open(h.path))), ["samples"])
        self.assertEqual(len(json.load(open(h.days_path))["days"]), 40)
        self.assertEqual(len(core.History(h.path).days), 40)

    def test_a_record_kept_in_the_old_place_is_still_found(self):
        import json
        h = blank_history()
        with open(h.path, "w") as f:
            json.dump({"samples": [], "rain_days": {"2026-06-01": 4.0},
                       "temp_days": {"2026-06-01": {"lo": 1, "hi": 9}},
                       "days": {"2026-06-01": {"gust": 7.0}}}, f)
        again = core.History(h.path)
        self.assertEqual(again.days["2026-06-01"]["gust"], 7.0)
        again.save(force=True)
        self.assertTrue(os.path.exists(again.days_path))

    def test_a_damaged_file_is_set_aside_and_the_backup_used(self):
        h = self.filled()
        h.save(force=True)                           # also makes today's backup
        with open(h.days_path, "w") as f:
            f.write('{"days": {"2026-06-')            # a write cut short
        again = core.History(h.path)
        self.assertEqual(len(again.days), 40)
        kept = [n for n in os.listdir(os.path.dirname(h.path)) if ".damaged-" in n]
        self.assertEqual(len(kept), 1)
        self.assertTrue(any("restored from the backup" in n for n in again.notices))

    def test_a_file_that_cannot_be_moved_is_never_written_over(self):
        h = self.filled()
        h.save(force=True)
        with open(h.days_path, "w") as f:
            f.write("not json")
        real = core.History._set_aside
        core.History._set_aside = lambda self, path: None
        try:
            again = core.History(h.path)
        finally:
            core.History._set_aside = real
        again.save(force=True)
        self.assertEqual(open(h.days_path).read(), "not json")
        self.assertTrue(again.storage_status()["locked"])

    def test_a_failed_save_is_said_and_then_unsaid(self):
        h = self.filled()
        folder = os.path.dirname(h.path)
        os.chmod(folder, 0o555)
        try:
            h.save(force=True)
            status = h.storage_status()
            self.assertFalse(status["ok"])
            self.assertIn("Cannot save", status["error"])
            self.assertIsNotNone(status["failing_since"])
        finally:
            os.chmod(folder, 0o755)
        h.save(force=True)
        self.assertTrue(h.storage_status()["ok"])
        self.assertIsNone(h.storage_status()["failing_since"])

    def test_one_backup_a_day(self):
        h = self.filled()
        h.save(force=True)
        h.save(force=True)
        self.assertEqual(h.storage_status()["backups"], 1)

    def test_thirty_kept_and_the_first_of_each_month_for_good(self):
        h = self.filled()
        start = datetime.date(2026, 1, 1)
        for i in range(100):
            h._backup_day = None
            h._backup_if_due(start + datetime.timedelta(days=i))
        days = [d for d, _p in h._backups()]
        firsts = [d for d in days if d.endswith("-01")]
        self.assertEqual(firsts, ["2026-04-01", "2026-03-01", "2026-02-01", "2026-01-01"])
        self.assertEqual(len(days) - len(firsts), core.BACKUPS_KEPT)
        self.assertEqual(days[0], "2026-04-10")

    def test_a_collapsed_record_is_not_copied_over_the_good_backups(self):
        h = self.filled()
        h._backup_if_due(datetime.date(2026, 7, 1))
        h.days.clear(); h.temp_days.clear()
        h.temp_days["2026-07-02"] = {"lo": 1.0, "hi": 2.0}
        h._backup_day = None
        self.assertFalse(h._backup_if_due(datetime.date(2026, 7, 2)))
        self.assertEqual([d for d, _p in h._backups()], ["2026-07-01"])
        self.assertTrue(any("shrunk" in n for n in h.notices))


class ImpossibleReadings(unittest.TestCase):
    """One bad number from the sensor would otherwise be a record for years."""

    def setUp(self):
        self.st = core.StationState(history=blank_history())
        self.today = datetime.date.today().isoformat()

    def test_a_reading_outside_what_the_sensor_can_mean_is_dropped(self):
        self.st.handle(obs_st(temp=21.0, gust=6.0))
        self.st.handle(obs_st(temp=21.5, gust=140.0))
        self.assertEqual(self.st.data["wind_gust_ms"], 6.0)
        self.assertEqual(self.st.history.days[self.today]["gust"], 6.0)
        self.assertEqual(self.st.rejected_today, 1)

    def test_a_glitch_is_dropped_and_the_card_keeps_the_last_good_value(self):
        self.st.handle(obs_st(temp=21.0))
        self.st.handle(obs_st(temp=48.0))
        self.assertEqual(self.st.data["temp_c"], 21.0)
        self.assertEqual(self.st.history.temp_days[self.today]["hi"], 21.0)

    def test_three_in_a_row_and_it_is_believed(self):
        self.st.handle(obs_st(pres=1000.0))
        for _ in range(3):
            self.st.handle(obs_st(pres=985.0))
        self.assertEqual(self.st.data["pres_mb"], 985.0)

    def test_an_ordinary_change_is_not_a_glitch(self):
        self.st.handle(obs_st(temp=21.0))
        self.st.handle(obs_st(temp=17.5))            # a gust front
        self.assertEqual(self.st.data["temp_c"], 17.5)
        self.assertEqual(self.st.rejected_today, 0)

    def test_the_backfill_is_screened_the_same_way(self):
        day = {}
        core.History.fold(day, 0, 5.0, gust_ms=300.0, pres_mb=20.0, temp_c=15.0)
        self.assertNotIn("gust", day)
        self.assertNotIn("pmin", day)
        self.assertEqual(day["tmean"], 15.0)


class StrikingARecord(unittest.TestCase):
    """A record that was a sensor fault, marked rather than deleted: deleted,
    the backfill would only put it back."""

    TODAY = datetime.date(2026, 9, 21)

    def setUp(self):
        h = self.h = blank_history()
        h.temp_days.update({"2026-07-01": {"lo": 12.0, "hi": 44.0},
                            "2026-07-02": {"lo": 14.0, "hi": 33.0}})
        h.days.update({"2026-07-01": {"gust": 61.0}, "2026-07-02": {"gust": 18.0}})
        h.rain_days.update({"2026-07-01": 90.0, "2026-07-02": 12.0})

    def test_the_next_best_takes_its_place(self):
        self.assertIsNone(self.h.strike("2026-07-01", "gust"))
        rec = self.h.station_records(today=self.TODAY)["all"]
        self.assertEqual(rec["gust"], {"v": 18.0, "date": "2026-07-02"})

    def test_hottest_and_the_rain_totals_honour_it_too(self):
        self.h.strike("2026-07-01", "hi")
        self.h.strike("2026-07-01", "rain")
        self.assertEqual(self.h.temp_record("all", self.TODAY)["hi"], (33.0, "2026-07-02"))
        self.assertEqual(self.h.temp_record("all", self.TODAY)["lo"], (12.0, "2026-07-01"))
        self.assertEqual(self.h.rain_year(self.TODAY), 12.0)

    def test_it_can_be_put_back(self):
        self.h.strike("2026-07-01", "gust")
        self.h.strike("2026-07-01", "gust", restore=True)
        self.assertEqual(self.h.station_records(today=self.TODAY)["all"]["gust"]["v"], 61.0)
        self.assertEqual(self.h.struck, {})

    def test_the_mark_survives_a_restart_and_the_number_is_kept(self):
        self.h.strike("2026-07-01", "gust")
        again = core.History(self.h.path)
        self.assertTrue(again.is_struck("2026-07-01", "gust"))
        self.assertEqual(again.days["2026-07-01"]["gust"], 61.0)

    def test_nonsense_is_refused(self):
        self.assertIn("Unknown", self.h.strike("2026-07-01", "mood"))
        self.assertIn("Not a date", self.h.strike("last tuesday", "gust"))

    def test_the_export_keeps_the_number_and_says_it_was_struck(self):
        self.h.strike("2026-07-01", "gust")
        lines = self.h.csv(temp="°F", wind="mph", pres="inHg", rain="in").split("\r\n")
        self.assertTrue(lines[0].startswith("date,high_F,low_F,mean_temp_F,rain_in,peak_gust_mph"))
        first = dict(zip(lines[0].split(","), lines[1].split(",")))
        self.assertEqual(first["date"], "2026-07-01")
        self.assertEqual(first["high_F"], "111.2")
        self.assertEqual(first["rain_in"], "3.54")
        self.assertEqual(first["peak_gust_mph"], "136.5")
        self.assertEqual(first["struck_as_not_real"], "gust")

    def test_units_asked_for_in_a_url(self):
        class Args: temp_unit, wind_unit, pres_unit, rain_unit = "°F", "mph", "inHg", "in"
        units = server.Handler._units("temp=C&wind=km%2Fh&pres=banana", Args)
        self.assertEqual(units, {"temp": "°C", "wind": "km/h", "pres": "inHg", "rain": "in"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
