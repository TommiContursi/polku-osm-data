# Polku's OpenStreetMap data

The build behind the OpenStreetMap data that [Polku](https://polku.si) reads: complete roads,
paths, lighting, trail details, winter ways, water hazards, overnight places, reedbeds, points
(fire places, springs, cave entrances, bunkers and tar kilns),
full-tag inspection and area geometry for Finland, Sweden, Norway and Estonia, as static
PMTiles archives at `https://ducksoft.fi/polku-osm/`.

Beside it, `https://ducksoft.fi/polku-routing/` mirrors BRouter's routing data from brouter.de
([docs/OPERATIONS.md](docs/OPERATIONS.md#routing-data)).
`https://ducksoft.fi/polku-mml/` holds a copy of the National Land Survey's background-map vector tiles
([docs/OPERATIONS.md](docs/OPERATIONS.md#nls-background-map)), and `https://ducksoft.fi/polku-digiroad/` a copy of
Digiroad's busy roads, paved roads, private roads and speed limits
([docs/OPERATIONS.md](docs/OPERATIONS.md#digiroad)). `https://ducksoft.fi/polku-terrain/`
holds a copy of Mapterhorn's terrain ([docs/OPERATIONS.md](docs/OPERATIONS.md#terrain)),
`https://ducksoft.fi/polku-basemap/` a monthly copy of OpenFreeMap's base map ([docs/OPERATIONS.md](docs/OPERATIONS.md#base-map)),
`https://ducksoft.fi/polku-lipas/` a copy of LIPAS's routes and places ([docs/OPERATIONS.md](docs/OPERATIONS.md#lipas)),
and `https://ducksoft.fi/polku-forest/` the forest's kind from Syke and Copernicus ([docs/OPERATIONS.md](docs/OPERATIONS.md#forest-kind)).

The repository is public so that its long builds run on GitHub's free runners. Its code
is not open source: see [LICENSE](LICENSE). The data keeps OpenStreetMap's licence.

## Status

Badges show the latest run on `main`. A run by hand or on a schedule only changes its badge when it runs: grey (no status) means it has not run yet.

| Data | Build | When | Polku promotion | When |
|---|---|---|---|---|
| OSM | [![OSM build](https://github.com/TommiContursi/polku-osm-data/actions/workflows/osm-data.yml/badge.svg?branch=main)](https://github.com/TommiContursi/polku-osm-data/actions/workflows/osm-data.yml) | Mon build, daily check | [![OSM promotion](https://github.com/TommiContursi/Polku/actions/workflows/osm-promote.yml/badge.svg?branch=main)](https://github.com/TommiContursi/Polku/actions/workflows/osm-promote.yml) | Thu |
| Routing | [![Routing build](https://github.com/TommiContursi/polku-osm-data/actions/workflows/routing-data.yml/badge.svg?branch=main)](https://github.com/TommiContursi/polku-osm-data/actions/workflows/routing-data.yml) | Wed update, daily check | [![Routing promotion](https://github.com/TommiContursi/Polku/actions/workflows/routing-promote.yml/badge.svg?branch=main)](https://github.com/TommiContursi/Polku/actions/workflows/routing-promote.yml) | Thu |
| NLS map | [![NLS map build](https://github.com/TommiContursi/polku-osm-data/actions/workflows/mml-data.yml/badge.svg?branch=main)](https://github.com/TommiContursi/polku-osm-data/actions/workflows/mml-data.yml) | by hand | [![NLS map promotion](https://github.com/TommiContursi/Polku/actions/workflows/mml-promote.yml/badge.svg?branch=main)](https://github.com/TommiContursi/Polku/actions/workflows/mml-promote.yml) | by hand |
| Terrain | [![Terrain build](https://github.com/TommiContursi/polku-osm-data/actions/workflows/terrain-data.yml/badge.svg?branch=main)](https://github.com/TommiContursi/polku-osm-data/actions/workflows/terrain-data.yml) | by hand | [![Terrain promotion](https://github.com/TommiContursi/Polku/actions/workflows/terrain-promote.yml/badge.svg?branch=main)](https://github.com/TommiContursi/Polku/actions/workflows/terrain-promote.yml) | by hand |
| Base map | [![Base map build](https://github.com/TommiContursi/polku-osm-data/actions/workflows/basemap-data.yml/badge.svg?branch=main)](https://github.com/TommiContursi/polku-osm-data/actions/workflows/basemap-data.yml) | 2nd of month | [![Base map promotion](https://github.com/TommiContursi/Polku/actions/workflows/basemap-promote.yml/badge.svg?branch=main)](https://github.com/TommiContursi/Polku/actions/workflows/basemap-promote.yml) | 4th of month |
| Search | [![Search build](https://github.com/TommiContursi/polku-osm-data/actions/workflows/search-data.yml/badge.svg?branch=main)](https://github.com/TommiContursi/polku-osm-data/actions/workflows/search-data.yml) | Tue | [![Search promotion](https://github.com/TommiContursi/Polku/actions/workflows/search-promote.yml/badge.svg?branch=main)](https://github.com/TommiContursi/Polku/actions/workflows/search-promote.yml) | Tue |
| Digiroad | [![Digiroad build](https://github.com/TommiContursi/polku-osm-data/actions/workflows/digiroad-data.yml/badge.svg?branch=main)](https://github.com/TommiContursi/polku-osm-data/actions/workflows/digiroad-data.yml) | by hand | — |  |
| LIPAS | [![LIPAS build](https://github.com/TommiContursi/polku-osm-data/actions/workflows/lipas-data.yml/badge.svg?branch=main)](https://github.com/TommiContursi/polku-osm-data/actions/workflows/lipas-data.yml) | by hand | [![LIPAS promotion](https://github.com/TommiContursi/Polku/actions/workflows/lipas-promote.yml/badge.svg?branch=main)](https://github.com/TommiContursi/Polku/actions/workflows/lipas-promote.yml) | by hand |
| Forest kind | [![Forest kind build](https://github.com/TommiContursi/polku-osm-data/actions/workflows/forest-data.yml/badge.svg?branch=main)](https://github.com/TommiContursi/polku-osm-data/actions/workflows/forest-data.yml) | by hand | — |  |
| Alerts | [![Alerts build](https://github.com/TommiContursi/polku-osm-data/actions/workflows/osm-data-alerts.yml/badge.svg?branch=main)](https://github.com/TommiContursi/polku-osm-data/actions/workflows/osm-data-alerts.yml) | after a failed OSM or routing run | — |  |

Schedules are in each workflow. Polku's badges show only while Polku is public.

## How a release reaches Polku

1. **Monday 03:17 UTC**, `osm-data.yml` picks one dated Geofabrik export shared by all four
   countries, downloads the four in one job, builds each country from that job's copy,
   completes the area geometry from the same Europe export and bundles the four into one release. When Geofabrik answers none of the runner's
   requests, or a download from it stalls, the run takes GWDG's mirror of Geofabrik's Europe export instead and clips the
   four countries from it with the polygons in `data/`; the manifest names the source each
   release was built from. Hashes, the schema, sample roads in every country
   and the Python tests are checked before anything is uploaded.
2. **The upload** (only when `OSM_UPLOAD_ENABLED` is `true`) puts the release in
   `releases/<release>/`, reads every file it sends back to check its SHA-256, checks the public
   Range, CORS and cache headers, and names it in `candidates/current.json`. The live
   `current.json` is not touched. With `OSM_SHARED_FILES` set to `true`, the files go in `files/`,
   named by their SHA-256, and only those the host doesn't hold yet are sent. With `OSM_CELLS`,
   each archive goes as cells of at most 1 MiB and an index of them, so a week sends only the
   cells that changed.
3. **Polku's own weekly job** finds the candidate (`publish.py candidate`), runs the app's
   checks against it in a browser, and promotes it with Polku's credentials in a job that holds
   only this repository's code at a pinned commit (`publish.py promote`). A job without
   credentials then checks polku.si in a browser (`health.py`), and another rolls back
   (`publish.py rollback`) if that fails. Rollback by hand is Polku's too. This repository never checks out Polku, and holds no token
   of Polku's.
4. **Daily at 07:17 UTC** this repository checks the live files: freshness (at most 14 days
   old), the manifest and Range reads of every archive. Polku's weekly job checks them in a
   browser.

A failed scheduled run opens a `watch` issue here, and a build that Geofabrik refused on
every failed job is re-run on new runners after 30 minutes, 1 hour and 2 hours
(`osm-data-alerts.yml`).

## The contract

Polku's reader (`js/osm-data.js`, `js/osm-geometry.js`) reads the manifest and the archives'
format described in [tools/osm_data/README.md](tools/osm_data/README.md). A change to either is a PR here first, then
a PR in Polku's reader with new fixtures. The fixtures in `tools/fixtures/` are the same files as Polku's.

## Working on it

```sh
python3.14 -m venv .venv
.venv/bin/pip install --require-hashes -r tools/osm_data/requirements.txt -r tools/osm_data/sftp-requirements.txt
.venv/bin/python -m unittest discover -s tools/osm_data -p 'test_*.py'
```

The `.txt` files lock every package by hash, since the SFTP jobs hold the host's password. They
are made from the `.in` files, taking only releases at least two weeks old:

```sh
cd tools/osm_data
uv pip compile --generate-hashes --python-version 3.14 --universal --exclude-newer YYYY-MM-DD --no-header requirements.in -o requirements.txt
uv pip compile --generate-hashes --python-version 3.14 --universal --exclude-newer YYYY-MM-DD --no-header sftp-requirements.in -o sftp-requirements.txt
```

Building, the host's configuration, recovery and retention are in
[docs/OPERATIONS.md](docs/OPERATIONS.md).
