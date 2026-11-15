"""Morning data job for Bluewater Bite NZ.

Runs on GitHub Actions each morning. Downloads only the Bay of Plenty box from:
  1. NOAA ACSPO Himawari-9 hourly SST (L3C, 0.02 deg) via NOAA CoastWatch THREDDS
  2. Moana Project ocean forecast (temperature at depth), once MOANA_URL is set
and writes small JSON files to data/ that the app reads.

Each source is optional: if one fails, the others still update and the app
falls back to its live NOAA feeds.
"""
import json, os, sys, socket, time, datetime as dt
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
    ("IMOS / Bureau of Meteorology Himawari L3C, hourly 0.02 deg", "https://thredds.aodn.org.au/thredds/catalog/IMOS/SRS/SST/ghrsst/L3C-1h/catalog.xml"),
    ("NOAA ACSPO Himawari-9 AHI L3C, hourly 0.02 deg", "https://coastwatch.noaa.gov/thredds/catalog/gridH09AHINRTL3CWW00/catalog.xml"),
    ("NOAA ACSPO Himawari-9 AHI L3C, hourly 0.02 deg", "http://coastwatch.noaa.gov/thredds/catalog/gridH09AHINRTL3CWW00/catalog.xml"),
]
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


def himawari():
    sets, label = [], None
    for label, url in HIMA_CATALOGS:
        try:
            sets = latest_datasets(url, HOURS)
            if sets:
                break
        except Exception as e:
            log("catalog failed:", url, str(e)[:120])
    if not sets:
        raise RuntimeError("no Himawari files found from any source")
    frames, stack = [], []
    for d in sorted(sets, key=lambda d: d.name):
        try:
            if time.time() - T0 > 900:
                log("time budget used, stopping at", len(stack), "files"); break
            url = d.access_urls.get("OPENDAP") or d.access_urls.get("OpenDAP")
            log("reading", d.name)
            ds = xr.open_dataset(url)
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
            log("skip", d.name, e, file=sys.stderr)
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


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    ok = 0
    for job in (himawari, moana):
        try:
            job(); ok += 1
        except Exception as e:
            print(f"{job.__name__} failed: {e}", file=sys.stderr)
    sys.exit(0 if ok else 1)
