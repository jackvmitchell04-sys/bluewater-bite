"""One-off probe of NOAA catalogs (run from GitHub, prints what exists)."""
import requests, re, sys
if sys.argv[1:2] == ["kd"]:
    import runpy; runpy.run_path("scripts/probe_kd.py"); sys.exit()
if sys.argv[1:2] == ["l2p"]:
    import runpy; runpy.run_path("scripts/probe_l2p.py"); sys.exit()
def get(u, t=60):
    try:
        r = requests.get(u, timeout=t); return r.status_code, r.text
    except Exception as e:
        return 0, str(e)
urls = sys.argv[1:] or [
 "https://www.star.nesdis.noaa.gov/thredds/catalog/CoastWatch/VIIRS/catalog.html",
 "https://www.star.nesdis.noaa.gov/thredds/catalog/CoastWatch/VIIRS/n20/catalog.html",
 "https://www.star.nesdis.noaa.gov/thredds/catalog/CoastWatch/VIIRS/n20/sst_acspo/catalog.html",
 "https://www.star.nesdis.noaa.gov/thredds/catalog/CoastWatch/VIIRS/n20/sst_acspo/day/catalog.html",
 "https://www.star.nesdis.noaa.gov/thredds/catalog/CoastWatch/VIIRS/n20/sst_acspo/day/DailySector/catalog.html",
 "https://coastwatch.noaa.gov/erddap/search/index.csv?searchFor=VIIRS+SST+sector&itemsPerPage=60",
 "https://coastwatch.noaa.gov/erddap/search/index.csv?searchFor=ACSPO+L3U&itemsPerPage=40",
 "https://coastwatch.noaa.gov/thredds/catalog.html",
 "https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/VIIRS_NOAA20_CorrectedReflectance_TrueColor/default/2026-11-16/250m/8/113/316.jpg",
]
for u in urls:
    c, t = get(u)
    print("=====", c, u)
    if u.endswith(".jpg"):
        print(len(t)); continue
    links = re.findall(r"href=['\"]([^'\"]+)['\"]", t)
    if links:
        print("\n".join(sorted(set(l for l in links if "catalog" in l or ".nc" in l))[:80]))
    else:
        print(t[:3000])
