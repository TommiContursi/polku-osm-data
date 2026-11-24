#!/usr/bin/env python3
"""A place-name search index for Finland, as static files: Photon's weekly OSM dump for Finland, the National Land
Survey's (NLS) place names that OSM lacks, and the names of the world's countries and larger towns beyond Finland.
Uploaded as a candidate in polku-search beside polku-osm.

Each key (a name's word, or the whole name without spaces, normalised) belongs to a group, its first two letters.
The root index names each group's index; a group's index names the group's files, by key prefix, and carries the
records changed since those files were made. A week's update so sends only the group indexes that changed, and a
group's files again only when its changes outgrow CHANGES. MML_API_KEY is only ever put into requests: never printed
or stored."""

import argparse
import bisect
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unicodedata
import urllib.request
from urllib.error import HTTPError
from urllib.parse import urlencode
import zipfile

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.osm_data import maintenance, publish
from tools.osm_data.publish import require
from tools.osm_data.routing import stamp

AGENT = 'polku-osm-data/1.0 (+https://github.com/TommiContursi/polku-osm-data)'
PHOTON = 'https://download1.graphhopper.com/public/europe/finland/'
PHOTON_FILE = re.compile(r'photon-dump-finland-master-(\d{6})\.jsonl\.zst')
NLS = 'https://avoin-paikkatieto.maanmittauslaitos.fi/geographic-names/features/v1/collections/placenames_simple/items'
GEONAMES = 'https://download.geonames.org/export/dump/cities15000.zip'
COVERAGE = Path(__file__).resolve().parents[2] / 'data' / 'finland-coverage.geojson'
FOLDER, ROOT, SUFFIX = 'polku-search', 'search.gzt', '.gzt'
BUDGET = 2 * 1024**3  # a release is about 50 MB; its updates share most of it
MAX_AGE = 14  # days: an older dump is refused
SHORT = 0.9  # of each source's count in the previous release, at least
TARGET = 48 * 1024  # a file's gzipped bytes, at most, unless one word's records alone are more
TOP = 30  # records in a split prefix's list, for queries that short
CHANGES = 24 * 1024  # a group's changed records, in bytes, before its files are made again
INLINE = 1024  # a file smaller than this, gzipped, goes into its group's index as text: half the files are,
INLINED = 8 * 1024  # up to this many of a group's gzipped bytes, so a large group's index stays small
AREA = 7  # the zoom of the tiles a common word's records are split by
PAGE, PAUSE = 10000, 1  # NLS: names a request, and seconds between requests
ATTRIBUTION = ('Places: © OpenStreetMap contributors (ODbL), via Photon; place names © National Land Survey of '
               'Finland, Geographic Names Register (CC BY 4.0); towns beyond Finland © GeoNames (CC BY 4.0)')
LICENSE = 'ODbL 1.0; CC BY 4.0'
LICENSE_TEXT = '''Polku's search index is made from three sources:

- OpenStreetMap's places and streets in Finland, © OpenStreetMap contributors, from Photon's dump of Nominatim,
  available under the Open Database License 1.0: https://opendatacommons.org/licenses/odbl/1-0/
  The index is a derived database under the same licence.
- The Geographic Names Register (Paikannimet) of the National Land Survey of Finland, licensed under Creative Commons
  Attribution 4.0 International: https://creativecommons.org/licenses/by/4.0/
- GeoNames' towns of 15,000 people or more (cities15000), licensed under Creative Commons Attribution 4.0
  International.

Changed: names normalised into search keys; NLS's names only where OSM has no place of that name nearby; positions
rounded; streets' segments merged by name and municipality.
'''

# --- Keys

FOLD = str.maketrans({'ä': 'a', 'å': 'a', 'ö': 'o', 'ø': 'o', 'æ': 'ae', 'đ': 'd', 'ŋ': 'n', 'ŧ': 't', 'ß': 'ss',
                      'ʒ': 'z', 'ǯ': 'z', 'ǥ': 'g', 'ʹ': ''})
SPLIT = re.compile(r'[^0-9a-z]+')


def words(text):
    """A name's words as search keys: lower case, ä å ö ø æ đ ŋ ŧ ß ʒ ǯ ǥ folded, every other mark dropped, split at
    anything but a–z and 0–9. Its cases are tools/fixtures/search-normalise.json."""
    folded = unicodedata.normalize('NFKD', text.lower().translate(FOLD))
    return [w for w in SPLIT.split(''.join(c for c in folded if not unicodedata.combining(c))) if w]


def keys(names):
    """Every word of every name, and each name of several words without its spaces ("iso h" finds Iso Haukijärvi)."""
    result = set()
    for name in names:
        ws = words(name)
        result.update(ws)
        if len(ws) > 1:
            result.add(''.join(ws))
    return result


def group(key):
    return key[:2]


# --- Records: a line each, id, names, kind, lon, lat, rank, municipality, extent (west,south,east,north or empty)

def line(r):
    extent = ','.join(f'{v:.3f}' for v in r['extent']) if r.get('extent') else ''
    return '\t'.join([r['id'], '|'.join(r['names']), r['kind'], f"{r['lon']:.4f}", f"{r['lat']:.4f}",
                      f"{r['rank']:.2f}", r.get('city', ''), extent])


def clean(text):
    return ' '.join(str(text).replace('|', '/').split())


def photon(lines):
    """Photon's dump: {id: record} and the data's time. An OSM object is one record under its most important class,
    a street one record per name and municipality, a postcode one record. Each country is a record beyond Finland."""
    head = json.loads(next(lines))
    require(head.get('type') == 'NominatimDumpFile', 'Not a Photon dump')
    timestamp = head['content']['data_timestamp']
    records, streets = {}, {}
    langs = ('name', 'name:fi', 'name:sv', 'name:se', 'name:smn', 'name:sms', 'name:en', 'alt_name', 'official_name',
             'short_name', 'old_name')
    for raw in lines:
        doc = json.loads(raw)
        if doc.get('type') == 'CountryInfo':
            for c in doc['content']:
                names = list(dict.fromkeys(clean(v) for k, v in sorted(c.get('name', {}).items())
                                           if k in ('name', 'name:fi', 'name:sv', 'name:en', 'name:se') and v.strip()))
                if names and c.get('country_code') and c['country_code'] != 'fi':
                    records['C' + c['country_code']] = {'id': 'C' + c['country_code'], 'names': names, 'kind': 'country',
                                                        'lon': 0.0, 'lat': 0.0, 'rank': 0.9}
            continue
        if doc.get('type') != 'Place':
            continue
        for c in doc['content']:
            if c.get('country_code') != 'fi':
                continue
            given = c.get('name') or {}
            names = []
            for k in langs:
                for part in str(given.get(k) or '').split(';'):
                    part = clean(part)
                    if part and part not in names:
                        names.append(part)
            if not names:
                continue  # addresses wait for their own part
            lon, lat = c['centroid']
            a = c.get('address') or {}
            r = {'names': names, 'kind': f"{c.get('osm_key')}/{c.get('osm_value')}", 'lon': lon, 'lat': lat,
                 'rank': float(c.get('importance') or 0),
                 'city': clean(a.get('city') or a.get('town') or a.get('village') or a.get('municipality') or '')}
            box = c.get('bbox')  # west, north, east, south
            if box and (abs(box[2] - box[0]) > 0.002 or abs(box[1] - box[3]) > 0.002):
                r['extent'] = (min(box[0], box[2]), min(box[1], box[3]), max(box[0], box[2]), max(box[1], box[3]))
            if c.get('osm_key') == 'place' and c.get('osm_value') == 'postcode':
                r['id'] = 'P' + names[0]
            elif c.get('osm_key') == 'highway' and c.get('object_type') == 'W':  # a bus stop stays a place of its own
                street = streets.setdefault((names[0], r['city']), [])
                street.append((c.get('object_type', 'X') + str(c.get('object_id')), r))
                continue
            else:
                r['id'] = c.get('object_type', 'X') + str(c.get('object_id'))
            if r['id'] not in records or r['rank'] > records[r['id']]['rank']:
                records[r['id']] = r
    for (name, city), segments in streets.items():
        ids = sorted(i for i, _ in segments)
        boxes = [s.get('extent') or (s['lon'], s['lat'], s['lon'], s['lat']) for _, s in segments]
        box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
        first = min(segments, key=lambda s: s[0])[1]
        r = {'id': 'S' + ids[0], 'names': list(dict.fromkeys(n for _, s in segments for n in s['names'])),
             'kind': first['kind'], 'lon': (box[0] + box[2]) / 2, 'lat': (box[1] + box[3]) / 2,
             'rank': max(s['rank'] for _, s in segments), 'city': city}
        if box[2] - box[0] > 0.002 or box[3] - box[1] > 0.002:
            r['extent'] = box
        records[r['id']] = r
    return records, timestamp


def dump_lines(path):
    """A zstd-compressed dump's lines, as text."""
    with open(path, 'rb') as source:
        process = subprocess.Popen(['zstd', '-dc'], stdin=source, stdout=subprocess.PIPE)
    try:
        yield from io.TextIOWrapper(process.stdout, encoding='utf-8')
    finally:
        process.stdout.close()
        require(process.wait() == 0, 'zstd could not read the dump')


# NLS's place types, as the kind a result shows: its common ones, read from the Finnish names each code holds most
# (2026-11-20: 1010210 mäki, vuori, vaara; 1020205 suo, neva; 2010210 lampi, järvi; 1010110 saari, luoto; …)
NLS_KINDS = {1010105: 'place/islands', 1010110: 'place/island', 1010115: 'natural/rock', 1010120: 'natural/cape',
             1010130: 'natural/beach', 1010205: 'natural/rocks', 1010210: 'natural/hill', 1010305: 'natural/valley',
             1010405: 'natural/shoal', 1020105: 'natural/heath', 1020205: 'natural/wetland', 1030105: 'natural/stone',
             2010110: 'natural/bay', 2010205: 'water/lakes', 2010210: 'water/lake', 2010215: 'natural/bay',
             2020105: 'waterway/stream', 2020110: 'waterway/channel', 2020115: 'waterway/rapids',
             3010105: 'place/village', 3010110: 'place/hamlet', 3020105: 'place/locality', 3030105: 'building/house',
             4010125: 'place/municipality', 4030105: 'amenity/place_of_worship', 5010105: 'boundary/national_park',
             6010205: 'place/suburb', 8030115: 'natural/beach'}
# the rest by the first digit of its code: terrain, water, settlements, public buildings, protected and cultural
# places, transport, industry and commerce, leisure, others
NLS_GROUPS = {1: 'natural/area', 2: 'natural/water', 3: 'place/locality', 4: 'building/public', 5: 'boundary/protected_area',
              6: 'landuse/transport', 7: 'landuse/commercial', 8: 'leisure/place', 9: 'natural/area'}
# NLS's scale relevance on Photon's importance scale: the median importance of the OSM places NLS's match
NLS_RANK = {25000: 0.11, 50000: 0.11, 100000: 0.13, 250000: 0.13, 500000: 0.15, 1000000: 0.17, 2000000: 0.41,
            4500000: 0.5, 8000000: 0.58}


def nls_page(url, key, sleep=time.sleep):
    """One page of NLS's place names, with one retry after a pause."""
    for attempt in (1, 2):
        try:
            separator = '&' if '?' in url else '?'
            request = urllib.request.Request(f"{url}{separator}{urlencode({'api-key': key})}",
                                             headers={'User-Agent': AGENT, 'Accept-Encoding': 'gzip'})
            with urllib.request.urlopen(request, timeout=300) as response:
                body = response.read()
                if response.headers.get('Content-Encoding') == 'gzip':
                    body = gzip.decompress(body)
                return json.loads(body)
        except (OSError, ValueError) as error:
            if isinstance(error, HTTPError) and error.code in (401, 403):
                raise RuntimeError(f'NLS refused the key ({error.code})') from None
            if attempt == 2:
                raise RuntimeError(f'NLS failed twice ({type(error).__name__})') from None
            sleep(60)


def fetch_nls(output, key, page=nls_page, sleep=time.sleep):
    """Every current place name, one line each: [place, name, language, type, scale, municipality, lon, lat], gzipped.
    Returns the count and the newest change among them."""
    require(re.fullmatch(r'[A-Za-z0-9-]{8,128}', key or ''), 'MML_API_KEY is missing or malformed')
    url, count, newest = f"{NLS}?{urlencode({'f': 'json', 'limit': PAGE})}", 0, ''
    with gzip.open(output, 'wt', encoding='utf-8') as out:
        while url:
            body = page(url, key, sleep)
            for f in body['features']:
                p = f['properties']
                if p.get('placeNameDeletionTime') or p.get('placeDeletionTime'):
                    continue
                lon, lat = f['geometry']['coordinates'][:2]
                out.write(json.dumps([p['placeId'], p['spelling'], p['language'], p['placeType'], p['scaleRelevance'],
                                      p.get('municipality') or '', round(lon, 5), round(lat, 5)], ensure_ascii=False) + '\n')
                newest = max(newest, p.get('placeNameModificationTime') or '', p.get('placeModificationTime') or '')
                count += 1
            url = next((link['href'] for link in body.get('links', []) if link.get('rel') == 'next'), None)
            if url:
                sleep(PAUSE)
    require(count > 500000, f'NLS gave only {count} place names')
    return count, newest


def nls(path):
    """NLS's places, {place: record}, from fetch_nls's file. Names in Finnish and Swedish first, then the Sámi ones."""
    order = {'fin': 0, 'swe': 1, 'sme': 2, 'smn': 3, 'sms': 4}
    places = {}
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        for raw in f:
            place, name, language, kind, scale, municipality, lon, lat = json.loads(raw)
            r = places.setdefault(place, {'id': f'L{place}', 'names': [], 'kind': NLS_KINDS.get(kind) or
                                          NLS_GROUPS.get(kind // 1000000, 'natural/area'),
                                          'lon': lon, 'lat': lat, 'rank': NLS_RANK.get(scale, 0.1), 'order': []})
            if clean(name) not in r['names']:
                r['names'].append(clean(name))
                r['order'].append(order.get(language, 9))
    for r in places.values():
        r['names'] = [n for _, n in sorted(zip(r.pop('order'), r['names']), key=lambda x: x[0])]
    return places


LATIN = re.compile(r"[A-Za-zÀ-ÿĀ-ž' .-]+")
EXONYMS = 100000  # people: a town this large keeps its names in other languages


def towns(path):
    """GeoNames' towns beyond Finland, {id: record}: their name and ASCII name, and a large town's names in Latin
    letters in other languages, so "Tukholma" finds Stockholm."""
    result = {}
    with zipfile.ZipFile(path) as z, z.open('cities15000.txt') as f:
        for raw in io.TextIOWrapper(f, encoding='utf-8'):
            row = raw.rstrip('\n').split('\t')
            if row[8] == 'FI':
                continue
            people = int(row[14] or 0)
            alternatives = [a for a in row[3].split(',') if LATIN.fullmatch(a) and not (a.isupper() and len(a) <= 3)
                            ] if people >= EXONYMS else []  # (not its airport's code)
            names = list(dict.fromkeys(clean(n) for n in [row[1], row[2], *alternatives] if clean(n)))
            result['G' + row[0]] = {'id': 'G' + row[0], 'names': names, 'kind': 'place/town', 'lon': float(row[5]),
                                    'lat': float(row[4]), 'rank': round(min(0.8, 0.3 + math.log10(max(people, 1)) / 20), 2),
                                    'city': row[8]}
    return result


def cell(lon, lat):
    return int(lat * 20), int(lon * 10)  # about 5.5 by 5 km in Finland


def merge(osm, names):
    """OSM's records, and NLS's places where OSM has no record of the same name in the same or a neighbouring cell."""
    seen = set()
    for r in osm.values():
        if r['id'][0] == 'C':
            continue  # a country has no position
        y, x = cell(r['lon'], r['lat'])
        for n in r['names']:
            seen.add((' '.join(words(n)), y, x))
    result = dict(osm)
    for r in names.values():
        y, x = cell(r['lon'], r['lat'])
        if not any((' '.join(words(n)), y + dy, x + dx) in seen
                   for n in r['names'] for dy in (-1, 0, 1) for dx in (-1, 0, 1)):
            result[r['id']] = r
    return result


# --- Files

def gz(text):
    return gzip.compress(text.encode(), 9, mtime=0)


def tile(lon, lat, z=AREA):
    lat = max(-85.0, min(85.0, lat))
    n = 2 ** z
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return f'{int((lon + 180) / 360 * n)}-{min(n - 1, int(y))}'


class Group:
    """One group's records by key, laid out in files: {name: bytes}, and the index rows [prefix, kind, bytes, sha256];
    a file under INLINE is a text row [prefix, kind, text] in the index instead. Kinds: L, every record with a key
    starting with the prefix; T, a split prefix's most important; W, the records whose key is the prefix exactly; A, an
    area's part of W, its prefix written word@x-y."""

    def __init__(self, lines, by_key):
        self.lines, self.by_key = lines, by_key  # {id: line}, {key: set of ids}
        self.keys = sorted(by_key)
        self.rank = {i: float(l.split('\t')[5]) for i, l in lines.items()}
        self.files, self.rows, self.texts, self.inlined = {}, [], [], 0

    def ids(self, prefix):
        lo = bisect.bisect_left(self.keys, prefix)
        hi = bisect.bisect_left(self.keys, prefix + '￿')
        return self.keys[lo:hi], set().union(*(self.by_key[k] for k in self.keys[lo:hi]))

    def body(self, ids):
        return '\n'.join(self.lines[i] for i in sorted(ids, key=lambda i: (-self.rank[i], i))) + '\n'

    def add(self, prefix, kind, ids):
        text = self.body(ids)
        data = gz(text)
        if len(data) < INLINE and self.inlined + len(data) <= INLINED:
            self.texts.append([prefix, kind, text])
            self.inlined += len(data)
            return
        sha = hashlib.sha256(data).hexdigest()
        self.files[sha + SUFFIX] = data
        self.rows.append([prefix, kind, len(data), sha])

    def fits(self, ids):
        return sum(len(self.lines[i]) + 1 for i in ids) <= TARGET * 4.5 and len(gz(self.body(ids))) <= TARGET

    def lay(self, prefix):
        keys, ids = self.ids(prefix)
        if not keys:
            return
        if self.fits(ids):
            self.add(prefix, 'L', ids)
            return
        self.add(prefix, 'T', sorted(ids, key=lambda i: (-self.rank[i], i))[:TOP])
        exact = self.by_key.get(prefix)
        if exact:
            if self.fits(exact):
                self.add(prefix, 'W', exact)
            else:
                areas = {}
                for i in exact:
                    f = self.lines[i].split('\t')
                    areas.setdefault(tile(float(f[3]), float(f[4])), set()).add(i)
                for area, part in sorted(areas.items()):
                    self.add(f'{prefix}@{area}', 'A', part)
        for c in sorted({k[len(prefix)] for k in keys if len(k) > len(prefix)}):
            self.lay(prefix + c)


def index(rows, texts, changed=(), removed=()):
    return gz(json.dumps({'schema': 1, 'files': rows, 'texts': texts, 'changed': sorted(changed),
                          'removed': sorted(removed)}, ensure_ascii=False, separators=(',', ':')))


def read_index(data):
    body = json.loads(gzip.decompress(data))
    require(body.get('schema') == 1, 'Unknown search index')
    return body


def layout(records, base=None):
    """The groups' files. Given base, the previous release ({group: (rows, texts, {id: line})}), a group keeps its files
    and lists what changed since them, until that outgrows CHANGES. Returns ({group: (index bytes, {name: bytes})},
    counts)."""
    lines, by_group = {}, {}
    for r in records.values():
        lines[r['id']] = line(r)
        for k in keys(r['names']):
            by_group.setdefault(group(k), {}).setdefault(k, set()).add(r['id'])
    result, counts = {}, {'kept': 0, 'made': 0, 'new': 0}
    for g in sorted(by_group):
        by_key = by_group[g]
        now = {i: lines[i] for ids in by_key.values() for i in ids}
        if base and g in base:
            rows, texts, old = base[g]
            changed = [now[i] for i in now if old.get(i) != now[i]]
            removed = [i for i in old if i not in now]
            if sum(len(l) + 1 for l in changed) + sum(len(i) + 1 for i in removed) <= CHANGES:
                result[g] = (index(rows, texts, changed, removed), {})
                counts['kept'] += 1
                continue
            counts['made'] += 1
        else:
            counts['new'] += 1
        made = Group(now, by_key)
        made.lay(g)
        result[g] = (index(made.rows, made.texts), made.files)
    return result, counts


def root_of(manifest):
    return next(f for f in manifest['files'] if f['path'] == ROOT)


def read_base(read, manifest):
    """The previous release's groups, {group: (rows, texts, {id: line})}: each group's records as its files hold them.
    read(entry) gives a file's bytes, checked against the entry."""
    root = read_index(read(root_of(manifest)))
    base = {}

    def one(row):
        g, size, sha = row
        body = read_index(read({'path': sha + SUFFIX, 'bytes': size, 'sha256': sha}))
        old = {}
        texts = [text for _, kind, text in body['texts'] if kind in 'LWA']
        texts += [gzip.decompress(read({'path': sha + SUFFIX, 'bytes': size, 'sha256': sha})).decode()
                  for _, kind, size, sha in body['files'] if kind in 'LWA']
        for text in texts:
            for l in text.splitlines():
                old[l.split('\t', 1)[0]] = l
        return g, (body['files'], body['texts'], old)  # what changed since is listed again, against these files

    with ThreadPoolExecutor(4) as pool:
        for g, value in pool.map(one, root['groups']):
            base[g] = value
    return base


def build(records, sources, output, base=None, now=None):
    """The release: the root index, every group's index and the files they name (all named by SHA-256), LICENSE.txt
    and a manifest, so publish.py uploads it. Files of base are named, not written: the host has them."""
    now = now or datetime.now(timezone.utc)
    output = Path(output)
    require(records and not output.exists(), 'No records, or the release folder exists')
    groups, counts = layout(records, base)
    output.mkdir(parents=True)
    rows = []
    for g, (data, files) in sorted(groups.items()):
        sha = hashlib.sha256(data).hexdigest()
        (output / (sha + SUFFIX)).write_bytes(data)
        for name, body in files.items():
            (output / name).write_bytes(body)
        rows.append([g, len(data), sha])
    root = gz(json.dumps({'schema': 1, 'groups': rows}, separators=(',', ':')))
    (output / ROOT).write_bytes(root)
    (output / 'LICENSE.txt').write_text(LICENSE_TEXT, encoding='utf-8')
    files = [{'path': name, 'bytes': (output / name).stat().st_size, 'sha256': publish.digest_file(output / name)}
             for name in (ROOT, 'LICENSE.txt')]
    release = stamp(now).replace('-', '').replace(':', '') + '-' + files[0]['sha256'][:12]
    inside = [r for r in records.values() if r['id'][0] not in 'CG']
    bounds = [round(f(r[k] for r in inside), 4) for f, k in ((min, 'lon'), (min, 'lat'), (max, 'lon'), (max, 'lat'))]
    manifest = {'schema': 1, 'release': release, 'built_at': stamp(now), 'source': sources, 'bounds': bounds,
                'coverage': json.loads(COVERAGE.read_text()),
                'datasets': {'search': {'format': publish.SEARCH, 'schema': 1, 'url': ROOT, 'bytes': files[0]['bytes'],
                                        'sha256': files[0]['sha256'], 'groups': len(rows), 'records': len(records),
                                        'changes': CHANGES}},
                'files': files, 'attribution': ATTRIBUTION, 'license': LICENSE}
    (output / 'manifest.json').write_bytes(json.dumps(publish.validate(manifest), sort_keys=True).encode())
    return manifest, counts


# --- Sources and the host

def get(url, target=None):
    """A source's file, to target, or its bytes."""
    request = urllib.request.Request(url, headers={'User-Agent': AGENT})
    with urllib.request.urlopen(request, timeout=600) as response:
        if target is None:
            return response.read()
        with open(target, 'wb') as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)


def latest_dump(listing):
    """The newest dated dump in GraphHopper's listing, and its name."""
    dates = sorted(set(PHOTON_FILE.findall(listing)))
    require(dates, 'No dated Photon dump for Finland')
    return f'photon-dump-finland-master-{dates[-1]}.jsonl.zst'


def fetch(work):
    """Photon's newest dump for Finland, checked by its MD5, NLS's names and GeoNames' towns, into work."""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    name = latest_dump(get(PHOTON).decode())
    get(PHOTON + name, work / 'photon.jsonl.zst')
    md5 = get(PHOTON + name + '.md5').decode().split()[0]
    digest = hashlib.md5()
    with open(work / 'photon.jsonl.zst', 'rb') as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    require(digest.hexdigest() == md5, 'Photon dump differs from its MD5')
    get(GEONAMES, work / 'cities15000.zip')
    count, newest = fetch_nls(work / 'nls.jsonl.gz', os.environ.get('MML_API_KEY'))
    (work / 'sources.json').write_text(json.dumps({
        'photon': {'url': PHOTON + name, 'md5': md5},
        'nls': {'url': NLS, 'names': count, 'modified': newest, 'fetched': stamp(datetime.now(timezone.utc))},
        'geonames': {'url': GEONAMES, 'fetched': stamp(datetime.now(timezone.utc))}}, indent=1))


def gather(work, now=None):
    """The records and their sources from fetch's folder. A dump older than MAX_AGE days is refused."""
    work = Path(work)
    sources = json.loads((work / 'sources.json').read_text())
    osm, timestamp = photon(dump_lines(work / 'photon.jsonl.zst'))
    age = (now or datetime.now(timezone.utc)) - datetime.fromisoformat(timestamp)
    require(age.days <= MAX_AGE, f'The Photon dump is {age.days} days old')
    sources['photon'] |= {'timestamp': timestamp, 'records': len(osm)}
    records = merge(osm, nls(work / 'nls.jsonl.gz'))
    beyond = towns(work / 'cities15000.zip')
    sources['geonames']['records'] = len(beyond)
    records.update(beyond)
    return records, sources


def require_counts(sources, previous):
    """Each source at least SHORT of what the previous release had: a source cut short never publishes as complete."""
    for name, field in (('photon', 'records'), ('nls', 'names'), ('geonames', 'records')):
        before = previous['source'].get(name, {}).get(field)
        require(not before or sources[name][field] >= SHORT * before,
                f'{name} gave {sources[name][field]} {field}, the previous release {before}')


def host_reader(url):
    """read(entry) for a release's files among the host's shared files, each checked by its SHA-256."""
    def read(entry):
        location = f'{url}/{publish.shared_path(entry)}'
        request = urllib.request.Request(location, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'})
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
        require(hashlib.sha256(body).hexdigest() == entry['sha256'], 'Hosted search file differs: ' + location)
        return body
    return read


def local_reader(*directories):
    """read(entry) for release folders that hold every file of a release between them, as a measurement keeps them:
    a release's folder holds only the files its base didn't have."""
    def read(entry):
        path = next((Path(d) / entry['path'] for d in directories if (Path(d) / entry['path']).exists()),
                    Path(directories[0]) / entry['path'])
        body = path.read_bytes()
        require(hashlib.sha256(body).hexdigest() == entry['sha256'], 'Search file differs: ' + entry['path'])
        return body
    return read


def newest(url):
    """The newest release on the host, the candidate or the live one, to build on; None before the first."""
    found = [json.loads(raw) for raw in (publish.found(url + '/candidates/current.json'), publish.found(url + '/current.json')) if raw]
    return max(found, key=lambda m: m['built_at']) if found else None


def folder_url():
    """polku-search's public URL, beside polku-osm's."""
    url = publish.public_url(os.environ.get('OSM_PUBLIC_URL', ''))
    require(url.endswith('/polku-osm'), 'OSM_PUBLIC_URL must end in /polku-osm')
    return url.removesuffix('/polku-osm') + '/' + FOLDER


def install_hosting():
    """polku-search and its .htaccess, polku-osm's own, once: an existing one is changed by hand."""
    config = publish.settings()
    require(config[4].endswith('/polku-osm'), 'OSM_PUBLIC_URL must end in /polku-osm')
    with publish.connection(config) as ftp:
        ftp.cwd('..')
        if FOLDER not in maintenance.children(ftp):
            ftp.mkd(FOLDER)
        require(ftp.kind(FOLDER) == 'directory', f'{FOLDER} needs a real directory')
        ftp.cwd(FOLDER)
        require('.htaccess' not in maintenance.children(ftp), f'{FOLDER}/.htaccess exists; change it by hand')
        ftp.storbinary('STOR .htaccess', io.BytesIO(Path(__file__).with_name('search.htaccess').read_bytes()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('fetch', help='download the sources into a work folder').add_argument('work', type=Path)
    making = commands.add_parser('build', help='write the release from the work folder, on the newest one on the host')
    making.add_argument('work', type=Path)
    making.add_argument('output', type=Path)
    making.add_argument('--base', type=Path, nargs='+', help='local release folders to build on, in place of the host, '
                        'the release first and then the folders of its bases')
    making.add_argument('--fresh', action='store_true', help='make every group\'s files again')
    commands.add_parser('upload', help='upload the release as the candidate').add_argument('directory', type=Path)
    commands.add_parser('install-hosting')
    commands.add_parser('candidate')
    promotion = commands.add_parser('promote')
    promotion.add_argument('release')
    promotion.add_argument('--sha256', required=True)
    promotion.add_argument('--directory', type=Path, required=True)
    back = commands.add_parser('rollback')
    back.add_argument('--manifest', type=Path, required=True)
    back.add_argument('--expected-current', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'fetch':
            fetch(args.work)
        elif args.command == 'build':
            records, sources = gather(args.work)
            base = None
            if args.base:
                previous = json.loads((args.base[0] / 'manifest.json').read_text())
                require_counts(sources, previous)
                base = read_base(local_reader(*args.base), previous)
            elif not args.fresh:
                url = folder_url()
                previous = newest(url)
                if previous:
                    require_counts(sources, previous)
                    base = read_base(host_reader(url), previous)
            manifest, counts = build(records, sources, args.output, base)
            print(json.dumps({'release': manifest['release'], **manifest['datasets']['search'], 'groups': counts}))
        elif args.command == 'upload':
            publish.upload(args.directory, folder=FOLDER, budget=BUDGET)
        elif args.command == 'install-hosting':
            install_hosting()
        elif args.command == 'candidate':
            print(json.dumps(publish.candidate(folder_url(), first=True) or {}, sort_keys=True))
            return
        elif args.command == 'promote':
            publish.promote_candidate(args.release, args.sha256, args.directory, FOLDER)
        else:
            publish.rollback(args.manifest, args.expected_current, FOLDER)
    except Exception as error:
        parser.exit(1, f'{args.command} failed: {error}\n')
    print(f'{args.command}: verified', file=sys.stderr)


if __name__ == '__main__':
    main()
