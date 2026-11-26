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

# ---- Clothing recommendation (window-based) -------------------------------
WINDOW_START, WINDOW_END = 9, 17        # 9am-5pm; hours 9..16 inclusive
RAIN_PROB_MIN = 50                      # % -- max probability in window
RAIN_TOTAL_MM = 1.0                     # total precip in window ...
RAIN_HOURLY_MM = 0.5                    # ... or any single hour at/above this
COLD_CAP_BELOW = 40                     # < 40F            -> Jacket + Cap
THICK_BELOW = 53                        # 40-53F           -> Thick Jacket
ENJOY_ABOVE = 70                        # all hrs > 70F    -> Enjoy; else fleece

REC_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&hourly=apparent_temperature,temperature_2m,precipitation_probability,precipitation"
    "&forecast_days=2&temperature_unit=fahrenheit&timezone=America%2FLos_Angeles"
).format(lat=LAT, lon=LON)


def get(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/geo+json, application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_nws():
    nws = {}
    try:
        for p in get(NWS_URL)["properties"]["periods"]:
            # NWS startTime: "2026-11-15T10:00:00-08:00"
            # Open-Meteo time: "2026-11-15T10:00"  -- match on the first 16 chars
            nws[p["startTime"][:16]] = p
    except Exception as e:
        print("NWS fetch failed, using Open-Meteo only:", e, flush=True)
    return nws


def fmt_hour(t):
    h = int(t[11:13])
    return "%d%s" % (h % 12 or 12, "am" if h < 12 else "pm")


def intensity(peak_mm_hr):
    """Standard rain-rate bands, judged on the wettest hour."""
    if peak_mm_hr < 2.5:
        return "light"
    if peak_mm_hr < 7.6:
        return "medium"
    return "heavy"


def amount_str(total_mm, peak_mm_hr):
    inches = total_mm / 25.4
    if inches < 0.005:
        return ""
    return "%.2f in, %s" % (inches, intensity(peak_mm_hr))


def recommend(nws):
    """Worst-case clothing call for the 9am-5pm window (today, or tomorrow
    once today's window is over). Rain beats temperature."""
    d = get(REC_URL)
    om = d["hourly"]
    local_now = time.gmtime(time.time() + d.get("utc_offset_seconds", -28800))
    today = time.strftime("%Y-%m-%d", local_now)
    tomorrow = om["time"][-1][:10]
    day, label = (today, "Today") if local_now.tm_hour < WINDOW_END else (tomorrow, "Tomorrow")

    rows = []
    for i, t in enumerate(om["time"]):
        if t[:10] != day or not (WINDOW_START <= int(t[11:13]) < WINDOW_END):
            continue
        feels = om["apparent_temperature"][i]
        pop = om["precipitation_probability"][i] or 0
        p = nws.get(t)
        if p:
            avg_t = (om["temperature_2m"][i] + p["temperature"]) / 2
            feels += avg_t - om["temperature_2m"][i]
            npop = (p.get("probabilityOfPrecipitation") or {}).get("value")
            if npop is not None:
                pop = round((pop + npop) / 2)
        rows.append((t, feels, pop, om["precipitation"][i] or 0.0))
    if not rows:
        return None

    low_t, low, _, _ = min(rows, key=lambda r: r[1])
    max_pop = max(r[2] for r in rows)
    total_mm = sum(r[3] for r in rows)
    max_hr_mm = max(r[3] for r in rows)
    wet_t = max(rows, key=lambda r: (r[2], r[3]))[0]

    rainy = max_pop >= RAIN_PROB_MIN and (total_mm >= RAIN_TOTAL_MM or max_hr_mm >= RAIN_HOURLY_MM)
    if rainy:
        text = "Rain Jacket"
        why = "Rain %d%% around %s - %s" % (max_pop, fmt_hour(wet_t), amount_str(total_mm, max_hr_mm))
    else:
        if low < COLD_CAP_BELOW:
            text = "Jacket + Cap"
        elif low < THICK_BELOW:
            text = "Thick Jacket"
        elif low <= ENJOY_ABOVE:
            text = "Thin Fleece"
        else:
            text = "Enjoy the day!"
        amt = amount_str(total_mm, max_hr_mm)
        why = "Low %d\u00b0 at %s - rain %d%%%s" % (round(low), fmt_hour(low_t), max_pop, ", " + amt if amt else "")
    return {"text": text, "why": why, "when": label + " 9am-5pm"}


def blend():
    om = get(OM_URL)["hourly"]
    nws = fetch_nws()

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

    try:
        rec = recommend(nws)
    except Exception as e:
        print("recommendation failed:", e, flush=True)
        rec = None

    return {
        "hourly": om,
        "recommendation": rec,
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
