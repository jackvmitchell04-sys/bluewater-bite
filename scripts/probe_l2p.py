"""Probe VIIRS L2P swath granules over the Bay of Plenty via THREDDS/OPeNDAP."""
import re, datetime as dt, requests, numpy as np, netCDF4, time
BOX = dict(lat0=-38.0, lat1=-35.85, lon0=175.15, lon1=178.25)
for sat in ("N20", "N21", "NPP"):
    base = f"https://coastwatch.noaa.gov/thredds/catalog/swath{sat}VIIRSNRTL2PWW00"
    for back in (0, 1):
        d = dt.datetime.utcnow() - dt.timedelta(days=back)
        url = f"{base}/{d:%Y}/{d.timetuple().tm_yday:03d}/catalog.html"
        t = requests.get(url, timeout=60).text
        files = sorted(set(re.findall(r"(\d{14}-STAR-L2P[^'\"<>]*?\.nc)", t)))
        print(sat, d.date(), url.split('thredds/')[1], len(files), "files", files[:1])
        # NZ passes: daytime ~23:30-02:30 UTC, night ~12:00-15:30 UTC
        cand = [f for f in files if f[8:10] in ("23", "00", "01", "02", "12", "13", "14", "15")]
        print("  candidates", len(cand))
        if back == 0 or not cand:
            continue
        t0 = time.time(); hits = 0
        for f in cand[:40]:
            ds_url = f"https://coastwatch.noaa.gov/thredds/dodsC/swath{sat}VIIRSNRTL2PWW00/{d:%Y}/{d.timetuple().tm_yday:03d}/{f}"
            try:
                nc = netCDF4.Dataset(ds_url)
            except Exception as e:
                print("  open fail", f, str(e)[:80]); continue
            if hits == 0:
                print("  vars", {k: v.shape for k, v in nc.variables.items()})
            la = nc.variables["lat"][::32, ::32]; lo = nc.variables["lon"][::32, ::32]
            m = (la > BOX["lat0"]) & (la < BOX["lat1"]) & (lo > BOX["lon0"]) & (lo < BOX["lon1"])
            if m.any():
                hits += 1
                jj, ii = np.where(m)
                r0, r1, c0, c1 = jj.min()*32, (jj.max()+1)*32, ii.min()*32, (ii.max()+1)*32
                sst = nc.variables["sea_surface_temperature"][0, r0:r1, c0:c1]
                ql = nc.variables["quality_level"][0, r0:r1, c0:c1]
                good = (ql >= 4) & ~np.ma.getmaskarray(sst)
                print(f"  HIT {f[:12]} rows {r0}-{r1} cols {c0}-{c1} good {good.mean():.0%} sst {np.ma.median(sst[good])-273.15 if good.any() else None}")
            nc.close()
        print("  scanned", len(cand[:40]), "in", round(time.time()-t0), "s, hits", hits)
