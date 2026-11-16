import netCDF4, numpy as np, re, requests
B = "https://coastwatch.noaa.gov/thredds"
for sat in ("n20", "n21"):
    root = f"kd490VIIRS{sat}SectorDailyZX00"; path = ""
    for depth in range(4):
        t = requests.get(f"{B}/catalog/{root}/{path}catalog.html", timeout=60).text
        subs = sorted(set(re.findall(r"href=['\"]([0-9A-Za-z_]+)/catalog\.html", t)))
        files = sorted(set(re.findall(r"([A-Za-z0-9_.\-]+\.nc)", t)))
        print(sat, path or "/", "subs", subs[-4:], "files", files[-3:])
        if files:
            f = files[-1]; u = f"{B}/dodsC/{root}/{path}{f}"
            try:
                nc = netCDF4.Dataset(u); print("OK", u); print({k: v.shape for k, v in nc.variables.items()})
                for k in ("lat", "latitude", "lon", "longitude"):
                    if k in nc.variables: v = nc.variables[k][:]; print(k, float(v.min()), float(v.max()), v.shape)
                for k, v in nc.variables.items():
                    if "kd" in k.lower(): a = np.ma.filled(v[:].astype(float), np.nan); print(k, {x: getattr(v, x) for x in v.ncattrs()}, "finite", np.isfinite(a).mean())
            except Exception as e:
                print("FAIL", u, str(e)[:160])
            break
        if not subs: break
        path += subs[-1] + "/"
