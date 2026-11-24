#!/usr/bin/env python3
"""Guarded pinned-host-key SFTP upload of candidates and their promotion; no hosting configuration is uploaded.

This repository uploads a verified candidate and names it in candidates/current.json. Polku's own weekly job runs
the app's checks against it and runs `promote` (and `health.py`) from Polku's checkout with Polku's credentials."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial
from datetime import datetime, timezone
import hmac
import hashlib
import gzip
import io
import json
import os
from pathlib import Path
import posixpath
import queue
import re
import stat
import sys
import threading
import time
import urllib.request
from urllib.error import HTTPError
from urllib.parse import urlsplit
import uuid

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.osm_data import maintenance

NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,120}\Z')
BLOCK = 4096
MAX_JSON = 2 * 1024 * 1024
SHARED = 'files'  # every release's files, named by their SHA-256, shared by the releases that name them
SHARED_NAME = re.compile(r'[a-f0-9]{64}(\.[A-Za-z0-9]{1,16})?\Z')
CELLS = 'pmtiles-json-cells'  # a dataset whose url is the index of its cells, not one archive
INDEX = '.cells.json'  # an archive's index takes its name, with this suffix for .pmtiles
SETTLE = 150  # seconds a public pointer may keep its last answer (settle)
SEARCH = 'search-gzt'  # a dataset whose url is a search index's root, which names its groups' indexes and their files
# SFTP channels at once on the one connection, gently on the host, for a release's many small files: each costs round
# trips, not bandwidth
CHANNELS = 4
BATCH = 64  # cells cut again from an archive, sent at once: at most 64 MiB in memory
RECONNECTS = 5  # a dropped connection: connect again and go on, after 30 s, 60 s, 120 s… up to 5 minutes
try:
    from paramiko import SSHException
except ImportError:  # (the checks of this module that need no SFTP)
    SSHException = OSError
CHANNEL_ERRORS = (OSError, EOFError, SSHException)


class Dropped(RuntimeError):
    """The SSH connection dropped part of the way; what was verified and moved stays."""


def require(ok, message):
    if not ok:
        raise ValueError(message)


def public_url(url):
    p = urlsplit(url)
    require(p.scheme == 'https' and p.hostname and not p.username and not p.password
            and not p.query and not p.fragment and not any(ord(c) < 33 for c in url),
            'Public URL must be HTTPS without credentials, query or fragment')
    return url.rstrip('/')


def shared_path(entry):
    """A file's place among the shared files: its SHA-256 and its own suffix."""
    return f"{SHARED}/{entry['sha256']}{posixpath.splitext(entry['path'])[1]}"


def share(entry):
    """The entry with its url among the shared files, relative to its release's folder."""
    return entry | {'url': '../../' + shared_path(entry)}


def place(release, entry, pointed=False):
    """Where a manifest's file lies in the product's folder. Without a url, in its release's own folder (the layout
    before the shared files); with one, among the shared files, relative to the release's folder or, in current.json,
    to the product's."""
    if 'url' not in entry:
        return f"releases/{release}/{entry['path']}"
    url = entry['url']
    path = url if pointed else posixpath.normpath(f'releases/{release}/{url}')
    require(isinstance(url, str) and path == shared_path(entry) and url == (path if pointed else '../../' + path)
            and SHARED_NAME.fullmatch(posixpath.basename(path)), 'Unsafe file URL')
    return path


def validate(manifest, pointed=False):
    release = manifest.get('release', '')
    require(manifest.get('schema') == 1 and isinstance(release, str) and NAME.fullmatch(release), 'Invalid release manifest')
    files = manifest.get('files', [])
    require(isinstance(files, list) and files, 'Manifest files are required')
    names, urls = set(), {}
    for f in files:
        name = f.get('path', '')
        require(isinstance(name, str) and NAME.fullmatch(name) and name not in {'current.json', 'manifest.json'}
                and name not in names, 'Unsafe or duplicate release file')
        require(type(f.get('bytes')) is int and f['bytes'] >= 0
                and re.fullmatch('[a-f0-9]{64}', f.get('sha256', '')), 'Invalid file integrity metadata')
        names.add(name)
        path = place(release, f, pointed)  # checks its url too
        urls[path if pointed else f.get('url', name)] = f
    require('LICENSE.txt' in names and manifest.get('datasets'), 'Release needs datasets and LICENSE.txt')
    for dataset in manifest['datasets'].values():
        url = dataset.get('url')
        entry = isinstance(url, str) and urls.get(url)
        suffix = {CELLS: '.json', SEARCH: '.gzt'}.get(dataset.get('format'), '.pmtiles')
        require(entry and url.endswith(suffix), 'Unsafe dataset URL')
        require(entry['bytes'] == dataset.get('bytes') and entry['sha256'] == dataset.get('sha256') and entry['bytes'] > 0,
                'Dataset and file integrity disagree')
    return manifest


def current(manifest):
    """current.json: the release's manifest with its URLs relative to the product's folder."""
    result = json.loads(json.dumps(manifest))
    for entry in [*result['files'], *result['datasets'].values()]:
        if 'url' in entry:
            entry['url'] = posixpath.normpath(f"releases/{result['release']}/{entry['url']}")
    return result


def cut(directory, manifest):
    """With OSM_CELLS: each archive replaced by the index of its cells, <name>.cells.json, written beside it."""
    from tools.osm_data import cells  # PMTiles is needed only to cut; the checks run on the standard library
    result = json.loads(json.dumps(manifest))
    files = {f['path']: f for f in result['files']}
    for dataset in result['datasets'].values():
        archive = dataset['url']
        print(f'Cut {archive} into cells', flush=True)
        body = cells.encoded(cells.index(directory / archive))
        path = archive.removesuffix('.pmtiles') + INDEX
        (directory / path).write_bytes(body)
        files[archive] = {'path': path, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
        dataset.update(url=path, bytes=len(body), sha256=files[archive]['sha256'], format=CELLS)
    result['files'] = list(files.values())
    return result


def cells_of(index_body, entry):
    """The cells an index names, as shared entries; the index must be the one its manifest names."""
    require(len(index_body) == entry['bytes'] and hashlib.sha256(index_body).hexdigest() == entry['sha256'],
            'Cell index differs from its manifest: ' + entry['path'])
    index = json.loads(index_body)
    require(index.get('schema') == 1 and type(index.get('zoom')) is int and isinstance(index.get('cells'), list)
            and index['cells'], 'Invalid cell index: ' + entry['path'])
    result = []
    for row in index['cells']:
        require(isinstance(row, list) and len(row) == 5 and all(type(v) is int for v in row[:4]) and row[3] > 0
                and isinstance(row[4], str) and re.fullmatch('[a-f0-9]{64}', row[4]), 'Invalid cell: ' + entry['path'])
        result.append(share({'path': row[4] + '.pmtiles', 'bytes': row[3], 'sha256': row[4]}))
    return result


def search_index(body, entry):
    """A search index's root or group index, gunzipped; it must be the one its entry names."""
    require(len(body) == entry['bytes'] and hashlib.sha256(body).hexdigest() == entry['sha256'],
            'Search index differs from its entry: ' + entry['path'])
    index = json.loads(gzip.decompress(body))
    rows = index.get('groups', index.get('files')) if isinstance(index, dict) and index.get('schema') == 1 else None
    width = 3 if 'groups' in index else 4
    require(isinstance(rows, list) and all(isinstance(row, list) and len(row) == width and type(row[-2]) is int
                                           and row[-2] > 0 and isinstance(row[-1], str)
                                           and re.fullmatch('[a-f0-9]{64}', row[-1]) for row in rows),
            'Invalid search index: ' + entry['path'])
    return [share({'path': row[-1] + '.gzt', 'bytes': row[-2], 'sha256': row[-1]}) for row in rows]


def indexes(manifest):
    """The manifest's cell indexes and search roots, as its file entries."""
    urls = {dataset['url'] for dataset in manifest['datasets'].values() if dataset.get('format') in (CELLS, SEARCH)}
    return [f for f in manifest['files'] if f.get('url', f['path']) in urls]


def named(manifest, read, read_all=None):
    """Every shared file the manifest's indexes name: a cells index's cells, a search root's group indexes and the
    files they name. read(entry) gives an index's bytes, or None where it isn't there: it then names nothing. read_all
    reads a list of them at once, in order."""
    for f in indexes(manifest):
        body = read(f)
        if body is None:
            continue
        if f['path'].endswith(INDEX):
            yield from cells_of(body, f)
            continue
        groups = search_index(body, f)
        yield from groups
        for group, body in zip(groups, read_all(groups) if read_all else map(read, groups)):
            if body is not None:
                yield from search_index(body, group)


def index_entry(dataset):
    """A cells dataset's index as a file entry."""
    return {'path': dataset['url'], 'bytes': dataset['bytes'], 'sha256': dataset['sha256']}


def hosted_named(ftp, manifest, present=None):
    """Every file the manifest's indexes on the host name. Given present, an index not among them names none."""
    def read(entry, channel=ftp):
        path = place(manifest['release'], entry)
        return retrieve(channel, path) if present is None or path in present else None
    return named(manifest, read, lambda entries: at_once(ftp, entries, lambda channel, entry: read(entry, channel)))


def at_once(ftp, items, work):
    """[work(channel, item)] for each item, in order, over CHANNELS channels of ftp's one SSH connection at once. A
    channel's error (OSError, EOFError, paramiko's SSHException), opening it too, sends what's left one by one over ftp
    itself, where a real error stops the run. Any other error, such as a file that differs, stops it at once."""
    items, done = list(items), {}
    if len(items) > 1:
        opened, free, failed = [], queue.SimpleQueue(), threading.Event()

        def give_up(error):
            if not failed.is_set():
                print(f'An SFTP channel failed ({type(error).__name__}); the rest go one at a time', flush=True)
            failed.set()
        try:
            for _ in range(min(CHANNELS, len(items))):
                opened.append(ftp.channel())
                free.put(opened[-1])
        except CHANNEL_ERRORS as error:
            give_up(error)

        def one(i):
            if failed.is_set():
                return
            channel = free.get()
            try:
                done[i] = work(channel, items[i])
            except CHANNEL_ERRORS as error:
                give_up(error)
            finally:
                free.put(channel)
        if not failed.is_set():
            with ThreadPoolExecutor(len(opened)) as pool:
                list(pool.map(one, range(len(items))))
        for channel in opened:
            channel.close_channel()
    return [done[i] if i in done else work(ftp, item) for i, item in enumerate(items)]


def shared_manifest(manifest):
    """The manifest as uploaded with OSM_SHARED_FILES: every file among the shared files, so a file that hasn't changed
    keeps its URL and isn't sent again."""
    result = json.loads(json.dumps(manifest))
    urls = {}
    for entry in result['files']:
        urls[entry['path']] = entry['url'] = share(entry)['url']
    for dataset in result['datasets'].values():
        dataset['url'] = urls[dataset['url']]
    return result


def settings():
    values = [os.environ.get('OSM_' + key, '') for key in ('FTP_HOST', 'FTP_USER', 'FTP_PASSWORD', 'FTP_ROOT', 'PUBLIC_URL', 'SFTP_HOST_KEY_SHA256')]
    host, user, password, root, url, fingerprint = values
    require(all(values), 'Missing SFTP publication settings, including OSM_SFTP_HOST_KEY_SHA256')
    require(re.fullmatch(r'[A-Za-z0-9.-]+', host), 'Invalid SFTP hostname')
    require(re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', fingerprint), 'Invalid pinned SSH host key SHA256 fingerprint')
    require(root.startswith('/') and root.endswith('/polku-osm') and root == posixpath.normpath(root)
            and '\\' not in root and not any(p in {'.', '..'} for p in root.split('/'))
            and not any(ord(c) < 32 or ord(c) == 127 for c in root), 'Unsafe FTP root: require a dedicated /polku-osm directory')
    return host, user, password, root, public_url(url), fingerprint


def kind_of(mode):
    return 'directory' if stat.S_ISDIR(mode) else 'file' if stat.S_ISREG(mode) else 'other'


class SFTPFiles:
    """Small FTP-like file-operation facade; all transfers are SSH-encrypted."""
    def __init__(self, transport, sftp):
        self.transport, self.sftp = transport, sftp

    def cwd(self, path):
        self.sftp.chdir(path)

    def nlst(self, path='.'):
        return self.sftp.listdir(path)

    def mkd(self, path):
        return self.sftp.mkdir(path)

    def size(self, path):
        return self.sftp.stat(path).st_size

    def kind(self, path):
        return kind_of(self.sftp.lstat(path).st_mode)

    def attributes(self, path='.'):
        """{name: (kind, size)} for a folder, in one request rather than two per entry."""
        return {a.filename: (kind_of(a.st_mode), a.st_size) for a in self.sftp.listdir_attr(path)}

    def remove(self, path):
        return self.sftp.remove(path)

    def rmd(self, path):
        return self.sftp.rmdir(path)

    def storbinary(self, command, body, rest=None):
        require(command.startswith('STOR '), 'Invalid upload command')
        path = command[5:]
        if rest is not None:
            require(self.kind(path) == 'file' and self.size(path) == rest, 'Resume offset or regular file changed')
        print(f'Upload {path} from byte {rest or 0}', flush=True)
        seen = rest or 0
        with self.sftp.open(path, 'r+b' if rest is not None else 'wb') as output:
            output.set_pipelined(True)
            if rest is not None:
                output.seek(rest)
            while chunk := body.read(1024 * 1024):
                output.write(chunk)
                seen += len(chunk)
                if seen // (64 * 1024 * 1024) != (seen - len(chunk)) // (64 * 1024 * 1024):
                    print(f'Uploaded {path}: {seen} bytes', flush=True)
            output.flush()
        print(f'Upload closed {path}: {seen} bytes', flush=True)

    def retrbinary(self, command, callback):
        require(command.startswith('RETR '), 'Invalid download command')
        print(f'Read back {command[5:]}', flush=True)
        seen = 0
        with self.sftp.open(command[5:], 'rb') as source:
            source.prefetch(file_size=source.stat().st_size, max_concurrent_requests=64)
            while chunk := source.read(1024 * 1024):
                callback(chunk)
                seen += len(chunk)
                if seen // (64 * 1024 * 1024) != (seen - len(chunk)) // (64 * 1024 * 1024):
                    print(f'Read back {command[5:]}: {seen} bytes', flush=True)
        print(f'Read back complete {command[5:]}: {seen} bytes', flush=True)

    def rename(self, old, new):
        # Immutable directory moves never overwrite an existing release.
        return self.sftp.rename(old, new)

    def replace(self, old, new):
        # POSIX rename-over-existing is mandatory for atomic pointer switching.
        # Do not delete current.json or fall back to an unsafe SFTP rename.
        return self.sftp.posix_rename(old, new)

    def channel(self):
        """Another SFTP channel of the same SSH connection, in the same folder, for at_once()."""
        import paramiko
        sftp = paramiko.SFTPClient.from_transport(self.transport)
        sftp.chdir(self.sftp.getcwd())
        return SFTPFiles(self.transport, sftp)

    def close_channel(self):
        self.sftp.close()

    def active(self):
        return self.transport.is_active()

    def close(self):
        try:
            self.sftp.close()
        finally:
            self.transport.close()


def open_sftp(host, user, password, expected_fingerprint):
    """Verify the server key *before* sending credentials, with no TOFU fallback."""
    import paramiko
    transport = paramiko.Transport((host, 22))
    try:
        transport.start_client(timeout=30)
        key = transport.get_remote_server_key()
        actual = 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode('ascii').rstrip('=')
        require(hmac.compare_digest(actual, expected_fingerprint),
                'SFTP server host key differs from pinned fingerprint; stop and verify with the host')
        transport.auth_password(username=user, password=password)
        require(transport.is_authenticated(), 'SFTP authentication failed')
        client = paramiko.SFTPClient.from_transport(transport)
        require(client is not None, 'SFTP subsystem unavailable')
        return SFTPFiles(transport, client)
    except BaseException:
        transport.close()
        raise


@contextmanager
def connection(config):
    host, user, password, root, _, fingerprint = config
    remote = None
    try:
        remote = open_sftp(host, user, password, fingerprint)
        remote.cwd(root)
        yield remote
    except Exception as error:
        message = str(error).replace(password, '[redacted]').replace(user, '[redacted]')
        if remote is not None and isinstance(error, CHANNEL_ERRORS) and not remote.active():
            raise Dropped(message) from None
        raise RuntimeError(message) from None
    finally:
        if remote is not None:
            remote.close()


def retrieve(ftp, path):
    result = bytearray()
    def take(chunk):
        require(len(result) + len(chunk) <= MAX_JSON, 'Manifest exceeds size limit')
        result.extend(chunk)
    ftp.retrbinary('RETR ' + path, take)
    return bytes(result)


def offsets(size):
    starts = {0, max(0, size // 2 - BLOCK // 2), max(0, size - BLOCK)}
    starts.update(n for n in (2 ** 31, 2 ** 32) if n < size)
    return [(s, min(s + BLOCK, size)) for s in sorted(starts)]


def digest_file(path):
    sha = hashlib.sha256()
    with path.open('rb') as body:
        for chunk in iter(lambda: body.read(1024 * 1024), b''):
            sha.update(chunk)
    return sha.hexdigest()


def verify_file(ftp, path, entry):
    """The whole file's SHA-256, and its bytes where the public check reads them."""
    size = entry['bytes']
    print(f'Verify full SHA-256 {path}', flush=True)
    require(ftp.size(path) == size, 'Uploaded file size differs')
    sha, seen = hashlib.sha256(), 0
    chunks = {s: bytearray() for s, _ in offsets(size)}
    def take(chunk):
        nonlocal seen
        sha.update(chunk)
        for start, end in offsets(size):
            a, b = max(start, seen), min(end, seen + len(chunk))
            if a < b:
                chunks[start].extend(chunk[a - seen:b - seen])
        seen += len(chunk)
    ftp.retrbinary('RETR ' + path, take)
    require(seen == size and sha.hexdigest() == entry['sha256'], 'Uploaded file SHA-256 differs')
    return {s: bytes(data) for s, data in chunks.items()}


def move_shared(ftp, staged, target):
    """A verified file into its place among the shared files."""
    if SHARED not in maintenance.children(ftp):
        ftp.mkd(SHARED)
    print(f'Move verified {staged} to {target}', flush=True)
    ftp.rename(staged, target)


def verify_manifest(ftp, base, raw):
    require(ftp.size(base + '/manifest.json') == len(raw) and retrieve(ftp, base + '/manifest.json') == raw,
            'Uploaded manifest differs')


def verify_remote(ftp, base, manifest, raw):
    """Every file the release names, hashed in full: in base (its own folder, or its staging) or among the shared
    files. The samples are keyed by where each file lies once the release is in place."""
    samples = {}
    for f in manifest['files']:
        path = place(manifest['release'], f)
        samples[path] = verify_file(ftp, path if 'url' in f else f"{base}/{f['path']}", f)
    at_once(ftp, hosted_named(ftp, manifest), lambda channel, entry: verify_file(channel, place(manifest['release'], entry), entry))
    verify_manifest(ftp, base, raw)
    return samples


def get(url, byte_range=None, immutable=False, encoding='gzip, br'):
    # The host can reject Python's default user agent, though browser requests succeed.
    headers = {'Origin': 'https://polku.si', 'Accept-Encoding': encoding,
               'User-Agent': 'Mozilla/5.0'}
    if byte_range:
        start, end, size = byte_range
        headers['Range'] = f'bytes={start}-{end - 1}'
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        h = response.headers
        require(h.get('Access-Control-Allow-Origin') == '*', 'Public response needs wildcard CORS')
        require(not h.get('Content-Encoding'), 'Public data must not use HTTP Content-Encoding')
        if urlsplit(url).path.endswith('/current.json'):
            directives = {value.strip().lower() for value in h.get('Cache-Control', '').split(',')}
            require('immutable' not in directives and directives & {'no-cache', 'no-store', 'max-age=0'},
                    'current.json must disable caching or require revalidation')
        if immutable:
            cache = h.get('Cache-Control', '').lower()
            require('immutable' in cache and 'max-age=31536000' in cache, 'Release needs immutable cache headers')
        mime = h.get('Content-Type', '').split(';')[0].strip().lower()
        if byte_range:
            require(response.status == 206 and h.get('Content-Range') == f'bytes {start}-{end - 1}/{size}', 'Invalid HTTP Range response')
            exposed = {v.strip().lower() for v in h.get('Access-Control-Expose-Headers', '').split(',')}
            require('*' in exposed or {'content-range', 'accept-ranges', 'content-length'} <= exposed, 'Range headers must be exposed to browsers')
            require(h.get('Accept-Ranges') == 'bytes' and h.get('Content-Length') == str(end - start), 'Invalid range length headers')
            require(mime in {'application/vnd.pmtiles', 'application/octet-stream'}, 'Invalid archive MIME type')
            body = response.read(end - start + 1)
            require(len(body) == end - start, 'Range body length differs')
        else:
            require(response.status == 200 and mime == 'application/json', 'Invalid manifest HTTP response')
            body = response.read(MAX_JSON + 1)
            require(len(body) <= MAX_JSON, 'Manifest exceeds size limit')
        return body


def probe(url, manifest, raw, samples=None):
    """The public manifest, and each archive's Range reads (or, for cells, the index and its first, middle and last
    cells); compared with the bytes just hashed, where there are any."""
    require(get(f"{url}/releases/{manifest['release']}/manifest.json", immutable=True) == raw, 'Public manifest differs')
    for dataset in current(manifest)['datasets'].values():
        reads = [(dataset['url'], dataset['bytes'])]
        if dataset.get('format') == CELLS:
            rows = cells_of(get(f"{url}/{dataset['url']}", immutable=True), index_entry(dataset))
            reads = [(shared_path(rows[i]), rows[i]['bytes']) for i in sorted({0, len(rows) // 2, len(rows) - 1})]
        for path, size in reads:
            for start, end in offsets(size):
                for encoding in ('identity', 'gzip, br'):
                    body = get(f'{url}/{path}', (start, end, size), True, encoding)
                    if samples and path in samples:
                        require(body == samples[path][start], 'Public archive bytes differ')


def settle(url, expected, limit, pause=2):
    """Seconds until the public URL answers `expected` (None: a 404), read again every `pause` s for up to `limit` s.
    The host can keep answering a name as it last did: polku-mml/current.json, asked for while missing, stayed 404
    after it was written (Polku run 45242600287)."""
    start = time.monotonic()
    while True:
        body = found(url)
        if body == expected:
            return time.monotonic() - start
        require(time.monotonic() - start < limit, f'{url} did not answer as written within {limit} s')
        time.sleep(pause)


def put_back(ftp, url, previous):
    """The live pointer as it was before a promotion: the saved bytes, or none at all."""
    if previous is None:
        ftp.remove('current.json')
    else:
        replace_pointer(ftp, previous)
    settle(url + '/current.json', previous, SETTLE)


def replace_pointer(ftp, raw, path='current.json'):
    name = posixpath.join(posixpath.dirname(path), f'current.{uuid.uuid4().hex}.tmp')
    ftp.storbinary('STOR ' + name, io.BytesIO(raw))
    require(ftp.size(name) == len(raw) and retrieve(ftp, name) == raw, 'Temporary pointer differs')
    # OSM_ALLOW_PUBLISH is set only after the operator verifies atomic overwrite on this host.
    ftp.replace(name, path)


def promote(ftp, url, manifest, previous, directory, samples, release_raw):
    """current.json names the release. The pointer it replaces is saved for rollback: null where there was none."""
    directory.mkdir(parents=True, exist_ok=True)  # a first promotion saves null too, where nothing was saved before
    (directory / f'rollback-{uuid.uuid4().hex}.json').write_bytes(previous or b'null')
    raw = json.dumps(current(manifest), sort_keys=True, separators=(',', ':')).encode()
    maintenance.prepare_publication(ftp, manifest, release_raw, previous)
    replace_pointer(ftp, raw)
    try:
        print(f"Public pointer after {settle(url + '/current.json', raw, SETTLE):.0f} s", flush=True)
        probe(url, manifest, release_raw, samples)
    except Exception:
        try:
            put_back(ftp, url, previous)
        except Exception:
            raise RuntimeError('Promotion verification failed; rollback also failed; use the saved rollback manifest') from None
        raise RuntimeError('Promotion verification failed; previous pointer restored') from None


def previous_pointer(ftp):
    if 'current.json' not in {posixpath.basename(p) for p in ftp.nlst()}:
        return None
    raw = retrieve(ftp, 'current.json')
    validate(json.loads(raw), pointed=True)
    return raw


def local_release(directory):
    raw = (directory / 'manifest.json').read_bytes()
    manifest = validate(json.loads(raw))
    for f in manifest['files']:
        path = directory / f['path']
        require(path.is_file() and not path.is_symlink() and path.stat().st_size == f['bytes']
                and digest_file(path) == f['sha256'], 'Local file integrity differs')
    return raw, manifest


def trusted_build_run(repository, run):
    require(type(run.get('id')) is int and run['id'] > 0 and run.get('status') == 'completed'
            and run.get('head_branch') == 'main' and run.get('head_repository', {}).get('full_name') == repository
            and run.get('event') in {'schedule', 'workflow_dispatch'}
            and run.get('path') == '.github/workflows/osm-data.yml',
            'Recovery needs a completed main OSM build from this repository')


def recovery_source(repository, run, jobs, artifacts):
    trusted_build_run(repository, run)
    countries = {'snapshot', *(f'build ({country})' for country in ('finland', 'sweden', 'norway', 'estonia'))}
    passed = {job['name'] for job in jobs if job.get('conclusion') == 'success'}
    # A geometry recovery verifies the original four-country run before reusing its artifacts.
    require('bundle' in passed and (countries <= passed or 'geometry' in passed),
            'All four country builds or verified geometry recovery, and bundle must have passed')
    candidates = [artifact for artifact in artifacts if artifact.get('name') == 'nordic-osm-release']
    require(len(candidates) == 1 and candidates[0].get('expired') is False
            and type(candidates[0].get('size_in_bytes')) is int and candidates[0]['size_in_bytes'] > 0,
            'Recovery needs one unexpired four-country release artifact')
    return str(run['id'])


def recovery_countries(repository, run, jobs, artifacts):
    trusted_build_run(repository, run)
    countries = ('finland', 'sweden', 'norway', 'estonia')
    required = {'snapshot', *(f'build ({country})' for country in countries)}
    passed = {job['name'] for job in jobs if job.get('conclusion') == 'success'}
    require(required <= passed, 'All four country builds must have passed')
    names = {f'osm-{country}-{kind}' for country in countries for kind in ('release', 'geometry-inputs')}
    selected = [a for a in artifacts if a.get('name') in names]
    require(len(selected) == len(names) and {a['name'] for a in selected} == names
            and all(a.get('expired') is False and type(a.get('size_in_bytes')) is int and a['size_in_bytes'] > 0 for a in selected),
            'Recovery needs every distinct unexpired country release and geometry input artifact')
    return str(run['id'])


def recovery_candidates(ftp, release):
    names = {posixpath.basename(p) for p in ftp.nlst()}
    stages = sorted(p for p in names if re.fullmatch(r'\.upload-' + re.escape(release) + r'-[a-f0-9]{32}', p))
    final = f'releases/{release}'
    if 'releases' in names:
        require(ftp.kind('releases') == 'directory', 'Releases needs a real directory')
        if release in {posixpath.basename(p) for p in ftp.nlst('releases')}:
            stages.append(final)
    return stages


def hosted(directory):
    """The local release's manifest as the host gets it, and its bytes: with OSM_SHARED_FILES its files go among the
    shared files, and with OSM_CELLS (which shares them too) each archive goes as its cells."""
    raw, manifest = local_release(directory)
    in_cells = os.environ.get('OSM_CELLS') == 'true'
    if in_cells or os.environ.get('OSM_SHARED_FILES') == 'true':
        manifest = validate(shared_manifest(cut(directory, manifest) if in_cells else manifest))
        raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    return raw, manifest


def inspect_release(directory):
    """Report the interrupted release's metadata without modifying the host."""
    raw, manifest = hosted(Path(directory))
    expected = {f['path']: f['bytes'] for f in manifest['files']} | {'manifest.json': len(raw)}
    result = {'release': manifest['release'], 'candidates': []}
    with connection(settings()) as ftp:
        have = maintenance.shared_files(ftp)
        result['shared'] = sorted(f['path'] for f in manifest['files'] if place(manifest['release'], f) in have)
        for base in recovery_candidates(ftp, manifest['release']):
            entry = {'path': base, 'kind': ftp.kind(base), 'files': {}}
            if entry['kind'] == 'directory':
                names = {posixpath.basename(p) for p in ftp.nlst(base)}
                for name, size in expected.items():
                    path = base + '/' + name
                    kind = ftp.kind(path) if name in names else 'missing'
                    entry['files'][name] = {'kind': kind, 'expected': size,
                                            'actual': ftp.size(path) if kind == 'file' else None}
            result['candidates'].append(entry)
    return result


def resume_upload(ftp, base, directory, manifest, raw, send):
    require(ftp.kind(base) == 'directory', 'Resume needs a real directory, not a symlink')
    names = {posixpath.basename(p) for p in ftp.nlst(base)}
    record = {f['path']: f['bytes'] for f in manifest['files']} | {'manifest.json': len(raw)}
    staged_cells = {name for name in names if SHARED_NAME.fullmatch(name)}  # made again from the archive, sent whole
    require(names - staged_cells <= record.keys() | {maintenance.UPLOAD}, 'Unexpected files in staging directory')
    if maintenance.UPLOAD in names:
        marker, _ = maintenance.upload_record(ftp, base, manifest['release'], datetime.now(timezone.utc))
        require(marker['files'] == record, 'Upload receipt differs from local release')
    expected = send | {'manifest.json': len(raw)}

    def source(name):
        """The manifest goes up as the host gets it, which isn't the local file when the files are shared."""
        return io.BytesIO(raw) if name == 'manifest.json' else (directory / name).open('rb')

    sizes = {}
    # Check every partial prefix before writing anything; complete files get full hash verification below.
    for name, size in expected.items():
        path = base + '/' + name
        if name not in names:
            continue
        require(ftp.kind(path) == 'file', 'Resume needs regular files, not symlinks')
        actual = sizes[name] = ftp.size(path)
        require(actual <= size, 'Staged file is larger than local file')
        if actual < size:
            with source(name) as body:
                def compare(chunk):
                    require(body.read(len(chunk)) == chunk, 'Staged file prefix differs')
                ftp.retrbinary('RETR ' + path, compare)
                require(body.tell() == actual, 'Staged file prefix size changed')
    for name in names - expected.keys() - {maintenance.UPLOAD}:
        print(f'Remove staged {base}/{name}: the shared files hold it, or it is a cell made and sent again', flush=True)
        ftp.remove(base + '/' + name)
    for name, size in expected.items():
        actual = sizes.get(name)
        if actual == size:
            print(f'Reuse complete {base}/{name}: {size} bytes; full hash verification follows', flush=True)
            continue
        with source(name) as body:
            body.seek(actual or 0)
            ftp.storbinary('STOR ' + base + '/' + name, body, rest=actual)


def into_folder(ftp, url, folder):
    """Into a product's folder beside polku-osm, once its hosting is in; its public URL. Without folder, polku-osm."""
    if not folder:
        return url
    url = product_url(url, folder)
    ftp.cwd('..')
    require(folder in maintenance.children(ftp), f'{folder} is missing: run install-hosting first')
    ftp.cwd(folder)
    require('.htaccess' in maintenance.children(ftp), f'{folder} has no .htaccess: run install-hosting first')
    return url


def product_url(osm_url, folder):
    """A product folder's public URL, beside polku-osm's."""
    require(osm_url.endswith('/polku-osm'), 'OSM_PUBLIC_URL must end in /polku-osm')
    return osm_url.removesuffix('/polku-osm') + '/' + folder


def install_hosting(folder):
    """A product's folder beside polku-osm, with a copy of hosting.htaccess, once: an existing one is changed by hand."""
    config = settings()
    product_url(config[4], folder)  # (polku-osm's URL ends as it should)
    with connection(config) as ftp:
        ftp.cwd('..')
        if folder not in maintenance.children(ftp):
            ftp.mkd(folder)
        require(ftp.kind(folder) == 'directory', f'{folder} needs a real directory')
        ftp.cwd(folder)
        require('.htaccess' not in maintenance.children(ftp), f'{folder}/.htaccess exists; change it by hand')
        ftp.storbinary('STOR .htaccess', io.BytesIO(Path(__file__).with_name('hosting.htaccess').read_bytes()))


def require_covers(folder, manifest):
    """A test box never replaces a larger copy: the release must cover the product's candidate and live release, or
    retention would remove the larger one, and a promotion would make the smaller one live."""
    url = product_url(public_url(os.environ.get('OSM_PUBLIC_URL', '')), folder)
    for pointer in ('candidates/current.json', 'current.json'):
        try:
            old = json.loads(get(f'{url}/{pointer}'))['bounds']
        except HTTPError as error:
            if error.code != 404:
                raise
            error.close()
            continue
        new = manifest['bounds']
        require(new[0] <= old[0] and new[1] <= old[1] and new[2] >= old[2] and new[3] >= old[3],
                f'{pointer} covers {old}, more than this release; upload a box that covers it')


def restore(directory, expected_current, folder):
    """Back to the pointer promote_candidate replaced, the one it saved in directory, unless another release went live
    meanwhile."""
    saved = sorted(Path(directory).glob('rollback-*.json'))
    require(len(saved) == 1, f'{len(saved)} saved pointers in {directory}; restore needs the one promote saved')
    rollback(saved[0], expected_current, folder=folder)


def upload(directory, resume=False, folder=None, budget=None):
    """Upload, verify and name the candidate; the live pointer is left to Polku's promotion. A file the shared files
    already hold isn't sent again. With folder, into that product's folder beside polku-osm, once its hosting is in;
    budget is the folder's size limit, the OSM data's by default. A dropped connection connects again and goes on from
    its stage: a file verified and moved among the shared files isn't sent again."""
    for attempt in range(RECONNECTS + 1):
        try:
            return upload_once(directory, resume, folder, budget, again=attempt > 0)
        except Dropped as error:
            if attempt == RECONNECTS:
                raise
            pause = min(30 * 2 ** attempt, 300)
            print(f'The connection dropped ({error}); connecting again in {pause} s', flush=True)
            time.sleep(pause)


def upload_once(directory, resume, folder, budget, again):
    """upload() over one connection; again, after a dropped one, goes on from this release's stage if there is one."""
    require(os.environ.get('OSM_ALLOW_PUBLISH') == 'true', 'Publication requires approved OSM_ALLOW_PUBLISH=true')
    budget, product = budget or maintenance.BUDGET, folder or 'OSM'
    directory = Path(directory)
    raw, manifest = hosted(directory)
    config = settings()
    with connection(config) as ftp:
        url = into_folder(ftp, config[-2], folder)
        previous = previous_pointer(ftp)
        if previous and 'geometry' in json.loads(previous)['datasets']:
            require('geometry' in manifest['datasets'], 'Publication would remove live geometry')
        release = manifest['release']
        final = f'releases/{release}'
        # A shared file is there only once its upload was hashed in full; retention keeps whatever this release names
        have, places = maintenance.shared_files(ftp), {f['path']: place(release, f) for f in manifest['files']}
        for f in manifest['files']:
            require(have.get(places[f['path']], f['bytes']) == f['bytes'], 'Shared file size differs: ' + places[f['path']])
        send, sending = {}, set(have)
        for f in manifest['files']:  # the same content twice in one release lies in one place: sent once
            if places[f['path']] not in sending:
                send[f['path']] = f['bytes']
                sending.add(places[f['path']])
        print(f'Send {len(send)} of {len(places)} files; the shared files hold the rest', flush=True)
        def local(entry):  # the release folder holds every index it names; a cell is cut again below
            path = directory / entry['path']
            return path.read_bytes() if path.exists() else None
        indexed = list(named(manifest, local))
        send_cells = {}  # each file the indexes name and the shared files lack, by its place, once
        for entry in indexed:
            if place(release, entry) not in sending:
                send_cells[place(release, entry)] = entry
                sending.add(place(release, entry))
        if indexes(manifest):
            print(f'Send {len(send_cells)} indexed files; the shared files hold the rest', flush=True)
        # Retention keeps all this release names, though its new indexes aren't on the host yet
        naming = set(places.values()) | {place(release, entry) for entry in indexed}
        expected = send | {'manifest.json': len(raw)}
        incoming = sum(expected.values()) + sum(entry['bytes'] for entry in send_cells.values())
        resume = resume or again and bool(recovery_candidates(ftp, release))
        if resume:
            candidates = recovery_candidates(ftp, release)
            require(final in candidates or len(candidates) == 1, 'Resume needs exactly one existing stage; inspect before proceeding')
            stage = final if final in candidates else candidates[0]
            require(ftp.kind(stage) == 'directory', 'Resume needs a real directory')
            names = maintenance.children(ftp, stage)
            require(all(ftp.kind(stage + '/' + name) == 'file' for name in names), 'Resume needs regular files')
            incoming = sum(max(0, size - ftp.size(stage + '/' + name)) if name in names else size
                           for name, size in expected.items()) + sum(entry['bytes'] for entry in send_cells.values())
        if previous is not None:
            retention = maintenance.prune(ftp, apply=True, incoming_bytes=incoming, incoming=manifest,
                                          incoming_files=naming, budget=budget, product=product)
            print(json.dumps(retention, sort_keys=True), flush=True)
        else:
            require(maintenance.usage(ftp) + incoming + 4096 <= budget,
                    f'{product} storage budget exceeded ({budget / 1024**3:g} GiB)')
        if resume:
            if stage == final:
                require(all(ftp.kind(final + '/' + name) == 'file' for name in expected), 'Release needs regular files')
            else:
                resume_upload(ftp, stage, directory, manifest, raw, send)
        else:
            if 'releases' not in {posixpath.basename(p) for p in ftp.nlst()}:
                ftp.mkd('releases')
            require(release not in {posixpath.basename(p) for p in ftp.nlst('releases')}, 'Immutable release already exists')
            stage = f'.upload-{release}-{uuid.uuid4().hex}'
            ftp.mkd(stage)
            maintenance.mark_upload(ftp, stage, manifest, raw)
            for name in send:
                with (directory / name).open('rb') as body:
                    ftp.storbinary('STOR ' + stage + '/' + name, body)
            ftp.storbinary('STOR ' + stage + '/manifest.json', io.BytesIO(raw))
        samples = {}
        for f in manifest['files']:
            if f['path'] not in send:
                continue
            staged, target = f"{stage}/{f['path']}", places[f['path']]
            samples[target] = verify_file(ftp, staged, f)
            if target != f"{final}/{f['path']}":
                move_shared(ftp, staged, target)
        if send_cells and SHARED not in maintenance.children(ftp):
            ftp.mkd(SHARED)

        def send_shared(channel, item):  # as move_shared(), but files/ is made once above rather than listed each time
            where, entry, opener = item
            staged = f"{stage}/{entry['path']}"
            with opener() as body:
                channel.storbinary('STOR ' + staged, body)
            verify_file(channel, staged, entry)
            print(f'Move verified {staged} to {where}', flush=True)
            try:
                channel.rename(staged, where)
            except CHANNEL_ERRORS:
                # sent again after a channel failed once its rename had gone through: it's there, at its size
                require(channel.size(where) == entry['bytes'], 'Shared file differs: ' + where)
                channel.remove(staged)
        # a search index's files, as the release folder holds them
        here = [(where, entry, partial((directory / entry['path']).open, 'rb'))
                for where, entry in send_cells.items() if (directory / entry['path']).exists()]
        at_once(ftp, here, send_shared)
        for where, _, _ in here:
            del send_cells[where]
        for f in indexes(manifest):  # each archive cut again, the same way; only its missing cells go up
            if not f['path'].endswith(INDEX) or not any(
                    place(release, entry) in send_cells for entry in cells_of((directory / f['path']).read_bytes(), f)):
                continue
            from tools.osm_data import cells
            batch = []
            for _, data in cells.cells(directory / (f['path'].removesuffix(INDEX) + '.pmtiles')):
                digest = hashlib.sha256(data).hexdigest()
                entry = share({'path': digest + '.pmtiles', 'bytes': len(data), 'sha256': digest})
                if send_cells.pop(place(release, entry), None) is None:
                    continue
                batch.append((place(release, entry), entry, partial(io.BytesIO, data)))
                if len(batch) == BATCH:
                    at_once(ftp, batch, send_shared)
                    batch = []
            at_once(ftp, batch, send_shared)
        require(not send_cells, 'Indexed files missing: ' + ', '.join(sorted(send_cells)[:3]))
        verify_manifest(ftp, stage, raw)
        if stage != final:
            if 'releases' not in {posixpath.basename(p) for p in ftp.nlst()}:
                ftp.mkd('releases')
            print(f'Rename verified {stage} to {final}', flush=True)
            ftp.rename(stage, final)
        print('Verify public immutable release before naming it the candidate', flush=True)
        probe(url, manifest, raw, samples)
        # Its _upload.json stays until promotion: retention keeps the candidate until the next upload names another
        if 'candidates' not in maintenance.children(ftp):
            ftp.mkd('candidates')
        replace_pointer(ftp, raw, 'candidates/current.json')
        require(get(url + '/candidates/current.json') == raw, 'Public candidate pointer differs')
        if previous is not None:
            print(json.dumps(maintenance.prune(ftp, apply=True, incoming=manifest, incoming_files=naming, budget=budget,
                                               product=product), sort_keys=True), flush=True)
        print(f'Candidate {release}: {url}/{final}/manifest.json', flush=True)


def found(url):
    """A public JSON file's bytes, as get() checks them, or None where the host answers 404."""
    try:
        return get(url)
    except HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        return None


def candidate(url, first=False):
    """The uploaded candidate newer than the live release, for Polku's checks, or None. Reads only public HTTP. With
    first, a product with nothing promoted yet has its first candidate; otherwise a missing live pointer stops."""
    url = public_url(url)
    raw = found(url + '/candidates/current.json')
    if raw is None:
        return None  # nothing uploaded yet
    manifest = validate(json.loads(raw))
    live = found(url + '/current.json') if first else get(url + '/current.json')
    live = live and validate(json.loads(live), pointed=True)
    if live and manifest['built_at'] <= live['built_at']:
        return None
    location = f"{url}/releases/{manifest['release']}/manifest.json"
    require(get(location, immutable=True) == raw, 'Candidate pointer differs from its release')
    return {'release': manifest['release'], 'manifest': location, 'sha256': hashlib.sha256(raw).hexdigest()}


def promote_candidate(release, sha256, directory, folder=None):
    """Make the candidate Polku checked live: the same manifest bytes, every file there at its size. With folder, that
    product's beside polku-osm."""
    require(os.environ.get('OSM_ALLOW_PUBLISH') == 'true', 'Publication requires approved OSM_ALLOW_PUBLISH=true')
    require(NAME.fullmatch(release) and re.fullmatch('[a-f0-9]{64}', sha256), 'Invalid candidate')
    directory = Path(directory)
    config = settings()
    with connection(config) as ftp:
        url = into_folder(ftp, config[-2], folder)
        previous = previous_pointer(ftp)
        final = f'releases/{release}'
        require(ftp.kind(final) == 'directory' and ftp.kind(final + '/manifest.json') == 'file', 'Candidate needs regular files')
        raw = retrieve(ftp, final + '/manifest.json')
        require(hashlib.sha256(raw).hexdigest() == sha256, 'Candidate manifest differs from the one checked')
        # Still the candidate: an upload that has named another removes this release unless it is live
        require(retrieve(ftp, 'candidates/current.json') == raw, 'Another candidate was uploaded meanwhile; check it first')
        manifest = validate(json.loads(raw))
        require(manifest['release'] == release, 'Candidate release differs')
        # The upload hashed every file; its author is the only writer, so sizes find what changed since
        expected = maintenance.own_files(manifest, raw)
        if maintenance.UPLOAD in maintenance.children(ftp, final):
            expected[maintenance.UPLOAD] = ftp.size(final + '/' + maintenance.UPLOAD)
        maintenance.validate_files(ftp, final, expected)
        have = maintenance.shared_files(ftp)
        for f in manifest['files']:
            if 'url' in f:
                require(have.get(place(release, f)) == f['bytes'], 'Shared file is missing or differs: ' + f['path'])
        for entry in hosted_named(ftp, manifest):
            require(have.get(place(release, entry)) == entry['bytes'], 'Indexed file is missing or differs: ' + entry['path'])
        live = json.loads(previous) if previous else None
        if not live or live['release'] != release:
            require(not live or manifest['built_at'] > live['built_at'], 'Candidate is not newer than the live release')
            require(not live or 'geometry' not in live['datasets'] or 'geometry' in manifest['datasets'],
                    'Publication would remove live geometry')
            probe(url, manifest, raw)
            print('Promote current.json and verify public pointer', flush=True)
            promote(ftp, url, manifest, previous, directory, None, raw)
        maintenance.record_publication(ftp, manifest, raw, previous)
        if maintenance.UPLOAD in maintenance.children(ftp, final):
            ftp.remove(final + '/' + maintenance.UPLOAD)


def rollback(manifest_path, expected_current=None, folder=None):
    """current.json back to a saved pointer; a saved null, from a first promotion, removes it. With folder, that
    product's."""
    require(os.environ.get('OSM_ALLOW_PUBLISH') == 'true', 'Publication requires approved OSM_ALLOW_PUBLISH=true')
    path = Path(manifest_path)
    target = json.loads(path.read_bytes())
    if target is None:
        require(expected_current is not None, 'A rollback to no live release needs --expected-current')
    else:
        validate(target, pointed=True)
    config = settings()
    with connection(config) as ftp:
        url = into_folder(ftp, config[-2], folder)
        previous = previous_pointer(ftp)
        if expected_current is not None:
            require(previous and json.loads(previous)['release'] == expected_current,
                    'Live release changed; do not roll back another publication')
        if target is None:
            put_back(ftp, url, None)
            return
        base = f"releases/{target['release']}"
        raw = retrieve(ftp, base + '/manifest.json')
        manifest = validate(json.loads(raw))
        require(current(manifest) == target, 'Rollback manifest differs from immutable release')
        samples = verify_remote(ftp, base, manifest, raw)
        probe(url, manifest, raw, samples)
        require(previous_pointer(ftp) == previous, 'Live pointer changed during rollback')
        promote(ftp, url, manifest, previous, path.parent, samples, raw)
        maintenance.record_publication(ftp, manifest, raw, previous)


def check(url):
    url = public_url(url)
    require(url.endswith('/current.json'), 'Check needs the current.json URL')
    manifest = validate(json.loads(get(url)), pointed=True)
    stamp = datetime.fromisoformat(manifest['source']['timestamp'].replace('Z', '+00:00'))
    require(stamp.tzinfo is not None and 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 14 * 86400,
            'OSM source timestamp is stale or in the future')
    base = url.removesuffix('/current.json')
    raw = get(f"{base}/releases/{manifest['release']}/manifest.json", immutable=True)
    release = validate(json.loads(raw))
    require(current(release) == manifest, 'Current pointer differs from immutable manifest')
    probe(base, release, raw)
    return manifest


def test_host():
    """Check live host SFTP/HTTP with tiny, isolated, automatically removed files."""
    config = settings()
    directory = 'host-test-' + uuid.uuid4().hex
    moved = directory + '-moved'
    body = b'Polku transfer test: ' + bytes(range(32))
    replacement = b'{"test":2}'
    with connection(config) as ftp:
        active = None
        try:
            ftp.mkd(directory)
            active = directory
            ftp.storbinary('STOR ' + active + '/sample.pmtiles', io.BytesIO(body))
            require(ftp.size(active + '/sample.pmtiles') == len(body)
                    and retrieve(ftp, active + '/sample.pmtiles') == body,
                    'Disposable SFTP upload verification failed')
            ftp.storbinary('STOR ' + active + '/pointer.json', io.BytesIO(b'{"test":1}'))
            ftp.storbinary('STOR ' + active + '/pointer.tmp', io.BytesIO(replacement))
            ftp.replace(active + '/pointer.tmp', active + '/pointer.json')
            require(retrieve(ftp, active + '/pointer.json') == replacement,
                    'SFTP POSIX overwrite verification failed')
            ftp.rename(directory, moved)
            active = moved
            require(ftp.size(active + '/sample.pmtiles') == len(body),
                    'SFTP directory rename verification failed')
            sample = get(config[-2] + '/' + active + '/sample.pmtiles',
                         (0, 10, len(body)), immutable=True)
            require(sample == body[:10], 'Public HTTP Range bytes differ from SFTP upload')
            # A name asked for while missing, then written, then removed: how long each answer lasts publicly
            fresh = config[-2] + '/' + active + '/fresh.json'
            settle(fresh, None, 0)
            ftp.storbinary('STOR ' + active + '/fresh.json', io.BytesIO(replacement))
            written = settle(fresh, replacement, SETTLE)
            ftp.remove(active + '/fresh.json')
            removed = settle(fresh, None, SETTLE)
            ftp.cwd(active)
            try:
                retention = maintenance.test_retention(ftp)
            finally:
                ftp.cwd('..')
            return {'sftp_write_and_rename': True, 'posix_overwrite': True, 'http_range_cors_and_cache': True,
                    'seconds_until_public': {'written_after_404': round(written, 1), 'removed': round(removed, 1)},
                    'test_cleanup': 'complete', 'retention': retention}
        finally:
            if active is not None:
                # Only descend into this invocation's uniquely named disposable test folder.
                def remove_test_tree(base):
                    for name in maintenance.children(ftp, base):
                        path = base + '/' + name
                        kind = ftp.kind(path)
                        require(kind in {'file', 'directory'}, 'Unexpected disposable test file')
                        if kind == 'directory':
                            remove_test_tree(path)
                        else:
                            ftp.remove(path)
                    ftp.rmd(base)
                remove_test_tree(active)


def verify_access():
    """Check protected credentials and the configured path without writing host files."""
    config = settings()
    with connection(config) as ftp:
        ftp.nlst()
        try:
            above = bool(ftp.nlst('..'))
        except OSError:
            above = False
    request = urllib.request.Request(config[-2] + '/current.json', method='HEAD',
                                    headers={'Origin': 'https://polku.si', 'User-Agent': 'Mozilla/5.0'})
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except HTTPError as error:
        response = error
    with response:
        # A login whose root folder is the OSM directory can't list the website around it
        return {'sftp_read_access': True, 'lists_above_root': above, 'public_status': response.status,
                'public_cors': response.headers.get('Access-Control-Allow-Origin') == '*'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    publication = commands.add_parser('upload')
    publication.add_argument('directory', type=Path)
    publication.add_argument('--resume', action='store_true', help='Reuse the existing stage or immutable release; never start a new upload')
    candidacy = commands.add_parser('candidate')
    candidacy.add_argument('url')
    candidacy.add_argument('--first', action='store_true', help='a product with nothing promoted yet has its first candidate')
    promotion = commands.add_parser('promote')
    promotion.add_argument('release')
    promotion.add_argument('--sha256', required=True, help="the checked manifest's SHA-256, from `candidate`")
    promotion.add_argument('--directory', type=Path, required=True, help='where the predecessor is saved for rollback')
    promotion.add_argument('--folder', help="a product's folder beside polku-osm, such as polku-digiroad")
    commands.add_parser('inspect').add_argument('directory', type=Path)
    commands.add_parser('inspect-retention')
    restore = commands.add_parser('rollback')
    restore.add_argument('--manifest', type=Path, required=True)
    restore.add_argument('--expected-current', help='only if this release is live: never undo another publication')
    restore.add_argument('--folder', help="a product's folder beside polku-osm, such as polku-digiroad")
    commands.add_parser('check').add_argument('url')
    commands.add_parser('verify-access')
    commands.add_parser('test-host')
    args = parser.parse_args()
    try:
        if args.command == 'upload':
            upload(args.directory, resume=args.resume)
        elif args.command == 'candidate':
            print(json.dumps(candidate(args.url, args.first) or {}, sort_keys=True))
            return
        elif args.command == 'promote':
            promote_candidate(args.release, args.sha256, args.directory, args.folder)
        elif args.command == 'inspect-retention':
            with connection(settings()) as ftp:
                print(json.dumps(maintenance.prune(ftp), sort_keys=True))
            return
        elif args.command == 'inspect':
            print(json.dumps(inspect_release(args.directory), sort_keys=True))
            print('Metadata only; no remote files changed or hashes verified.')
            return
        elif args.command == 'rollback':
            rollback(args.manifest, args.expected_current, args.folder)
        elif args.command == 'test-host':
            print(json.dumps(test_host(), sort_keys=True))
        elif args.command == 'verify-access':
            print(json.dumps(verify_access(), sort_keys=True))
            print('Only pinned-host-key SFTP read access checked; no files written or publication verified.')
            return
        else:
            check(args.url)
    except Exception as error:
        parser.exit(1, f'{args.command} failed: {error}\n')
    print(f'{args.command}: verified')


if __name__ == '__main__':
    main()
