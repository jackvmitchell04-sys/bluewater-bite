"""Audit: search NOAA ERDDAP servers for candidate datasets and report how fresh each one is,
then test every input the app actually uses. Run from GitHub Actions (browsers/sandboxes can't reach these)."""
import json, sys, time, datetime as dt, requests
SERVERS = ["https://coastwatch.noaa.gov/erddap", "https://coastwatch.pfeg.noaa.gov/erddap", "https://polarwatch.noaa.gov/erddap"]
SEARCHES = ["chlorophyll VIIRS daily", "chlorophyll OLCI daily", "chlorophyll multi sensor daily", "kd490 daily",
            "ACSPO L3S LEO daily SST", "sea surface temperature daily 0.02", "MUR SST", "sea surface height", "OSTIA", "geo-polar blended"]
now = dt.datetime.utcnow()
def info_end(server, ds):
    try:
        r = requests.get(f"{server}/info/{ds}/index.json", timeout=60).json()
        rows = r["table"]["rows"]
        end = next((x[4] for x in rows if x[1] == "NC_GLOBAL" and x[2] == "time_coverage_end"), None)
        res = next((x[4] for x in rows if x[1] == "NC_GLOBAL" and x[2] in ("spatial_resolution", "geospatial_lat_resolution")), "")
        return end, res
    except Exception as e:
        return "ERR " + str(e)[:40], ""
seen = set()
for s in SERVERS:
    for q in SEARCHES:
        try:
            r = requests.get(f"{s}/search/index.json", params={"searchFor": q, "page": 1, "itemsPerPage": 25}, timeout=60)
            rows = r.json()["table"]["rows"]; cols = r.json()["table"]["columnNames"]
        except Exception as e:
            print(f"[{s.split('/')[2]}] search '{q}' failed: {str(e)[:60]}"); continue
        for row in rows:
            d = dict(zip(cols, row)); ds = d["Dataset ID"]
            if (s, ds) in seen or not d.get("griddap"):
                continue
            seen.add((s, ds))
            t = d["Title"]
            if not any(k in t.lower() for k in ("daily", "day", "1-day", "hourly", "near real")):
                continue
            end, res = info_end(s, ds)
            try:
                age = (now - dt.datetime.fromisoformat(end.replace("Z", ""))).total_seconds() / 86400
                agestr = f"{age:5.1f} d"
            except Exception:
                agestr = "  ?  "
            print(f"AGE {agestr} | {s.split('/')[2]:28s} | {ds:48s} | {res:12s} | {t[:110]}")
            time.sleep(0.2)
