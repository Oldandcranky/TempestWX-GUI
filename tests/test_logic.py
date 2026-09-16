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


if __name__ == "__main__":
    unittest.main(verbosity=2)
