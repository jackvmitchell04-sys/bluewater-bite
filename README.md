# Bluewater Bite NZ

Offshore bite forecast and hotspot maps for Great Barrier Island to Cape Runaway.

- `index.html` is the whole app. GitHub Pages serves it.
- `scripts/fetch_data.py` runs every morning (about 4:37am NZ time) via GitHub Actions and saves all ocean data into `data/` (browsers can't read most of these sources directly).

## Run the morning job by hand
Actions tab > "Morning ocean data" > Run workflow.

## What the morning job produces

| File | What it is |
|---|---|
| `data/ocean.json` | MUR 1 km SST (6 days), climatology, VIIRS + Sentinel-3 OLCI chlorophyll (2 km when current, else 9 km), Kd490, altimetry SLA |
| `data/himawari.json` | Hourly Himawari-9 SST composite |
| `data/bathy_hd.png` + `.json` | NIWA Bay of Plenty 25 m multibeam survey merged over GEBCO, 16-bit PNG (one-time) |
| `data/model.json` | Copernicus Mercator 1/12° surface currents and sea level (24 h mean): detailed arrows, convergence/upwelling, eddies |
| `data/tiles/` | NIWA survey at ~40 m in 0.25° tiles, loaded when zoomed in (10 m contours) |
| `data/currents.json` | Geostrophic + Ekman surface currents, ECMWF IFS 0.25° wind (last 24 h and 5-day) |
| `data/forecast.json` | 5-day SST forecast change (Copernicus Mercator via Open-Meteo) |
| `data/tides.json` | Hourly model tide for Tauranga, Whitianga, Whakatāne |
| `data/reserves.json` | DOC marine reserves and Hauraki Gulf high protection areas (Motiti areas are built into the app) |
| `data/alerts.json` | Marks with a strong front or warm push nearby |
| `data/sharp.png` + `.json` | Sharp SST: raw NOAA-20/21 VIIRS passes (ACSPO L2P, 750 m) from the last 36 h, newest pixel wins, levelled to MUR, cloud gaps from MUR; fronts traced at 750 m |
| `data/l2p_index.json` | Which raw passes cover the Bay (the gridded passes themselves are kept in the GitHub Actions cache) |
| `data/truecolour.jpg` (+ `_natural`) + `.json` | Clearest true colour view of the newest day from NASA VIIRS (NOAA-20, NOAA-21, Suomi NPP), cloud gaps filled from the other satellites and up to 2 days back; water contrast enhanced |
| `data/dumping.json` | Charted dumping grounds (LINZ), including the ammunition dumping ground (the Ammo Dump) |

Bite alerts: click **Watch** on the repo (or keep default notifications on) to get each morning's alert by email and in the GitHub app.
