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
import time
import unittest
import threading
import json

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


class OneBannerPerStatement(unittest.TestCase):
    """The office issued a fog statement twice, four minutes apart, the
    second with a typo fixed, and never withdrew the first."""

    def feat(self, sent, text, event="Special Weather Statement",
             sender="NWS Chicago IL", expires="2026-09-27T09:30:00-05:00", ends=None):
        return {"properties": {"id": "urn:" + sent, "event": event, "senderName": sender,
                               "sent": sent, "expires": expires, "ends": ends,
                               "headline": event + " issued", "severity": "Moderate",
                               "description": text, "areaDesc": "Boone; McHenry; Lake"}}

    def parse(self, *feats):
        # As seen at 8 that morning: these are the real fog statement's times,
        # and it ended at 9:30, after which the expiry filter rightly drops it.
        then = __import__("time").mktime((2026, 9, 27, 8, 0, 0, 0, 0, -1))
        return server.AlertsFetcher.parse({"features": list(feats)}, now=then)

    def test_the_same_statement_twice_is_one_banner_the_newer_kept(self):
        out = self.parse(self.feat("2026-09-27T07:04:00-05:00", "Fog over northwets Indiana."),
                         self.feat("2026-09-27T07:08:00-05:00", "Fog over northwest Indiana."))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["copies"], 2)
        self.assertEqual(out[0]["sent"], "2026-09-27T07:08:00-05:00")
        self.assertIn("northwest", out[0]["description"])

    def test_two_different_statements_ending_together_stay_two(self):
        out = self.parse(self.feat("2026-09-27T07:04:00-05:00", "Dense fog this morning across the area."),
                         self.feat("2026-09-27T07:08:00-05:00", "A line of strong storms will move through late tonight."))
        self.assertEqual(len(out), 2)

    def test_different_events_never_fold(self):
        out = self.parse(self.feat("2026-09-27T07:04:00-05:00", "Snow.", event="Winter Weather Advisory"),
                         self.feat("2026-09-27T07:08:00-05:00", "Snow.", event="Winter Storm Warning"))
        self.assertEqual(len(out), 2)

    def test_an_alert_past_its_end_is_over_whatever_the_feed_says(self):
        import time as _t
        late = _t.mktime((2026, 9, 27, 9, 45, 0, 0, 0, -1))          # 9:45 local, after a 9:30 end
        out = server.AlertsFetcher.parse({"features": [self.feat("2026-09-27T07:08:00-05:00", "Fog.")]}, now=late)
        self.assertEqual(out, [])
        early = _t.mktime((2026, 9, 27, 9, 0, 0, 0, 0, -1))
        self.assertEqual(len(server.AlertsFetcher.parse({"features": [self.feat("2026-09-27T07:08:00-05:00", "Fog.")]}, now=early)), 1)

    def test_the_text_is_unwrapped_but_keeps_its_paragraphs(self):
        out = self.parse(self.feat("2026-09-27T07:04:00-05:00",
            "Locally dense fog will reduce\nvisibilities to a quarter\nmile.\n\nUse your low beams if\nfog is encountered."))
        self.assertEqual(out[0]["description"],
            "Locally dense fog will reduce visibilities to a quarter mile.\n\nUse your low beams if fog is encountered.")
        self.assertEqual(out[0]["areas"], ["Boone", "McHenry", "Lake"])


class AlertTimes(unittest.TestCase):
    """The banner says when an alert stops, which needs the hazard's end kept
    alongside the message's expiry — they differ, most of all for warnings."""

    def one(self, **props):
        base = {"event": "Flood Watch", "severity": "Moderate",
                "senderName": "NWS Chicago IL",
                "expires": "2026-09-20T07:00:00-05:00"}
        base.update(props)
        # As seen from the morning these samples were written, before they end.
        then = __import__("time").mktime((2026, 9, 20, 6, 0, 0, 0, 0, -1))
        return server.AlertsFetcher.parse({"features": [{"properties": base}]}, now=then)[0]

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


class ToolsMistakes(unittest.TestCase):
    """Figures the Ookla CLI reports that the line cannot have produced. They
    were taken at face value, and one of them set a week's "worst" latency
    to thirty-seven days."""

    def parse(self, *rs):
        return server.SpeedtestFetcher.parse(rows(*rs))

    def test_a_latency_of_thirty_seven_days_is_not_a_latency(self):
        out = self.parse(result(idle=10.3, loaded=3251667954.1))
        self.assertIsNone(out["bloat"])
        self.assertEqual(out["history"][0]["loaded"], None)
        self.assertFalse(any("under load" in i for i in out["issues"]))
        self.assertIn("download latency", out["dropped"])

    def test_one_leg_overflowing_leaves_the_other(self):
        r = result(idle=10.0, loaded=25.0)
        r["data"]["upload"]["latency"]["iqm"] = 3251667954.1
        out = self.parse(r)
        self.assertEqual(out["bloat"], 15.0)
        self.assertEqual(out["dropped"], ["upload latency"])

    def test_heavy_loss_on_a_fast_test_is_the_probe_not_the_line(self):
        out = self.parse(result(down_mbps=754.0, loss=86.6))
        self.assertIsNone(out["loss"])
        self.assertFalse(any("loss" in i for i in out["issues"]))
        self.assertEqual(out["dropped"], ["loss"])
        self.assertEqual(out["status"], "good")

    def test_heavy_loss_on_a_slow_test_is_believed(self):
        out = self.parse(result(down_mbps=12.0, loss=40.0))
        self.assertEqual(out["loss"], 40.0)
        self.assertIn("Packet loss 40.0%", out["issues"])

    def test_light_loss_on_a_fast_test_is_still_believed(self):
        out = self.parse(result(down_mbps=800.0, loss=2.5))
        self.assertEqual(out["loss"], 2.5)
        self.assertIn("Packet loss 2.5%", out["issues"])

    def test_loss_outside_a_percentage_is_nothing(self):
        self.assertIsNone(self.parse(result(down_mbps=5.0, loss=140.0))["loss"])

    def test_the_week_says_what_it_left_out(self):
        week = server.SpeedtestFetcher.window(
            rows(result(loss=86.6), result(idle=10.0, loaded=3251667954.1, at="2026-09-14T11:00:00Z")),
            7, now=__import__("time").mktime((2026, 9, 14, 13, 0, 0, 0, 0, -1)))
        dropped = [p["dropped"] for p in week["points"]]
        self.assertEqual(sorted(map(str, dropped)), sorted(["['loss']", "['download latency', 'upload latency']"]))


class WinterWords(unittest.TestCase):
    """Frost, freeze and frostbite, on NWS's thresholds."""

    TODAY = datetime.date(2026, 10, 12)

    def days(self, *lows_f):
        return [{"date": (self.TODAY + datetime.timedelta(days=i)).isoformat(),
                 "tmin_c": (f - 32) * 5 / 9} for i, f in enumerate(lows_f)]

    def outlook(self, *lows_f):
        return core.winter_outlook(self.days(*lows_f), today=self.TODAY)

    def test_a_mild_week_says_nothing(self):
        self.assertIsNone(self.outlook(45, 41, 38, 40))

    def test_tonight_is_the_lower_of_today_and_tomorrow(self):
        got = self.outlook(45, 33, 50, 50)          # the cold is tomorrow morning
        self.assertEqual((got["kind"], got["when"]), ("frost", "tonight"))
        self.assertAlmostEqual(got["low_c"] * 9 / 5 + 32, 33, places=5)

    def test_the_thresholds(self):
        self.assertEqual(self.outlook(36)["kind"], "frost")
        self.assertEqual(self.outlook(32)["kind"], "freeze")
        self.assertEqual(self.outlook(28)["kind"], "hard")
        self.assertIsNone(self.outlook(36.5))

    def test_a_cold_night_later_in_the_week_is_named(self):
        got = self.outlook(45, 44, 40, 30)          # Thursday: Oct 15 2026
        self.assertEqual((got["kind"], got["when"], got["date"]), ("freeze", "Thursday", "2026-10-15"))

    def test_only_the_first_cold_night_is_reported(self):
        self.assertEqual(self.outlook(45, 35, 20, 10)["kind"], "frost")

    def test_yesterday_does_not_count(self):
        days = self.days(20, 45, 45)
        days[0]["date"] = (self.TODAY - datetime.timedelta(days=1)).isoformat()
        self.assertIsNone(core.winter_outlook(days, today=self.TODAY))

    def test_freezing_since_is_the_start_of_the_run(self):
        t = 1_700_000_000
        rows = [(t, 2.0), (t + 600, 0.5), (t + 1200, -0.2), (t + 1800, -1.0), (t + 2400, -1.5)]
        self.assertEqual(core.freezing_since(rows, now=t + 2500), t + 1200)

    def test_not_below_freezing_now_is_nothing(self):
        t = 1_700_000_000
        self.assertIsNone(core.freezing_since([(t, -5.0), (t + 600, 0.5)], now=t + 700))

    def test_a_gap_in_the_record_ends_the_run(self):
        t = 1_700_000_000
        rows = [(t, -5.0), (t + 5000, -4.0), (t + 5600, -3.0)]   # over an hour between the first two
        self.assertEqual(core.freezing_since(rows, now=t + 5700), t + 5000)

    def test_a_stale_record_is_not_below_freezing_now(self):
        t = 1_700_000_000
        self.assertIsNone(core.freezing_since([(t, -5.0)], now=t + 7200))

    def test_frostbite_times_follow_the_chart(self):
        self.assertIsNone(core.frostbite_minutes(-10))
        self.assertEqual(core.frostbite_minutes(-18), 30)
        self.assertEqual(core.frostbite_minutes(-33), 10)
        self.assertEqual(core.frostbite_minutes(-50), 5)

    def test_the_state_carries_them(self):
        st = core.StationState(history=blank_history())
        st.handle(obs_st(temp=-20.0, gust=9.0))
        st.data["wind_avg_ms"] = 9.0
        snap = st.snapshot()
        self.assertEqual(snap["derived"]["feels_model"], "Wind chill")
        self.assertEqual(snap["derived"]["frostbite_min"], 30)
        self.assertIsNotNone(snap["derived"]["freezing_since"])


class CheckNow(unittest.TestCase):
    """One fetch on demand, and what it says."""

    def dash(self, **fetchers):
        class Args: udp_port = 50222; obs_stations = ""
        d = server.Dashboard.__new__(server.Dashboard)
        d.args = Args()
        d.state = core.StationState(history=blank_history())
        d.ai = {}
        for name in ("forecast", "observations", "air", "pollen", "speedtest",
                     "nws", "alerts", "backfill"):
            setattr(d, name, fetchers.get(name))
        return d

    class Fake(server.PollingFetcher):
        LABEL = "Forecast"
        def __init__(self, result=None, exc=None):
            server.PollingFetcher.__init__(self, threading.Event())
            self.result, self.exc, self.stored = result, exc, None
        def fetch_once(self):
            if self.exc: raise self.exc
            return self.result
        def store(self, fresh): self.stored = fresh

    def test_a_working_source_reports_what_came_back_and_refreshes_it(self):
        f = self.Fake({"days": [1] * 10, "current": {"temp_c": 12.5, "code": 3}})
        out = self.dash(forecast=f).check("forecast")
        self.assertTrue(out["ok"])
        self.assertEqual(out["summary"], "10 days; now 12.5 °C")
        self.assertIsNotNone(f.stored)
        self.assertIn("took_ms", out)

    def test_a_failing_source_reports_the_failure_and_the_http_code(self):
        import urllib.error, io
        exc = urllib.error.HTTPError("https://x", 503, "nope", {}, io.BytesIO(b""))
        f = self.Fake(exc=exc)
        out = self.dash(forecast=f).check("forecast")
        self.assertFalse(out["ok"])
        self.assertIn("HTTP 503", out["error"])
        self.assertIsNone(f.stored)

    def test_ai_status_reads_the_ones_that_are_on_and_names_the_ones_that_fail(self):
        def F(name, **kw):
            f = self.Fake(**kw)
            f.NAME = name
            return f
        d = self.dash()
        d.ai = {"claude": F("Claude", result={"status": "major_outage"}),
                "gemini": F("Gemini", exc=ValueError("none of its components were found"))}
        out = d.check("ai")
        self.assertFalse(out["ok"])
        self.assertIn("Claude major outage", out["error"])
        self.assertIn("Gemini:", out["error"])
        self.assertNotIn("ChatGPT", out["error"])          # switched off, so not asked

    def test_ai_status_says_how_each_services_components_were_chosen(self):
        def F(name, result):
            f = self.Fake(result)
            f.NAME = name
            return f
        d = self.dash()
        d.ai = {"claude": F("Claude", {"status": "operational", "via": None}),
                "chatgpt": F("ChatGPT", {"status": "degraded_performance", "via": "group"})}
        out = d.check("ai")
        self.assertTrue(out["ok"])
        self.assertIn("Claude operational ·", out["summary"])                # counts everything: nothing to say
        self.assertIn("ChatGPT degraded performance (from the page's own group)", out["summary"])
        d.ai["chatgpt"] = F("ChatGPT", {"status": "operational", "via": "names"})
        self.assertIn("(from the fixed list of names", d.check("ai")["summary"])

    def test_the_result_says_how_the_components_were_chosen(self):
        raw = {"components": [comp("Conversations", id="chat-conv")]}
        self.assertEqual(server.ChatGptStatusFetcher.parse(raw, ids={"chat-conv"})["via"], "group")
        self.assertEqual(server.ChatGptStatusFetcher.parse(raw, ids={"nope"})["via"], "names")
        self.assertEqual(server.ChatGptStatusFetcher.parse(raw)["via"], "names")
        self.assertIsNone(server.ClaudeStatusFetcher.parse(raw)["via"])      # it counts everything

    def test_ai_status_with_everything_off_says_so(self):
        out = self.dash().check("ai")
        self.assertFalse(out["ok"])
        self.assertIn("switched off", out["error"])

    def test_a_source_that_is_off_says_so(self):
        out = self.dash().check("pollen")
        self.assertEqual(out["error"], "No pollen key is saved")

    def test_the_hub_counts_the_last_minutes_packets(self):
        d = self.dash()
        for _ in range(3): d.state.handle({"type": "rapid_wind", "serial_number": "ST-1", "ob": [0, 1.0, 90]}, ("10.0.0.47", 1))
        d.state.handle(obs_st(), ("10.0.0.47", 1))
        out = d.check("hub")
        self.assertTrue(out["ok"])
        self.assertIn("4 packets in the last minute", out["summary"])
        self.assertIn("rapid_wind 3", out["summary"])
        self.assertIn("from 10.0.0.47", out["summary"])

    def test_a_silent_hub_is_told_what_to_check(self):
        out = self.dash().check("hub")
        self.assertFalse(out["ok"])
        self.assertIn("UDP 50222", out["error"])

    def test_nothing_is_echoed_that_could_carry_a_token(self):
        out = self.dash().check("forecast")
        self.assertNotIn("http", json.dumps(out).lower().replace("http 4", ""))


class Logging(unittest.TestCase):
    """One log: to stdout with a time, into a ring, and on to syslog."""

    def setUp(self):
        self.saved = (core.LOG.syslog, list(core.LOG.lines))
        core.LOG.syslog = None
        core.LOG.lines.clear()

    def tearDown(self):
        core.LOG.syslog = self.saved[0]

    def listener(self):
        import socket
        r = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        r.bind(("127.0.0.1", 0)); r.settimeout(2)
        self.addCleanup(r.close)
        return r

    def test_a_line_is_kept_with_its_level(self):
        core.log("hub", "no packets for 2m", "warning")
        last = core.LOG.recent(1)[0]
        self.assertEqual((last["tag"], last["level"], last["text"]), ("hub", "warning", "no packets for 2m"))

    def test_an_unknown_level_is_info(self):
        core.log("x", "y", "loud")
        self.assertEqual(core.LOG.recent(1)[0]["level"], "info")

    def test_the_syslog_line_is_rfc_3164_with_the_right_priority(self):
        r = self.listener()
        core.LOG.syslog = core.Syslog("127.0.0.1", r.getsockname()[1])
        core.log("alert", "Tornado Warning (Extreme)", "critical")
        line = r.recv(2048).decode()
        self.assertTrue(line.startswith("<130>"), line)          # local0 (16*8) + critical (2)
        self.assertRegex(line, r"^<130>[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d \S+ tempest\[\d+\]: alert: Tornado Warning")
        self.assertEqual(core.LOG.syslog.sent, 1)

    def test_debug_lines_stay_local(self):
        r = self.listener()
        core.LOG.syslog = core.Syslog("127.0.0.1", r.getsockname()[1])
        core.log("x", "quiet", "debug")
        self.assertEqual(core.LOG.syslog.sent, 0)
        self.assertEqual(core.LOG.recent(1)[0]["text"], "quiet")

    def test_a_server_that_is_not_there_is_counted_not_raised(self):
        sl = core.Syslog("127.0.0.1", 9, proto="tcp")         # discard port, nothing listens
        self.assertFalse(sl.send("info", "x", "y"))
        self.assertEqual(sl.failed, 1)
        self.assertTrue(sl.last_error)

    def test_the_status_says_where_it_goes(self):
        core.LOG.syslog = core.Syslog("10.0.0.101", 514)
        st = core.LOG.syslog_status()
        self.assertEqual((st["enabled"], st["host"], st["port"], st["proto"]), (True, "10.0.0.101", 514, "udp"))
        core.LOG.syslog = None
        self.assertEqual(core.LOG.syslog_status(), {"enabled": False})


class FetcherLogLines(unittest.TestCase):
    """The first failure and the recovery, not every retry."""

    class Flaky(server.PollingFetcher):
        LABEL = "Forecast"
        REFRESH = 0.01; RETRY = 0.01
        def __init__(self, outcomes):
            server.PollingFetcher.__init__(self, threading.Event())
            self.outcomes = list(outcomes)
        def fetch_once(self):
            o = self.outcomes.pop(0) if self.outcomes else {"ok": True}
            if o is None:
                self.stop_event.set(); return {"ok": True}
            if isinstance(o, Exception): raise o
            return o
        def backoff(self, fails): return 0.01

    def test_three_failures_are_one_warning_and_one_recovery(self):
        core.LOG.lines.clear()
        import urllib.error, io
        boom = urllib.error.HTTPError("https://x", 503, "nope", {}, io.BytesIO(b""))
        f = self.Flaky([boom, boom, boom, {"ok": True}, None])
        f.run()
        lines = [l for l in core.LOG.recent() if l["tag"] == "forecast"]
        self.assertEqual([l["level"] for l in lines], ["warning", "notice"])
        self.assertIn("HTTP 503", lines[0]["text"])
        self.assertIn("back after 3 failures", lines[1]["text"])


class AlertLogLines(unittest.TestCase):
    def test_an_alert_appearing_and_ending_at_its_severity(self):
        core.LOG.lines.clear()
        f = server.AlertsFetcher(0, 0, threading.Event())
        f.store([{"id": "a", "event": "Tornado Warning", "severity": "Extreme", "ends": "2026-09-27T21:15:00-05:00", "copies": 1}])
        f.store([])
        lines = [l for l in core.LOG.recent() if l["tag"] == "alert"]
        self.assertEqual([(l["level"], l["text"][:20]) for l in lines],
                         [("critical", "Tornado Warning (Ext"), ("notice", "Tornado Warning ende")])


def comp(name, status="operational", id=None):
    return {"id": id or name, "name": name, "status": status}


class StatusPages(unittest.TestCase):
    """What the AI status card says. The shapes are those the pages really send."""

    def test_claude_is_the_worst_of_its_components(self):
        raw = {"components": [comp("claude.ai"), comp("Claude API", "partial_outage"),
                              {"id": "g", "name": "Group", "status": "operational", "group": True}]}
        self.assertEqual(server.ClaudeStatusFetcher.parse(raw, now=1.0)["status"], "partial_outage")

    def test_all_operational_is_operational(self):
        self.assertEqual(server.ClaudeStatusFetcher.parse({"components": [comp("a"), comp("b")]})["status"], "operational")

    def test_chatgpt_ignores_the_api_and_codex(self):
        raw = {"components": [comp("Conversations"), comp("Realtime", "major_outage"),
                              comp("Codex API", "major_outage"), comp("Ads Manager", "partial_outage")]}
        self.assertEqual(server.ChatGptStatusFetcher.parse(raw)["status"], "operational")

    def test_chatgpt_counts_its_own_parts(self):
        raw = {"components": [comp("Voice mode", "partial_outage"), comp("Realtime")]}
        self.assertEqual(server.ChatGptStatusFetcher.parse(raw)["status"], "partial_outage")

    def test_renamed_components_are_an_error_not_green(self):
        with self.assertRaises(ValueError):
            server.ChatGptStatusFetcher.parse({"components": [comp("Realtime")]})
        f = server.ChatGptStatusFetcher(threading.Event())
        self.assertIn("none of its components", f.describe(ValueError("none of its components were found")))

    LAYOUT = {"summary": {"structure": {"items": [
        {"group": {"name": "APIs", "components": [{"component_id": "api-login", "name": "Login"}]}},
        {"group": {"name": "ChatGPT", "components": [{"component_id": "chat-login", "name": "Login"},
                                                     {"component_id": "chat-conv", "name": "Conversations"}]}}]}}}

    def test_the_chatgpt_group_is_read_from_the_pages_own_layout(self):
        self.assertEqual(server.ChatGptStatusFetcher.group_ids(self.LAYOUT), {"chat-login", "chat-conv"})
        self.assertIsNone(server.ChatGptStatusFetcher.group_ids({"summary": {}}))
        self.assertIsNone(server.ChatGptStatusFetcher.group_ids({}))

    def test_two_components_called_login_are_told_apart_by_id(self):
        raw = {"components": [comp("Login", "major_outage", id="api-login"),
                              comp("Login", id="chat-login"), comp("Conversations", id="chat-conv")]}
        ids = server.ChatGptStatusFetcher.group_ids(self.LAYOUT)
        self.assertEqual(server.ChatGptStatusFetcher.parse(raw, ids=ids)["status"], "operational")
        # without the layout both count, which is the fallback's known cost
        self.assertEqual(server.ChatGptStatusFetcher.parse(raw)["status"], "major_outage")

    def test_ids_that_match_nothing_fall_back_to_the_names(self):
        raw = {"components": [comp("Conversations", "partial_outage", id="x"), comp("Realtime", id="y")]}
        self.assertEqual(server.ChatGptStatusFetcher.parse(raw, ids={"not-there"})["status"], "partial_outage")

    def test_a_layout_that_cannot_be_read_only_costs_the_refinement(self):
        summary = {"components": [comp("Conversations", "degraded_performance", id="chat-conv")]}
        f = server.ChatGptStatusFetcher(threading.Event())
        def get(url, fail):
            if url == f.LAYOUT and fail:
                raise OSError("layout is down")
            return self.LAYOUT if url == f.LAYOUT else summary
        for fail in (True, False):
            f.get = lambda url, fail=fail: get(url, fail)
            self.assertEqual(f.fetch_once()["status"], "degraded_performance")

    def test_an_unknown_status_is_not_fine(self):
        self.assertEqual(server.ClaudeStatusFetcher.parse({"components": [comp("x", "on_fire")]})["status"], "on_fire")

    def gemini(self, *incidents):
        return server.GeminiStatusFetcher.parse(list(incidents), now=1.0)["status"]

    @staticmethod
    def google(status, product="npdyhgECDJ6tB66MxXyo"):
        return {"id": "i", "affected_products": [{"title": "t", "id": product}],
                "most_recent_update": {"status": status}}

    def test_gemini_is_fine_when_every_incident_is_over(self):
        self.assertEqual(self.gemini(self.google("AVAILABLE")), "operational")

    def test_an_open_outage_is_red(self):
        self.assertEqual(self.gemini(self.google("SERVICE_OUTAGE")), "major_outage")

    def test_an_open_disruption_is_amber(self):
        self.assertEqual(self.gemini(self.google("SERVICE_DISRUPTION")), "degraded_performance")

    def test_the_worst_open_incident_wins(self):
        self.assertEqual(self.gemini(self.google("SERVICE_DISRUPTION"), self.google("SERVICE_OUTAGE")), "major_outage")

    def test_another_google_product_is_not_gemini(self):
        self.assertEqual(self.gemini(self.google("SERVICE_OUTAGE", product="gmail")), "operational")

    def test_a_feed_that_is_not_a_list_is_an_error(self):
        with self.assertRaises(ValueError):
            server.GeminiStatusFetcher.parse({"error": "nope"})


class AiStatusSettings(unittest.TestCase):
    def cfg(self):
        import tempfile
        return server.Config(os.path.join(tempfile.mkdtemp(), "c.json"), {})

    def test_everything_is_on_at_its_built_in_address_by_default(self):
        c = self.cfg()
        self.assertEqual(c.ai_status, {k: {"enabled": True, "url": ""} for k in ("claude", "chatgpt", "gemini")})
        self.assertIn("status.claude.com", c.public()["ai_defaults"]["claude"])

    def test_a_choice_is_saved_and_survives_a_reload(self):
        c = self.cfg()
        changed, err = c.apply({"ai_status": {"gemini": {"enabled": False},
                                              "claude": {"url": " https://example.org/summary.json "}}})
        self.assertEqual((changed, err), (["ai_status"], ""))
        again = server.Config(c.path, {})
        self.assertFalse(again.ai_status["gemini"]["enabled"])
        self.assertEqual(again.ai_status["claude"]["url"], "https://example.org/summary.json")
        self.assertTrue(again.ai_status["chatgpt"]["enabled"])

    def test_an_address_that_is_not_a_web_address_is_refused(self):
        c = self.cfg()
        for bad in ("ftp://x.example/y", "not a url", "https://", "javascript:alert(1)", 5):
            self.assertTrue(c.apply({"ai_status": {"claude": {"url": bad}}})[1], bad)
        self.assertEqual(c.ai_status["claude"]["url"], "")

    def test_an_empty_address_goes_back_to_the_built_in_one(self):
        c = self.cfg()
        c.apply({"ai_status": {"claude": {"url": "https://example.org/x"}}})
        c.apply({"ai_status": {"claude": {"url": ""}}})
        self.assertEqual(c.ai_status["claude"]["url"], "")

    def dash(self, c):
        d = server.Dashboard.__new__(server.Dashboard)
        d.config, d.ai = c, {}
        return d

    def test_switching_a_service_off_stops_its_fetcher_and_on_starts_a_new_one(self):
        c = self.cfg()
        d = self.dash(c)
        started = []
        real = server.StatusFeed.start
        server.StatusFeed.start = lambda self: started.append(self.NAME)   # no network in a test
        try:
            d.apply_ai_status()
            self.assertEqual(sorted(d.ai), ["chatgpt", "claude", "gemini"])
            old = d.ai["gemini"]
            c.apply({"ai_status": {"gemini": {"enabled": False}}})
            d.apply_ai_status()
            self.assertNotIn("gemini", d.ai)
            self.assertTrue(old.stop_event.is_set())
            c.apply({"ai_status": {"gemini": {"enabled": True}}})
            d.apply_ai_status()
            self.assertIn("gemini", d.ai)
            self.assertIsNot(d.ai["gemini"], old)
        finally:
            server.StatusFeed.start = real

    def test_the_log_says_what_was_switched_and_stays_quiet_at_boot(self):
        c = self.cfg()
        d = self.dash(c)
        real = server.StatusFeed.start
        server.StatusFeed.start = lambda self: None
        say = lambda: [l["text"] for l in core.LOG.recent() if l["tag"] == "ai status"]
        try:
            core.LOG.lines.clear()
            d.apply_ai_status(quiet=True)
            self.assertEqual(say(), [])
            c.apply({"ai_status": {"gemini": {"enabled": False}}}); d.apply_ai_status()
            c.apply({"ai_status": {"gemini": {"enabled": True}}}); d.apply_ai_status()
            c.apply({"ai_status": {"claude": {"url": "https://example.org/x"}}}); d.apply_ai_status()
            c.apply({"ai_status": {"claude": {"url": ""}}}); d.apply_ai_status()
            d.apply_ai_status()                                   # nothing changed: nothing said
            self.assertEqual(say(), ["Gemini switched off", "Gemini switched on",
                                     "Claude reading from https://example.org/x",
                                     "Claude reading from the built-in address"])
            core.LOG.lines.clear()
            c.apply({"ai_status": {"chatgpt": {"url": "https://example.org/y"}}})
            d2 = self.dash(c)
            d2.apply_ai_status(quiet=True)                        # a boot with an address set says so
            self.assertEqual(say(), ["ChatGPT reading from https://example.org/y"])
        finally:
            server.StatusFeed.start = real

    def test_a_changed_address_is_a_new_fetcher_reading_from_it(self):
        c = self.cfg()
        d = self.dash(c)
        real = server.StatusFeed.start
        server.StatusFeed.start = lambda self: None
        try:
            d.apply_ai_status()
            same = d.ai["claude"]
            d.apply_ai_status()
            self.assertIs(d.ai["claude"], same)              # nothing changed, nothing restarted
            c.apply({"ai_status": {"claude": {"url": "https://example.org/x"}}})
            d.apply_ai_status()
            self.assertIsNot(d.ai["claude"], same)
            self.assertEqual(d.ai["claude"].url, "https://example.org/x")
        finally:
            server.StatusFeed.start = real


class AiStatusLogLines(unittest.TestCase):
    """The log says when a service changes state, not on every poll."""

    def lines(self, f, *results, tag="claude status"):
        core.LOG.lines.clear()
        for r in results:
            f.store(dict({"fetched_at": 1.0}, **r))
            f.data = f.data or {}
        return [(l["level"], l["text"]) for l in core.LOG.recent() if l["tag"] == tag]

    def claude(self):
        return server.ClaudeStatusFetcher(threading.Event())

    def test_a_service_that_stays_fine_says_nothing(self):
        self.assertEqual(self.lines(self.claude(), *[{"status": "operational"}] * 3), [])

    def test_degrading_worsening_and_recovering_are_lines_at_their_severity(self):
        got = self.lines(self.claude(), {"status": "operational"}, {"status": "degraded_performance"},
                         {"status": "degraded_performance"}, {"status": "major_outage"},
                         {"status": "operational"})
        self.assertEqual([lv for lv, _ in got], ["notice", "warning", "notice"])
        self.assertEqual(got[0][1], "operational → degraded performance")
        self.assertEqual(got[1][1], "degraded performance → major outage")
        self.assertIn("back to operational after", got[2][1])

    def test_a_service_found_down_at_start_is_news_and_one_found_fine_is_not(self):
        self.assertEqual(self.lines(self.claude(), {"status": "partial_outage"}),
                         [("warning", "operational → partial outage")])

    def test_the_fallback_to_names_is_said_once_with_its_reason_and_so_is_the_return(self):
        f = server.ChatGptStatusFetcher(threading.Event())
        f.note = "URLError"
        got = self.lines(f, {"status": "operational", "via": "group"},        # fine: nothing to say
                         {"status": "operational", "via": "names"},
                         {"status": "operational", "via": "names"},
                         {"status": "operational", "via": "group"}, tag="chatgpt status")
        self.assertEqual([lv for lv, _ in got], ["warning", "notice"])
        self.assertIn("fixed list of names", got[0][1])
        self.assertIn("(URLError)", got[0][1])
        self.assertIn("page's own group", got[1][1])

    def test_starting_on_the_names_is_a_warning(self):
        f = server.ChatGptStatusFetcher(threading.Event())
        got = self.lines(f, {"status": "operational", "via": "names"}, tag="chatgpt status")
        self.assertEqual([lv for lv, _ in got], ["warning"])


class ReliabilityRecord(unittest.TestCase):
    """How often a service has been degraded or down, and the promises the
    record makes: time not seen is not counted, and a file that will not parse
    is never treated as empty."""

    T0 = time.mktime((2026, 9, 29, 12, 0, 0, 0, 0, -1))      # noon, local
    DAY = "2026-09-29"

    def new(self, sub=""):
        import tempfile
        d = tempfile.mkdtemp()
        h = core.History(os.path.join(d, "history.json"))
        path = os.path.join(d, sub, server.AiHistory.FILE)
        return server.AiHistory(path, h), h, path

    @staticmethod
    def see(rec, t, status, parts=(), key="claude"):
        rec.observe(key, {"fetched_at": t, "status": status, "parts": list(parts)})

    def test_time_counts_towards_what_the_earlier_sighting_said(self):
        rec, _, _ = self.new()
        for dt, st in ((0, "operational"), (120, "operational"), (240, "degraded_performance"),
                       (360, "degraded_performance"), (480, "operational")):
            self.see(rec, self.T0 + dt, st)
        day = rec.services["claude"]["days"][self.DAY]
        self.assertEqual((day["ok"], day["deg"], day["out"]), (240.0, 240.0, 0.0))

    def test_an_outage_is_counted_apart_from_a_degradation(self):
        rec, _, _ = self.new()
        for dt, st in ((0, "operational"), (120, "partial_outage"), (240, "major_outage"),
                       (360, "operational")):
            self.see(rec, self.T0 + dt, st)
        day = rec.services["claude"]["days"][self.DAY]
        self.assertEqual((day["ok"], day["deg"], day["out"]), (120.0, 0.0, 240.0))

    def test_time_is_split_at_local_midnight(self):
        rec, _, _ = self.new()
        before = time.mktime((2026, 9, 29, 23, 59, 0, 0, 0, -1))
        self.see(rec, before, "operational")
        self.see(rec, before + 120, "operational")
        days = rec.services["claude"]["days"]
        self.assertEqual((days["2026-09-29"]["ok"], days["2026-09-30"]["ok"]), (60.0, 60.0))

    def test_a_silence_is_counted_as_nothing_and_flags_the_event_it_hides(self):
        rec, _, _ = self.new()
        self.see(rec, self.T0, "degraded_performance")            # first sighting: the start is unknown
        self.see(rec, self.T0 + 3600, "operational")               # an hour of silence
        self.assertEqual(rec.services["claude"]["days"], {})
        ev = rec.services["claude"]["events"][0]
        self.assertTrue(ev["approx_start"] and ev["approx_end"])

    def test_a_seen_start_and_end_are_not_flagged(self):
        rec, _, _ = self.new()
        for dt, st in ((0, "operational"), (120, "degraded_performance"), (240, "operational")):
            self.see(rec, self.T0 + dt, st)
        ev = rec.services["claude"]["events"][0]
        self.assertEqual((ev["start"], ev["end"]), (self.T0 + 120, self.T0 + 240))
        self.assertFalse(ev["approx_start"] or ev["approx_end"])

    def test_an_event_takes_the_worst_state_and_names_its_parts(self):
        rec, _, _ = self.new()
        self.see(rec, self.T0, "operational")
        self.see(rec, self.T0 + 120, "degraded_performance", ["Voice mode"])
        self.see(rec, self.T0 + 240, "major_outage", ["Login", "Voice mode"])
        self.see(rec, self.T0 + 360, "degraded_performance", ["Search"])
        ev = rec.services["claude"]["events"]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["worst"], ev[0]["end"]), ("major_outage", None))
        self.assertEqual(ev[0]["parts"], ["Voice mode", "Login", "Search"])

    def test_an_open_event_survives_a_restart_and_closes_afterwards(self):
        rec, h, path = self.new()
        self.see(rec, self.T0, "operational")
        self.see(rec, self.T0 + 120, "degraded_performance", ["Login"])
        again = server.AiHistory(path, h)                            # a deploy
        self.assertIsNone(again.services["claude"]["events"][0]["end"])
        self.see(again, self.T0 + 3600, "operational")
        ev = again.services["claude"]["events"][0]
        self.assertEqual(ev["end"], self.T0 + 3600)
        self.assertTrue(ev["approx_end"])                            # the deploy hid when

    def test_the_report_sums_the_windows_and_counts_incidents_by_kind(self):
        rec, _, _ = self.new()
        for dt, st in ((0, "operational"), (120, "degraded_performance"), (240, "operational"),
                       (360, "major_outage"), (480, "operational")):
            self.see(rec, self.T0 + dt, st)
        got = rec.report(now=self.T0 + 600, watching={"claude"})
        svc = got["services"]["claude"]
        self.assertEqual(len(svc["days"]), 90)
        self.assertEqual(svc["days"][-1]["date"], self.DAY)
        w = svc["windows"]["7"]
        self.assertEqual((w["ok"], w["deg"], w["out"], w["observed"]), (240.0, 120.0, 120.0, 480.0))
        self.assertEqual((w["deg_events"], w["out_events"]), (1, 1))
        self.assertEqual([i["worst"] for i in svc["incidents"]], ["major_outage", "degraded_performance"])
        self.assertTrue(got["available"] and svc["watching"])
        self.assertNotIn("chatgpt", got["services"])                # never seen, not watched

    def test_a_window_only_counts_its_own_days(self):
        rec, _, _ = self.new()
        old = self.T0 - 20 * 86400
        for dt, st in ((0, "operational"), (120, "degraded_performance"), (240, "operational")):
            self.see(rec, old + dt, st)
        svc = rec.report(now=self.T0)["services"]["claude"]
        self.assertEqual(svc["windows"]["7"]["deg_events"], 0)
        self.assertEqual(svc["windows"]["30"]["deg_events"], 1)
        self.assertEqual(svc["windows"]["7"]["observed"], 0.0)
        self.assertEqual(svc["windows"]["30"]["deg"], 120.0)

    def test_nothing_recorded_yet_says_so(self):
        rec, _, _ = self.new()
        got = rec.report(now=self.T0)
        self.assertFalse(got["available"])
        self.assertIn("Nothing recorded", got["error"])

    def test_a_file_that_will_not_parse_is_set_aside_not_treated_as_empty(self):
        rec, h, path = self.new()
        self.see(rec, self.T0, "degraded_performance")
        with open(path, "w") as f:
            f.write("{not json")
        again = server.AiHistory(path, h)
        self.assertEqual(again.services, {})
        asides = [n for n in os.listdir(os.path.dirname(path)) if ".damaged-" in n]
        self.assertEqual(len(asides), 1)
        with open(os.path.join(os.path.dirname(path), asides[0])) as f:
            self.assertEqual(f.read(), "{not json")               # the evidence is kept
        self.assertTrue(any("damaged" in n for n in h.notices))
        self.see(again, self.T0 + 5, "operational")
        self.assertEqual(json.load(open(path))["services"]["claude"]["last"]["status"], "operational")

    def test_a_record_of_the_wrong_shape_is_damaged_too(self):
        rec, h, path = self.new()
        with open(path, "w") as f:
            json.dump({"services": {"claude": {"days": 5}}}, f)
        again = server.AiHistory(path, h)
        self.assertEqual(again.services, {})
        self.assertTrue([n for n in os.listdir(os.path.dirname(path)) if ".damaged-" in n])

    def test_a_file_that_cannot_be_moved_is_never_written_over(self):
        rec, h, path = self.new()
        with open(path, "w") as f:
            f.write("{not json")
        h._set_aside = lambda p: None
        again = server.AiHistory(path, h)
        self.assertTrue(again.locked)
        self.see(again, self.T0, "degraded_performance")
        with open(path) as f:
            self.assertEqual(f.read(), "{not json")

    def test_a_save_that_fails_is_kept_where_the_banner_reads_it(self):
        rec, h, _ = self.new(sub="missing-directory")
        self.see(rec, self.T0, "degraded_performance")                # a change: saved at once
        self.assertIn(server.AiHistory.FILE, h._save_errors)

    def test_a_quiet_stretch_is_saved_now_and_then_not_every_poll(self):
        rec, h, path = self.new()
        self.see(rec, self.T0, "operational")
        self.assertTrue(os.path.exists(path))                         # the first sighting: when it began
        os.remove(path)
        self.see(rec, self.T0 + 120, "operational")
        self.assertFalse(os.path.exists(path))                        # nothing changed, and only just saved
        rec._saved = time.time() - rec.SAVE_EVERY - 1
        self.see(rec, self.T0 + 240, "operational")
        self.assertTrue(os.path.exists(path))                         # a quiet stretch, saved in due course

    def test_an_incident_on_every_chatgpt_component_keeps_every_name(self):
        rec, _, _ = self.new()
        names = ["C%02d" % i for i in range(15)]                       # ChatGPT's group has fifteen
        self.see(rec, self.T0, "operational")
        self.see(rec, self.T0 + 120, "degraded_performance", names)
        self.assertEqual(rec.services["claude"]["events"][0]["parts"], names)
        raw = {"components": [comp(n, "degraded_performance") for n in names]}
        self.assertEqual(len(server.ClaudeStatusFetcher.parse(raw)["parts"]), 15)

    def test_a_record_that_cannot_be_kept_is_not_the_status_page_failing(self):
        core.LOG.lines.clear()
        def boom(fresh): raise RuntimeError("disk")
        f = server.ClaudeStatusFetcher(threading.Event(), record=boom)
        f.after_success({"status": "operational"})                    # must not raise
        f.after_success({"status": "operational"})
        lines = [l for l in core.LOG.recent() if l["tag"] == "claude status"]
        self.assertEqual(len(lines), 1)                               # once, not every poll
        self.assertIn("could not record", lines[0]["text"])

    def test_a_reading_names_the_components_that_are_not_operational(self):
        raw = {"components": [comp("Login"), comp("Search", "degraded_performance"),
                              comp("Agent", "partial_outage")]}
        self.assertEqual(server.ClaudeStatusFetcher.parse(raw)["parts"], ["Agent", "Search"])
        self.assertEqual(server.GeminiStatusFetcher.parse([])["parts"], [])


class ServiceHistory(unittest.TestCase):
    """Filling the days before the live record from each service's own list of
    past incidents."""

    T0 = ReliabilityRecord.T0                      # noon on a day, local
    DAY = 86400

    def at(self, days_ago, hour):
        base = datetime.datetime.fromtimestamp(self.T0).replace(hour=0, minute=0, second=0, microsecond=0)
        return (base - datetime.timedelta(days=days_ago) + datetime.timedelta(hours=hour)).timestamp()

    @staticmethod
    def iso(t):
        return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123Z")

    def incident(self, t0, t1, impact="minor", comps=(("c1", "claude.ai"),), worst=None, name="x"):
        updates = [{"affected_components": [{"code": c, "name": n, "old_status": "operational", "new_status": worst}
                                            for c, n in comps]}] if worst else []
        return {"id": name, "name": name, "impact": impact, "started_at": self.iso(t0),
                "created_at": self.iso(t0), "resolved_at": None if t1 is None else self.iso(t1),
                "components": [{"id": c, "name": n} for c, n in comps], "incident_updates": updates}

    # ── the parsers ──
    def test_a_time_with_a_z_and_milliseconds_is_read(self):
        self.assertEqual(server._iso_epoch("2026-09-29T17:28:07.123Z"), server._iso_epoch("2026-09-29T17:28:07+00:00"))
        self.assertEqual(server._iso_epoch("2026-09-29T12:28:07-05:00"), server._iso_epoch("2026-09-29T17:28:07Z"))
        self.assertIsNone(server._iso_epoch("soon"))
        self.assertIsNone(server._iso_epoch(None))

    def test_statuspage_incidents_become_events_at_their_worst_state(self):
        raw = {"incidents": [
            self.incident(self.at(9, 10), self.at(9, 11.5), impact="minor", worst="partial_outage", name="a"),
            self.incident(self.at(7, 10), self.at(7, 12), impact="minor", name="b"),            # no component states: the impact
            self.incident(self.at(5, 10), self.at(5, 11), impact="none", name="notice"),          # a notice, not a fault
            self.incident(self.at(2, 10), None, impact="major", name="open"),                    # the live record's
            self.incident(self.at(20, 10), self.at(20, 11), impact="critical", name="old", worst="major_outage")]}
        events, covered = server.ClaudeStatusFetcher.parse_history(raw)
        self.assertEqual([(round(e["start"]), e["worst"]) for e in events],
                         [(round(self.at(9, 10)), "partial_outage"), (round(self.at(7, 10)), "degraded_performance"),
                          (round(self.at(20, 10)), "major_outage")])
        self.assertEqual(events[0]["parts"], ["claude.ai"])
        self.assertEqual(round(covered), round(self.at(20, 10)))      # the oldest incident, skipped or not

    def test_claude_counts_every_incident_on_its_page(self):
        raw = {"incidents": [dict(self.incident(self.at(3, 1), self.at(3, 2), name="a"), components=[])]}
        self.assertEqual(len(server.ClaudeStatusFetcher.parse_history(raw)[0]), 1)

    # ── ChatGPT's history comes from the page's own component impacts ──
    PARTS = {"chat-conv": "Conversations", "chat-login": "Login"}

    def imp(self, cid, h0, h1, status="degraded_performance", inc="inc1"):
        stamp = lambda t: self.iso(t).replace(".123Z", ".000Z")
        return {"component_id": cid, "end_at": "$undefined" if h1 is None else stamp(self.at(*h1)),
                "id": "i-%s-%s" % (cid, h0[1]), "start_at": stamp(self.at(*h0)), "status": status,
                "status_page_incident_id": inc}

    def page(self, impacts, split=None, uptimes=True, window=90):
        """A status page shaped like OpenAI's: the data in JSON strings inside
        script tags, an incident's own impacts early on, and the page-level
        list beside the uptime figures."""
        head = ('{"history_window_days":%s,"incident":{"component_impacts":[%s]},' % (
            window, json.dumps(self.imp("chat-conv", (0, 5), None, inc="ongoing"))) if window else '{"incident":{},')
        flight = head + '"component_impacts":' + json.dumps(impacts) + (
            ',"component_uptimes":[{"component_id":"chat-conv","uptime":"100.00"}]}' if uptimes else "}")
        cut = split if split is not None else len(flight)
        chunks = [flight[:cut]] + ([flight[cut:]] if cut < len(flight) else [])
        return "<html>" + "".join("<script>self.__next_f.push([1,%s])</script>" % json.dumps(c) for c in chunks) + "</html>"

    def events(self, impacts, **kw):
        return server.ChatGptStatusFetcher.parse_page_history(self.page(impacts, **kw), self.PARTS, now=self.T0)

    def test_chatgpt_history_is_the_impacts_on_its_own_components_joined_per_incident(self):
        impacts = [self.imp("chat-conv", (9, 10), (9, 10.5)),
                   self.imp("chat-login", (9, 10.3), (9, 10.8), "partial_outage"),     # overlaps: one event
                   self.imp("api-rt", (9, 10), (9, 12), "full_outage", "inc2"),        # the API's, not ChatGPT's
                   self.imp("chat-conv", (9, 14), (9, 14.2))]                           # same incident, later: its own
        events, _ = self.events(impacts)
        self.assertEqual([(round(e["start"]), round(e["end"]), e["worst"], e["parts"]) for e in events],
                         [(round(self.at(9, 10)), round(self.at(9, 10.8)), "partial_outage", ["Conversations", "Login"]),
                          (round(self.at(9, 14)), round(self.at(9, 14.2)), "degraded_performance", ["Conversations"])])

    def test_a_value_split_across_two_script_tags_is_read_whole(self):
        impacts = [self.imp("chat-conv", (9, 10), (9, 11)), self.imp("chat-login", (8, 1), (8, 2), "partial_outage", "inc3")]
        whole, _ = self.events(impacts)
        for cut in (60, 200, 333):
            split, _ = self.events(impacts, split=cut)
            self.assertEqual(split, whole)
        self.assertEqual(len(whole), 2)

    def test_an_impact_still_going_lasts_until_now_and_a_full_outage_is_the_worst_state(self):
        events, _ = self.events([self.imp("chat-conv", (0, 3), None, "full_outage")])
        self.assertEqual((events[0]["worst"], events[0]["end"]), ("major_outage", self.T0))

    def test_the_record_is_complete_from_its_oldest_impact_and_no_further_back_than_the_window(self):
        recent = [self.imp("chat-conv", (9, 10), (9, 11))]
        _, covered = self.events(recent)                                  # nothing older: complete from there
        self.assertEqual(round(covered), round(self.at(9, 10)))
        old = [self.imp("chat-conv", (100, 10), (100, 11)), self.imp("chat-conv", (9, 10), (9, 11), inc="inc2")]
        self.assertEqual(self.events(old)[1], self.T0 - 90 * 86400)       # ninety days, the default window
        self.assertEqual(self.events(old, window=30)[1], self.T0 - 30 * 86400)
        self.assertEqual(self.events(old, window=0)[1], self.T0 - 90 * 86400)   # a page that names no window
        # An impact on some other component counts towards how far back it reaches: it is the same list.
        other = recent + [self.imp("api-rt", (40, 1), (40, 2), inc="inc9")]
        self.assertEqual(round(self.events(other)[1]), round(self.at(40, 1)))

    def test_a_page_that_does_not_hold_the_history_is_refused_not_read_as_clean(self):
        good = [self.imp("chat-conv", (9, 10), (9, 11))]
        for html in ("<html>no data here</html>", self.page(good, uptimes=False), self.page([]),
                     self.page(good).replace('component_uptimes', 'component_uptimez')):
            with self.assertRaises(ValueError):
                server.ChatGptStatusFetcher.parse_page_history(html, self.PARTS, now=self.T0)

    def test_the_impacts_of_one_incident_do_not_stand_in_for_the_whole_record(self):
        # The page-level list is the one beside the uptime figures. An incident's
        # own list, earlier in the data, is ignored.
        events, _ = self.events([self.imp("chat-conv", (9, 10), (9, 11))])
        self.assertEqual(len(events), 1)
        self.assertNotEqual(round(events[0]["start"]), round(self.at(0, 5)))

    def test_chatgpt_history_needs_the_pages_own_layout_to_say_which_components_are_its_own(self):
        f = server.ChatGptStatusFetcher(threading.Event())
        f.get = lambda url: {"summary": {"structure": {"items": []}}}
        f.get_text = lambda url: self.page([self.imp("chat-conv", (9, 10), (9, 11))])
        with self.assertRaises(ValueError):
            f.history()
        layout = {"summary": {"structure": {"items": [{"group": {"name": "ChatGPT", "components": [
            {"component_id": "chat-conv", "name": "Conversations"}]}}]}}}
        f.get = lambda url: layout
        events, _ = f.history()
        self.assertEqual([e["parts"] for e in events], [["Conversations"]])

    def test_an_empty_or_undated_history_is_refused(self):
        for raw in ({}, {"incidents": []}, {"incidents": [{"name": "x"}]}):
            with self.assertRaises(ValueError):
                server.ClaudeStatusFetcher.parse_history(raw)
        for raw in ([], {"error": 1}):
            with self.assertRaises(ValueError):
                server.GeminiStatusFetcher.parse_history(raw)

    def google(self, t0, t1, product="npdyhgECDJ6tB66MxXyo", status="AVAILABLE", impact="SERVICE_INFORMATION"):
        stamp = lambda t: datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        return {"id": "i", "begin": stamp(t0), "end": None if t1 is None else stamp(t1),
                "affected_products": [{"title": "t", "id": product}],
                "most_recent_update": {"status": status}, "status_impact": impact}

    def test_gemini_history_is_its_own_finished_incidents_across_the_whole_feed(self):
        raw = [self.google(self.at(3, 1), self.at(3, 2), impact="SERVICE_OUTAGE"),
               self.google(self.at(4, 1), self.at(4, 3), impact="SERVICE_DISRUPTION"),
               self.google(self.at(5, 1), self.at(5, 2), product="gmail"),                       # another product
               self.google(self.at(1, 1), None, status="SERVICE_DISRUPTION"),                    # still open: the live record's
               self.google(self.at(40, 1), self.at(40, 2), product="gmail")]                     # sets how far back the feed reaches
        events, covered = server.GeminiStatusFetcher.parse_history(raw)
        self.assertEqual(sorted(e["worst"] for e in events), ["degraded_performance", "major_outage"])
        self.assertEqual(round(covered), round(self.at(40, 1)))

    # ── filling the record ──
    def record(self):
        rec, h, path = ReliabilityRecord().new()
        rec.observe("claude", {"fetched_at": self.T0, "status": "operational", "parts": []})    # the live record begins
        return rec, h, path

    def ev(self, d0, h0, d1, h1, worst="degraded_performance"):
        return {"start": self.at(d0, h0), "end": self.at(d1, h1), "worst": worst, "parts": []}

    def day(self, rec, days_ago):
        return rec.services["claude"]["days"].get(
            datetime.datetime.fromtimestamp(self.at(days_ago, 12)).strftime("%Y-%m-%d"))

    def test_the_days_before_the_live_record_are_filled_and_the_live_days_are_not(self):
        rec, _, _ = self.record()
        live = dict(rec.services["claude"]["days"])
        self.assertTrue(rec.backfill("claude", [self.ev(9, 10, 9, 11.5, "major_outage")], self.at(12, 3)))
        self.assertEqual(self.day(rec, 9)["out"], 5400.0)
        self.assertEqual(self.day(rec, 9)["ok"] + 5400.0, server.AiHistory._day_seconds(
            datetime.datetime.fromtimestamp(self.at(9, 12)).strftime("%Y-%m-%d")))
        self.assertEqual(self.day(rec, 8), {"ok": server.AiHistory._day_seconds(
            datetime.datetime.fromtimestamp(self.at(8, 12)).strftime("%Y-%m-%d")), "deg": 0.0, "out": 0.0})
        self.assertIsNone(self.day(rec, 13))                                # before the list reaches
        self.assertEqual({k: v for k, v in rec.services["claude"]["days"].items() if k in live}, live)  # today untouched
        today = datetime.datetime.fromtimestamp(self.T0).strftime("%Y-%m-%d")
        self.assertNotIn(today, {k for k in rec.services["claude"]["days"] if k not in live})   # nothing written on or after it

    def test_doing_it_twice_changes_nothing(self):
        rec, _, _ = self.record()
        rec.backfill("claude", [self.ev(9, 10, 9, 12)], self.at(12, 3))
        once = json.dumps(rec.services["claude"]["days"], sort_keys=True), len(rec.services["claude"]["events"])
        rec.backfill("claude", [self.ev(9, 10, 9, 12)], self.at(12, 3))
        self.assertEqual((json.dumps(rec.services["claude"]["days"], sort_keys=True),
                          len(rec.services["claude"]["events"])), once)

    def test_overlapping_incidents_count_once_and_the_worse_state_wins(self):
        rec, _, _ = self.record()
        rec.backfill("claude", [self.ev(9, 10, 9, 12), self.ev(9, 11, 9, 13, "major_outage")], self.at(12, 3))
        self.assertEqual((self.day(rec, 9)["deg"], self.day(rec, 9)["out"]), (3600.0, 7200.0))

    def test_an_incident_over_midnight_is_split_between_the_days(self):
        rec, _, _ = self.record()
        rec.backfill("claude", [self.ev(5, 23, 4, 1, "partial_outage")], self.at(12, 3))
        self.assertEqual((self.day(rec, 5)["out"], self.day(rec, 4)["out"]), (3600.0, 3600.0))

    def test_an_incident_running_into_the_live_days_is_cut_at_them_and_left_to_them(self):
        rec, _, _ = self.record()
        straddling = {"start": self.at(1, 23), "end": self.T0 + 3 * 3600, "worst": "degraded_performance", "parts": []}
        rec.backfill("claude", [straddling], self.at(5, 3))
        self.assertEqual(self.day(rec, 1)["deg"], 3600.0)                    # its part before today
        self.assertEqual(rec.services["claude"]["events"], [])               # not listed: today's record has it
        self.assertEqual(rec.services["claude"]["backfill"]["events"], 0)

    def test_an_incident_early_on_the_first_live_day_is_not_listed_without_being_counted(self):
        # History covers whole days. An incident on the first live day, before
        # the live record began, would otherwise be listed while its time was
        # in no day's total.
        rec, _, _ = self.record()
        early = {"start": self.at(0, 3), "end": self.at(0, 4), "worst": "major_outage", "parts": []}
        rec.backfill("claude", [early], self.at(5, 3))
        self.assertEqual(rec.services["claude"]["events"], [])
        listed = sum(1 for e in rec.services["claude"]["events"])
        counted = sum(d["out"] for d in rec.services["claude"]["days"].values())
        self.assertEqual((listed, counted), (0, 0.0))

    def test_past_events_are_listed_apart_from_the_live_ones_and_the_open_one_stays_last(self):
        rec, _, _ = self.record()
        rec.observe("claude", {"fetched_at": self.T0 + 120, "status": "degraded_performance", "parts": ["Login"]})
        rec.backfill("claude", [self.ev(9, 10, 9, 12), self.ev(20, 1, 20, 2)], self.at(25, 3))
        ev = rec.services["claude"]["events"]
        self.assertEqual([e.get("src") for e in ev], ["history", "history", None])
        self.assertEqual(ev[-1]["end"], None)
        self.assertLess(ev[0]["start"], ev[1]["start"])
        rec.observe("claude", {"fetched_at": self.T0 + 240, "status": "operational", "parts": []})   # and it can still close
        self.assertEqual(rec.services["claude"]["events"][-1]["end"], self.T0 + 240)

    def test_nothing_is_written_until_the_live_record_has_begun(self):
        rec, _, path = ReliabilityRecord().new()
        self.assertFalse(rec.backfill("claude", [self.ev(9, 10, 9, 12)], self.at(12, 3)))
        self.assertEqual(rec.services, {})
        self.assertFalse(os.path.exists(path))

    def test_a_history_filled_the_old_way_is_filled_again_once(self):
        # Filled when only six names were kept: done again, so an incident's
        # whole list replaces the first six. Then left alone.
        rec, h, path = self.record()
        rec.backfill("claude", [self.ev(9, 10, 9, 12)], self.at(12, 3))
        del rec.services["claude"]["backfill"]["keeps"]
        rec._save(True)
        again = server.AiHistory(path, h)
        self.assertFalse(again.backfilled("claude"))
        wide = dict(self.ev(9, 10, 9, 12), parts=["C%02d" % i for i in range(15)])
        again.backfill("claude", [wide], self.at(12, 3))
        self.assertTrue(again.backfilled("claude"))
        self.assertEqual([len(e["parts"]) for e in again.services["claude"]["events"] if e.get("src") == "history"], [15])
        self.assertTrue(server.AiHistory(path, h).backfilled("claude"))

    def test_it_is_remembered_across_a_restart_and_reported(self):
        rec, h, path = self.record()
        self.assertFalse(rec.backfilled("claude"))
        rec.backfill("claude", [self.ev(9, 10, 9, 12)], self.at(12, 3))
        again = server.AiHistory(path, h)
        self.assertTrue(again.backfilled("claude"))
        got = again.report(now=self.T0, watching={"claude"})
        first = datetime.datetime.fromtimestamp(self.at(12, 3)).date()
        self.assertEqual(got["covered_from"], first.isoformat())
        self.assertEqual(got["watched_days"], (datetime.datetime.fromtimestamp(self.T0).date() - first).days + 1)
        self.assertEqual(got["services"]["claude"]["backfill"]["events"], 1)

    def test_the_background_pass_fills_each_service_once_and_says_so(self):
        rec, _, _ = self.record()
        calls = []
        class F:
            def history(self_):
                calls.append(1)
                return [self.ev(9, 10, 9, 12)], self.at(12, 3)
        d = server.Dashboard.__new__(server.Dashboard)
        d.stop, d.ai, d.ai_history = threading.Event(), {"claude": F()}, rec
        passes = []
        d.stop.wait = lambda n: (passes.append(n), d.stop.set() if len(passes) > 1 else None)
        core.LOG.lines.clear()
        d._backfill_ai()
        self.assertEqual(len(calls), 1)                                       # not asked again once it has it
        self.assertTrue(rec.backfilled("claude"))
        lines = [l["text"] for l in core.LOG.recent() if l["tag"] == "ai status"]
        self.assertEqual(len(lines), 1)
        self.assertIn("Claude history: 1 incident, back to", lines[0])

    def test_a_history_that_cannot_be_read_is_said_once_and_retried_later_not_recorded_as_clean(self):
        rec, _, _ = self.record()
        class F:
            def history(self_): raise ValueError("its incidents do not say which components they touched")
        d = server.Dashboard.__new__(server.Dashboard)
        d.stop, d.ai, d.ai_history = threading.Event(), {"claude": F()}, rec
        passes = []
        d.stop.wait = lambda n: (passes.append(n), d.stop.set() if len(passes) > 2 else None)
        core.LOG.lines.clear()
        d._backfill_ai()
        self.assertFalse(rec.backfilled("claude"))
        self.assertLessEqual(set(rec.services["claude"]["days"]),        # nothing invented for the days before
                             {datetime.datetime.fromtimestamp(self.T0).strftime("%Y-%m-%d")})
        lines = [(l["level"], l["text"]) for l in core.LOG.recent() if l["tag"] == "ai status"]
        self.assertEqual(len(lines), 1)                                       # once, over three passes
        self.assertEqual(lines[0][0], "warning")
        self.assertIn("do not say which components", lines[0][1])

    def test_a_service_that_is_switched_off_is_not_asked(self):
        rec, _, _ = self.record()
        d = server.Dashboard.__new__(server.Dashboard)
        d.stop, d.ai, d.ai_history = threading.Event(), {}, rec
        d.stop.wait = lambda n: d.stop.set()
        d._backfill_ai()
        self.assertFalse(rec.backfilled("claude"))


class SyslogSettings(unittest.TestCase):
    def cfg(self):
        import tempfile
        return server.Config(os.path.join(tempfile.mkdtemp(), "c.json"), {})

    def test_off_by_default_pointing_at_the_nas(self):
        self.assertEqual(self.cfg().syslog, {"enabled": False, "host": "127.0.0.1", "port": 514, "proto": "udp", "obs": False})

    def test_a_good_block_is_saved_and_survives_a_reload(self):
        c = self.cfg()
        changed, err = c.apply({"syslog": {"enabled": True, "host": "10.0.0.101", "port": "514", "proto": "udp", "obs": True}})
        self.assertEqual((changed, err), (["syslog"], ""))
        again = server.Config(c.path, {})
        self.assertEqual(again.syslog["host"], "10.0.0.101")
        self.assertTrue(again.syslog["enabled"] and again.syslog["obs"])

    def test_nonsense_is_refused_with_a_reason(self):
        c = self.cfg()
        self.assertIn("port", c.apply({"syslog": {"port": "lots"}})[1])
        self.assertIn("port", c.apply({"syslog": {"port": 70000}})[1])
        self.assertIn("protocol", c.apply({"syslog": {"proto": "carrier pigeon"}})[1])
        self.assertIn("host", c.apply({"syslog": {"host": "two words"}})[1])
        self.assertEqual(c.syslog["port"], 514)

    def test_nothing_secret_is_in_the_public_view(self):
        c = self.cfg()
        self.assertIn("syslog", c.public())
        self.assertNotIn("token", c.public())


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
