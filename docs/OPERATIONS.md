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

**Actions → Polku OSM data → build** runs the weekly build by hand. Each country job has 150
minutes, with a 40-minute download step: each PBF transfer gets 30 minutes and retries only
in its first five. Every Geofabrik download after the snapshot step retries any error, a
404 included, three times 30 seconds apart. Nothing gets past a refused connection:
Geofabrik refuses whole /24 networks, two of GitHub's runner networks among them, and a job
keeps its runner's address. Some it leaves unanswered: a request times out with no bytes
received. So when every failed job of a build on `main` was refused or unanswered,
**Polku OSM alerts** re-runs the failed jobs on new runners, up to 15 attempts in all,
unless a newer build exists. A re-run that succeeds goes on to upload. The checksum and
polygon come from beside the file actually served, since Geofabrik is moving countries into
folders of their own. The source must be at most 14 days old. Source and release artifacts
are kept 14 days.

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

   It requires the manifest bytes Polku checked (by SHA-256), every file at its size and
   nothing else in the folder, a release built after the live one, and that live geometry
   isn't dropped. It checks the public Range reads again, writes a pending receipt naming the
   true predecessor, renames a temporary pointer over `current.json`, reads it back and checks
   the release over HTTPS, and restores the predecessor if that fails. The predecessor is
   saved for rollback. A rerun after promotion only finishes the receipt.
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
archive. Polku's weekly job checks the live release in a browser: 16 places in a desktop and
an emulated phone profile, with Overpass forbidden, every archive of the manifest read, and
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
until `candidates/current.json` names the new one, then removes it. Interrupted staging is kept 7 days.
Unmarked releases and unknown files are never deleted. Receipts live in
`maintenance/`. The whole deletion plan must validate before anything is removed; unsafe
paths, symlinks, unexpected files, a changed pointer or corrupt ownership records stop it,
and an interrupted deletion keeps its manifest until last. The 80 GiB budget counts the whole
directory, `candidates/` included. **inspect-retention** prints the plan without changing
anything.

## The host

`https://ducksoft.fi/polku-osm/` on OVH. Install the sample
[`hosting.htaccess`](../tools/osm_data/hosting.htaccess) in that directory by hand (it is never
uploaded): no listing, no HTTP gzip or Brotli, MIME types, wildcard read-only CORS with the
Range headers exposed, immutable release caching, and `current.json` revalidated, which also
covers `candidates/current.json`.

SFTP on port 22, with the host key's SHA-256 fingerprint verified independently (never from
an unauthenticated `ssh-keyscan` alone) and pinned. **test-host** writes tiny files into a
unique `host-test-*` folder and checks upload, readback, rename over an existing file,
folder rename, public Range, CORS and caching, and the retention rules, then removes the
folder. **verify-access** checks the login, listing and the public pointer without writing.

The environment `osm-data-production`, limited to `main`:

| Secret | Value |
| --- | --- |
| `OSM_FTP_HOST` | OVH's SFTP hostname, without a scheme or port |
| `OSM_FTP_USER` | The SFTP user |
| `OSM_FTP_PASSWORD` | Its password |
| `OSM_SFTP_HOST_KEY_SHA256` | The verified host key, `SHA256:...` |

Repository variables (the daily check needs the URL outside the environment):

| Variable | Value |
| --- | --- |
| `OSM_FTP_ROOT` | Absolute SFTP path ending in `/polku-osm` |
| `OSM_PUBLIC_URL` | `https://ducksoft.fi/polku-osm` |
| `OSM_UPLOAD_ENABLED` | `true` once uploads should follow every successful build |

The host sees which release and roughly which area a browser reads, through its byte
ranges; keep its access logs as short as its operation allows.

## Routing data

`routing-data.yml` mirrors BRouter's routing data, brouter.de's `segments4/`: 5°×5° `.rd5` squares, 10.1 GB for
the world on 2026-11-22. It keeps to the terms brouter.de's maintainer set on 2026-11-12
([abrensch/brouter#1011](https://github.com/abrensch/brouter/issues/1011)): one initial download, then syncs by its
daily change files, as BRouter's Android app does, and no service promise. #7 holds the decisions.

- **update**, Wednesdays 05:43 UTC: for each square, our candidate's copy, brouter.de's
  `diff/<square>/<md5>.df5` for it, and BRouter 1.7.10's own `recoverFromDelta` and `checkFileIntegrity`
  (`Rd5.java`, with the release jar pinned by SHA-256). brouter.de must know the new copy's MD5, since next week's
  change file is named by it. One file at a time, at most 16 Mbit/s, with a User-Agent naming this repository. A
  failure other than 404 waits 5 minutes once, then stops the run.
- **A square without its change file** keeps our copy until its last full download is 90 days old, then downloads
  whole: brouter.de allows that about once a quarter. brouter.de keeps about 9 days of change files and makes none
  for squares under 1 MiB (557 of 1,142). The run's summary counts `quarterly` (1 MiB and over), `small` and `kept`.
- **initial** downloads the squares we have no copy of, once: the first run, and any square brouter.de adds (`waiting`
  in the summary). A scheduled run never does.
- **Only 60°N and up for now** (`NORTH` in `routing.py`). South of it the elevation is CGIAR's SRTM, whose terms
  leave redistribution unclear (#7). North of it, Sonny's LiDAR models (CC BY 4.0).
- **Upload:** each square goes into `polku-routing/staging/` and is read back with its SHA-256, and a record beside it
  lets a stopped run go on from there. Then the manifest, the rename to `releases/<release>/`, the public Range, CORS
  and cache checks of every square's index, and `candidates/current.json`. Releases other than the candidate and the
  live one are then removed: two copies of the data, three during an update. It needs the host's free space by
  `statvfs` for the incoming bytes and 2 GiB more, and stays within its own 32 GiB budget.
- **check**, daily at 07:43 UTC: the candidate, and `current.json` once Polku promotes one. The pointer equals its
  immutable manifest, squares with change files are at most 14 days old and the small ones at most 100, and every
  square's index is read by Range.
- **space** reads the host's free space over SFTP and both folders' sizes. **install-hosting** puts
  [`routing.htaccess`](../tools/osm_data/routing.htaccess) into `polku-routing/` once; a later change is made by hand.
- A failed update or check opens its own `watch` issue, as for the OSM data.

The live `current.json` is Polku's promotion, a later PR in Polku. The candidate pointer holds its release's manifest
bytes, as for the OSM data, and the manifest's format is in
[tools/osm_data/README.md](../tools/osm_data/README.md#routing-data).

## Licensing

Each release carries OSM's attribution and the ODbL link in `LICENSE.txt`, the manifest and
the archives' metadata. Geofabrik is recorded as the extract provider. The routing manifest carries OSM's
attribution, BRouter's and Sonny's (CC BY 4.0, https://sonny.4lima.de); squares south of 60°N would add CIAT's.
