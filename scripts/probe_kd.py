import netCDF4, numpy as np
for sat in ("n20", "n21"):
    for u in (f"https://coastwatch.noaa.gov/thredds/dodsC/CoastWatch/VIIRS/{sat}/kd490/DailySectorAgg/ZX00",):
        try:
            nc = netCDF4.Dataset(u)
            print("OK", u); print({k: v.shape for k, v in nc.variables.items()})
            t = nc.variables["time"]; print("time units", t.units, "last", netCDF4.num2date(t[-3:], t.units))
            for k in ("lat", "latitude", "rows", "y"):
                if k in nc.variables: print(k, nc.variables[k][:3], nc.variables[k][-3:])
            for k in ("lon", "longitude", "cols", "x"):
                if k in nc.variables: print(k, nc.variables[k][:3], nc.variables[k][-3:])
            print({k: {a: getattr(v, a) for a in v.ncattrs()} for k, v in nc.variables.items() if k.lower().startswith("kd")})
        except Exception as e:
            print("FAIL", u, str(e)[:200])
