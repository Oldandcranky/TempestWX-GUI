#!/usr/bin/env python3
"""Fetch NOAA's 1991-2020 climate normals for a station, and print them the
way docker-compose.yml wants them.

    python3 tools/normals.py 42.1681 -88.4281          # nearest station
    python3 tools/normals.py 42.1681 -88.4281 --list   # the nearest twelve
    python3 tools/normals.py --station USC00112048     # a particular one

The three lines this prints are what the Rainfall gauge, the Temperature
comparisons and the outlook's "normal" band are measured against. NCEI's
Access Data Service is keyless. Standard library only, like the rest.
"""

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request

SEARCH = "https://www.ncei.noaa.gov/access/services/search/v1/data"
DATA = "https://www.ncei.noaa.gov/access/services/data/v1"
DATASET = "normals-monthly-1991-2020"
TYPES = ("MLY-TMAX-NORMAL", "MLY-TMIN-NORMAL", "MLY-PRCP-NORMAL")
UA = {"User-Agent": "tempest-dashboard normals tool (github.com/Oldandcranky/TempestWX-GUI)"}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 3958.8 * math.asin(math.sqrt(a))


def nearby(lat, lon, span=0.45):
    """Stations with all three normals inside a box about the point, nearest
    first. The search service takes a bounding box, not a radius."""
    q = urllib.parse.urlencode({
        "dataset": DATASET,
        "bbox": "%.2f,%.2f,%.2f,%.2f" % (lat + span, lon - span, lat - span, lon + span),
        "dataTypes": ",".join(TYPES), "limit": 100})
    out = []
    for r in get(SEARCH + "?" + q).get("results", []):
        coords = (r.get("location") or {}).get("coordinates") or [None, None]
        st = (r.get("stations") or [{}])[0]
        have = {t.get("id") for t in r.get("dataTypes", [])}
        if coords[0] is None or not st.get("id") or not set(TYPES) <= have:
            continue
        out.append((miles(lat, lon, coords[1], coords[0]), st["id"], st.get("name") or st["id"]))
    return sorted(out)


def normals(station):
    """Twelve highs, twelve lows (°F) and twelve rainfalls (inches)."""
    q = urllib.parse.urlencode({"dataset": DATASET, "stations": station,
                                "dataTypes": ",".join(TYPES), "format": "json",
                                "units": "standard"})
    return reshape(get(DATA + "?" + q))


def reshape(rows):
    hi, lo, pr = [None] * 12, [None] * 12, [None] * 12
    for r in rows:
        m = int(r["DATE"]) - 1
        hi[m] = float(r["MLY-TMAX-NORMAL"])
        lo[m] = float(r["MLY-TMIN-NORMAL"])
        pr[m] = float(r["MLY-PRCP-NORMAL"])
    if None in hi or None in lo or None in pr:
        missing = [i + 1 for i in range(12) if None in (hi[i], lo[i], pr[i])]
        raise ValueError("no normals for month(s) %s" % missing)
    return hi, lo, pr


def compose_lines(hi, lo, pr):
    return ('      TEMPEST_TEMP_NORMAL_HIGH: "%s"' % ",".join("%.1f" % v for v in hi),
            '      TEMPEST_TEMP_NORMAL_LOW: "%s"' % ",".join("%.1f" % v for v in lo),
            '      TEMPEST_RAIN_MONTHLY: "%s"' % ",".join("%.2f" % v for v in pr))


def main(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("lat", nargs="?", type=float)
    p.add_argument("lon", nargs="?", type=float)
    p.add_argument("--station", help="a NOAA station id, e.g. USC00112048")
    p.add_argument("--list", action="store_true", help="list the nearest stations and stop")
    a = p.parse_args(argv)
    if a.station:
        station, name, dist = a.station, a.station, None
    elif a.lat is not None and a.lon is not None:
        found = nearby(a.lat, a.lon)
        if not found:
            sys.exit("no station with all three normals within about 30 miles")
        if a.list:
            for d, sid, name in found[:12]:
                print("  %5.1f mi  %-12s %s" % (d, sid, name))
            return
        dist, station, name = found[0]
    else:
        p.error("give lat lon, or --station")
    hi, lo, pr = normals(station)
    print("# NOAA 1991-2020 normals, %s (%s%s)" % (
        name, station, "" if dist is None else ", %.1f mi away" % dist))
    print("# annual rainfall %.2f in" % sum(pr))
    for line in compose_lines(hi, lo, pr):
        print(line)


if __name__ == "__main__":
    main(sys.argv[1:])
