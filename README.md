# Bluewater Bite NZ

Offshore bite forecast and hotspot maps for Great Barrier Island to Cape Runaway.

- `index.html` is the whole app. GitHub Pages serves it.
- `scripts/fetch_data.py` runs every morning (about 4:37am NZ time) via GitHub Actions. It saves hourly Himawari SST and, once set up, Moana Project temperature at depth into `data/`.
- Everything else (MUR SST, chlorophyll, clarity, sea height, bathymetry, currents) loads live in the browser from NOAA and Open-Meteo.

## Run the morning job by hand
Actions tab > "Morning ocean data" > Run workflow.

## Turn on temperature at depth
After Moana Project grants data access, add the OPeNDAP link as a repository secret named `MOANA_URL`
(Settings > Secrets and variables > Actions > New repository secret).

## What the morning job produces

| File | What it is |
|---|---|
| `data/ocean.json` | MUR 1 km SST (6 days), climatology, VIIRS + Sentinel-3 OLCI chlorophyll (2 km when current, else 9 km), Kd490, altimetry SLA |
| `data/himawari.json` | Hourly Himawari-9 SST composite |
| `data/bathy_hd.png` + `.json` | NIWA Bay of Plenty 25 m multibeam survey merged over GEBCO, 16-bit PNG (one-time) |
| `data/currents.json` | Geostrophic + Ekman surface currents, ECMWF IFS 0.25° wind (last 24 h and 5-day) |
| `data/forecast.json` | 5-day SST forecast change (Copernicus Mercator via Open-Meteo) |
| `data/tides.json` | Hourly model tide for Tauranga, Whitianga, Whakatāne |
| `data/reserves.json` | DOC marine reserves and Hauraki Gulf high protection areas (Motiti areas are built into the app) |
| `data/alerts.json` | Marks with a strong front or warm push nearby; also posted as a GitHub issue so you get notified |

Bite alerts: click **Watch** on the repo (or keep default notifications on) to get each morning's alert by email and in the GitHub app.
