"""Which regional/sector datasets cover the Bay of Plenty box, and what variables do the new candidates have."""
import requests, time
BOX = (-38.05, -35.85, 175.15, 178.25)
S = ["https://coastwatch.noaa.gov/erddap", "https://polarwatch.noaa.gov/erddap", "https://coastwatch.pfeg.noaa.gov/erddap"]
def meta(s, ds):
    r = requests.get(f"{s}/info/{ds}/index.json", timeout=60).json()["table"]["rows"]
    g = {x[2]: x[4] for x in r if x[1] == "NC_GLOBAL"}
    vars_ = [x[1] for x in r if x[0] == "variable"]
    dims = [x[1] for x in r if x[0] == "dimension"]
    return g, vars_, dims
for q in ["OLCI chlorophyll sector daily", "VIIRS chlorophyll sector daily", "NOAA-20 VIIRS chlorophyll daily", "OLCI chlorophyll daily global", "kd490 sector daily", "Sentinel-3B OLCI chlorophyll"]:
    for s in S[:2] + S[2:]:
        try:
            rows = requests.get(f"{s}/search/index.json", params={"searchFor": q, "page": 1, "itemsPerPage": 200}, timeout=60).json()["table"]
        except Exception as e:
            print("search fail", s, q, str(e)[:60]); continue
        ci = rows["columnNames"].index("Dataset ID")
        for row in rows["rows"]:
            ds = row[ci]
            try:
                g, v, d = meta(s, ds)
                la0, la1 = float(g.get("geospatial_lat_min", 99)), float(g.get("geospatial_lat_max", -99))
                lo0, lo1 = float(g.get("geospatial_lon_min", 999)), float(g.get("geospatial_lon_max", -999))
                if lo1 > 180 and lo0 >= 0:
                    hit = la0 <= BOX[1] and la1 >= BOX[0] and lo0 <= BOX[3] and lo1 >= BOX[2]
                else:
                    hit = la0 <= BOX[1] and la1 >= BOX[0] and lo0 <= BOX[3] and lo1 >= BOX[2]
                if hit:
                    print(f"COVERS | {s.split('/')[2]} | {ds} | lat {la0:.1f}..{la1:.1f} lon {lo0:.1f}..{lo1:.1f} | end {g.get('time_coverage_end')} | vars {v} dims {d}")
            except Exception as e:
                pass
            time.sleep(0.1)
        break
for s, ds in [("https://coastwatch.noaa.gov/erddap", "noaacwBLENDEDsshDaily"), ("https://coastwatch.noaa.gov/erddap", "noaacwBLENDEDNRTcurrentsDaily"),
              ("https://coastwatch.noaa.gov/erddap", "noaacwLEOACSPOSSTL3SnrtCDaily"), ("https://coastwatch.pfeg.noaa.gov/erddap", "nesdisVHNnoaa20chlaDaily")]:
    try:
        g, v, d = meta(s, ds); print(f"VARS | {ds} | {v} | dims {d} | end {g.get('time_coverage_end')}")
    except Exception as e:
        print("VARS fail", ds, str(e)[:80])
