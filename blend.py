"""
Blends Open-Meteo and NWS hourly forecasts into one small JSON file that
LaraPaper's weather recipe polls instead of hitting either API directly.

Why this exists:
  - NWS's hourly forecast returns ~156 hours of data with no way to ask
    for fewer. LaraPaper's Liquid renderer rejects a payload that large
    ("Context too large for external liquid renderer").
  - Open-Meteo alone works fine size-wise, but doesn't have a human
    forecaster's local corrections the way NWS does.
  - This script fetches both, averages temperature/rain/wind hour-by-hour
    for the hours they overlap, and writes ONE small file. If NWS is
    unreachable, it falls back to Open-Meteo alone. If both fail, it
    keeps serving the last good file rather than going blank.

Output shape matches Open-Meteo's own "hourly" block, so the Liquid
template written against Open-Meteo's field names works unchanged.
"""

import json
import os
import time
import threading
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

# ---- Configuration -------------------------------------------------------
LAT, LON = 47.6163, -122.0356          # Sammamish, WA
UA = "trmnl-larapaper (sanjeevd.junk@gmail.com)"  # NWS requires a contact UA
OUT_DIR = "/data"
INTERVAL = 1800                         # refresh every 30 minutes on success
RETRY_INTERVAL = 300                    # retry sooner after a failure
HOURS = 7                               # hours of forecast to keep
OUT = os.path.join(OUT_DIR, "weather.json")

# NWS grid for the coordinates above (SEW office). Re-derive with:
#   curl -s -A "<UA>" https://api.weather.gov/points/<lat>,<lon>
NWS_URL = "https://api.weather.gov/gridpoints/SEW/134,67/forecast/hourly"

OM_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&hourly=temperature_2m,apparent_temperature,precipitation_probability,"
    "weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m,is_day"
    "&forecast_hours={h}&temperature_unit=fahrenheit&wind_speed_unit=mph"
    "&timezone=America%2FLos_Angeles"
).format(lat=LAT, lon=LON, h=HOURS)


def get(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/geo+json, application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def blend():
    om = get(OM_URL)["hourly"]

    nws = {}
    try:
        for p in get(NWS_URL)["properties"]["periods"]:
            # NWS startTime: "2026-11-15T10:00:00-08:00"
            # Open-Meteo time: "2026-11-15T10:00"  -- match on the first 16 chars
            nws[p["startTime"][:16]] = p
    except Exception as e:
        print("NWS fetch failed, using Open-Meteo only:", e, flush=True)

    used = 0
    for i, t in enumerate(om["time"]):
        p = nws.get(t)
        if not p:
            continue
        used += 1

        om_t = om["temperature_2m"][i]
        avg_t = (om_t + p["temperature"]) / 2
        om["temperature_2m"][i] = avg_t
        # NWS has no feels-like; shift Open-Meteo's by the same delta we
        # just applied to the actual temperature, rather than losing it.
        om["apparent_temperature"][i] += avg_t - om_t

        pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
        if pop is not None and om["precipitation_probability"][i] is not None:
            om["precipitation_probability"][i] = round(
                (om["precipitation_probability"][i] + pop) / 2
            )

        try:
            ws = float(p["windSpeed"].split()[0])
            om["wind_speed_10m"][i] = (om["wind_speed_10m"][i] + ws) / 2
        except (ValueError, KeyError, IndexError):
            pass
        # Gusts, wind direction, and the weather-code-derived icon are left
        # as Open-Meteo's values -- NWS doesn't provide them in a usable form.

    return {
        "hourly": om,
        "nws_hours_blended": used,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def loop():
    while True:
        try:
            data = blend()
            tmp = OUT + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, OUT)  # atomic swap -- LaraPaper never reads a half-written file
            print("updated, NWS hours blended:", data["nws_hours_blended"], flush=True)
            time.sleep(INTERVAL)
        except Exception as e:
            print("update failed, keeping previous file:", e, flush=True)
            time.sleep(RETRY_INTERVAL)


os.makedirs(OUT_DIR, exist_ok=True)
threading.Thread(target=loop, daemon=True).start()
ThreadingHTTPServer(
    ("0.0.0.0", 8081), partial(SimpleHTTPRequestHandler, directory=OUT_DIR)
).serve_forever()
