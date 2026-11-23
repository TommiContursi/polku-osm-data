# Operating Polku's OSM data

This repository builds and uploads; Polku promotes. The history before 2026-11 (the first
publications, the measurements behind the time limits) is in Polku's own repository, where
this build used to live.

## Build

Python 3.14.3 with `tools/osm_data/requirements.txt`. A Linux runner or an ordinary computer
builds the data; nothing runs on the host. Keep sources and output out of Git (`osm-build/`).

```sh
python3.14 -m venv osm-build/venv
osm-build/venv/bin/pip install --require-hashes -r tools/osm_data/requirements.txt
curl --fail --location --retry 3 https://download.geofabrik.de/europe/finland-latest.osm.pbf -o osm-build/finland.osm.pbf
curl --fail --location --retry 3 https://download.geofabrik.de/europe/finland-latest.osm.pbf.md5 -o osm-build/finland.osm.pbf.md5
osm-build/venv/bin/python tools/osm_data/builder.py osm-build/finland.osm.pbf osm-build/release --md5 osm-build/finland.osm.pbf.md5
```

To see a local release in the app, use Polku's `tools/osm_data/preview.py --release DIR`
from Polku's checkout.

**Actions → Polku OSM data → build** runs the weekly build by hand. The snapshot job picks the
date and downloads all four countries itself, one after another (a 150-minute step: each PBF
transfer gets 30 minutes and retries only in its first five), and hands each to its country
job as an artifact. Geofabrik once answered the snapshot check and then refused two of the
country jobs' runners (2026-11-23), so the country jobs never ask it themselves; each has 150
minutes to build. Every Geofabrik download after the snapshot check retries any error, a
404 included, three times 30 seconds apart. Nothing gets past a refused connection:
Geofabrik refuses whole /24 networks, two of GitHub's runner networks among them, and a job
keeps its runner's address. Some it leaves unanswered: a request times out with no bytes
received. So when every failed job of a build on `main` was refused or unanswered,
**Polku OSM alerts** re-runs the failed jobs on new runners, 30 minutes after the first
attempt, then 1 hour and 2 hours after the next ones, so four attempts in all, unless a newer
build exists by then. Re-running at once looked like a burst to Geofabrik (15 attempts in 24
minutes, 2026-11-23). A re-run that succeeds goes on to upload. The checksum
comes from beside the file actually served, since Geofabrik is moving countries into folders
of their own; the clipping polygons are this repository's own copies in `data/`, so no build
waits on Geofabrik for them. The source must be at most 14 days old. Source and release
artifacts are kept 14 days.

GitHub may start the weekly schedule hours late (the 03:17 UTC schedule began at 12:42 UTC on 2026-11-23,
after a manual build of the same export). So once the snapshot step has picked its dated
export, a scheduled build stops when another build of that export is going on or has
succeeded: each build marks its export with an `osm-snapshot-<date>` artifact. The stopped
run ends without building anything, and the alerts report nothing for it. A manual build
always runs, and so does a scheduled one when GitHub's API doesn't answer.

**The second source.** When Geofabrik answers none of the snapshot step's requests (no HTTP
answer at all; a 404 means a date isn't ready, and the run fails for the alerts' re-run), the
run takes [GWDG's mirror](https://ftp5.gwdg.de/pub/misc/openstreetmap/download.geofabrik.de/)
of Geofabrik's Europe export, which carries Geofabrik's own dated file and its MD5. So does
a country download that fails, stalls or times out partway, when the mirror's MD5 file names the
same dated export (Norway's stopped at 1.01 of 1.38 GB on 2026-11-23); the countries already
downloaded are dropped, since one run never mixes the two. The
mirror holds Europe alone, so the snapshot job downloads it instead (up to 185 minutes, the same
resumable transfer as the geometry job), checks the published MD5, and clips the four
countries from it in one pass (`osmium extract --strategy complete_ways`, the polygons in
`data/`), keeping the export's replication timestamp in each country's header. The country
jobs build from those artifacts, as on Geofabrik's path, and the geometry job takes its
Europe export from the mirror too. The run's summary says which source it used, and each
manifest's `source` names the mirrored export it was clipped from, with the tool
(`clipped`) and the polygon (`coverage`), so a mirrored release is never mistaken for
Geofabrik's own: the clipped countries differ slightly from Geofabrik's at the borders. A build mixing the two sources'
countries is refused. The Europe export is the same file from either place, so when Geofabrik
doesn't answer the geometry job's runner, or its transfer stops, and the mirror's MD5 file names
the same dated export, the geometry job takes the mirror's copy, going on from where Geofabrik's
transfer stopped (the MD5 covers both parts), and its manifest names the mirror. The mirror keeps only the newest export, so a build that runs past its nightly
update (about 23:50 UTC) fails at the geometry job's epoch check rather than mixing two
dates. The polygons are copies of Geofabrik's of 2026-11-23, updated by hand when Geofabrik
changes a country's extent (a test pins each one's SHA-256); each manifest records the
polygon's path and SHA-256.

The geometry job then completes the area roots from the same dated Europe export (350
minutes, a 185-minute download step). The 35 GB export has taken from 42 minutes to over 2
hours, so a transfer that stops, or stays under 1 MB/s for 5 minutes, goes on from where it
stopped on a new connection, for up to 3 hours. Four tries in a row that add no bytes end
it, so a refused runner still fails soon and is re-run. The bundle job joins the four countries, checks
every overlapping original object, and reads every country's sample roads from the combined
release. **geometry-candidate**, with an earlier build's run ID in **release**, reuses its
verified country artifacts and builds only the geometry.

## Upload

With `OSM_UPLOAD_ENABLED=true`, a successful weekly or manual build uploads its bundle:

1. Retention runs first (below), and the 80 GiB budget must hold the incoming bytes.
2. The files go into a unique `.upload-<release>-<hex>/` folder with an ownership record
   (`_upload.json`), are read back over SFTP to check every file's SHA-256, and the folder is
   renamed to `releases/<release>/`.
3. The public manifest and bytes at each archive's start, middle and end (and 2 and 4 GiB
   offsets where they exist) are read over HTTPS, with and without browser-like
   `Accept-Encoding`: 206, `Content-Range`, length, CORS, MIME and immutable caching, no HTTP
   compression.
4. `candidates/current.json` is replaced, atomically, with the release's manifest. The live
   `current.json` is not touched, and the ownership record stays until promotion.

**Shared files.** With the repository variable `OSM_SHARED_FILES` set to `true`, every file goes in
`files/`, named by its SHA-256 and its suffix, and `releases/<release>/` holds only the manifest, whose
URLs name those files. A file `files/` already holds at its size isn't sent again; a file that is sent
is hashed in full in the staging folder before it moves to `files/`. A file that doesn't change keeps
its URL from release to release. Turn it on only once Polku's promotion is pinned to a commit that
reads this layout: older code refuses such a candidate and changes nothing.

**Cells.** With `OSM_CELLS` set to `true` (it shares the files too), the upload cuts each archive into
cells of at most 1 MiB and writes their index, `<name>.cells.json`, beside the archive. The index goes
up in the archive's place, and a cell goes to `files/` only when `files/` doesn't hold it yet: about a
quarter of the bytes in a week (measured 2–8 Nov 2026). The cut takes some 15 minutes, and the first
upload of cells sends every cell once; if it runs out of time, **resume-existing** goes on. Turn it on
only once Polku's reader reads cells and its promotion is pinned to a commit that does: older readers
don't accept the format.

An interrupted upload is never started again from nothing. **inspect-existing**, with the
build's run ID in **release**, reports the staged files' sizes without changing the host.
**resume-existing** then reuses the release if it was already renamed, or exactly one
matching staging folder: complete files aren't sent again, partial ones are compared with
the artifact before only their missing end is sent, and every file still passes the full
SHA-256 check. **upload-existing** uploads a verified build's bundle again. All three need a
completed `main` build of this repository whose bundle passed, and its unexpired
`nordic-osm-release` artifact.

## Promotion, in Polku

Polku's `osm-promote.yml` runs weekly, in jobs that keep Polku's code and the publishing
credentials apart:

1. **check** (Polku's checkout and this repository's at a pinned commit, no credentials):
   reads `candidates/current.json` (`publish.py candidate`). A candidate built after the live
   release is checked in Polku's app in a browser, reading the candidate's own manifest where
   it lies on the host (`preview.py --manifest`): `tools/smoke.py osmdata` and
   `measure_browser.py --country all`. Without one, the live release is checked instead.
2. **promote** (only this repository's checkout at the pin, no GitHub token, Polku's
   `osm-data-production` environment limited to `main`):

   ```sh
   python tools/osm_data/publish.py promote RELEASE --sha256 SHA --directory osm-promote
   ```

   It requires the manifest bytes Polku checked (by SHA-256), every file at its size (in the
   release's folder or in `files/`) and nothing else in the folder, a release built after the
   live one, and that live geometry isn't dropped. It checks the public Range reads again,
   writes a pending receipt naming the true predecessor, renames a temporary pointer over
   `current.json`, reads it back and checks the release over HTTPS, and restores the predecessor
   if that fails. The predecessor is saved for rollback. A rerun after promotion only finishes
   the receipt.
3. **live** (Polku's checkout, no credentials): `health.py osm-health --release-dir osm-promote`
   requires the promoted release to be live and runs Polku's browser checks against polku.si.
4. **rollback**, only when the live check failed (only this repository's checkout, the
   environment): `publish.py rollback --manifest <the saved predecessor> --expected-current
   RELEASE` verifies the predecessor's every file over SFTP and HTTPS and switches the pointer
   back, unless another release has gone live meanwhile. The run stays failed.

Rollback by hand runs the same way, to the saved predecessor. The pin moves only through a Polku PR;
when the contract changes, the PR here comes first, and Polku's PR moves the pin together with
its reader.

## Monitoring

**check**, daily at 07:17 UTC and by hand, needs only Python's standard library: the live
source at most 14 days old, the pointer equal to its immutable manifest, `current.json`
revalidated (never immutable or fresh without revalidation), and Range reads of every
archive (of a dataset in cells: its index and its first, middle and last cells). Polku's
weekly job checks the live release in a browser: 16 places in a desktop and an emulated phone
profile, with Overpass forbidden, every archive of the manifest read, and
six lake and park cases when the release has area geometry.

`osm-data-alerts.yml` handles only this repository's scheduled and manual runs on `main`. It
opens an owner-assigned `watch` issue for a failed build, upload or check, updates it on
repeated failure, and closes it on the matching recovery. A build that didn't upload isn't a
recovery. **failure-test** and **recovery-test** prove the inbox path.

GitHub turns off a public repository's scheduled workflows after 60 days without activity in
it. Polku's weekly check fails once the live data is over 14 days old, so a stopped build
can't go unnoticed for long; re-enable the workflows here when it happens.

## Retention

Before an upload, `maintenance.py` removes only files it owns. It keeps the live release, its
saved predecessor (for rollback) and the candidate. An upload keeps the candidate it replaces
until `candidates/current.json` names the new one, then removes it. `files/` is the publisher's own: a
file there goes once nothing that stays names it, neither a release (directly, or through a cell
index), a stopped upload's staging nor the upload under way. They all stay while a release that stays
has no manifest; a corrupt manifest, a missing index, or any other name in `files/`, stops retention.
A stopped upload's staging may hold a cell on its way, named by its SHA-256; it goes with the
staging. Interrupted staging is kept 7 days.
Unmarked releases and unknown files are never deleted. Receipts live in
`maintenance/`. The whole deletion plan must validate before anything is removed; unsafe
paths, symlinks, unexpected files, a changed pointer or corrupt ownership records stop it,
and an interrupted deletion keeps its manifest until last. The 80 GiB budget counts the whole
directory, `candidates/` included. **inspect-retention** prints the plan without changing
anything.

## The host

The data is served from `https://ducksoft.fi/polku-osm/`, `https://ducksoft.fi/polku-routing/`, `https://ducksoft.fi/polku-mml/`
and `https://ducksoft.fi/polku-digiroad/`.
Each folder has its own `.htaccess`: [`hosting.htaccess`](../tools/osm_data/hosting.htaccess),
installed by hand, [`routing.htaccess`](../tools/osm_data/routing.htaccess) (Routing data,
above), [`mml.htaccess`](../tools/osm_data/mml.htaccess) in `polku-mml/` (NLS background map, below) and a copy of
`hosting.htaccess` in `polku-digiroad/` (Digiroad traffic volumes, below). They turn off listing and HTTP compression, set the MIME types, allow read-only CORS with
the Range headers exposed, cache releases as immutable and make `current.json` revalidate, which
also covers `candidates/current.json`.

**test-host** writes tiny files into a unique `host-test-*` folder and checks upload, readback,
renames, public Range, CORS and caching, and the retention rules, then removes the folder.
**verify-access** checks the login, listing and the public pointer without writing.

The jobs that use the host read the `osm-data-production` environment, limited to `main`, by the
secret names in `.github/workflows/`. `OSM_PUBLIC_URL` and `OSM_UPLOAD_ENABLED` must be repository
variables, since job conditions read them; `OSM_UPLOAD_ENABLED` set to `true` makes every
successful build upload. The host's login and host key are documented in Polku's repository.

The host sees which release and roughly which area a browser reads, through its byte
ranges; keep its access logs as short as its operation allows.

## Routing data

`routing-data.yml` copies brouter.de's routing data (`segments4/`, 5°×5° `.rd5` squares) weekly, checks it and
publishes it at `https://ducksoft.fi/polku-routing/` with a manifest. It keeps to brouter.de's terms
([abrensch/brouter#1011](https://github.com/abrensch/brouter/issues/1011)): one initial download, then syncs by its
change files, one file at a time and at most 16 Mbit/s.

- **initial**, by hand: downloads the squares there is no copy of yet.
- **update**, Wednesdays 05:43 UTC: applies brouter.de's change file to each square with BRouter 1.7.10's own code
  (`Rd5.java`, the release jar pinned by SHA-256) and checks it. A square without one is downloaded whole at most
  every 90 days. After a failed Wednesday, run **update** by hand within two days, while the change files last.
- **check**, daily at 07:43 UTC: the candidate, and `current.json` once there is one.
- **install-hosting**, once: puts [`routing.htaccess`](../tools/osm_data/routing.htaccess) into `polku-routing/`.

Each square that changed also gets the mirror's change file from the previous release's square: brouter.de's own, or
one made with the same jar and checked by applying it. Each square and change file goes in `polku-routing/files/`,
named by its SHA-256, and is read back with it before the release is named in `candidates/current.json`. A square the
folder already holds (one that didn't change) isn't sent again, and a stopped run goes on from where it stopped.
Releases other than the candidate and the live one are removed, then the files neither of them names. A failed update
or check opens a `watch` issue. The manifest is in [tools/osm_data/README.md](../tools/osm_data/README.md#routing-data).

## Licensing

Each release carries OSM's attribution and the ODbL link in `LICENSE.txt`, the manifest and
the archives' metadata. Geofabrik is recorded as the extract provider. The routing manifest carries OSM's
attribution, BRouter's, Sonny's (CC BY 4.0, https://sonny.4lima.de) and CIAT's citation for SRTM south of 60°N.

## NLS background map

`mml-data.yml`, by hand, copies the National Land Survey's (MML) background-map vector tiles (`taustakartta` v21,
zooms 11–14 over 19.0–31.6°E and 59.3–70.1°N: 892,856 tiles) into one PMTiles archive, and uploads it as a candidate
to `https://ducksoft.fi/polku-mml/`. The format is in [tools/osm_data/README.md](../tools/osm_data/README.md#nls-background-map).

- **fetch** asks about 40 tiles a second over 8 keep-alive connections, with the key in the repository secret
  `MML_API_KEY`, and a User-Agent naming this repository. A 429, 5xx, timeout or answer slower than 2 s halves the
  rate, down to one a second; each 30 s without trouble raises it by one. A 404 is kept as an empty tile, and another
  4xx is recorded and not asked again. A 401 or 403, 5 minutes without a good answer, or 429s at one a second stop the
  run, and so does a hand check that doesn't find Helsinki in its tile at every zoom. It runs in up to three parts of
  5 h 20 min, each keeping its tiles in an `mml-store-<part>` artifact; a later run given the run's ID in **store**
  goes on from the newest.
- **pack** needs an answer for every tile and reads the archive back tile for tile. `tiles.tsv.gz` keeps each tile's
  `Last-Modified`, so an update can ask again with `If-Modified-Since`.
- **upload**, only from `main`, puts [`mml.htaccess`](../tools/osm_data/mml.htaccess) into `polku-mml/` once. The
  release goes through `staging/` (a stopped upload goes on from where it stopped), is read back with its SHA-256,
  checked over HTTPS and named in `candidates/current.json`. Releases other than the candidate and the live one go,
  and a release whose box doesn't cover the candidate's is refused.
- **Promotion** is Polku's `mml-promote.yml`, run by hand, like the OSM data's. `mml.py candidate` finds a
  candidate built after the live release, over public HTTP only, and Polku's check reads it in the app. Then
  `mml.py promote` checks the release's manifest hash and file sizes, and that it is still the candidate, since an
  upload removes a release that is neither the candidate nor live. It reads the release over HTTPS and puts the
  manifest's own bytes in `polku-mml/current.json`, the release the app reads. The pointer it replaced is kept. If
  polku.si then fails its check, `mml.py restore` puts that pointer back, or removes `current.json` if there was
  none, so the app reads MML's own service. It refuses if another release went live meanwhile or the old release is
  gone.

A smaller **box** tests the whole job. A pull request that changes the job fetches central Helsinki in two parts and
uploads nothing. The tiles are CC BY 4.0: "© Maanmittauslaitos, Maastotietokanta" and the month fetched, in the
manifest, the archive's metadata and `LICENSE.txt`.

## Digiroad traffic volumes

`digiroad-data.yml`, by hand, copies Digiroad's busy roads: the roads of 800 or more vehicles a day in Väylävirasto's
WFS layer `dr_liikennemaara`, 117,061 of them on 2026-11-23. Väylävirasto's updates to it ended on 2026-09-18, when
Fintraffic took Digiroad over, so the copy runs again only when a source with newer data exists. The format is in
[tools/osm_data/README.md](../tools/osm_data/README.md#digiroad-traffic-volumes).

- **install-hosting**, once: makes `polku-digiroad/` and puts a copy of
  [`hosting.htaccess`](../tools/osm_data/hosting.htaccess) in it.
- **copy** asks the WFS for 5,000 roads at a time, ordered by ID, with 5 s between pages, and retries a failed page once
  after 5 minutes. The count must equal the WFS's own. The release is about 9.5 MB in 1,904 zoom-11 tiles, and goes up
  as about 20 cells through `publish.py`, as the OSM data does: read back with its SHA-256, checked over HTTPS and
  named in `candidates/current.json`. A cell the host already holds isn't sent again.

There is no promotion yet, so Polku reads the candidate. The data is CC BY 4.0: "Traffic
volumes: © Fintraffic / Digiroad, from Väylävirasto's open data, CC BY 4.0", in the manifest, the archive's metadata and
`LICENSE.txt`, which also says what was changed.
