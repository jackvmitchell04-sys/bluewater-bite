"""Morning data job for Bluewater Bite NZ.

Runs on GitHub Actions each morning. Downloads only the Bay of Plenty box from:
  1. NOAA ACSPO Himawari-9 hourly SST (L3C, 0.02 deg) via NOAA CoastWatch THREDDS
  2. NOAA ERDDAP SST, chlorophyll, clarity; NIWA bathymetry; Open-Meteo model currents, wind, tides; DOC reserves
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
import requests as _rq

_rq_get = _rq.get


_DEAD = {}


def _get_deadline(url, params=None, timeout=60, **kw):
    """requests.get with a hard wall-clock limit: a server that trickles bytes can't stall the job for 20 minutes.
    A host that fails to connect twice is skipped for the rest of the run, so the mirrors take over at once."""
    limit = timeout if isinstance(timeout, (int, float)) else 120
    host = url.split("/")[2]
    if _DEAD.get(host, 0) >= 2:
        raise _rq.ConnectionError(f"{host} skipped: not answering this run")
    t0 = time.monotonic()
    try:
        r = _rq_get(url, params=params, timeout=(10, min(limit, 60)), stream=True, **kw)
    except (_rq.ConnectionError, _rq.Timeout):
        _DEAD[host] = _DEAD.get(host, 0) + 1
        raise
    buf = []
    for chunk in r.iter_content(1 << 16):
        buf.append(chunk)
        if time.monotonic() - t0 > limit:
            r.close()
            raise _rq.Timeout(f"{url.split('/')[2]} took over {limit}s")
    r._content = b"".join(buf); r._content_consumed = True
    return r


_rq.get = _get_deadline
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
    t_start = time.time()
    for d in sorted(sets, key=lambda d: d.name):
        try:
            if time.time() - t_start > 600:
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


ERDS = ["https://coastwatch.pfeg.noaa.gov/erddap/griddap/", "https://upwell.pfeg.noaa.gov/erddap/griddap/", "https://polarwatch.noaa.gov/erddap/griddap/",
        "https://oceanwatch.pifsc.noaa.gov/erddap/griddap/", "https://coastwatch.noaa.gov/erddap/griddap/", "https://coastwatch.pfeg.noaa.gov/erddap/griddap/"]   # last entry: retry the main server after the others


_SRV_PATH = os.path.join(OUT, "servers.json")
try:
    _SRV = json.load(open(_SRV_PATH))
except Exception:
    _SRV = {}


def _server_order(ds):
    """Ask the server that last delivered this dataset first; servers that said 'not here' in the last 24 h go last
    (not dropped, so a server that gains the dataset is picked up again within a day)."""
    rec = _SRV.get(ds, {}); good = rec.get("good"); miss = rec.get("miss", {})
    now = time.time()
    recent_miss = {h for h, t in miss.items() if now - t < 86400}
    order = sorted(ERDS, key=lambda e: (e.split("/")[2] != good, e.split("/")[2] in recent_miss))
    return order


def _server_note(ds, host, ok):
    rec = _SRV.setdefault(ds, {"miss": {}})
    if ok:
        rec["good"] = host; rec.setdefault("miss", {}).pop(host, None)
    else:
        rec.setdefault("miss", {})[host] = time.time()
    try:
        with open(_SRV_PATH, "w") as f:
            json.dump(_SRV, f, separators=(",", ":"))
    except Exception:
        pass


def erddap_frames(ds, var, time="", alt=False, stride=1, pad=0.0):
    """Download a box from NOAA ERDDAP as CSV; return lats (N to S), lons, times and one 2D grid per time."""
    import requests, io, csv
    la0, la1, lo0, lo1 = BOX["lat0"] - pad, BOX["lat1"] + pad, BOX["lon0"] - pad, BOX["lon1"] + pad
    last = None
    r = None
    order = _server_order(ds)
    for erd in order:
        for latc in (f"[({la0:.3f}):{stride}:({la1:.3f})]", f"[({la1:.3f}):{stride}:({la0:.3f})]"):
            q = var + time + ("[(0.0)]" if alt else "") + latc + f"[({lo0:.3f}):{stride}:({lo1:.3f})]"
            try:
                r = requests.get(erd + ds + ".csv?" + requests.utils.quote(q, safe=""), timeout=150)
                r.raise_for_status(); break
            except Exception as e:
                last = e; r = None
                log("  ", erd.split("/")[2], str(e)[:60])
                if any(x in str(e) for x in ("403", "500", "502", "503", "timed out", "skipped")):
                    break          # server refusing us: try the next mirror
        if r is None and last is not None and "404" in str(last) and any(k in (getattr(getattr(last, "response", None), "text", "") or "") for k in ("unknown datasetID", "Resource not found")):
            _server_note(ds, erd.split("/")[2], False)          # this server doesn't hold the dataset (not just 'no data for that day')
        if r is None and erd == order[-2]:
            __import__("time").sleep(60)
        if r is not None:
            _server_note(ds, erd.split("/")[2], True)
            if erd != order[0]:
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


# ---------------------------------------------------------------- multi-sensor ocean colour composite
CHL_SOURCES = [   # (dataset, stride, label)  finest first: on the same day the finest sensor wins
    ("noaacwS3AOLCIchlaDaily", 1, "Sentinel-3A OLCI"),
    ("noaacwS3BOLCIchlaDaily", 1, "Sentinel-3B OLCI"),
    ("thredds:chloraVIIRSn21SectorDailyZX00", 1, "NOAA-21 VIIRS 750 m"),
    ("thredds:chloraVIIRSn20SectorDailyZX00", 1, "NOAA-20 VIIRS 750 m"),
    ("noaacwN20VIIRSchlaDaily", 1, "NOAA-20 VIIRS"),
]
KD_SOURCES = [("thredds:kd490VIIRSn21SectorDailyZX00", 1, "NOAA-21 VIIRS 750 m"),
              ("thredds:kd490VIIRSn20SectorDailyZX00", 1, "NOAA-20 VIIRS 750 m")]   # S-NPP ended 2 Nov 2026
SECTOR_CACHE = os.environ.get("SECTOR_CACHE", os.path.join(os.path.dirname(__file__), "..", ".cache", "sector"))


def thredds_sector_frames(root, var, d0, pad=0.05, res=0.0075):
    """Daily CoastWatch VIIRS sector files (750 m, NZ sector ZX) on THREDDS: find the Bay in each file, cut it out over
    OPeNDAP, bin to a regular lat/lon grid. Same return shape as erddap_frames. Cut-outs are cached between runs."""
    import netCDF4, re, requests
    os.makedirs(SECTOR_CACHE, exist_ok=True)
    with open(os.path.expanduser("~/.dodsrc"), "w") as f:
        f.write("HTTP.TIMEOUT=90\nHTTP.CONNECTTIMEOUT=15\n")
    B = "https://coastwatch.noaa.gov/thredds"
    la0, la1, lo0, lo1 = BOX["lat0"] - pad, BOX["lat1"] + pad, BOX["lon0"] - pad, BOX["lon1"] + pad
    lats = np.round(np.arange(la1, la0 - 1e-9, -res), 4); lons = np.round(np.arange(lo0, lo1 + 1e-9, res), 4)
    files = []
    for yr in sorted({d0.year, dt.date.today().year}):
        try:
            rq = requests.get(f"{B}/catalog/{root}/{yr}/catalog.html", timeout=60); rq.raise_for_status()
            names = sorted(set(re.findall(r"([A-Za-z0-9_.\-]+\.nc)", rq.text)))
        except Exception as e:                                        # server down: use the cut-outs already cached
            pre, suf = f"{root}_{yr}_", f".{var}.npy"
            names = sorted(fn[len(pre):-len(suf)] for fn in os.listdir(SECTOR_CACHE) if fn.startswith(pre) and fn.endswith(suf))
            log(f"colour: {root} catalog unavailable ({str(e)[:50]}), using {len(names)} cached files")
        for f in names:
            m = re.search(r"(?:C|V)(\d{4})(\d{3})", f)
            if m:
                d = dt.date(int(m.group(1)), 1, 1) + dt.timedelta(days=int(m.group(2)) - 1)
                if d >= d0:
                    files.append((d, f"{yr}/{f}"))
    times, frames = [], []
    for d, f in sorted(files):
        cf = os.path.join(SECTOR_CACHE, (root + "_" + f).replace("/", "_") + f".{var}.npy")
        if os.path.exists(cf):
            G = np.load(cf)
        else:
            try:
                nc = netCDF4.Dataset(f"{B}/dodsC/{root}/{f}")
                # every ZX00 file is on the same fixed sector grid: find the Bay once (lat/lon are blanked under cloud,
                # so look in whichever file has them) and reuse that block for all files and products
                bf = os.path.join(SECTOR_CACHE, "ZX00_block.json")
                blk = json.load(open(bf)) if os.path.exists(bf) else None
                R, C = nc.variables["swath_latitude"].shape[-2:]
                if blk and blk.get("shape") == [int(R), int(C)]:
                    r0, r1, c0, c1 = blk["block"]
                else:
                    st = 20
                    sla = np.ma.filled(nc.variables["swath_latitude"][0, 0, ::st, ::st].astype(float), np.nan)
                    slo = np.ma.filled(nc.variables["swath_longitude"][0, 0, ::st, ::st].astype(float), np.nan)
                    m = (sla > la0) & (sla < la1) & (slo > lo0) & (slo < lo1)
                    if not m.any():
                        nc.close(); continue
                    jj, ii = np.where(m)
                    r0, r1, c0, c1 = int(max(0, (jj.min() - 3) * st)), int(min(R, (jj.max() + 4) * st)), int(max(0, (ii.min() - 3) * st)), int(min(C, (ii.max() + 4) * st))
                    if slo[m].min() < lo0 + .15 and slo[m].max() > lo1 - .15 and sla[m].min() < la0 + .15 and sla[m].max() > la1 - .15:   # whole Bay seen
                        json.dump({"shape": [int(R), int(C)], "block": [r0, r1, c0, c1]}, open(bf, "w"))
                v = np.ma.filled(nc.variables[var][0, 0, r0:r1, c0:c1].astype(float), np.nan)
                la_ = np.ma.filled(nc.variables["swath_latitude"][0, 0, r0:r1, c0:c1].astype(float), np.nan)
                lo_ = np.ma.filled(nc.variables["swath_longitude"][0, 0, r0:r1, c0:c1].astype(float), np.nan)
                nc.close()
            except Exception as e:
                log(f"colour: {root} {f} failed: {str(e)[:80]}"); continue
            ok = np.isfinite(v) & (v > 0) & np.isfinite(la_) & np.isfinite(lo_)
            gi = np.round((lo_[ok] - lo0) / res).astype(int); gj = np.round((la1 - la_[ok]) / res).astype(int)
            k = (gi >= 0) & (gi < len(lons)) & (gj >= 0) & (gj < len(lats))
            S = np.zeros((len(lats), len(lons))); N = np.zeros_like(S)
            np.add.at(S, (gj[k], gi[k]), v[ok][k]); np.add.at(N, (gj[k], gi[k]), 1)
            with np.errstate(invalid="ignore"):
                G = np.where(N > 0, S / N, np.nan)
            np.save(cf, G.astype(np.float32))
        times.append(d.isoformat()); frames.append(G)
    if not frames:
        raise RuntimeError(f"{root}: no files over the Bay since {d0}")
    return list(lats), list(lons), times, frames
MERGE_RES = 0.015


def _resample(la, lo, a, LA, LO):
    """Bilinear-ish sample of a source grid (lats N->S) onto target points; NaN where the source has no data."""
    la = np.asarray(la); lo = np.asarray(lo)
    fy = (la[0] - LA) / (la[0] - la[-1]) * (len(la) - 1); fx = (LO - lo[0]) / (lo[-1] - lo[0]) * (len(lo) - 1)
    y0 = np.clip(np.floor(fy).astype(int), 0, len(la) - 2); x0 = np.clip(np.floor(fx).astype(int), 0, len(lo) - 2)
    ty = np.clip(fy - y0, 0, 1); tx = np.clip(fx - x0, 0, 1)
    c = [a[y0, x0], a[y0, x0 + 1], a[y0 + 1, x0], a[y0 + 1, x0 + 1]]; w = [(1 - ty) * (1 - tx), (1 - ty) * tx, ty * (1 - tx), ty * tx]
    num = sum(np.where(np.isfinite(ci), ci * wi, 0) for ci, wi in zip(c, w)); den = sum(np.where(np.isfinite(ci), wi, 0) for ci, wi in zip(c, w))
    out = np.where(den > 0.3, num / np.maximum(den, 1e-9), np.nan)
    out[(fy < 0) | (fy > len(la) - 1) | (fx < 0) | (fx > len(lo) - 1)] = np.nan
    return out


def surface_flow(LA, LO):
    """Surface current (m/s) on the target grid for moving older satellite pixels: yesterday's Copernicus 1/12 deg
    model (includes wind drift), else NOAA geostrophic currents. Zero where unknown."""
    try:
        m = json.load(open(os.path.join(OUT, "model.json")))
        g = lambda k: np.array([[np.nan if v is None else v for v in r] for r in m["frames"][k]], float)
        u, v = _resample(m["lats"], m["lons"], g(0), LA, LO), _resample(m["lats"], m["lons"], g(1), LA, LO)
        src = "Copernicus model currents"
    except Exception:
        if "geo" not in OCEAN:
            return None, None, None
        (la, lo, _, gu), (_, _, _, gv) = OCEAN["geo"]
        u, v = _resample(la, lo, gu[0], LA, LO), _resample(la, lo, gv[0], LA, LO); src = "NOAA geostrophic currents"
    u = np.where(np.isfinite(u), np.clip(u, -1.5, 1.5), 0.0); v = np.where(np.isfinite(v), np.clip(v, -1.5, 1.5), 0.0)
    return u, v, src


def merged_colour(sources, var, latest, lookback=5, ndays=6):
    """Newest clear pixel from any sensor, day by day. Returns (packed frames, age-of-pixel frame, label) or (None, None, None)."""
    lats = list(np.round(np.arange(BOX["lat1"], BOX["lat0"] - 1e-9, -MERGE_RES), 4)); lons = list(np.round(np.arange(BOX["lon0"], BOX["lon1"] + 1e-9, MERGE_RES), 4))
    LO, LA = np.meshgrid(lons, lats)
    d_end = dt.date.fromisoformat(latest); d0 = d_end - dt.timedelta(days=lookback + ndays - 1)
    obs = []          # (date, rank, grid)
    used = []
    for rank, (ds, stride, label) in enumerate(sources):
        got = False
        for alt in (True, False):
            try:
                if ds.startswith("thredds:"):
                    if not alt:
                        break
                    la, lo, tt, arr = thredds_sector_frames(ds[8:], var, d0)
                else:
                    la, lo, tt, arr = erddap_frames(ds, var, f"[({d0.isoformat()}T00:00:00Z):1:(last)]", alt=alt, stride=stride, pad=0.05)
                for t, a in zip(tt, arr):
                    if np.isfinite(a).sum() > 50:
                        obs.append((dt.date.fromisoformat(t), rank, _resample(la, lo, a, LA, LO)))
                log(f"colour: {label} {len(tt)} passes, newest {tt[-1] if tt else '-'}")
                used.append(f"{label} (to {tt[-1]})"); got = True; break
            except Exception as e:
                log(f"colour: {ds} {'alt' if alt else 'no-alt'} failed: {str(e)[:90]}")
        time.sleep(1)
    if not obs:
        return None, None, None
    newest = max(max(o[0] for o in obs), dt.date.fromisoformat(latest))   # project to the SST day so every layer lines up
    dates = [newest - dt.timedelta(days=ndays - 1 - k) for k in range(ndays)]
    # older pixels are moved with the surface current to where that water should be on the target day
    U, V, flow_src = surface_flow(LA, LO)
    frames, age = [], None
    for day in dates:
        val = np.full(LA.shape, np.nan); vday = np.full(LA.shape, -1e9)
        for d, rank, g in sorted(obs, key=lambda o: (o[0], -o[1])):     # older first, finer sensor last on the same day
            a = (day - d).days
            if d > day or a > lookback:
                continue
            if a > 0 and U is not None:
                secs = a * 86400.0
                g = _resample(lats, lons, g, LA - V * secs / 111320.0, LO - U * secs / (111320.0 * np.cos(np.radians(LA))))
            ok = np.isfinite(g) & (g > 0)
            val[ok] = g[ok]; vday[ok] = d.toordinal()
        frames.append(val)
        age = np.where(vday > 0, day.toordinal() - vday, np.nan)
    cov = float(np.isfinite(frames[-1]).mean())
    log(f"colour: merged {var} to {newest}, {cov:.0%} of the box has a clear pixel within {lookback} days")
    if cov < 0.05:
        log(f"colour: {var} too cloudy to use today"); return None, None, None
    label = "Merged " + ", ".join(used) + f": newest clear pixel within {lookback} days" + (f", older pixels moved with {flow_src}" if U is not None else "") + ", 1.5 km grid"
    return pack(lats, lons, [d.isoformat() for d in dates], np.array(frames), digits=3), pack(lats, lons, [dates[-1].isoformat()], age[None], digits=0)["frames"][0], label


def best_recent(candidates, var, latest, time="[last-5:1:last]", digits=3, max_age=5):
    """Try each (dataset, label) in order; use the first whose newest day is within max_age days of the SST date.
    Near-real-time products with cloud gaps get a running composite (most recent clear pixel wins)."""
    for ds, label in candidates:
        for alt in (True, False):
            try:
                la, lo, tt, arr = erddap_frames(ds, var, time, alt=alt, pad=0.1)
                age = (dt.date.fromisoformat(latest) - dt.date.fromisoformat(tt[-1])).days
                if age > max_age:
                    log(f"ocean: {ds} newest day {tt[-1]} is {age} days behind, trying next"); break
                if "NRT" in label:
                    for k in range(1, len(arr)):
                        arr[k] = np.where(np.isfinite(arr[k]), arr[k], arr[k - 1])
                return pack(la, lo, tt, arr, digits=digits), label
            except Exception as e:
                log("ocean:", ds, "alt" if alt else "no-alt", "failed:", str(e)[:100])
    return None, None


def ocean():
    """All NOAA ERDDAP layers the app needs. Each part is independent: anything that fails keeps yesterday's copy,
    and every part carries its own dates so the app can tell how fresh it is."""
    path = os.path.join(OUT, "ocean.json")
    try:
        out = json.load(open(path))
    except Exception:
        out = {}
    out["updated"] = dt.datetime.utcnow().isoformat(timespec="minutes") + "Z"
    sst = None
    prev_mur = out.get("sst") if str(out.get("sst_label", "")).startswith("NASA JPL MUR") else None
    for ds, stride, label in [("jplMURSST41", 2, "NASA JPL MUR, 1 km daily"), ("jplMURSST41", 2, "NASA JPL MUR, 1 km daily"),
                              ("noaacwBLENDEDsstDNDaily", 1, "NOAA Geo-Polar Blended, 5 km daily (MUR server unavailable)")]:
        if ds.startswith("noaacw") and prev_mur and (dt.date.today() - dt.date.fromisoformat(prev_mur["times"][-1])).days <= 3:
            # a 1-2 day old 1 km MUR beats a fresh 5 km blend for finding breaks
            log("ocean: MUR refused; keeping the previous MUR download", prev_mur["times"][-1], "instead of the 5 km blend"); break
        try:
            lats, lons, times, sst = erddap_frames(ds, "analysed_sst", "[last-5:1:last]", stride=stride, pad=0.05)
            if np.nanmean(sst) > 200:
                sst = sst - 273.15
            out["sst_label"] = label; out["sst"] = pack(lats, lons, times, sst); OCEAN["sst"] = (lats, lons, times, sst)
            log("ocean:", label, times[0], "to", times[-1]); break
        except Exception as e:
            log("ocean: SST", ds, "failed:", str(e)[:100]); sst = None
            if ds == "jplMURSST41" and "403" in str(e):
                __import__("time").sleep(90)       # the main NOAA server rate-limits; one patient retry
    if sst is None and out.get("sst"):
        o = out["sst"]; arr = np.array([[[np.nan if v is None else v for v in r] for r in f] for f in o["frames"]], float)
        OCEAN["sst"] = (o["lats"], o["lons"], o["times"], arr); log("ocean: SST kept from previous run", o["times"][-1])
    latest = OCEAN["sst"][2][-1] if "sst" in OCEAN else dt.date.today().isoformat()
    out.pop("clim", None)   # SST vs long-term average isn't used
    try:
        g = erddap_frames("noaacwBLENDEDNRTcurrentsDaily", "u_current", "[last]", pad=0.5), erddap_frames("noaacwBLENDEDNRTcurrentsDaily", "v_current", "[last]", pad=0.5)
        OCEAN["geo"] = g; log("ocean: geostrophic currents", g[0][2][-1])
    except Exception as e:
        log("ocean: geostrophic currents failed:", str(e)[:100])
    chl, age, label = merged_colour(CHL_SOURCES, "chlor_a", latest)
    if chl:
        out["chl"], out["chl_age"], out["chl_label"] = chl, age, label
    else:
      chl, label = best_recent([("noaacwNPPN20S3ASCIDINEOF2kmDaily", "NOAA VIIRS + Copernicus Sentinel-3 OLCI, 2 km gap-filled daily"),
                              ("nesdisVHNnoaaSNPPnoaa20chlaGapfilledDaily", "NOAA VIIRS gap-filled, 9 km daily"),
                              ("noaacwN20VIIRSchlaDaily", "NOAA-20 VIIRS NRT, 4 km daily, cloud gaps filled from earlier days"),
                              ("noaacwNPPVIIRSchlaDaily", "S-NPP VIIRS NRT, 4 km daily, cloud gaps filled from earlier days")], "chlor_a", latest)
      if chl:
        out["chl"], out["chl_label"] = chl, label; out.pop("chl_age", None); log("ocean: chl", label, chl["times"][-1])
    try:
        try:
            r = erddap_frames("noaacwBLENDEDsshDaily", "sla", "[last-5:1:last]", pad=0.5); out["sla_label"] = "NOAA blended multi-mission altimetry (S-3A/B, CryoSat-2, Jason-3, SARAL), 25 km daily"
        except Exception as e:
            log("ocean: blended sla failed, trying old product:", str(e)[:80])
            r = erddap_frames("nesdisSSH1day", "sla", "[last-5:1:last]", pad=0.5); out["sla_label"] = "NOAA altimetry 25 km"
        OCEAN["sla"] = r; out["sla"] = pack(*r, digits=3); log("ocean: sla", r[2][-1])
    except Exception as e:
        log("ocean: sla failed:", str(e)[:120])
    try:
        la_, lo_, tt_, a_ = erddap_frames("noaacwLEOACSPOSSTL3SnrtCDaily", "sea_surface_temperature", "[last-2:1:last]", pad=0.02)
        if np.nanmean(a_) > 200:
            a_ = a_ - 273.15
        comp = a_[0].copy()
        for k in range(1, len(a_)):
            comp = np.where(np.isfinite(a_[k]), a_[k], comp)
        out["acspo"] = pack(la_, lo_, [tt_[-1]], comp[None], digits=2); out["acspo_label"] = f"NOAA ACSPO L3S-LEO observed SST, 2 km, {tt_[0]} to {tt_[-1]}"
        log(f"ocean: ACSPO observed SST {tt_[-1]}, {np.isfinite(comp).mean():.0%} clear")
    except Exception as e:
        log("ocean: ACSPO failed:", str(e)[:100])
    kd, kage, label = merged_colour(KD_SOURCES, "kd_490", latest)
    if kd:
        k = kd; kd = {"lats": k["lats"], "lons": k["lons"], "times": k["times"][-1:], "frames": k["frames"][-1:]}
        out["kd"], out["kd_label"], out["kd_age"] = kd, label, kage
    for var in (() if kd or out.get("chl") else ("kd_490",)):   # stale gap-filled products only if there is no chlorophyll to estimate from
        kd, label = best_recent([("noaacwNPPN20S3AkdSCIDINEOF2kmDaily", "VIIRS + Sentinel-3 OLCI Kd490, 2 km"),
                                 ("nesdisNPPN20S3AkdSCIDINEOFDaily", "VIIRS + Sentinel-3 OLCI Kd490, 9 km")], var, latest, time="[last]", digits=4, max_age=7)
        if kd:
            out["kd"], out["kd_label"] = kd, label; log("ocean: kd", label); break
    # fill clarity gaps (or all of it) from the 4-satellite chlorophyll: in open ocean, clarity is set by plankton,
    # Kd490 = 0.0166 + 0.0773 * Chl^0.6715 (Morel et al. 2007). Direct Kd490 readings always win where they exist.
    if out.get("chl"):
        C = np.array([[np.nan if v is None else v for v in row] for row in out["chl"]["frames"][-1]], dtype=float)
        E = 0.0166 + 0.0773 * np.power(np.clip(C, 0.01, 60), 0.6715); E[~np.isfinite(C)] = np.nan
        CA = np.array([[np.nan if v is None else v for v in row] for row in out["chl_age"]], dtype=float) if out.get("chl_age") else np.zeros_like(C)
        same_grid = kd and out.get("kd_age") is not None and kd["lats"] == out["chl"]["lats"] and kd["lons"] == out["chl"]["lons"]
        if same_grid:
            K = np.array([[np.nan if v is None else v for v in row] for row in kd["frames"][-1]], dtype=float)
            KA = np.array([[np.nan if v is None else v for v in row] for row in out["kd_age"]], dtype=float)
            direct = np.isfinite(K); fill = ~direct & np.isfinite(E)
            K[fill] = E[fill]; KA[fill] = CA[fill]
            out["kd"] = pack(out["chl"]["lats"], out["chl"]["lons"], kd["times"], K[None], digits=4); out["kd_age"] = pack(out["chl"]["lats"], out["chl"]["lons"], kd["times"], KA[None], digits=0)["frames"][0]
            out["kd_label"] = label + f"; {fill.mean():.0%} of the box estimated from chlorophyll where clouds hid the clarity passes"
            log(f"ocean: clarity {direct.mean():.0%} direct, {fill.mean():.0%} estimated from chlorophyll")
        elif not kd:
            out["kd"] = pack(out["chl"]["lats"], out["chl"]["lons"], out["chl"]["times"][-1:], E[None], digits=4); out["kd_age"] = out.get("chl_age")
            out["kd_label"] = "Estimated from the 4-satellite chlorophyll (open-ocean plankton relationship); no direct clarity pass got through the cloud"
            kd = out["kd"]; log(f"ocean: clarity estimated from chlorophyll, {np.isfinite(E).mean():.0%} of the box")
    if not kd:
        for k in ("kd", "kd_label", "kd_age"):
            out.pop(k, None)
        log("ocean: no usable clarity data today")
    if not out.get("sst"):
        raise RuntimeError("no SST at all")
    with open(path, "w") as f:
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
                r = requests.get(base, params=q, timeout=180); r.raise_for_status(); break
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
    if "geo" in OCEAN:
        (gla, glo, gt, gu), (_, _, _, gv) = OCEAN["geo"]
        gla = np.array(gla); glo = np.array(glo)
        def gat(a, b, F):
            j = np.abs(gla - a).argmin(); i = np.abs(glo - b).argmin(); v = F[0][j, i]
            return float(v) if np.isfinite(v) else None
    sla_fresh = "geo" not in OCEAN and "sla" in OCEAN and "sst" in OCEAN and (dt.date.fromisoformat(OCEAN["sst"][2][-1]) - dt.date.fromisoformat(OCEAN["sla"][2][-1])).days <= 10
    if not sla_fresh:
        log("currents: using NOAA blended geostrophic currents" if "geo" in OCEAN else "currents: no altimetry, Ekman drift only")
    if sla_fresh:
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
        if "geo" in OCEAN:
            ugv, vgv = gat(plat, plon, gu), gat(plat, plon, gv)
            if ugv is None or vgv is None or np.hypot(ugv, vgv) > 1.5:
                ugv = vgv = None
        elif geo:
            ugv, vgv = geo[2](plat, plon, geo[0]), geo[2](plat, plon, geo[1])
            if ugv is None or vgv is None or np.hypot(ugv, vgv) > 1.5:
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
        json.dump({"source": "NOAA blended geostrophic currents + Ekman drift from ECMWF IFS 0.25 deg wind", "updated": dt.datetime.utcnow().isoformat(timespec="minutes") + "Z",
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
    bay_rise = float(np.nanmedian(last - first))       # warming everywhere isn't news; warming more than the bay is
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
        if t >= 18.5 and rise >= 0.8 and rise - bay_rise >= 0.4:
            why.append(f"warmed {rise:+.1f} C in 5 days ({rise - bay_rise:+.1f} C more than the bay)")
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


# ---------------------------------------------------------------- high-res model currents + dynamic sea level
def model_grid():
    """Copernicus Mercator 1/12 deg surface currents and sea level, 24 h mean (removes the tide), on a 0.08 deg grid.
    Used for detailed current arrows, convergence/upwelling zones and eddies."""
    pts = ocean_points(0.08, margin=0.04, min_depth=8)
    log("model_grid:", len(pts), "points")
    R = openmeteo(MARINE, pts, {"hourly": "ocean_current_velocity,ocean_current_direction,sea_level_height_msl", "past_days": 1,
                                "forecast_days": 1, "timezone": "UTC", "wind_speed_unit": "ms"}, chunk=50)
    lats = sorted({p[0] for p in pts}, reverse=True); lons = sorted({p[1] for p in pts})
    L = {v: k for k, v in enumerate(lats)}; O = {v: k for k, v in enumerate(lons)}
    U, V, E = (np.full((len(lats), len(lons)), np.nan) for _ in range(3))
    now = dt.datetime.utcnow()
    for (la, lo), d in zip(pts, R):
        H = d.get("hourly") or {}
        us, vs, es = [], [], []
        for t, sp, di, h in zip(H.get("time", []), H.get("ocean_current_velocity", []), H.get("ocean_current_direction", []), H.get("sea_level_height_msl", [])):
            tt = dt.datetime.fromisoformat(t)
            if not (now - dt.timedelta(hours=25) <= tt <= now):
                continue
            if sp is not None and di is not None:
                s = sp
                us.append(s * np.sin(np.radians(di))); vs.append(s * np.cos(np.radians(di)))   # direction the current flows toward
            if h is not None:
                es.append(h)
        if len(us) >= 12:
            U[L[la], O[lo]] = np.mean(us); V[L[la], O[lo]] = np.mean(vs)
        if len(es) >= 20:
            E[L[la], O[lo]] = np.mean(es)
    E = E - np.nanmean(E)
    spd = np.nanmedian(np.hypot(U, V))
    if spd > 1.2:            # API answered in km/h despite the unit request
        U /= 3.6; V /= 3.6
    log(f"model_grid: median current {np.nanmedian(np.hypot(U, V)):.2f} m/s")
    ok = np.isfinite(U).mean()
    if ok < 0.3:
        raise RuntimeError(f"model grid too sparse ({ok:.0%})")
    out = {"source": "Copernicus Mercator 1/12 deg (SMOC) via Open-Meteo, 24 h mean", "updated": dt.datetime.utcnow().isoformat(timespec="minutes") + "Z",
           **pack(lats, lons, ["u", "v", "eta"], np.array([U, V, E]), digits=3)}
    with open(os.path.join(OUT, "model.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
    log(f"model_grid: wrote {len(lats)}x{len(lons)}, {ok:.0%} filled")


# ---------------------------------------------------------------- full-detail survey tiles (10 m contours when zoomed in)
TILE_DEG, TILE_RES = 0.25, 0.0004      # 625 x 625 px tiles, ~35 x 44 m cells


def bathy_tiles():
    """One-time job: NIWA survey at ~40 m in 0.25 deg PNG tiles, loaded by the app only when zoomed in."""
    import requests, io
    from PIL import Image
    tdir = os.path.join(OUT, "tiles"); idx = os.path.join(tdir, "index.json")
    if os.path.exists(idx):
        log("bathy_tiles: already saved, skipping"); return
    os.makedirs(tdir, exist_ok=True)
    meta = json.load(open(os.path.join(OUT, "bathy_hd.json")))
    px = np.array(Image.open(os.path.join(OUT, "bathy_hd.png")).convert("RGB")).astype(np.int32)
    base = ((px[..., 0] << 8) | px[..., 1]) - meta["offset"]; srcmask = px[..., 2] > 127
    n = int(round(TILE_DEG / TILE_RES))
    nti = int(np.ceil((BOX["lon1"] - BOX["lon0"]) / TILE_DEG)); ntj = int(np.ceil((BOX["lat1"] - BOX["lat0"]) / TILE_DEG))
    url = f"{NIWA}/Bay_of_Plenty_25m_DTM/ImageServer/exportImage"
    tiles = []
    for tj in range(ntj):
        for ti in range(nti):
            w = BOX["lon0"] + ti * TILE_DEG; nlat = BOX["lat1"] - tj * TILE_DEG
            # only where the base grid says NIWA has data and there's some sea
            bi0 = int((w - meta["lon0"]) / meta["rx"]); bj0 = int((meta["lat1"] - nlat) / meta["ry"])
            sub = srcmask[max(0, bj0):bj0 + int(TILE_DEG / meta["ry"]), max(0, bi0):bi0 + int(TILE_DEG / meta["rx"])]
            sea = base[max(0, bj0):bj0 + int(TILE_DEG / meta["ry"]), max(0, bi0):bi0 + int(TILE_DEG / meta["rx"])] > 0
            if sub.size == 0 or sub.mean() < 0.05 or sea.mean() < 0.02:
                continue
            q = {"bbox": f"{w},{nlat - TILE_DEG},{w + TILE_DEG},{nlat}", "bboxSR": 4326, "imageSR": 4326, "size": f"{n},{n}",
                 "format": "tiff", "pixelType": "F32", "interpolation": "RSP_BilinearInterpolation", "f": "image"}
            try:
                r = requests.get(url, params=q, timeout=180); r.raise_for_status()
                import tifffile
                arr = tifffile.imread(io.BytesIO(r.content)).astype(np.float32)
                if arr.ndim == 3:
                    arr = arr[..., 0]
            except Exception as e:
                log("bathy_tiles:", ti, tj, "failed", str(e)[:100]); continue
            lon_c = w + (np.arange(n) + 0.5) * TILE_RES; lat_c = nlat - (np.arange(n) + 0.5) * TILE_RES
            fi = np.clip(((lon_c - meta["lon0"]) / meta["rx"]).round().astype(int), 0, meta["nx"] - 1)
            fj = np.clip(((meta["lat1"] - lat_c) / meta["ry"]).round().astype(int), 0, meta["ny"] - 1)
            fill = base[np.ix_(fj, fi)].astype(np.float32)               # depth from the base grid where the survey has gaps
            ok = np.isfinite(arr) & (np.abs(arr) < 12000) & (arr != 0)
            depth = np.where(ok, -arr, fill)
            v = np.clip(np.round(depth) + 20000, 0, 65535).astype(np.uint32)
            rgb = np.stack([(v >> 8).astype(np.uint8), (v & 255).astype(np.uint8), np.zeros_like(v, dtype=np.uint8)], axis=-1)
            Image.fromarray(rgb, "RGB").save(os.path.join(tdir, f"{ti}_{tj}.png"), optimize=True)
            tiles.append([ti, tj]); log(f"bathy_tiles: {ti}_{tj} ok ({ok.mean():.0%} survey)")
            time.sleep(0.5)
    with open(idx, "w") as f:
        json.dump({"source": "NIWA Bay of Plenty 25 m survey", "lon0": BOX["lon0"], "lat1": BOX["lat1"], "deg": TILE_DEG, "res": TILE_RES, "n": n,
                   "offset": 20000, "tiles": tiles}, f)
    log("bathy_tiles: wrote", len(tiles), "tiles")


# ---------------------------------------------------------------- charted dumping grounds (the "Ammo Dump")
DUMP_CAT = {"1": "Chemical waste dumping ground", "2": "Nuclear waste dumping ground", "3": "Explosives dumping ground", "4": "Spoil ground", "5": "Vessel dumping ground", "6": "Dumping ground"}


def dumping():
    """LINZ chart dumping-ground polygons (S-57 DMPGRD) in the box, via public ArcGIS mirrors of the LINZ layers."""
    import requests
    q = {"where": "1=1", "geometry": f"{BOX['lon0']},{BOX['lat0']},{BOX['lon1']},{BOX['lat1']}", "geometryType": "esriGeometryEnvelope", "inSR": 4326,
         "spatialRel": "esriSpatialRelIntersects", "outFields": "*", "outSR": 4326, "f": "geojson"}
    items = []
    for url in ("https://services3.arcgis.com/fp1tibNcN9mbExhG/ArcGIS/rest/services/Marine_dumping_grounds/FeatureServer/430/query",
                "https://gis.niwa.co.nz/server/rest/services/Dredge_disposal_areas/MapServer/287/query"):
        try:
            g = requests.get(url, params=q, timeout=90).json()
        except Exception as e:
            log("dumping:", url.split("/")[2], "failed", str(e)[:80]); continue
        for ft in g.get("features", []):
            pr = {k.lower(): v for k, v in (ft.get("properties") or {}).items()}; geom = ft.get("geometry") or {}
            polys = geom.get("coordinates") or []
            if geom.get("type") == "Polygon":
                polys = [polys]
            rings = [[[round(x, 5), round(y, 5)] for x, y in ring] for poly in polys for ring in poly]
            if not rings:
                continue
            cat = str(pr.get("catdpg") or "").strip()[:1]
            items.append({"cat": DUMP_CAT.get(cat, pr.get("type1") or "Dumping ground"), "name": pr.get("objnam") or pr.get("nobjnm") or pr.get("location") or "",
                          "info": " ".join(str(pr.get(k) or "") for k in ("inform", "ninfom", "txtdsc")).strip(), "status": pr.get("status"), "rings": rings})
        if items:
            log(f"dumping: {len(items)} areas from {url.split('/')[2]}: " + "; ".join(f"{i['cat']} {i['name']}".strip() for i in items)); break
    if not items:
        raise RuntimeError("no dumping grounds found")
    with open(os.path.join(OUT, "dumping.json"), "w") as f:
        json.dump({"source": "LINZ nautical chart dumping grounds (S-57 DMPGRD)", "items": items}, f, separators=(",", ":"))


# ---------------------------------------------------------------- sharp SST: raw VIIRS passes at native 750 m
L2P_SATS = ("N20", "N21")                     # S-NPP stopped producing L2P in 2026
SHARP_RES = 0.0075                            # ~750 m, the VIIRS native pixel
SHARP_HOURS = 36                              # passes older than this aren't used
L2P_CACHE = os.environ.get("L2P_CACHE", os.path.join(os.path.dirname(__file__), "..", ".cache", "l2p"))


def _nanbox(a, r):
    """NaN-aware box mean"""
    ok = np.isfinite(a)
    s = _box(np.where(ok, a, 0.0), r); c = _box(ok.astype(np.float64), r)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(c > 0.05, s / c, np.nan)


def sharp():
    """Raw VIIRS swath passes (ACSPO L2P, native 750 m) from NOAA-20 and NOAA-21, cut to the Bay over OPeNDAP.
    Newest clear pixel wins (36 h window); each pass is levelled to MUR at 30 km scale so day and night passes
    don't seam, while keeping its fine detail. Cloud gaps are filled from MUR. Fronts are traced on the sharp field."""
    import netCDF4, re, requests
    from PIL import Image
    with open(os.path.expanduser("~/.dodsrc"), "w") as f:            # no OPeNDAP call may hang the job
        f.write("HTTP.TIMEOUT=60\nHTTP.CONNECTTIMEOUT=15\n")
    os.makedirs(L2P_CACHE, exist_ok=True)
    for fn in os.listdir(L2P_CACHE):                                  # drop cached passes older than 3 days
        if time.time() - os.path.getmtime(os.path.join(L2P_CACHE, fn)) > 3 * 86400:
            os.remove(os.path.join(L2P_CACHE, fn))
    idx_path = os.path.join(OUT, "l2p_index.json")
    try:
        idx = json.load(open(idx_path))
    except Exception:
        idx = {}
    now = dt.datetime.utcnow()
    nx, ny = int(round((BOX["lon1"] - BOX["lon0"]) / SHARP_RES)), int(round((BOX["lat1"] - BOX["lat0"]) / SHARP_RES))
    passes = []
    t_start = time.time()
    for sat in L2P_SATS:
        for back in (0, 1, 2):
            d = now - dt.timedelta(days=back)
            if back == 2 and d < now - dt.timedelta(hours=SHARP_HOURS + 24):
                continue
            path = f"swath{sat}VIIRSNRTL2PWW00/{d:%Y}/{d.timetuple().tm_yday:03d}"
            try:
                rq = requests.get(f"https://coastwatch.noaa.gov/thredds/catalog/{path}/catalog.html", timeout=60); rq.raise_for_status()
                files = sorted(set(re.findall(r"(\d{14}-STAR-L2P[^'\"<>]*?\.nc)", rq.text)))
            except Exception as e:                                    # server down: use the passes already gridded on earlier runs
                stamp = f"{d:%Y%m%d}"
                files = sorted(fn[len(sat) + 1:-4] for fn in os.listdir(L2P_CACHE) if fn.startswith(f"{sat}_{stamp}") and fn.endswith(".npy"))
                log("sharp:", sat, d.date(), f"catalog unavailable ({str(e)[:40]}), using {len(files)} cached passes")
            for fn in files:
                t = dt.datetime.strptime(fn[:14], "%Y%m%d%H%M%S")
                if (now - t).total_seconds() > SHARP_HOURS * 3600 or t.hour not in (23, 0, 1, 2, 3, 11, 12, 13, 14, 15):
                    continue                                              # NZ day passes ~23-03 UTC, night ~11-15 UTC
                key = f"{sat}/{fn[:14]}"
                url = f"https://coastwatch.noaa.gov/thredds/dodsC/{path}/{fn}"
                info = idx.get(key, "new")
                if info is None:
                    continue                                              # scanned before, misses the Bay
                cf = os.path.join(L2P_CACHE, key.replace("/", "_") + ".npy")
                if info != "new" and os.path.exists(cf):                  # gridded on an earlier run (GitHub Actions cache)
                    G = np.load(cf)
                    passes.append({"sat": sat, "t": t, "grid": G, "night": t.hour in (11, 12, 13, 14, 15)}); continue
                if time.time() - t_start > 9 * 60:
                    log("sharp: time budget used"); break
                try:
                    nc = netCDF4.Dataset(url)
                    if info == "new":
                        st = 48
                        la = np.ma.filled(nc.variables["lat"][::st, ::st], np.nan); lo = np.ma.filled(nc.variables["lon"][::st, ::st], np.nan)
                        m = (la > BOX["lat0"] - .3) & (la < BOX["lat1"] + .3) & (lo > BOX["lon0"] - .3) & (lo < BOX["lon1"] + .3)
                        if not m.any():
                            idx[key] = None; nc.close(); continue
                        jj, ii = np.where(m); R, C = nc.variables["lat"].shape
                        info = [int(max(0, (jj.min() - 1) * st)), int(min(R, (jj.max() + 2) * st)), int(max(0, (ii.min() - 1) * st)), int(min(C, (ii.max() + 2) * st))]
                        idx[key] = info
                    r0, r1, c0, c1 = info
                    la = np.ma.filled(nc.variables["lat"][r0:r1, c0:c1], np.nan); lo = np.ma.filled(nc.variables["lon"][r0:r1, c0:c1], np.nan)
                    sst = np.ma.filled(nc.variables["sea_surface_temperature"][0, r0:r1, c0:c1].astype(np.float64), np.nan)
                    bias = np.ma.filled(nc.variables["sses_bias"][0, r0:r1, c0:c1].astype(np.float64), 0.0)
                    ql = np.ma.filled(nc.variables["quality_level"][0, r0:r1, c0:c1], 0)
                    nc.close()
                except Exception as e:
                    log("sharp:", key, "failed", str(e)[:80]); continue
                good = (ql >= 4) & np.isfinite(sst)
                if good.sum() < 200:
                    idx[key] = None; continue                             # all cloud over the Bay: never re-fetch
                good &= _box(good.astype(np.float64), 1) > 0.99           # drop pixels touching cloud: edges are often contaminated
                if good.sum() < 200:
                    continue
                v = sst[good] - bias[good] - 273.15
                gi = np.round((lo[good] - BOX["lon0"]) / SHARP_RES).astype(int); gj = np.round((BOX["lat1"] - la[good]) / SHARP_RES).astype(int)
                k = (gi >= 0) & (gi < nx) & (gj >= 0) & (gj < ny) & (v > 5) & (v < 32)
                if k.sum() < 200:
                    continue
                S = np.zeros((ny, nx)); N = np.zeros((ny, nx))
                np.add.at(S, (gj[k], gi[k]), v[k]); np.add.at(N, (gj[k], gi[k]), 1)
                with np.errstate(invalid="ignore"):
                    G = np.where(N > 0, S / N, np.nan)
                passes.append({"sat": sat, "t": t, "grid": G, "night": t.hour in (11, 12, 13, 14, 15)})
                np.save(cf, G.astype(np.float32))
                log(f"sharp: {sat} {t:%d %H:%M}Z {np.isfinite(G).mean():.0%} of the Bay clear")
    # forget scans older than 4 days
    cutoff = (now - dt.timedelta(days=4)).strftime("%Y%m%d%H%M%S")
    idx = {k: v for k, v in idx.items() if k.split("/")[1] >= cutoff}
    with open(idx_path, "w") as f:
        json.dump(idx, f, separators=(",", ":"))
    if not passes:
        raise RuntimeError("no clear VIIRS passes over the Bay in the last 36 h")
    # MUR background on the sharp grid
    o = json.load(open(os.path.join(OUT, "ocean.json")))["sst"]
    mla, mlo = np.array(o["lats"]), np.array(o["lons"]); M = np.array(o["frames"][-1], dtype=np.float64)
    gla = BOX["lat1"] - (np.arange(ny) + 0.5) * SHARP_RES; glo = BOX["lon0"] + (np.arange(nx) + 0.5) * SHARP_RES
    fj = np.interp(-gla, -mla, np.arange(len(mla))); fi = np.interp(glo, mlo, np.arange(len(mlo)))
    j0 = np.clip(np.floor(fj).astype(int), 0, len(mla) - 2); i0 = np.clip(np.floor(fi).astype(int), 0, len(mlo) - 2)
    wj = (fj - j0)[:, None]; wi = (fi - i0)[None, :]
    Mi = (M[j0][:, i0] * (1 - wj) * (1 - wi) + M[j0 + 1][:, i0] * wj * (1 - wi) + M[j0][:, i0 + 1] * (1 - wj) * wi + M[j0 + 1][:, i0 + 1] * wj * wi)
    Mi = np.where(np.isfinite(Mi), Mi, _nanbox(Mi, 3))
    # newest first; each pass levelled to MUR at ~30 km so passes join without seams
    passes.sort(key=lambda p: p["t"], reverse=True)
    out = np.full((ny, nx), np.nan); age = np.full((ny, nx), 255.0); used = []
    for p in passes:
        G = p["grid"]; G = np.where(np.isfinite(G), G, _nanbox(G, 1)) if np.isfinite(G).mean() > 0.02 else G   # fill single-pixel holes
        off = _nanbox(G - Mi, 20); off = np.where(np.isfinite(off), off, np.nanmedian(G - Mi))
        Gc = G - off
        take = np.isfinite(Gc) & ~np.isfinite(out)
        out[take] = Gc[take]; age[take] = (now - p["t"]).total_seconds() / 3600
        if take.mean() > 0.002:
            used.append({"sat": p["sat"], "time": p["t"].strftime("%Y-%m-%dT%H:%MZ"), "night": p["night"], "share": round(float(take.mean()), 3)})
    obs = np.isfinite(out)
    wgt = _box(obs.astype(np.float64), 2)                               # feather cloud edges into MUR
    final = np.where(obs, out, Mi); final = wgt * np.where(obs, out, _nanbox(out, 3)) + (1 - wgt) * Mi
    final = np.where(np.isfinite(final), final, Mi)
    land = ~np.isfinite(Mi)
    # fronts on the sharp field, observed water only: gradient ridges (non-max suppression across the front)
    fs = np.where(land, np.nan, final); fs = np.where(np.isfinite(fs), _nanbox(fs, 1), np.nan)   # 3x3 smoothing: sensor noise isn't a front
    fs = np.where(np.isfinite(fs), fs, Mi)
    gy, gx = np.gradient(fs, SHARP_RES * 111.32, SHARP_RES * 111.32 * np.cos(np.radians(-37)))
    gy = -gy; g = np.hypot(gx, gy)
    g[land | ~obs] = 0
    thr = max(0.12, float(np.nanpercentile(g[obs & ~land], 95))) if (obs & ~land).sum() > 100 else 9
    ang = np.arctan2(gy, gx); di = np.round(np.cos(ang)).astype(int); dj = -np.round(np.sin(ang)).astype(int)
    J, I = np.mgrid[0:ny, 0:nx]
    a = g[np.clip(J + dj, 0, ny - 1), np.clip(I + di, 0, nx - 1)]; b = g[np.clip(J - dj, 0, ny - 1), np.clip(I - di, 0, nx - 1)]
    ridge = (g >= thr) & (g >= a) & (g >= b)
    # keep only continuous lines (8-connected, at least 7 pixels = ~5 km); isolated specks are noise or pass seams
    lab = np.zeros(ridge.shape, np.int32); keep = np.zeros_like(ridge); n_lab = 0
    for sj, si in zip(*np.where(ridge)):
        if lab[sj, si]:
            continue
        n_lab += 1; stack = [(sj, si)]; lab[sj, si] = n_lab; comp = []
        while stack:
            j, i = stack.pop(); comp.append((j, i))
            for dj_ in (-1, 0, 1):
                for di_ in (-1, 0, 1):
                    y, x = j + dj_, i + di_
                    if 0 <= y < ny and 0 <= x < nx and ridge[y, x] and not lab[y, x]:
                        lab[y, x] = n_lab; stack.append((y, x))
        if len(comp) >= 7:
            for j, i in comp:
                keep[j, i] = True
    fj_, fi_ = np.where(keep)
    fronts = [[round(float(gla[j]), 4), round(float(glo[i]), 4), round(float(g[j, i]), 3)] for j, i in zip(fj_, fi_)]
    v = np.clip(np.round((np.where(land, 0, final) + 5) * 100), 0, 65535).astype(np.uint32)
    code = np.where(land, 255, np.where(obs, np.clip(np.round(age), 1, 254), 0)).astype(np.uint8)
    Image.fromarray(np.stack([(v >> 8).astype(np.uint8), (v & 255).astype(np.uint8), code], -1), "RGB").save(os.path.join(OUT, "sharp.png"), optimize=True)
    with open(os.path.join(OUT, "sharp.json"), "w") as f:
        json.dump({"updated": now.strftime("%Y-%m-%dT%H:%MZ"), "res": SHARP_RES, "lon0": BOX["lon0"], "lat1": BOX["lat1"], "nx": nx, "ny": ny,
                   "observed": round(float(obs[~land].mean()), 3), "passes": used, "front_threshold": round(thr, 3), "fronts": fronts[:30000]}, f, separators=(",", ":"))
    log(f"sharp: {obs[~land].mean():.0%} of the sea from raw passes, {len(used)} passes, {len(fronts)} front pixels (>{thr:.2f} C/km)")


# ---------------------------------------------------------------- true colour: clearest recent VIIRS photo
TC_SATS = ("NOAA21", "NOAA20", "SNPP")


def _box(a, r):
    """mean over a (2r+1)^2 box, numpy only"""
    p = np.pad(a, r, mode="edge").cumsum(0).cumsum(1); p = np.pad(p, ((1, 0), (1, 0)))
    k = 2 * r + 1
    return (p[k:, k:] - p[:-k, k:] - p[k:, :-k] + p[:-k, :-k]) / (k * k)


def _tc_enhance(out):
    """contrast stretch over open water only, so colour edges show; land and cloud left as photographed"""
    m = out.mean(2)
    water = (m < 55) & (out[..., 2] >= out[..., 1])
    sea = _box(water.astype(np.float32), 2)                          # soft mask: dark, blue-dominated open water (not land or cloud)
    seapix = out[water]
    if len(seapix) < 1000:
        return out
    lo = np.percentile(seapix, 1, axis=0); hi = np.percentile(seapix, 99.5, axis=0)
    st = np.clip((out - lo) / (hi - lo + 1e-6), 0, 1) ** 0.9 * 190 + 8
    return out * (1 - sea[..., None]) + st * sea[..., None]


def truecolour():
    """NASA GIBS VIIRS corrected-reflectance tiles (plate carree, so they line up with the map) for the last 3 days
    from all three VIIRS satellites. Each pixel is scored for cloud/glare; the newest day wins wherever it is clear,
    and within a day the clearest satellite is used. Older days only fill what the newest day has under cloud."""
    import requests, io
    from PIL import Image
    z, res = 8, 0.5625 / 2 ** 8                       # level 8: 0.0022 deg per pixel (~240 m), VIIRS native 375 m
    span = 512 * res
    c0, c1 = int((BOX["lon0"] + 180) // span), int((BOX["lon1"] + 180) // span)
    r0, r1 = int((90 - BOX["lat1"]) // span), int((90 - BOX["lat0"]) // span)
    W, H = (c1 - c0 + 1) * 512, (r1 - r0 + 1) * 512
    x0, y0 = int(round((BOX["lon0"] + 180 - c0 * span) / res)), int(round((90 - BOX["lat1"] - r0 * span) / res))
    w, h = int(round((BOX["lon1"] - BOX["lon0"]) / res)), int(round((BOX["lat1"] - BOX["lat0"]) / res))
    nz_today = (dt.datetime.utcnow() + dt.timedelta(hours=13)).date()
    days = [nz_today - dt.timedelta(days=k) for k in range(3)]
    shots = []
    for day in days:
        for sat in TC_SATS:
            img = np.zeros((H, W, 3), np.uint8); got = 0
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    u = f"https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/VIIRS_{sat}_CorrectedReflectance_TrueColor/default/{day}/250m/{z}/{r}/{c}.jpg"
                    try:
                        rq = requests.get(u, timeout=60)
                        if rq.status_code != 200 or not rq.content:
                            continue
                        t = np.asarray(Image.open(io.BytesIO(rq.content)).convert("RGB"))
                    except Exception:
                        continue
                    img[(r - r0) * 512:(r - r0 + 1) * 512, (c - c0) * 512:(c - c0 + 1) * 512] = t; got += 1
            img = img[y0:y0 + h, x0:x0 + w].astype(np.float32)
            if not got:
                continue
            mean = img.mean(2); sat_ = img.max(2) - img.min(2)
            nodata = img.max(2) <= 3                                      # outside the swath: pure black (clear deep ocean is dark but not 0)
            # clouds and sun glare are bright and grey; sea is dark. Smooth so choices come in patches, not speckle
            bad = np.clip((mean - 70) / 60, 0, 1) * np.clip(1.2 - sat_ / 90, 0.2, 1)
            bad = _box(bad, 4); bad[nodata] = 9
            clear = float(((bad < 0.25) & ~nodata).mean())
            log(f"truecolour: {sat} {day} {got} tiles, {clear:.0%} clear")
            if clear > 0.02:
                shots.append({"day": day, "sat": sat, "img": img, "bad": bad, "clear": clear})
    if not shots:
        raise RuntimeError("no VIIRS true colour in the last 3 days")
    # base: the best single photo of the newest day (most cloud-free, least sun glare on the clear sea);
    # then fill only its cloud gaps from the other photos, colour-matched to the base and feathered, so no patchwork
    newest_day = max(s["day"] for s in shots)
    for s in shots:
        good = (s["bad"] < 0.3)
        s["good"] = good
        s["glare"] = float(s["img"].mean(2)[good].mean()) if good.any() else 255.0
        s["q"] = s["clear"] - max(0.0, s["glare"] - 15) / 120 - 0.5 * (newest_day - s["day"]).days
    shots.sort(key=lambda s: -s["q"])
    base = shots[0]
    out = base["img"].copy(); alpha = _box(base["good"].astype(np.float32), 3); src = np.zeros((h, w), np.float32)
    for k, s in enumerate(shots[1:], 1):
        need = 1 - alpha
        if need.mean() < 0.01:
            break
        both = base["good"] & s["good"]
        img = s["img"].copy()
        if both.sum() > 500:                                          # match brightness/colour to the base where both are clear
            for ch in range(3):
                x, y = img[..., ch][both], out[..., ch][both]
                a = np.clip(np.cov(x, y)[0, 1] / (x.var() + 1e-6), 0.6, 1.6); b = y.mean() - a * x.mean()
                img[..., ch] = a * img[..., ch] + b
        wgt = need * _box(s["good"].astype(np.float32), 3)
        out = out * (1 - wgt[..., None]) + img * wgt[..., None]
        alpha = alpha + wgt * (1 - alpha); src = np.where(wgt > 0.5, k, src)
        s["share"] = float((wgt > 0.5).mean())
    base["share"] = float((src == 0).mean())
    Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).save(os.path.join(OUT, "truecolour_natural.jpg"), quality=86, optimize=True)
    Image.fromarray(np.clip(_tc_enhance(out), 0, 255).astype(np.uint8)).save(os.path.join(OUT, "truecolour.jpg"), quality=86, optimize=True)
    used = [{"day": str(s["day"]), "sat": s["sat"], "share": round(s.get("share", 0.0), 3), "clear": round(s["clear"], 3)} for s in shots]
    used = [u for u in used if u["share"] > 0.005]
    newest = max(u["day"] for u in used)
    with open(os.path.join(OUT, "truecolour.json"), "w") as f:
        json.dump({"updated": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%MZ"), "newest": newest, "lon0": BOX["lon0"], "lon1": BOX["lon0"] + w * res,
                   "lat1": BOX["lat1"], "lat0": BOX["lat1"] - h * res, "clear": round(float((alpha > 0.5).mean()), 3), "sources": used}, f)
    log("truecolour: mosaic from " + ", ".join(f"{u['sat']} {u['day']} {u['share']:.0%}" for u in used))


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    ok = 0
    # most important first; slow model downloads are skipped if the morning budget runs out
    only = set(sys.argv[1:])
    for job in (bathy, bathy_hd, bathy_tiles, ocean, alerts, himawari, sharp, truecolour, reserves, dumping, currents, tides, model_grid, forecast):
        if only and job.__name__ not in only:
            continue
        if job in (model_grid, forecast) and time.time() - T0 > 25 * 60:
            print(f"{job.__name__} skipped: time budget used, keeping yesterday's file", file=sys.stderr); continue
        try:
            job(); ok += 1
        except Exception as e:
            print(f"{job.__name__} failed: {e}", file=sys.stderr)
    sys.exit(0 if ok else 1)
