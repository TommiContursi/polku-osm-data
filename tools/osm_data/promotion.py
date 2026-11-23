"""A product's candidate goes live only when Polku promotes it: Polku's promote workflow checks the candidate in the
app, then `promote` gives current.json the manifest's same bytes, and `restore` puts the replaced pointer back when
the live check fails. Used by the NLS background map (mml.py) and the routing data (routing.py)."""

from collections import namedtuple
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError

from tools.osm_data import maintenance, publish
from tools.osm_data.publish import require

SETTLE = 150  # seconds the public pointer may keep its last answer (publish.settle)

# host(): the SFTP login in the product's folder and its public URL; url(): that URL over public HTTP only;
# validate(manifest); held(ftp, raw): the manifest a pointer's bytes name, its release and every file on the host;
# probe(url, manifest, raw): the public release reads as uploaded
Product = namedtuple('Product', 'host url validate held probe')


def candidate(product):
    """The uploaded candidate built after the live release, for Polku's checks, or None. Reads only public HTTP."""
    url = product.url()
    raw, live = (public_bytes(url + path) for path in ('/candidates/current.json', '/current.json'))
    if raw is None:
        return None  # nothing uploaded yet
    manifest = product.validate(json.loads(raw))
    live = live and json.loads(live)  # None: nothing promoted yet
    if live and manifest['built_at'] <= live['built_at']:
        return None
    location = f"{url}/releases/{manifest['release']}/manifest.json"
    require(publish.get(location, immutable=True) == raw, 'Candidate pointer differs from its release')
    return {'release': manifest['release'], 'manifest': location, 'sha256': hashlib.sha256(raw).hexdigest()}


def public_bytes(url):
    """A pointer's bytes over HTTPS, or None where there is none (404)."""
    try:
        return publish.get(url)
    except HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        return None


def put_back(ftp, url, previous):
    """The live pointer as it was: the saved bytes, or none at all, so the app reads the outside service."""
    if previous is None:
        ftp.remove('current.json')
    else:
        publish.replace_pointer(ftp, previous)
    publish.settle(url + '/current.json', previous, SETTLE)


def promote(product, release, sha256, directory):
    """Make the candidate Polku checked live: current.json gets the manifest's same bytes. The pointer it replaces goes
    to DIRECTORY/previous.json (null where there was none), for restore."""
    require(os.environ.get('OSM_ALLOW_PUBLISH') == 'true', 'Publication requires approved OSM_ALLOW_PUBLISH=true')
    require(publish.NAME.fullmatch(release) and re.fullmatch('[a-f0-9]{64}', sha256), 'Invalid candidate')
    with product.host() as (ftp, url):
        raw = publish.retrieve(ftp, f'releases/{release}/manifest.json')
        require(hashlib.sha256(raw).hexdigest() == sha256, 'Candidate manifest differs from the one checked')
        # Still the candidate: an upload that has named another prunes this release unless it is live
        require(publish.retrieve(ftp, 'candidates/current.json') == raw, 'Another candidate was uploaded meanwhile; check it first')
        manifest = product.held(ftp, raw)
        require(manifest['release'] == release, 'Candidate release differs')
        previous = publish.retrieve(ftp, 'current.json') if 'current.json' in maintenance.children(ftp) else None
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'previous.json').write_bytes(previous or b'null')
        if previous == raw:
            return  # promoted already: a restore puts back the same
        require(not previous or manifest['built_at'] > json.loads(previous)['built_at'], 'Candidate is not newer than the live release')
        product.probe(url, manifest, raw)
        publish.replace_pointer(ftp, raw)
        try:
            print(f"Public pointer after {publish.settle(url + '/current.json', raw, SETTLE):.0f} s", flush=True)
        except Exception:
            put_back(ftp, url, previous)
            raise


def restore(product, directory, expected_current):
    """Back to the pointer promote replaced, unless another release went live meanwhile."""
    require(os.environ.get('OSM_ALLOW_PUBLISH') == 'true', 'Publication requires approved OSM_ALLOW_PUBLISH=true')
    previous = Path(directory, 'previous.json').read_bytes()
    with product.host() as (ftp, url):
        live = json.loads(publish.retrieve(ftp, 'current.json'))['release'] if 'current.json' in maintenance.children(ftp) else None
        require(live == expected_current, 'Live release changed; do not roll back another publication')
        previous = None if previous == b'null' else previous
        if previous is not None:
            product.held(ftp, previous)
        put_back(ftp, url, previous)
