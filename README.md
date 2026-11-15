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
