"""Morning data job for Bluewater Bite NZ.

Runs on GitHub Actions each morning. Downloads only the Bay of Plenty box from:
  1. NOAA ACSPO Himawari-9 hourly SST (L3C, 0.02 deg) via NOAA CoastWatch THREDDS
  2. Moana Project ocean forecast (temperature at depth), once MOANA_URL is set
and writes small JSON files to data/ that the app reads.

Each source is optional: if one fails, the others still update and the app
falls back to its live NOAA feeds.
"""
import json, os, sys, socket, time, warnings, datetime as dt
warnings.filterwarnings("ignore", category=DeprecationWarning)
socket.setdefaulttimeout(60)
T0 = time.time()
def log(*a):
    print(f"[{time.time()-T0:6.1f}s]", *a, flush=True)
import numpy as np
import xarray as xr
from siphon.catalog import TDSCatalog

BOX = dict(lat0=-38.05, lat1=-35.85, lon0=175.15, lon1=178.25)
OUT = os.path.join(os.path.dirname(__file__), "..", "data")
HIMA_CATALOGS = [
    ("NOAA ACSPO Himawari-9 L3C via CoastWatch, hourly 0.02 deg", "https://coastwatch.noaa.gov/thredds/catalog/gridH09AHINRTL3CWW00/catalog.xml"),
    ("NOAA ACSPO Himawari-9 L3C via NCEI, hourly 0.02 deg", "https://www.ncei.noaa.gov/thredds-ocean/catalog/ghrsst/L3C/PACIFIC/H09/STAR/catalog.xml"),
]
MAX_AGE_HOURS = 72
HOURS = 12


def subset(ds, lat_name="lat", lon_name="lon"):
    lat = ds[lat_name]
    lat_slice = slice(BOX["lat1"], BOX["lat0"]) if lat[0] > lat[-1] else slice(BOX["lat0"], BOX["lat1"])
    return ds.sel({lat_name: lat_slice, lon_name: slice(BOX["lon0"], BOX["lon1"])})


def grid_json(lats, lons, arr):
    """Store a 2D field north-to-south with nulls for missing values."""
    lats = np.asarray(lats); arr = np.asarray(arr, dtype=float)
    if lats[0] < lats[-1]:
        lats = lats[::-1]; arr = arr[::-1, :]
    rows = [[None if not np.isfinite(v) else round(float(v), 2) for v in row] for row in arr]
    return {"lats": [round(float(x), 4) for x in lats], "lons": [round(float(x), 4) for x in np.asarray(lons)], "values": rows}


def latest_datasets(cat_url, n):
    """Walk the THREDDS catalog (newest folders first) and return the newest n datasets."""
    found = []
    def walk(cat, depth=0):
        if len(found) >= n or depth > 4:
            return
        log("  catalog level", depth, len(cat.catalog_refs), "folders,", len(cat.datasets), "files")
        for name in sorted(cat.datasets, reverse=True):
            if name.endswith(".nc"):
                found.append(cat.datasets[name])
                if len(found) >= n:
                    return
        for ref in sorted(cat.catalog_refs, reverse=True):
            if len(found) >= n:
                return
            walk(cat.catalog_refs[ref].follow(), depth + 1)
    log("opening catalog", cat_url)
    walk(TDSCatalog(cat_url))
    log("found", len(found), "files")
    return found


def open_remote(d):
    """Open a THREDDS dataset using whichever service is offered: OPeNDAP, NetCDF Subset, or plain download."""
    import requests, tempfile
    urls = {k.lower(): v for k, v in d.access_urls.items()}
    ncss = urls.get("netcdfsubset")
    r = None
    if ncss:
        q = {"var": "sea_surface_temperature,quality_level", "north": BOX["lat1"], "south": BOX["lat0"],
             "east": BOX["lon1"], "west": BOX["lon0"], "accept": "netcdf4"}
        for attempt in range(2):
            try:
                r = requests.get(ncss, params=q, timeout=90); r.raise_for_status(); break
            except Exception as e:
                log("  subset attempt failed:", str(e)[:100]); r = None
    if r is None:
        dap = next((v for k, v in urls.items() if k in ("opendap", "dap4")), None)
        if dap:
            return xr.open_dataset(dap, engine="netcdf4")
        r = requests.get(urls["httpserver"], timeout=300); r.raise_for_status()
    f = tempfile.NamedTemporaryFile(suffix=".nc", delete=False); f.write(r.content); f.close()
    return xr.open_dataset(f.name, engine="netcdf4").load()


def himawari():
    sets, label = [], None
    for label, url in HIMA_CATALOGS:
        try:
            sets = latest_datasets(url, HOURS)
            newest = max(d.name for d in sets) if sets else ""
            stamp = newest[:10]
            age = (dt.datetime.utcnow() - dt.datetime.strptime(stamp, "%Y%m%d%H")).total_seconds() / 3600 if stamp.isdigit() else 1e9
            log(f"newest file {newest} ({age:.0f} h old)")
            if sets and age <= MAX_AGE_HOURS:
                break
            sets = []
        except Exception as e:
            log("catalog failed:", url, str(e)[:120])
    if not sets:
        raise RuntimeError("no Himawari files found from any source")
    frames, stack = [], []
    for d in sorted(sets, key=lambda d: d.name):
        try:
            if time.time() - T0 > 900:
                log("time budget used, stopping at", len(stack), "files"); break
            log("reading", d.name, "services:", ",".join(d.access_urls))
            ds = open_remote(d)
            ds = subset(ds)
            sst = ds["sea_surface_temperature"].isel(time=0)
            if "quality_level" in ds:
                sst = sst.where(ds["quality_level"].isel(time=0) >= 4)
            sst = sst - 273.15 if float(sst.mean(skipna=True)) > 200 else sst
            stack.append(sst.values)
            t = str(ds["time"].values[0])[:16]
            frames.append(t)
            lats, lons = ds["lat"].values, ds["lon"].values
        except Exception as e:
            log("skip", d.name, str(e)[:150])
    if not stack:
        raise RuntimeError("no Himawari data read")
    arr = np.array(stack)
    # composite: most recent valid pixel wins, so fronts are as fresh as the clouds allow
    latest = np.full(arr.shape[1:], np.nan)
    for layer in arr:
        latest = np.where(np.isfinite(layer), layer, latest)
    coverage = float(np.isfinite(latest).mean())
    out = {"source": label, "updated": dt.datetime.utcnow().isoformat(timespec="minutes") + "Z",
           "hours": frames, "coverage": round(coverage, 3), **grid_json(lats, lons, latest)}
    with open(os.path.join(OUT, "himawari.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"himawari: {len(frames)} hours, coverage {coverage:.0%}")


def moana():
    url = os.environ.get("MOANA_URL")
    if not url:
        print("moana: MOANA_URL not set yet, skipping")
        return
    ds = xr.open_dataset(url)
    tvar = next(v for v in ["temp", "temperature", "thetao", "sea_water_temperature"] if v in ds)
    latn = next(v for v in ["lat", "latitude", "lat_rho"] if v in ds.variables)
    lonn = next(v for v in ["lon", "longitude", "lon_rho"] if v in ds.variables)
    depthn = next((v for v in ["depth", "z", "lev"] if v in ds[tvar].dims), None)
    if depthn is None or ds[latn].ndim != 1:
        raise RuntimeError("Moana file is on a model grid this script doesn't handle yet; send the URL to Claude to adapt")
    ds = subset(ds, latn, lonn)
    t = ds[tvar].isel(time=-1) if "time" in ds[tvar].dims else ds[tvar]
    layers = {}
    for depth in (50, 100, 200):
        field = t.sel({depthn: depth}, method="nearest")
        layers[str(depth)] = grid_json(ds[latn].values, ds[lonn].values, field.values)
    out = {"source": "Moana Project ocean forecast (MetService)", "updated": dt.datetime.utcnow().isoformat(timespec="minutes") + "Z", "layers": layers}
    with open(os.path.join(OUT, "moana.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("moana: wrote", list(layers))


ERDS = ["https://coastwatch.pfeg.noaa.gov/erddap/griddap/", "https://upwell.pfeg.noaa.gov/erddap/griddap/", "https://coastwatch.noaa.gov/erddap/griddap/"]


def erddap_frames(ds, var, time="", alt=False, stride=1, pad=0.0):
    """Download a box from NOAA ERDDAP as CSV; return lats (N to S), lons, times and one 2D grid per time."""
    import requests, io, csv
    la0, la1, lo0, lo1 = BOX["lat0"] - pad, BOX["lat1"] + pad, BOX["lon0"] - pad, BOX["lon1"] + pad
    last = None
    r = None
    for erd in ERDS:
        for latc in (f"[({la0:.3f}):{stride}:({la1:.3f})]", f"[({la1:.3f}):{stride}:({la0:.3f})]"):
            q = var + time + ("[(0.0)]" if alt else "") + latc + f"[({lo0:.3f}):{stride}:({lo1:.3f})]"
            try:
                r = requests.get(erd + ds + ".csv?" + requests.utils.quote(q, safe=""), timeout=300)
                r.raise_for_status(); break
            except Exception as e:
                last = e; r = None
                if "403" in str(e) or "503" in str(e) or "timed out" in str(e):
                    break          # server refusing us: try the next mirror
        if r is not None:
            if erd != ERDS[0]:
                log("  (from mirror", erd.split("/")[2] + ")")
            break
    if r is None:
        raise last
    rows = list(csv.reader(io.StringIO(r.text)))
    head = rows[0]; rows = rows[2:]
    ti = head.index("time") if "time" in head else None
    li, oi, vi = head.index("latitude"), head.index("longitude"), head.index(var)
    lats = sorted({float(x[li]) for x in rows}, reverse=True)
    lons = sorted({float(x[oi]) for x in rows})
    times = sorted({x[ti] for x in rows}) if ti is not None else ["x"]
    L = {v: k for k, v in enumerate(lats)}; O = {v: k for k, v in enumerate(lons)}; T = {v: k for k, v in enumerate(times)}
    arr = np.full((len(times), len(lats), len(lons)), np.nan)
    for x in rows:
        try:
            arr[T[x[ti]] if ti is not None else 0, L[float(x[li])], O[float(x[oi])]] = float(x[vi])
        except ValueError:
            pass
    return lats, lons, [t[:10] for t in times], arr


def pack(lats, lons, times, arr, digits=2):
    frames = [[[None if not np.isfinite(v) else round(float(v), digits) for v in row] for row in a] for a in arr]
    return {"lats": [round(v, 4) for v in lats], "lons": [round(v, 4) for v in lons], "times": times, "frames": frames}


OCEAN = {}   # arrays kept for the jobs that run after ocean()


def best_recent(candidates, var, latest, time="[last-5:1:last]", digits=3, max_age=5):
    """Try each (dataset, label) in order; use the first whose newest day is within max_age days of the MUR SST date."""
    for ds, label in candidates:
        for alt in (True, False):
            try:
                r = erddap_frames(ds, var, time, alt=alt, pad=0.1)
                age = (dt.date.fromisoformat(latest) - dt.date.fromisoformat(r[2][-1])).days
                if age > max_age:
                    log(f"ocean: {ds} newest day {r[2][-1]} is {age} days behind, trying next"); break
                return pack(*r, digits=digits), label
            except Exception as e:
                log("ocean:", ds, "alt" if alt else "no-alt", "failed:", str(e)[:100])
    return None, None


def ocean():
    """All NOAA ERDDAP layers the app needs (browsers can't read ERDDAP directly)."""
    out = {"updated": dt.datetime.utcnow().isoformat(timespec="minutes") + "Z"}
    lats, lons, times, sst = erddap_frames("jplMURSST41", "analysed_sst", "[last-5:1:last]", stride=2, pad=0.02)
    OCEAN["sst"] = (lats, lons, times, sst)
    out["sst"] = pack(lats, lons, times, sst); log("ocean: MUR SST", times[0], "to", times[-1])
    try:
        out["clim"] = pack(*erddap_frames("jplMURSST41clim", "mean_sst", f"[({dt.datetime.strptime(times[-1], '%Y-%m-%d').timetuple().tm_yday})]", stride=2, pad=0.02), digits=2); log("ocean: clim ok")
    except Exception as e:
        log("ocean: clim failed:", str(e)[:120])
    chl, label = best_recent([("noaacwNPPN20S3ASCIDINEOF2kmDaily", "NOAA VIIRS + Copernicus Sentinel-3 OLCI, 2 km gap-filled daily"),
                              ("nesdisVHNnoaaSNPPnoaa20chlaGapfilledDaily", "NOAA VIIRS gap-filled, 9 km daily")], "chlor_a", times[-1])
    if chl:
        out["chl"], out["chl_label"] = chl, label; log("ocean: chl", label)
    try:
        r = erddap_frames("nesdisSSH1day", "sla", "[last-5:1:last]", pad=0.5)
        OCEAN["sla"] = r; out["sla"] = pack(*r, digits=3); log("ocean: sla ok")
    except Exception as e:
        log("ocean: sla failed:", str(e)[:120])
    for var in ("kd_490", "kd490"):
        kd, label = best_recent([("noaacwNPPN20S3AkdSCIDINEOF2kmDaily", "VIIRS + Sentinel-3 OLCI Kd490, 2 km"),
                                 ("nesdisNPPN20S3AkdSCIDINEOFDaily", "VIIRS + Sentinel-3 OLCI Kd490, 9 km")], var, times[-1], time="[last]", digits=4, max_age=7)
        if kd:
            out["kd"], out["kd_label"] = kd, label; log("ocean: kd", label); break
    with open(os.path.join(OUT, "ocean.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))


def bathy():
    """One-time job: full-resolution GEBCO (15 arc-second, ~450 m) depth grid for fine contours."""
    import requests, base64, io, csv
    path = os.path.join(OUT, "bathy.json")
    if os.path.exists(path):
        log("bathy: already saved, skipping"); return
    pad = 0.02
    la0, la1, lo0, lo1 = BOX["lat0"] - pad, BOX["lat1"] + pad, BOX["lon0"] - pad, BOX["lon1"] + pad
    text, label = None, None
    for ds, var, lbl in [("GEBCO_2020", "elevation", "GEBCO 2020, 15 arc-second"), ("GEBCO_2020", "altitude", "GEBCO 2020, 15 arc-second"),
                         ("etopo180", "altitude", "NOAA ETOPO1, 1 arc-minute")]:
        q = f"{var}[({la0}):1:({la1})][({lo0}):1:({lo1})]"
        try:
            r = requests.get(f"https://coastwatch.pfeg.noaa.gov/erddap/griddap/{ds}.csv?" + requests.utils.quote(q, safe=""), timeout=300)
            r.raise_for_status(); text, label = r.text, lbl; break
        except Exception as e:
            log("bathy source failed:", ds, var, str(e)[:100])
    if text is None:
        raise RuntimeError("no bathymetry source reachable")
    rows = list(csv.reader(io.StringIO(text)))[2:]
    lats = sorted({float(r[0]) for r in rows}, reverse=True)
    lons = sorted({float(r[1]) for r in rows})
    li = {v: k for k, v in enumerate(lats)}; oi = {v: k for k, v in enumerate(lons)}
    grid = np.full((len(lats), len(lons)), -32768, dtype=np.int16)
    for r in rows:
        try:
            z = float(r[2])
        except ValueError:
            continue
        grid[li[float(r[0])], oi[float(r[1])]] = int(max(-30000, min(30000, round(-z))))  # depth in metres, negative = land
    res = (lons[-1] - lons[0]) / (len(lons) - 1)
    out = {"source": label, "lat1": lats[0], "lon0": lons[0], "res": res, "nx": len(lons), "ny": len(lats),
           "depth_int16_b64": base64.b64encode(grid.astype("<i2").tobytes()).decode()}
    with open(path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    log(f"bathy: {label}, {len(lats)}x{len(lons)} cells, {res*111.32*1000:.0f} m spacing")


# ---------------------------------------------------------------- shared helpers
WX = "https://api.open-meteo.com/v1/forecast"
MARINE = "https://marine-api.open-meteo.com/v1/marine"


def openmeteo(base, pts, params, chunk=80):
    """Open-Meteo multi-location request in chunks. Returns one dict per point."""
    import requests
    res = []
    for k in range(0, len(pts), chunk):
        c = pts[k:k + chunk]
        q = dict(params, latitude=",".join(f"{p[0]:.3f}" for p in c), longitude=",".join(f"{p[1]:.3f}" for p in c))
        r = None
        for attempt in range(3):
            try:
                r = requests.get(base, params=q, timeout=120); r.raise_for_status(); break
            except Exception as e:
                log("  open-meteo retry", attempt, str(e)[:120]); r = None; time.sleep(6)
        if r is None:
            raise RuntimeError("open-meteo unreachable")
        d = r.json()
        res.extend(d if isinstance(d, list) else [d])
        time.sleep(1.5)
    return res


def gebco_depth():
    """GEBCO depth grid from bathy.json (positive down, negative land) with a bilinear sampler."""
    import base64
    o = json.load(open(os.path.join(OUT, "bathy.json")))
    g = np.frombuffer(base64.b64decode(o["depth_int16_b64"]), dtype="<i2").reshape(o["ny"], o["nx"]).astype(float)
    def sample(la, lo):
        fy = (o["lat1"] - np.asarray(la)) / o["res"]; fx = (np.asarray(lo) - o["lon0"]) / o["res"]
        y0 = np.clip(np.floor(fy).astype(int), 0, o["ny"] - 2); x0 = np.clip(np.floor(fx).astype(int), 0, o["nx"] - 2)
        ty = np.clip(fy - y0, 0, 1); tx = np.clip(fx - x0, 0, 1)
        return (g[y0, x0] * (1 - ty) * (1 - tx) + g[y0, x0 + 1] * (1 - ty) * tx + g[y0 + 1, x0] * ty * (1 - tx) + g[y0 + 1, x0 + 1] * ty * tx)
    return sample


def ocean_points(step, margin=0.05, min_depth=5):
    sample = gebco_depth()
    pts = []
    la = BOX["lat0"] + margin
    while la <= BOX["lat1"] - margin + 1e-9:
        lo = BOX["lon0"] + margin
        while lo <= BOX["lon1"] - margin + 1e-9:
            if float(sample(la, lo)) > min_depth:
                pts.append((round(la, 3), round(lo, 3)))
            lo += step
        la += step
    return pts


# ---------------------------------------------------------------- NIWA 25 m survey bathymetry (C-MAP style detail)
NIWA = "https://gis.niwa.co.nz/raster/rest/services"
HD_RES = 0.0015   # ~140 m cells: the 25 m survey, averaged down so the app stays phone-sized


def bathy_hd():
    """One-time job: NIWA multibeam bathymetry (Bay of Plenty 25 m, Hauraki Gulf 20 m) merged over GEBCO, saved as a 16-bit PNG."""
    import requests, io
    from PIL import Image
    png, meta = os.path.join(OUT, "bathy_hd.png"), os.path.join(OUT, "bathy_hd.json")
    if os.path.exists(png) and os.path.exists(meta):
        log("bathy_hd: already saved, skipping"); return
    nx = int(round((BOX["lon1"] - BOX["lon0"]) / HD_RES)); ny = int(round((BOX["lat1"] - BOX["lat0"]) / HD_RES))
    rx, ry = (BOX["lon1"] - BOX["lon0"]) / nx, (BOX["lat1"] - BOX["lat0"]) / ny
    lons = BOX["lon0"] + (np.arange(nx) + 0.5) * rx; lats = BOX["lat1"] - (np.arange(ny) + 0.5) * ry
    LO, LA = np.meshgrid(lons, lats)
    depth = gebco_depth()(LA, LO)            # positive down, negative = land height
    src = np.zeros(depth.shape, dtype=np.uint8)
    names = []
    try:
        cat = requests.get(NIWA, params={"f": "json"}, timeout=60).json()
        names = [s["name"] for s in cat.get("services", []) if s.get("type") == "ImageServer" and any(k in s["name"].lower() for k in ("bay_of_plenty", "hauraki", "bop"))]
        log("bathy_hd: NIWA image services:", names)
    except Exception as e:
        log("bathy_hd: catalog failed", str(e)[:100])
    if "Bay_of_Plenty_25m_DTM" not in names:
        names.append("Bay_of_Plenty_25m_DTM")
    names.sort(key=lambda n: "plenty" in n.lower())    # Bay of Plenty last so it wins where grids overlap
    used = []
    for name in names:
        base = f"{NIWA}/{name}/ImageServer"
        arr = None
        for fmt in ("tiff", "bsq"):
            q = {"bbox": f"{BOX['lon0']},{BOX['lat0']},{BOX['lon1']},{BOX['lat1']}", "bboxSR": 4326, "imageSR": 4326, "size": f"{nx},{ny}",
                 "format": fmt, "pixelType": "F32", "interpolation": "RSP_BilinearInterpolation", "f": "image"}
            try:
                r = requests.get(base + "/exportImage", params=q, timeout=300); r.raise_for_status()
                if r.headers.get("content-type", "").startswith(("text", "application/json")):
                    raise RuntimeError(r.text[:200])
                if fmt == "tiff":
                    try:
                        import tifffile
                        arr = tifffile.imread(io.BytesIO(r.content)).astype(np.float32)
                    except Exception as e:
                        log("  tifffile failed, trying Pillow:", str(e)[:80])
                        arr = np.array(Image.open(io.BytesIO(r.content)), dtype=np.float32)
                else:
                    arr = np.frombuffer(r.content[:nx * ny * 4], dtype="<f4").reshape(ny, nx)
                break
            except Exception as e:
                log("bathy_hd:", name, fmt, "failed:", str(e)[:150])
        if arr is None:
            continue
        if arr.ndim == 3:
            arr = arr[..., 0]
        if arr.shape != depth.shape:
            log("bathy_hd:", name, "unexpected shape", arr.shape); continue
        ok = np.isfinite(arr) & (np.abs(arr) < 12000) & (arr != 0)
        depth[ok] = -arr[ok]; src[ok] = 1
        used.append(name); log(f"bathy_hd: {name} covers {ok.mean():.0%} of the box")
    v = np.clip(np.round(depth) + 20000, 0, 65535).astype(np.uint32)
    rgb = np.stack([(v >> 8).astype(np.uint8), (v & 255).astype(np.uint8), (src * 255).astype(np.uint8)], axis=-1)
    Image.fromarray(rgb, "RGB").save(png, optimize=True)
    label = ("NIWA " + " + ".join(n.replace("_DTM", "").replace("_", " ") for n in used) + " survey over GEBCO") if used else "GEBCO 2020"
    with open(meta, "w") as f:
        json.dump({"source": label, "lon0": float(lons[0]), "lat1": float(lats[0]), "rx": rx, "ry": ry, "nx": nx, "ny": ny,
                   "offset": 20000, "niwa_coverage": round(float(src.mean()), 3)}, f)
    log(f"bathy_hd: wrote {nx}x{ny} ({os.path.getsize(png)/1e6:.1f} MB), {label}")


# ---------------------------------------------------------------- ECMWF wind + Ekman-corrected surface currents
def currents():
    """Total surface current = geostrophic (from altimetry) + wind-driven Ekman drift (ECMWF IFS 0.25 deg wind)."""
    pts = ocean_points(0.25, margin=0.1)
    log("currents:", len(pts), "points")
    W = openmeteo(WX, pts, {"hourly": "wind_speed_10m,wind_direction_10m", "models": "ecmwf_ifs025", "past_days": 1,
                            "forecast_days": 6, "wind_speed_unit": "ms", "timezone": "Pacific/Auckland"})
    now = dt.datetime.utcnow() + dt.timedelta(hours=13)
    geo = None
    if "sla" in OCEAN:
        lats, lons, times, sla = OCEAN["sla"]
        eta = sla[-1]; la = np.array(lats); lo = np.array(lons)
        deta_dy = np.gradient(eta, la * 111320.0, axis=0)
        deta_dx = np.gradient(eta, axis=1) / np.gradient(lo)[None, :] / (111320.0 * np.cos(np.radians(la))[:, None])
        f = 2 * 7.2921e-5 * np.sin(np.radians(la))[:, None]
        ug, vg = -9.81 / f * deta_dy, 9.81 / f * deta_dx
        def at(a, b, F):
            j = np.abs(la - a).argmin(); i = np.abs(lo - b).argmin()
            v = F[j, i]; return float(v) if np.isfinite(v) else None
        geo = (ug, vg, at)
    out, days = [], None
    for (plat, plon), d in zip(pts, W):
        H = d.get("hourly") or {}
        T = [dt.datetime.fromisoformat(t) for t in H.get("time", [])]
        sp, di = H.get("wind_speed_10m", []), H.get("wind_direction_10m", [])
        uu = [(-s * np.sin(np.radians(x)), -s * np.cos(np.radians(x))) if s is not None and x is not None else None for s, x in zip(sp, di)]
        past = [u for t, u in zip(T, uu) if u and now - dt.timedelta(hours=24) <= t <= now]
        if not past:
            continue
        wu, wv = float(np.mean([p[0] for p in past])), float(np.mean([p[1] for p in past]))
        # Ekman surface drift: ~0.0127 W / sqrt(sin|lat|), 45 deg to the left of the wind in the Southern Hemisphere
        k = 0.0127 / np.sqrt(np.sin(np.radians(abs(plat))))
        c, s_ = np.cos(np.radians(45)), np.sin(np.radians(45))
        ue, ve = k * (wu * c - wv * s_), k * (wu * s_ + wv * c)
        ugv = vgv = None
        if geo:
            ugv, vgv = geo[2](plat, plon, geo[0]), geo[2](plat, plon, geo[1])
            if ugv is not None and np.hypot(ugv, vgv) > 1.5:
                ugv = vgv = None
        u, v = ue + (ugv or 0), ve + (vgv or 0)
        # daily mean wind for the next 6 NZ days (for the forecast map)
        by = {}
        for t, w in zip(T, uu):
            if w and t.date() >= now.date():
                by.setdefault(t.date().isoformat(), []).append(w)
        days = sorted(by)[:6]
        dw = [[round(float(np.mean([w[0] for w in by[x]])), 1), round(float(np.mean([w[1] for w in by[x]])), 1)] for x in days]
        out.append({"lat": plat, "lon": plon, "u": round(u, 3), "v": round(v, 3), "ue": round(ue, 3), "ve": round(ve, 3),
                    "ug": None if ugv is None else round(ugv, 3), "vg": None if vgv is None else round(vgv, 3),
                    "wu": round(wu, 1), "wv": round(wv, 1), "wd": dw})
    with open(os.path.join(OUT, "currents.json"), "w") as f:
        json.dump({"source": "Geostrophic (NOAA altimetry) + Ekman drift from ECMWF IFS 0.25 deg wind", "updated": dt.datetime.utcnow().isoformat(timespec="minutes") + "Z",
                   "wind_days": days, "points": out}, f, separators=(",", ":"))
    log("currents: wrote", len(out), "points")


# ---------------------------------------------------------------- 5-day SST forecast
def forecast():
    pts = ocean_points(0.1)
    log("forecast:", len(pts), "points")
    R = openmeteo(MARINE, pts, {"hourly": "sea_surface_temperature", "forecast_days": 6, "timezone": "Pacific/Auckland"})
    lats = sorted({p[0] for p in pts}, reverse=True); lons = sorted({p[1] for p in pts})
    L = {v: k for k, v in enumerate(lats)}; O = {v: k for k, v in enumerate(lons)}
    dates = None; arr = None
    for (la, lo), d in zip(pts, R):
        H = d.get("hourly") or {}
        by = {}
        for t, v in zip(H.get("time", []), H.get("sea_surface_temperature", [])):
            if v is not None:
                by.setdefault(t[:10], []).append(v)
        ds = sorted(by)[:6]
        if len(ds) < 3:
            continue
        if dates is None:
            dates = ds; arr = np.full((len(dates), len(lats), len(lons)), np.nan)
        for k, x in enumerate(dates):
            if x in by:
                arr[k, L[la], O[lo]] = np.mean(by[x])
    if arr is None:
        raise RuntimeError("no forecast data")
    delta = arr - arr[0]
    out = {"source": "Copernicus Mercator ocean model via Open-Meteo (change vs today, added to MUR 1 km)", "updated": dt.datetime.utcnow().isoformat(timespec="minutes") + "Z",
           **pack(lats, lons, dates, delta, digits=2), "base": pack(lats, lons, dates[:1], arr[:1], digits=2)["frames"][0]}
    with open(os.path.join(OUT, "forecast.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
    log("forecast: wrote", dates)


# ---------------------------------------------------------------- tides
TIDE_PORTS = [("Tauranga", -37.60, 176.20), ("Whitianga", -36.80, 175.78), ("Whakatane", -37.92, 177.01)]


def tides():
    out = []
    for name, la, lo in TIDE_PORTS:
        got = None
        for k in range(5):     # step offshore until the model has a sea cell
            p = (la + 0.03 * k, lo + 0.03 * k)
            d = openmeteo(MARINE, [p], {"hourly": "sea_level_height_msl", "past_days": 1, "forecast_days": 8, "timezone": "UTC"})[0]
            h = (d.get("hourly") or {}).get("sea_level_height_msl") or []
            if sum(v is not None for v in h) > 100:
                got = (p, d["hourly"]["time"][0], h); break
        if got:
            out.append({"name": name, "lat": got[0][0], "lon": got[0][1], "start": got[1] + "Z", "h": got[2]})
            log("tides:", name, "ok")
    if not out:
        raise RuntimeError("no tide data")
    with open(os.path.join(OUT, "tides.json"), "w") as f:
        json.dump({"source": "Copernicus/FES tide model via Open-Meteo", "updated": dt.datetime.utcnow().isoformat(timespec="minutes") + "Z", "ports": out}, f, separators=(",", ":"))


# ---------------------------------------------------------------- marine reserves and protection areas
RESERVE_SOURCES = [
    ("Marine reserve", "https://services1.arcgis.com/3JjYDyG3oajxU6HO/arcgis/rest/services/DOC_Marine_Reserves/FeatureServer/0/query",
     {"where": "1=1", "geometry": f"{BOX['lon0']},{BOX['lat0']},{BOX['lon1']},{BOX['lat1']}", "geometryType": "esriGeometryEnvelope", "inSR": 4326,
      "spatialRel": "esriSpatialRelIntersects", "outFields": "*", "outSR": 4326, "f": "geojson"}),
    ("High protection area", "item:4b0a74880f554ce1a8e1fa46a3bbd3ce", None),
    ("High protection area", "https://doc-deptconservation.opendata.arcgis.com/api/download/v1/items/4b0a74880f554ce1a8e1fa46a3bbd3ce/geojson", {"layers": 0}),
]
BOX_QUERY = {"where": "1=1", "geometry": f"{BOX['lon0']},{BOX['lat0']},{BOX['lon1']},{BOX['lat1']}", "geometryType": "esriGeometryEnvelope", "inSR": 4326,
             "spatialRel": "esriSpatialRelIntersects", "outFields": "*", "outSR": 4326, "f": "geojson"}


def reserves():
    import requests
    items = []
    have_hpa = False
    for kind, url, params in RESERVE_SOURCES:
        if kind == "High protection area" and have_hpa:
            continue
        try:
            if url.startswith("item:"):
                meta = requests.get(f"https://www.arcgis.com/sharing/rest/content/items/{url[5:]}", params={"f": "json"}, timeout=60).json()
                url, params = meta["url"].rstrip("/") + ("/0" if not meta["url"].rstrip("/")[-1].isdigit() else "") + "/query", BOX_QUERY
                log("reserves: HPA feature service", url)
            g = requests.get(url, params=params, timeout=120).json()
            if kind == "High protection area" and g.get("features"):
                have_hpa = True
        except Exception as e:
            log("reserves:", kind, "failed", str(e)[:100]); continue
        for ft in g.get("features", []):
            pr = ft.get("properties") or {}; geom = ft.get("geometry") or {}
            name = next((str(pr[k]) for k in ("Name", "NAME", "name", "Official_Name", "OfficialName", "Site_Name") if pr.get(k)), None) \
                or next((str(v) for v in pr.values() if isinstance(v, str) and ("Area" in v or "Reserve" in v)), kind)
            polys = geom.get("coordinates") or []
            if geom.get("type") == "Polygon":
                polys = [polys]
            rings = []
            for poly in polys:
                for ring in poly:
                    kept, last = [], None
                    for x, y in ring:
                        if last is None or abs(x - last[0]) + abs(y - last[1]) > 0.0003:
                            kept.append([round(x, 5), round(y, 5)]); last = (x, y)
                    if len(kept) > 3:
                        rings.append(kept)
            if not rings:
                continue
            xs = [p[0] for r in rings for p in r]; ys = [p[1] for r in rings for p in r]
            if max(xs) < BOX["lon0"] or min(xs) > BOX["lon1"] or max(ys) < BOX["lat0"] or min(ys) > BOX["lat1"]:
                continue
            if kind == "High protection area" and "seafloor" in name.lower():
                continue
            items.append({"name": name, "kind": "High protection area" if "high protection" in name.lower() else kind, "rings": rings})
        log("reserves:", kind, "->", len(items), "so far")
    if not items:
        raise RuntimeError("no reserve polygons")
    with open(os.path.join(OUT, "reserves.json"), "w") as f:
        json.dump({"source": "Department of Conservation", "updated": dt.datetime.utcnow().isoformat(timespec="minutes") + "Z", "items": items}, f, separators=(",", ":"))


# ---------------------------------------------------------------- morning alerts
MOTITI = [[[176.416492, -37.524206], [176.442055, -37.526889], [176.425474, -37.581453], [176.361756, -37.600719], [176.350051, -37.591381], [176.346041, -37.580159]],
          [[176.527828, -37.586617], [176.551402, -37.596707], [176.530326, -37.621452], [176.515162, -37.616305], [176.510144, -37.608026], [176.51198, -37.598403]],
          [[176.568542, -37.648364], [176.587756, -37.67195], [176.573084, -37.68332], [176.540229, -37.687466], [176.513728, -37.684779], [176.517109, -37.669504]]]


def _inpoly(x, y, ring):
    c = False
    for a in range(len(ring)):
        (xi, yi), (xj, yj) = ring[a], ring[a - 1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            c = not c
    return c


def app_marks():
    """Read the charted and approximate marks straight from index.html so there's one list to maintain."""
    import re
    html = open(os.path.join(os.path.dirname(__file__), "..", "index.html"), encoding="utf-8").read()
    out = []
    for m in re.finditer(r'\["([^"]+)",\s*([-+()\d./ ]+),\s*([-+()\d./ ]+),\s*"', html):
        try:
            la, lo = eval(m.group(2), {"__builtins__": {}}), eval(m.group(3), {"__builtins__": {}})
            if -39 < la < -35 and 174 < lo < 179:
                out.append((m.group(1), la, lo))
        except Exception:
            pass
    return out


def alerts():
    """Flag marks with a strong SST front or a warm push nearby, in the striped marlin range."""
    lats, lons, times, sst = OCEAN["sst"]
    la, lo = np.array(lats), np.array(lons)
    last, first = sst[-1], sst[0]
    gy = np.gradient(last, la * 111.32, axis=0)
    gx = np.gradient(last, axis=1) / (np.gradient(lo)[None, :] * 111.32 * np.cos(np.radians(la))[:, None])
    grad = np.hypot(gx, gy)                    # deg C per km
    rings = [r for r in MOTITI]
    try:
        rings += [r for it in json.load(open(os.path.join(OUT, "reserves.json")))["items"] for r in it["rings"]]
    except Exception:
        pass
    items = []
    for name, mla, mlo in app_marks():
        if any(_inpoly(mlo, mla, r) for r in rings):
            continue
        dy = (la[:, None] - mla) * 111.32; dx = (lo[None, :] - mlo) * 111.32 * np.cos(np.radians(mla))
        near = np.hypot(dx, dy) <= 3 * 1.852
        if not near.any():
            continue
        t = np.nanmean(last[near]); g = np.nanmax(grad[near]); rise = np.nanmean(last[near] - first[near])
        if not np.isfinite(t):
            continue
        why = []
        if 18.5 <= t <= 23 and g >= 0.08:
            why.append(f"front {g * 2:.2f} C per 2 km")
        if t >= 18.5 and rise >= 0.8:
            why.append(f"warmed {rise:+.1f} C in 5 days")
        if why:
            items.append({"mark": name, "lat": round(mla, 4), "lon": round(mlo, 4), "sst": round(float(t), 1), "grad": round(float(g), 3),
                          "rise": round(float(rise), 2), "why": ", ".join(why)})
    items.sort(key=lambda x: -x["grad"])
    items = items[:8]
    out = {"date": times[-1], "species": "Striped marlin (18.5 to 23 C)", "items": items}
    with open(os.path.join(OUT, "alerts.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
    body = os.path.join(os.path.dirname(__file__), "..", "alert_issue.md")
    if items:
        with open(body, "w") as f:
            f.write(f"Bite alert {times[-1]}: " + ", ".join(i["mark"] for i in items[:3]) + "\n")
            f.write(f"Marks with a strong SST front or warm push within 3 nm, water in the striped marlin range. Satellite day {times[-1]}.\n\n")
            for i in items:
                f.write(f"- **{i['mark']}** ({abs(i['lat']):.3f}S {i['lon']:.3f}E): {i['sst']} C, {i['why']}\n")
            f.write("\nOpen the map: https://jackvmitchell04-sys.github.io/bluewater-bite/\n")
    log("alerts:", len(items), "marks flagged")


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    ok = 0
    for job in (bathy, bathy_hd, ocean, currents, forecast, tides, reserves, alerts, himawari, moana):
        try:
            job(); ok += 1
        except Exception as e:
            print(f"{job.__name__} failed: {e}", file=sys.stderr)
    sys.exit(0 if ok else 1)
