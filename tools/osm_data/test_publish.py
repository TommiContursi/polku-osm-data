"""Publication boundaries use in-memory SFTP and HTTP; no live credentials."""

import copy
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from email.message import Message
import ftplib
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import importlib.util
import json
import os
from pathlib import Path
import posixpath
import shutil
import subprocess
from types import SimpleNamespace
import tempfile
from textwrap import dedent
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from tools.osm_data import builder, cells, publish
from tools.osm_data.publish import open_sftp as real_open_sftp
from tools.osm_data.test_cells import tile, write


ROOT = '/srv/example/polku-osm'
PUBLIC = 'https://data.example/polku-osm'
ENV = {'OSM_FTP_HOST': 'ftp.example', 'OSM_FTP_USER': 'user-private',
       'OSM_FTP_PASSWORD': 'password-private', 'OSM_FTP_ROOT': ROOT,
       'OSM_PUBLIC_URL': PUBLIC, 'OSM_ALLOW_PUBLISH': 'true',
       'OSM_SFTP_HOST_KEY_SHA256': 'SHA256:' + 'A' * 43}
ARCHIVE = b'PMTiles' + bytes(range(256)) * 40
LICENSE = b'OpenStreetMap contributors, ODbL 1.0\n'


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def manifest(release, built_at='2026-11-20T00:00:00Z'):
    files = [{'path': name, 'bytes': len(data),
              'sha256': hashlib.sha256(data).hexdigest()}
             for name, data in [('ways.pmtiles', ARCHIVE), ('LICENSE.txt', LICENSE)]]
    return {'schema': 1, 'release': release, 'built_at': built_at,
            'source': {'timestamp': datetime.now(timezone.utc).isoformat()},
            'datasets': {'ways': {'url': 'ways.pmtiles', 'bytes': len(ARCHIVE),
                                  'sha256': files[0]['sha256']}}, 'files': files}


def pointer(value):
    result = copy.deepcopy(value)
    result['datasets']['ways']['url'] = f"releases/{value['release']}/ways.pmtiles"
    return result


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value


class Host:
    """The server enforces encrypted transfers and keeps atomic rename observable."""
    def __init__(self):
        self.files = {}
        self.dirs = {ROOT, ROOT + '/releases'}
        self.fail_upload = self.corrupt_upload = self.fail_pointer = False
        self.bad_range = self.bad_after_pointer = self.changed_manifest_after_pointer = False
        self.denied_login = False
        self.connections = self.pointer_writes = 0
        self.pointer_cache = 'no-cache'
        self.uploads = []
        self.unsafe = set()
        self.release_headers = {'Access-Control-Allow-Origin': '*',
                                'Access-Control-Expose-Headers': 'Content-Length, Content-Range, Accept-Ranges',
                                'Accept-Ranges': 'bytes', 'Cache-Control': 'public, max-age=31536000, immutable'}

    def add_release(self, value, current=False):
        base = ROOT + '/releases/' + value['release']
        self.dirs.add(base)
        self.files.update({base + '/manifest.json': encoded(value),
                           base + '/ways.pmtiles': ARCHIVE, base + '/LICENSE.txt': LICENSE})
        if current:
            self.files[ROOT + '/current.json'] = encoded(pointer(value))

    def ftp(self, *args, **kwargs):
        host = self
        self.connections += 1
        _, user, password, _fingerprint = args
        if self.denied_login:
            raise ftplib.error_perm(f'530 {user} {password}')
        if (user, password) != (ENV['OSM_FTP_USER'], ENV['OSM_FTP_PASSWORD']):
            raise AssertionError('wrong credentials')

        class Connection:
            def __init__(self):
                self.path, self.protected = '/', True
            def connect(self, server, port=21, **kwargs):
                if server != 'ftp.example' or port != 21:
                    raise AssertionError('expected explicit FTPS on port 21')
            def login(self, user, passwd):
                if host.denied_login:
                    raise ftplib.error_perm(f'530 {user} {passwd}')
                if (user, passwd) != (ENV['OSM_FTP_USER'], ENV['OSM_FTP_PASSWORD']):
                    raise AssertionError('wrong credentials')
            def prot_p(self):
                self.protected = True
            def abs(self, path):
                if not self.protected:
                    raise AssertionError('unencrypted data operation')
                return posixpath.normpath(posixpath.join(self.path, path))
            def cwd(self, path):
                path = self.abs(path)
                if path not in host.dirs:
                    raise ftplib.error_perm('550 missing directory')
                self.path = path
            def pwd(self):
                return self.path
            def mkd(self, path):
                path = self.abs(path)
                if path in host.dirs:
                    raise ftplib.error_perm('550 exists')
                host.dirs.add(path)
                return path
            def nlst(self, path='.'):
                base = self.abs(path).rstrip('/') + '/'
                return [p for p in host.files.keys() | host.dirs if p.startswith(base) and '/' not in p[len(base):]]
            def remove(self, path):
                path = self.abs(path)
                if path not in host.files:
                    raise FileNotFoundError(2, 'No such file', path)
                del host.files[path]
            def rmd(self, path):
                path = self.abs(path)
                if path not in host.dirs or any(p.startswith(path + '/') for p in host.files):
                    raise OSError('Directory missing or not empty')
                host.dirs.remove(path)
            def size(self, path):
                path = self.abs(path)
                if path not in host.files:
                    raise ftplib.error_perm('550 missing')
                return len(host.files[path])
            def kind(self, path):
                path = self.abs(path)
                return 'other' if path in host.unsafe else 'directory' if path in host.dirs else 'file'
            def attributes(self, path='.'):
                return {posixpath.basename(p): (self.kind(p), len(host.files.get(p, b''))) for p in self.nlst(path)}
            def voidcmd(self, cmd):
                return '200 OK'
            def storbinary(self, cmd, body, *args, **kwargs):
                name = self.abs(cmd.removeprefix('STOR '))
                if host.fail_upload and name.endswith('ways.pmtiles'):
                    raise OSError('upload failed')
                data = body.read()
                offset = kwargs.get('rest')
                host.uploads.append((name, offset, len(data)))
                if offset is not None:
                    if len(host.files[name]) != offset:
                        raise OSError('Resume offset changed')
                    data = host.files[name] + data
                host.files[name] = data[:-1] + b'!' if host.corrupt_upload and name.endswith('ways.pmtiles') else data
            def retrbinary(self, cmd, callback, *args, **kwargs):
                name = self.abs(cmd.removeprefix('RETR '))
                if name not in host.files:
                    raise ftplib.error_perm('550 missing')
                callback(host.files[name])
            def replace(self, old, new):
                if (new.endswith('/pointer.json') or new.startswith('maintenance/') or new == 'candidates/current.json'
                        or self.abs(new).endswith(('/polku-mml/current.json', '/polku-digiroad/current.json'))):
                    old, new = self.abs(old), self.abs(new)
                    if host.fail_pointer:
                        raise OSError('POSIX rename extension unsupported')
                    host.files[new] = host.files.pop(old)
                    return
                return self.rename(old, new)
            def rename(self, old, new):
                old, new = self.abs(old), self.abs(new)
                if new == ROOT + '/current.json':
                    if host.fail_pointer:
                        raise ftplib.error_perm('550 atomic overwrite unsupported')
                    host.pointer_writes += 1
                elif new in host.files or new in host.dirs:
                    raise ftplib.error_perm('550 immutable target exists')
                if old in host.files:
                    host.files[new] = host.files.pop(old)
                elif old in host.dirs:
                    host.dirs.remove(old)
                    host.dirs.add(new)
                    for path in list(host.files):
                        if path.startswith(old + '/'):
                            host.files[new + path[len(old):]] = host.files.pop(path)
                else:
                    raise ftplib.error_perm('550 missing rename source')
            def quit(self):
                pass
            close = quit
        return Connection()

    def http(self, req, **kwargs):
        path = urlsplit(req.full_url).path
        name = ROOT + path.removeprefix('/polku-osm')
        if name not in self.files:
            raise HTTPError(req.full_url, 404, 'missing', {}, None)
        if req.get_header('Origin') != 'https://polku.si':
            raise AssertionError('public probe must test app Origin')
        data = self.files[name]
        if self.changed_manifest_after_pointer and self.pointer_writes == 1 and path.endswith('/manifest.json'):
            changed = json.loads(data)
            changed['attribution'] = 'unexpected replacement'
            data = encoded(changed)
        headers = dict(self.release_headers)
        if path.endswith('/current.json'):
            headers['Cache-Control'] = self.pointer_cache
        headers['Content-Type'] = 'application/json' if path.endswith('.json') else 'application/vnd.pmtiles'
        byte_range = req.get_header('Range')
        if byte_range:
            start, end = map(int, byte_range.removeprefix('bytes=').split('-'))
            headers['Content-Range'] = f'bytes {start}-{end}/{len(data)}'
            headers['Content-Length'] = str(end - start + 1)
            bad = self.bad_range or (self.bad_after_pointer and self.pointer_writes == 1)
            return Response(data[start:end + 1], 200 if bad else 206, headers)
        headers['Content-Length'] = str(len(data))
        return Response(data, headers=headers)


class PublishTests(unittest.TestCase):
    def test_the_sftp_jobs_lock_has_pmtiles_for_the_cut(self):
        lock = (Path(__file__).with_name('sftp-requirements.txt')).read_text()
        self.assertIn('pmtiles==3.7.0', lock, 'the upload cuts archives into cells')

    def test_unattended_upload_is_gated_and_recovery_leaves_time_to_verify(self):
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/osm-data.yml').read_text()
        self.assertNotIn('rollback', workflow, "the live pointer is Polku's: its weekly job promotes and rolls back")
        automatic = workflow.split('\n  upload:\n', 1)[1].split('\n  upload-existing:', 1)[0]
        condition = next(line.split('if:',1)[1].strip() for line in automatic.splitlines() if line.strip().startswith('if:'))
        for event,ref,operation,enabled,expected in [
            ('schedule','refs/heads/main','','true',True),
            ('workflow_dispatch','refs/heads/main','build','true',True),
            ('workflow_dispatch','refs/heads/feature','build','true',False),
            ('workflow_dispatch','refs/heads/main','build','',False),
            ('workflow_dispatch','refs/heads/main','geometry-candidate','true',False),
            ('workflow_dispatch','refs/heads/main','check','true',False),
            ('pull_request','refs/heads/main','build','true',False)]:
            expression=condition.removeprefix('${{').removesuffix('}}').replace('!cancelled()','True').replace('needs.bundle.result',repr('success'))
            for name,value in [('github.event_name',event),('github.ref',ref),('inputs.operation',operation),('vars.OSM_UPLOAD_ENABLED',enabled)]:
                expression=expression.replace(name,repr(value))
            self.assertEqual(eval(expression.replace('&&','and').replace('||','or'),{'__builtins__':{}}),expected)
        # GitHub's dynamic run-name metadata reaches the alerts.
        from tools.osm_data.alerts import classify
        event={'workflow':{'path':'.github/workflows/osm-data.yml'},'workflow_run':{
            'name':'OSM data / upload-existing','head_branch':'main','head_repository':{'full_name':'TommiContursi/polku-osm-data'},
            'event':'workflow_dispatch','conclusion':'failure','html_url':'https://github.com/TommiContursi/polku-osm-data/actions/runs/123'}}
        self.assertEqual(classify(event,'TommiContursi/polku-osm-data')['kind'],'update')
        recovery=workflow.split('\n  upload-existing:\n',1)[1].split('\n  monitor:',1)[0]
        self.assertIn('run-id: ${{ steps.source.outputs.run_id }}',recovery)
        for publication in (automatic,recovery):
            self.assertIn('\n    timeout-minutes: 350\n',publication,
                          'Slow artifact download, a cut into cells and full verification, within six hours')
            for switch in ('OSM_SHARED_FILES', 'OSM_CELLS'):
                self.assertIn(f'{switch}: ${{{{ vars.{switch} }}}}',publication,'a resume keeps the layout it began with')

    def test_recovery_source_accepts_failed_publication_with_verified_main_bundle(self):
        run={'id':123,'status':'completed','conclusion':'failure','head_branch':'main',
             'head_repository':{'full_name':'TommiContursi/polku-osm-data'},'event':'workflow_dispatch','path':'.github/workflows/osm-data.yml'}
        jobs=[{'name':name,'conclusion':'success'} for name in ['snapshot','bundle',
              'build (finland)','build (sweden)','build (norway)','build (estonia)']]
        artifact={'name':'nordic-osm-release','expired':False,'size_in_bytes':100}
        self.assertEqual(publish.recovery_source('TommiContursi/polku-osm-data',run,jobs,[artifact]),'123')
        for change in [dict(head_branch='feature'),dict(head_repository={'full_name':'attacker/polku-osm-data'}),
                       dict(status='in_progress'),dict(event='pull_request'),dict(path='.github/workflows/other.yml')]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                publish.recovery_source('TommiContursi/polku-osm-data',dict(run,**change),jobs,[artifact])
        for artifacts in [[],[dict(artifact,expired=True)],[artifact,artifact]]:
            with self.subTest(artifacts=artifacts),self.assertRaises(ValueError):
                publish.recovery_source('TommiContursi/polku-osm-data',run,jobs,artifacts)
        for failed in range(len(jobs)):
            partial=copy.deepcopy(jobs);partial[failed]['conclusion']='failure'
            with self.subTest(job=jobs[failed]['name']),self.assertRaises(ValueError):
                publish.recovery_source('TommiContursi/polku-osm-data',run,partial,[artifact])

    def test_publication_recovery_accepts_a_verified_geometry_bundle(self):
        run={'id':123,'status':'completed','conclusion':'failure','head_branch':'main',
             'head_repository':{'full_name':'TommiContursi/polku-osm-data'},'event':'workflow_dispatch','path':'.github/workflows/osm-data.yml'}
        jobs=[{'name':name,'conclusion':'success'} for name in ('geometry','bundle')]
        artifact={'name':'nordic-osm-release','expired':False,'size_in_bytes':100}
        self.assertEqual(publish.recovery_source('TommiContursi/polku-osm-data',run,jobs,[artifact]),'123')
        for i in range(len(jobs)):
            bad=copy.deepcopy(jobs);bad[i]['conclusion']='failure'
            with self.assertRaises(ValueError):publish.recovery_source('TommiContursi/polku-osm-data',run,bad,[artifact])

    def test_automatic_upload_after_recovery_requires_a_successful_bundle_and_no_cancellation(self):
        workflow=(Path(__file__).resolve().parents[2]/'.github/workflows/osm-data.yml').read_text()
        job=workflow.split('\n  upload:\n',1)[1].split('\n  upload-existing:',1)[0]
        condition=next(l.split('if:',1)[1].strip() for l in job.splitlines() if l.strip().startswith('if:'))
        self.assertIn('!cancelled()',condition)
        self.assertIn('needs.bundle.result',condition)
        for cancelled,bundle,expected in [(False,'success',True),(True,'success',False),(False,'failure',False)]:
            expression=condition.removeprefix('${{').removesuffix('}}').replace('!cancelled()',repr(not cancelled))
            for name,value in [('needs.bundle.result',bundle),('github.event_name','workflow_dispatch'),('github.ref','refs/heads/main'),('inputs.operation','build'),('vars.OSM_UPLOAD_ENABLED','true')]:expression=expression.replace(name,repr(value))
            self.assertEqual(eval(expression.replace('&&','and').replace('||','or'),{'__builtins__':{}}),expected)

    def test_geometry_recovery_requires_trusted_completed_country_jobs_and_artifacts(self):
        run={'id':123,'status':'completed','conclusion':'cancelled','head_branch':'main',
             'head_repository':{'full_name':'TommiContursi/polku-osm-data'},'event':'workflow_dispatch','path':'.github/workflows/osm-data.yml'}
        countries=('finland','sweden','norway','estonia')
        jobs=[{'name':name,'conclusion':'success'} for name in ['snapshot',*(f'build ({c})' for c in countries)]]
        artifacts=[{'name':f'osm-{c}-{kind}','expired':False,'size_in_bytes':100} for c in countries for kind in ('release','geometry-inputs')]
        self.assertEqual(publish.recovery_countries('TommiContursi/polku-osm-data',run,jobs,artifacts),'123')
        for change in [dict(head_branch='feature'),dict(head_repository={'full_name':'other/polku-osm-data'}),dict(status='in_progress'),dict(event='pull_request'),dict(path='.github/workflows/other.yml')]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                publish.recovery_countries('TommiContursi/polku-osm-data',dict(run,**change),jobs,artifacts)
        for failed in range(len(jobs)):
            partial=copy.deepcopy(jobs);partial[failed]['conclusion']='failure'
            with self.subTest(job=jobs[failed]['name']),self.assertRaises(ValueError):
                publish.recovery_countries('TommiContursi/polku-osm-data',run,partial,artifacts)
        for invalid in [artifacts[:-1],artifacts+[artifacts[0]],[dict(a,expired=True) for a in artifacts]]:
            with self.subTest(artifacts=invalid),self.assertRaises(ValueError):
                publish.recovery_countries('TommiContursi/polku-osm-data',run,jobs,invalid)

    def test_weekly_and_candidate_geometry_are_gated(self):
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/osm-data.yml').read_text()
        job = workflow.split('\n  geometry:\n', 1)[1].split('\n  bundle:', 1)[0]
        condition = next(line.strip()[4:] for line in job.splitlines() if line.strip().startswith('if:'))
        for event, ref, operation, build, cancelled, expected in [
            ('schedule', 'main', '', 'success', False, True),
            ('schedule', 'main', '', 'failure', False, False),
            ('schedule', 'main', '', 'skipped', False, False),
            ('schedule', 'main', '', 'success', True, False),
            ('workflow_dispatch', 'main', 'build', 'success', False, True),
            ('workflow_dispatch', 'main', 'geometry-candidate', 'skipped', False, True),
            ('workflow_dispatch', 'main', 'geometry-candidate', 'skipped', True, False),
            ('workflow_dispatch', 'feature', 'geometry-candidate', 'skipped', False, False),
        ]:
            with self.subTest(event=event, ref=ref, operation=operation, build=build, cancelled=cancelled):
                expr = condition.removeprefix('${{').removesuffix('}}').strip()
                for name, value in [('!cancelled()', not cancelled), ('needs.build.result', build),
                                    ('github.event_name', event), ('github.ref', 'refs/heads/' + ref),
                                    ('inputs.operation', operation)]:
                    expr = expr.replace(name, repr(value))
                self.assertEqual(eval(expr.replace('&&', 'and').replace('||', 'or'), {'__builtins__': {}}), expected)

    def test_bundle_requires_successful_geometry_and_no_cancellation(self):
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/osm-data.yml').read_text()
        job = workflow.split('\n  bundle:\n', 1)[1].split('\n  upload:', 1)[0]
        condition = next(line.strip()[4:] for line in job.splitlines() if line.strip().startswith('if:'))
        for cancelled, geometry, expected in [
            (False, 'success', True), (False, 'failure', False), (False, 'cancelled', False),
            (False, 'skipped', False), (True, 'success', False),
        ]:
            with self.subTest(cancelled=cancelled, geometry=geometry):
                expr = condition.removeprefix('${{').removesuffix('}}').strip()
                for name, value in [('!cancelled()', not cancelled), ('needs.geometry.result', geometry)]:
                    expr = expr.replace(name, repr(value))
                self.assertEqual(eval(expr.replace('&&', 'and').replace('||', 'or'), {'__builtins__': {}}), expected)
        self.assertIn('run-id: ${{ needs.geometry.outputs.run_id }}', job)

    def test_geofabrik_requests_retry_a_refused_connection(self):
        # curl's --retry alone gives up at once when Geofabrik refuses the runner or briefly answers 404 for a file it has (#1131)
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/osm-data.yml').read_text().replace('\\\n', ' ')
        requests = [line for line in workflow.splitlines() if 'curl ' in line and '127.0.0.1' not in line]
        self.assertEqual(len(requests), 14, "the snapshot check and its mirror fallback, the countries, their checksums and "
                         "their mirror fallback, the mirror's Europe export and checksum, the reference check and its mirror fallback, "
                         "the reference size, its two downloads and their mirror fallback, and BRouter's release for the tests")
        for line in requests:
            with self.subTest(line=line.strip()[:90]):
                # the snapshot check reads a 404 as a date not ready yet, so it retries only a refusal; the Europe
                # exports retry in a loop of their own, which goes on from where the transfer stopped
                resumable = '--continue-at -' in line and line.lstrip().startswith('until ')
                self.assertTrue(resumable or ('--retry-connrefused' if '$osm_region' in line else '--retry-all-errors') in line, line)

    def test_europe_download_goes_on_from_where_it_stopped(self):
        # Geofabrik's 35 GB export stopped at 91 % on 2026-11-21 after 2 hours, and curl's own --retry starts again from nothing.
        # When the mirror holds the same dated export, the rest comes from there (#1).
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/osm-data.yml').read_text()
        step = workflow.split('      - name: Download dated reference and checksum\n', 1)[1].split('\n      - name:', 1)[0]
        script = dedent(step.split('        run: |\n', 1)[1])
        export = bytes(range(256)) * 4096
        requests, mirrored, failing, offered = [], [], [], []

        class Hosts(BaseHTTPRequestHandler):
            def do_GET(self):
                mirror = self.path.startswith('/mirror/')
                (mirrored if mirror else requests).append((self.path, self.headers.get('Range')))
                if failing and not mirror:
                    self.send_error(503)
                    return
                if self.path.endswith('.md5'):
                    body = hashlib.md5(export).hexdigest().encode() + f'  europe-{offered[0] if mirror else "261120"}.osm.pbf\n'.encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                start = int(self.headers['Range'][6:].split('-')[0]) if self.headers.get('Range') else 0
                self.send_response(206 if start else 200)
                self.send_header('Content-Length', str(len(export) - start))
                if start:
                    self.send_header('Content-Range', f'bytes {start}-{len(export) - 1}/{len(export)}')
                self.end_headers()
                # Geofabrik's first two transfers stop part of the way, as a dropped or stalled connection does
                self.wfile.write(export[start:start + 300000] if not mirror and len(requests) <= 2 else export[start:])
                self.close_connection = True

            def log_message(self, *args):
                pass

        with HTTPServer(('127.0.0.1', 0), Hosts) as server, tempfile.TemporaryDirectory() as tmp:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                bin = Path(tmp) / 'bin'
                bin.mkdir()
                (bin / 'sleep').write_text('#!/bin/sh\n')
                (bin / 'sleep').chmod(0o755)
                host = f'http://127.0.0.1:{server.server_port}'
                url, mirror = host + '/europe-261120.osm.pbf', host + '/mirror/europe-latest.osm.pbf'
                for case, fail, offer in [('stopped twice', False, '261121'), ('never answered', True, '261121'),
                                          ('stopped, the mirror has the same export', False, '261120')]:
                    with self.subTest(case):
                        build = Path(tmp) / case
                        (build / 'osm-build').mkdir(parents=True)
                        requests.clear()
                        mirrored.clear()
                        failing[:] = [True] if fail else []
                        offered[:] = [offer]
                        output = build / 'output'
                        env = dict(os.environ, SOURCE_URL=url, SNAPSHOT='261120', MIRROR=host + '/mirror/', GITHUB_OUTPUT=str(output),
                                   GITHUB_STEP_SUMMARY=os.devnull, PATH=f'{bin}:{os.defpath}', NO_PROXY='127.0.0.1', no_proxy='127.0.0.1')
                        result = subprocess.run(['bash', '-e', '-c', script], cwd=build, env=env, capture_output=True, text=True, timeout=30)
                        if fail:
                            self.assertEqual(result.returncode, 1, result)
                            self.assertIn('The Europe export stopped at 0 bytes', result.stdout)
                            self.assertEqual(len(requests), 4, 'four tries that add no bytes end it')
                            continue
                        self.assertEqual(result.returncode, 0, result)
                        self.assertEqual((build / 'osm-build/europe.osm.pbf').read_bytes(), export)
                        if offer == '261120':
                            self.assertEqual(requests, [('/europe-261120.osm.pbf', None)])
                            self.assertEqual(mirrored, [('/mirror/europe-latest.osm.pbf.md5', None), ('/mirror/europe-latest.osm.pbf', 'bytes=300000-'),
                                                        ('/mirror/europe-latest.osm.pbf.md5', None)])
                            self.assertEqual(output.read_text(), f'url={mirror}\n')
                        else:
                            self.assertEqual(requests, [('/europe-261120.osm.pbf', None), ('/europe-261120.osm.pbf', 'bytes=300000-'),
                                                        ('/europe-261120.osm.pbf', 'bytes=600000-'), ('/europe-261120.osm.pbf.md5', None)])
                            self.assertEqual(output.read_text(), f'url={url}\n')
                            self.assertIn(f'Downloaded {url}', result.stdout)
            finally:
                server.shutdown()
                worker.join()

    def test_snapshot_retries_temporary_failure_and_takes_the_mirror_only_when_geofabrik_is_silent(self):
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/osm-data.yml').read_text()
        step = workflow.split('\n      - id: snapshot\n', 1)[1].split('\n      - ', 1)[0]
        script = dedent(step.split('        run: |\n', 1)[1])
        latest = '/europe-261115.osm.pbf.md5'
        older = '/europe-261114.osm.pbf.md5'
        mirrored = '/europe-latest.osm.pbf.md5'
        requests = []
        responses = {}
        offered = []

        class Checksums(BaseHTTPRequestHandler):
            def do_HEAD(self):
                requests.append(self.path)
                codes = responses.get(self.path)
                self.send_response_only(codes.pop(0) if codes else 200)
                self.end_headers()

            def do_GET(self):
                requests.append(self.path)
                if self.path != mirrored or not offered:
                    self.send_error(404)
                    return
                body = b'7700b4c585e58b1c1f81113168d193de  europe-' + offered[0].encode() + b'.osm.pbf\n'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        with HTTPServer(('127.0.0.1', 0), Checksums) as server, tempfile.TemporaryDirectory() as tmp:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                host = f'http://127.0.0.1:{server.server_port}'
                date = ('date() { case "$3" in "0 days ago") echo 261116;; "1 days ago") echo 261115;;'
                        ' "2 days ago") echo 261114;; "3 days ago") echo 261113;; esac; }\n')
                # a refused runner: no HTTP answer at all, as curl reports it
                refused = 'curl() { case "$*" in *geofabrik.invalid*) echo "${@: -1} HTTP 000 (curl 7)"; return 7;; esac; command curl "$@"; }\n'
                output = Path(tmp) / 'output'
                env = dict(os.environ, GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=os.devnull,
                           MIRROR=host + '/', NO_PROXY='127.0.0.1', no_proxy='127.0.0.1')
                cases = [(True, [503, 200], [], ['261116'], '261115', 'geofabrik'),
                         (True, [404], [], ['261116'], '261114', 'geofabrik'),
                         # Geofabrik answered, but no date is complete yet: the alerts' re-run waits for it
                         (True, [404], [404], ['261116'], '', ''),
                         # Geofabrik answered nothing: the mirror's own export, when it is recent enough
                         (False, [], [], ['261116'], '261116', 'mirror'),
                         (False, [], [], ['261113'], '261113', 'mirror'),
                         (False, [], [], ['261110'], '', ''),
                         (False, [], [], [], '', '')]
                for answered, latest_codes, older_codes, mirror_offer, selected, source in cases:
                    with self.subTest(answered=answered, selected=selected, offer=mirror_offer):
                        responses.update({latest: list(latest_codes), older: list(older_codes)})
                        offered[:] = mirror_offer
                        requests.clear()
                        output.write_text('')
                        local = script.replace('https://download.geofabrik.de', host if answered else 'http://geofabrik.invalid')
                        result = subprocess.run(['bash', '-e', '-c', date + refused + local], env=env,
                                                capture_output=True, text=True, timeout=40)
                        self.assertEqual(result.returncode, 0 if selected else 1, result)
                        self.assertEqual(output.read_text(), f'stamp={selected}\nsource={source}\n' if selected else '')
                        self.assertEqual(mirrored in requests, not answered, 'the mirror is read only when Geofabrik is silent')
                        if not answered:
                            continue
                        self.assertEqual(requests.count(latest), 2 if selected == '261115' else 1)
                        self.assertEqual(requests.count(older), 0 if selected == '261115' else 1)
                        for stamp in ['261115'] if selected == '261115' else ['261115', '261114']:
                            for region in ['europe/finland', 'europe/sweden', 'europe/norway', 'europe/estonia']:
                                self.assertIn(f'/{region}-{stamp}.osm.pbf.md5 HTTP 200 (curl 0)', result.stdout)
                        status = '200 (curl 0)' if selected == '261115' else '404 (curl 22)'
                        self.assertIn(f'{latest} HTTP {status}', result.stdout)
            finally:
                server.shutdown()
                worker.join()

    def test_the_geometry_takes_the_mirror_s_copy_of_its_dated_export_when_geofabrik_is_silent(self):
        # Geofabrik refuses some runner networks (#1); the mirror's MD5 file names the dated export it holds
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/osm-data.yml').read_text()
        step = workflow.split("      - name: Take the mirror's copy of the same dated export when Geofabrik doesn't answer this runner\n", 1)[1]
        script = dedent(step.split('        run: |\n', 1)[1].split('\n      - ', 1)[0])
        offered, requests = [], []

        class Hosts(BaseHTTPRequestHandler):
            def do_HEAD(self):
                requests.append(self.path)
                self.send_response_only(200)
                self.end_headers()

            def do_GET(self):
                requests.append(self.path)
                body = b'7700b4c585e58b1c1f81113168d193de  europe-' + offered[0].encode() + b'.osm.pbf\n'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        with HTTPServer(('127.0.0.1', 0), Hosts) as server, tempfile.TemporaryDirectory() as tmp:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                host = f'http://127.0.0.1:{server.server_port}'
                refused = 'curl() { case "$*" in *geofabrik.invalid*) printf 000; return 7;; esac; command curl "$@"; }\n'
                output = Path(tmp) / 'output'
                geofabrik, mirror = 'http://geofabrik.invalid/europe-261115.osm.pbf', host + '/europe-latest.osm.pbf'
                answering = host + '/europe-261115.osm.pbf'
                cases = [(answering, '261115', '261115', answering),
                         (geofabrik, '261115', '261115', mirror),
                         # the mirror has moved on: the same file is gone, so the download fails and the alerts re-run it
                         (geofabrik, '261115', '261116', geofabrik),
                         # countries clipped from the mirror already name the mirror's export
                         (mirror, 'latest', '261115', mirror)]
                for asked, snapshot, offer, expected in cases:
                    with self.subTest(asked=asked, snapshot=snapshot, offer=offer):
                        offered[:] = [offer]
                        requests.clear()
                        output.write_text('')
                        env = dict(os.environ, GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=os.devnull, MIRROR=host + '/',
                                   SOURCE_URL=asked, SNAPSHOT=snapshot, NO_PROXY='127.0.0.1', no_proxy='127.0.0.1')
                        result = subprocess.run(['bash', '-e', '-c', refused + script], env=env, capture_output=True, text=True, timeout=40)
                        self.assertEqual(result.returncode, 0, result)
                        self.assertEqual(output.read_text(), f'url={expected}\n')
                        self.assertEqual('/europe-latest.osm.pbf.md5' in requests, asked == geofabrik,
                                         'the mirror is read only when Geofabrik is silent')
            finally:
                server.shutdown()
                worker.join()

    def test_one_job_fetches_the_countries_and_hands_each_to_its_build(self):
        # Geofabrik answered the snapshot check, then refused two build runners (2026-11-23), so the builds never ask it.
        # The mirror holds Europe alone, so on that path the same job clips the countries (#1).
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/osm-data.yml').read_text()
        self.assertIn('  MIRROR: ' + builder.MIRROR + '\n', workflow, 'the workflow and builder.py name one mirror')
        job = workflow.split('\n  snapshot:\n', 1)[1].split('\n  build:', 1)[0]
        self.assertIn('md5sum --check --strict', job, 'the published checksum is checked before clipping')
        self.assertIn('grep -q "  europe-$SNAPSHOT\\.osm\\.pbf\\$"', job, 'the mirror may have moved on to a newer export')
        for country in ('finland', 'sweden', 'norway', 'estonia'):
            self.assertIn(f'name: osm-{country}-source\n          path: osm-build/sources/{country}\n', job)
        build = workflow.split('\n  build:\n', 1)[1].split('\n  geometry:', 1)[0]
        self.assertIn('name: osm-${{ matrix.country }}-source\n          path: osm-build\n', build)
        self.assertNotIn('curl', build, 'a country build downloads nothing itself')
        self.assertIn('--coverage "data/$COUNTRY.poly"', build, "the build's declared extent is the polygon it was clipped with")

    @unittest.skipUnless(shutil.which('osmium'), 'osmium-tool, as the snapshot job installs it on the mirror path')
    def test_the_mirror_path_clips_four_countries_keeping_the_export_s_epoch(self):
        import osmium
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/osm-data.yml').read_text()
        step = workflow.split("      - name: Clip the four countries in one pass, keeping the export's replication timestamp\n", 1)[1]
        script = dedent(step.split('        run: |\n', 1)[1].split('\n      - ', 1)[0]).replace('/usr/bin/time -v ', '')
        stamp = '2026-11-22T21:20:48Z'
        nodes = ''.join(f'<node id="{i}" version="1" lon="{x}" lat="{y}"/>' for i, (x, y) in enumerate([(1, 1), (1.5, 1), (5, 5), (5.5, 5)], 1))
        xml = f'<osm version="0.6">{nodes}<way id="1" version="1"><nd ref="1"/><nd ref="2"/><tag k="highway" v="path"/></way>' \
              '<way id="2" version="1"><nd ref="3"/><nd ref="4"/><tag k="highway" v="track"/></way></osm>'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'osm-build').mkdir()
            (root / 'data').mkdir()
            (root / 'export.osm').write_text(xml)
            header = osmium.io.Header()
            header.set('osmosis_replication_timestamp', stamp)
            with osmium.SimpleWriter(str(root / 'osm-build/europe.osm.pbf'), header=header) as out:
                for obj in osmium.FileProcessor(str(root / 'export.osm')):
                    out.add(obj)
            # Finland and Sweden take the first way, Norway and Estonia the second
            for country, (x, y) in [('finland', (0, 0)), ('sweden', (0, 0)), ('norway', (4, 4)), ('estonia', (4, 4))]:
                (root / f'data/{country}.poly').write_text(f'{country}\n1\n{x} {y}\n{x + 2} {y}\n{x + 2} {y + 2}\n{x} {y + 2}\n{x} {y}\nEND\nEND\n')
            env = dict(os.environ, MIRROR=builder.MIRROR)
            result = subprocess.run(['bash', '-e', '-c', script], cwd=tmp, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / 'osm-build/europe.osm.pbf').exists(), 'the 35 GB export makes room for the builds')
            for country, way in [('finland', 1), ('sweden', 1), ('norway', 2), ('estonia', 2)]:
                with self.subTest(country=country):
                    clipped = root / 'osm-build/sources' / country
                    pbf = clipped / 'source.osm.pbf'
                    with osmium.io.Reader(str(pbf)) as reader:
                        self.assertEqual(reader.header().get('osmosis_replication_timestamp'), stamp)
                    self.assertEqual([w.id for w in osmium.FileProcessor(str(pbf), osmium.osm.WAY)], [way])
                    self.assertEqual((clipped / 'source.md5').read_text().split()[0], hashlib.md5(pbf.read_bytes()).hexdigest())
                    self.assertEqual((clipped / 'source-url.txt').read_text(), builder.MIRROR + 'europe-latest.osm.pbf\n')
                    self.assertRegex((clipped / 'clipped-with.txt').read_text(), r'^osmium [0-9.]+ extract --strategy complete_ways\n$')

    def test_country_checksum_comes_from_beside_the_file_served(self):
        # Geofabrik moves countries into folders of their own; its old addresses keep a different checksum (#1151)
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/osm-data.yml').read_text()
        step = workflow.split('name: Download the four countries and their checksums', 1)[1].split('\n      - name:', 1)[0]
        script = dedent(step.split('        run: |\n', 1)[1])
        countries = ('finland', 'sweden', 'norway', 'estonia')
        pbf = {c: f'{c} itself'.encode() for c in countries}
        md5 = {c: hashlib.md5(pbf[c]).hexdigest().encode() + f'  {c}-261118.osm.pbf\n'.encode() for c in countries}
        moved, kept = {}, {}
        for c in countries:
            moved.update({f'/europe/{c}-261118.osm.pbf': (308, b'', f'/europe/{c}/{c}-261118.osm.pbf'),
                          f'/europe/{c}-261118.osm.pbf.md5': (200, b'0' * 32 + f'  {c}-261118.osm.pbf\n'.encode(), None),
                          f'/europe/{c}/{c}-261118.osm.pbf': (200, pbf[c], None),
                          f'/europe/{c}/{c}-261118.osm.pbf.md5': (200, md5[c], None)})
            kept.update({f'/europe/{c}-261118.osm.pbf': (200, pbf[c], None), f'/europe/{c}-261118.osm.pbf.md5': (200, md5[c], None)})
        files = {}

        class Geofabrik(BaseHTTPRequestHandler):
            def do_GET(self):
                code, body, location = files.get(self.path, (404, b'', None))
                self.send_response(code)
                if location: self.send_header('Location', location)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args): pass

        with HTTPServer(('127.0.0.1', 0), Geofabrik) as server, tempfile.TemporaryDirectory() as tmp:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                local = script.replace('https://download.geofabrik.de', f'http://127.0.0.1:{server.server_port}')
                env = dict(os.environ, SNAPSHOT='261118', NO_PROXY='127.0.0.1', no_proxy='127.0.0.1')
                for layout, served in [('moved', moved), ('kept', kept)]:
                    with self.subTest(layout=layout):
                        files.clear(); files.update(served)
                        result = subprocess.run(['bash', '-e', '-c', local], cwd=tmp, env=env, capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        for c in countries:
                            source = Path(tmp) / 'osm-build/sources' / c
                            self.assertEqual((source / 'source.osm.pbf').read_bytes(), pbf[c])
                            self.assertEqual((source / 'source.md5').read_bytes(), md5[c])
            finally:
                server.shutdown()
                worker.join()

    def test_a_failed_country_download_clips_all_four_from_the_mirror_s_same_export(self):
        # Norway's transfer stalled at 1.01 of 1.38 GB on 2026-11-23; the mirror holds Europe alone, so all four are clipped (#1)
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/osm-data.yml').read_text()
        step = workflow.split('name: Download the four countries and their checksums', 1)[1].split('\n      - name:', 1)[0]
        script = dedent(step.split('        run: |\n', 1)[1])
        offered = []

        class Hosts(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith('/mirror/'):
                    body = b'0' * 32 + f'  europe-{offered[0]}.osm.pbf\n'.encode()
                elif self.path.endswith('.md5'):
                    body = b'0' * 32 + b'  country.osm.pbf\n'
                else:
                    body = b'a country'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        with HTTPServer(('127.0.0.1', 0), Hosts) as server, tempfile.TemporaryDirectory() as tmp:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                host = f'http://127.0.0.1:{server.server_port}'
                stalled = 'curl() { case "$*" in *norway-261118.osm.pbf*) echo "curl: (28) Operation timed out" >&2; return 28;; esac; command curl "$@"; }\n'
                local = script.replace('https://download.geofabrik.de', host)
                for offer, code, source in [('261118', 0, 'source=mirror\n'), ('261119', 1, '')]:
                    with self.subTest(offer=offer):
                        offered[:] = [offer]
                        build = Path(tmp) / offer
                        build.mkdir()
                        output = build / 'output'
                        env = dict(os.environ, SNAPSHOT='261118', MIRROR=host + '/mirror/', GITHUB_OUTPUT=str(output),
                                   GITHUB_STEP_SUMMARY=os.devnull, NO_PROXY='127.0.0.1', no_proxy='127.0.0.1')
                        result = subprocess.run(['bash', '-e', '-c', stalled + local], cwd=build, env=env, capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, code, result)
                        self.assertEqual(output.read_text() if output.exists() else '', source)
                        if code == 0:
                            self.assertFalse((build / 'osm-build/sources').exists(), "Geofabrik's countries never join clipped ones")
                        self.assertNotIn('estonia', result.stdout, 'the countries after the failed one are not asked for')
            finally:
                server.shutdown()
                worker.join()

    def test_country_downloads_and_builds_have_time(self):
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/osm-data.yml').read_text()
        snapshot = workflow.split('\n  snapshot:\n', 1)[1].split('\n  build:', 1)[0]
        build = workflow.split('\n  build:\n', 1)[1].split('\n  geometry:', 1)[0]
        download = snapshot.split('      - name: Download the four countries', 1)[1].split('\n      - name:', 1)[0]
        curl = next(line for line in download.splitlines() if '/source.osm.pbf"' in line).split()
        transfer = int(curl[curl.index('--max-time') + 1])
        self.assertGreaterEqual(transfer, 22 * 60, 'the slowest Norway transfer needed over 21 minutes')
        retries = int(curl[curl.index('--retry-max-time') + 1])
        step = int(download.split('timeout-minutes: ', 1)[1].splitlines()[0])
        job = int(snapshot.split('timeout-minutes: ', 1)[1].splitlines()[0])
        self.assertGreater(retries, 0, 'a stalled large transfer must not restart late in the step')
        self.assertLessEqual(transfer + retries, step * 60 // 4 - 60, 'four countries, one after another, with their checksums')
        self.assertLess(step, job)
        self.assertGreaterEqual(int(build.split('timeout-minutes: ', 1)[1].splitlines()[0]), 100,
                                'leave time for the measured 86-minute build and its checks')

    def test_reference_download_leaves_time_to_compile(self):
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/osm-data.yml').read_text()
        geometry = workflow.split('\n  geometry:\n', 1)[1].split('\n  bundle:', 1)[0]
        download = geometry.split('      - name: Download dated reference and checksum', 1)[1].split('\n      - name:', 1)[0]
        transfer = int(download.split('deadline=$((SECONDS + ', 1)[1].split(')', 1)[0])
        self.assertGreaterEqual(transfer, 150 * 60, 'the Europe transfer has needed over 2 hours (2026-11-21)')
        step = int(download.split('timeout-minutes: ', 1)[1].splitlines()[0])
        job = int(geometry.split('timeout-minutes: ', 1)[1].splitlines()[0])
        self.assertLess(transfer, step * 60, 'leave time for the checksum')
        self.assertGreaterEqual(job - step, 160, 'leave a separate compilation and artifact budget')
        self.assertLessEqual(job, 360, "GitHub's hosted runners stop a job after 6 hours")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.release_dir = Path(self.tmp.name) / 'new-release'
        self.release_dir.mkdir()
        self.new, self.old = manifest('new-release'), manifest('previous-release', '2026-11-13T00:00:00Z')
        for name, data in [('ways.pmtiles', ARCHIVE), ('LICENSE.txt', LICENSE), ('manifest.json', encoded(self.new))]:
            (self.release_dir / name).write_bytes(data)
        self.host = Host()
        self.host.add_release(self.old, current=True)
        self.previous = self.host.files[ROOT + '/current.json']
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.dict('os.environ', ENV, clear=True))
        patches.enter_context(patch.object(publish, 'open_sftp', self.host.ftp))
        patches.enter_context(patch.object(publish.urllib.request, 'urlopen', self.host.http))

    def publish(self, resume=False):
        publish.upload(self.release_dir, resume=resume)
        sha256 = hashlib.sha256((self.release_dir / 'manifest.json').read_bytes()).hexdigest()
        publish.promote_candidate(self.new['release'], sha256, self.release_dir)

    def test_verified_release_switches_pointer_and_saves_previous(self):
        self.publish()
        current = json.loads(self.host.files[ROOT + '/current.json'])
        self.assertEqual(current, pointer(self.new))
        self.assertTrue(any(p.read_bytes() == self.previous for p in self.release_dir.glob('rollback*.json')))
        self.assertEqual(self.host.files[ROOT + '/releases/new-release/LICENSE.txt'], LICENSE)
        self.assertFalse(any(p.endswith('.htaccess') for p in self.host.files))

    def test_upload_names_the_candidate_and_leaves_the_live_release(self):
        self.assertIsNone(publish.candidate(PUBLIC), 'nothing uploaded yet')
        publish.upload(self.release_dir)
        self.assertEqual(self.host.files[ROOT + '/current.json'], self.previous)
        self.assertEqual(self.host.pointer_writes, 0)
        self.assertIn(ROOT + '/releases/new-release/_upload.json', self.host.files, 'kept until promotion')
        raw = encoded(self.new)
        self.assertEqual(self.host.files[ROOT + '/candidates/current.json'], raw)
        self.assertEqual(publish.candidate(PUBLIC), {'release': 'new-release', 'sha256': hashlib.sha256(raw).hexdigest(),
                                                     'manifest': PUBLIC + '/releases/new-release/manifest.json'})
        publish.promote_candidate('new-release', hashlib.sha256(raw).hexdigest(), self.release_dir)
        self.assertEqual(json.loads(self.host.files[ROOT + '/current.json']), pointer(self.new))
        self.assertNotIn(ROOT + '/releases/new-release/_upload.json', self.host.files)
        self.assertIsNone(publish.candidate(PUBLIC), 'the candidate is live')

    def test_upload_removes_the_candidate_it_replaces_only_after_naming_the_new_one(self):
        self.host.dirs.update({ROOT + '/maintenance', ROOT + '/candidates', ROOT + '/releases/waiting'})
        self.host.files[ROOT + '/maintenance/r-previous-release.json'] = encoded({
            'version': 1, 'release': 'previous-release', 'previous_release': None,
            'published_at': datetime.now(timezone.utc).isoformat(), 'manifest_sha256': hashlib.sha256(encoded(self.old)).hexdigest()})
        waiting = manifest('waiting')
        self.host.add_release(waiting)
        with publish.connection(publish.settings()) as ftp:
            publish.maintenance.mark_upload(ftp, 'releases/waiting', waiting, encoded(waiting))
        self.host.files[ROOT + '/candidates/current.json'] = encoded(waiting)
        seen, real = [], publish.replace_pointer
        def pointer(ftp, raw, path='current.json'):
            seen.append(ROOT + '/releases/waiting/ways.pmtiles' in self.host.files)
            real(ftp, raw, path)
        with patch.object(publish, 'replace_pointer', pointer):
            publish.upload(self.release_dir)
        self.assertEqual(seen, [True], 'the pointer never names a deleted release')
        self.assertNotIn(ROOT + '/releases/waiting', self.host.dirs)
        self.assertIn(ROOT + '/releases/new-release/ways.pmtiles', self.host.files)
        self.assertIn(ROOT + '/releases/previous-release/ways.pmtiles', self.host.files)

    def test_upload_fails_when_the_public_candidate_pointer_is_cached(self):
        self.host.pointer_cache = 'public, max-age=31536000, immutable'
        with self.assertRaisesRegex(RuntimeError, 'current.json must disable caching'):
            publish.upload(self.release_dir)

    def test_promotion_refuses_a_candidate_that_changed_or_is_not_newer(self):
        publish.upload(self.release_dir)
        sha256 = hashlib.sha256(encoded(self.new)).hexdigest()
        base = ROOT + '/releases/new-release/'
        for change, error in [('manifest', 'differs from the one checked'), ('short', 'size differs'),
                              ('extra', 'Unexpected files'), ('older', 'not newer'),
                              ('replaced', 'Another candidate was uploaded meanwhile')]:
            with self.subTest(change=change):
                files = dict(self.host.files)
                if change == 'manifest':
                    self.host.files[base + 'manifest.json'] = encoded(dict(self.new, attribution='changed'))
                elif change == 'replaced':  # another upload named its release and will remove this one
                    self.host.files[ROOT + '/candidates/current.json'] = encoded(manifest('other-release'))
                elif change == 'short':
                    self.host.files[base + 'ways.pmtiles'] = ARCHIVE[:-1]
                elif change == 'extra':
                    self.host.files[base + 'other.pmtiles'] = ARCHIVE
                else:
                    self.host.files[ROOT + '/current.json'] = encoded(pointer(dict(self.old, built_at='2026-11-21T00:00:00Z')))
                    self.host.files[ROOT + '/releases/previous-release/manifest.json'] = encoded(dict(self.old, built_at='2026-11-21T00:00:00Z'))
                before = dict(self.host.files)
                with self.assertRaisesRegex(RuntimeError, error):
                    publish.promote_candidate('new-release', sha256, self.release_dir)
                self.assertEqual(self.host.files, before)
                self.assertEqual(self.host.pointer_writes, 0)
                self.host.files = files
        with patch.dict('os.environ', {'OSM_ALLOW_PUBLISH': ''}), self.assertRaisesRegex(ValueError, 'approved'):
            publish.promote_candidate('new-release', sha256, self.release_dir)

    def test_publication_cannot_remove_live_geometry(self):
        geometry = {**self.old['datasets']['ways'], 'url': 'geometry.pmtiles'}
        self.old['datasets']['geometry'] = geometry
        self.old['files'].append({**self.old['files'][0], 'path': geometry['url']})
        self.host.add_release(self.old)
        self.host.files[ROOT + '/releases/previous-release/geometry.pmtiles'] = ARCHIVE
        current = encoded(publish.current(self.old))
        for resume in (False, True):
            with self.subTest(resume=resume):
                self.host.files[ROOT + '/current.json'] = current
                if resume:
                    self.stage({'ways.pmtiles': ARCHIVE, 'LICENSE.txt': LICENSE, 'manifest.json': encoded(self.new)})
                with self.assertRaisesRegex(RuntimeError, 'remove live geometry'):
                    self.publish(resume=resume)
                self.assertEqual(self.host.files[ROOT + '/current.json'], current)
                self.assertEqual(self.host.uploads, [])

    def test_publication_and_monitor_verify_the_lighting_archive_too(self):
        data = b'PMTiles lighting' + bytes(range(256)) * 5
        digest = hashlib.sha256(data).hexdigest()
        self.new['datasets']['lit'] = {'url': 'lit.pmtiles', 'bytes': len(data), 'sha256': digest}
        self.new['files'].append({'path': 'lit.pmtiles', 'bytes': len(data), 'sha256': digest})
        (self.release_dir / 'lit.pmtiles').write_bytes(data)
        (self.release_dir / 'manifest.json').write_bytes(encoded(self.new))
        self.publish()
        current = json.loads(self.host.files[ROOT + '/current.json'])
        self.assertEqual(current['datasets']['lit']['url'], 'releases/new-release/lit.pmtiles')
        self.assertEqual(self.host.files[ROOT + '/releases/new-release/lit.pmtiles'], data)
        publish.check(PUBLIC + '/current.json')
        http = self.host.http
        def fail_lighting(req, **kwargs):
            response = http(req, **kwargs)
            if req.full_url.endswith('/lit.pmtiles'):
                response.status = 200
            return response
        with patch.object(publish.urllib.request, 'urlopen', fail_lighting):
            with self.assertRaisesRegex(ValueError, 'Range'):
                publish.check(PUBLIC + '/current.json')

    def stage(self, files):
        stage = '.upload-new-release-' + 'a' * 32
        self.host.dirs.add(ROOT + '/' + stage)
        self.host.files.update({ROOT + '/' + stage + '/' + name: data for name, data in files.items()})
        return stage

    def test_inspection_reports_partial_bytes_without_writes_or_approval(self):
        stage = self.stage({'ways.pmtiles': ARCHIVE[:4000]})
        before = dict(self.host.files), set(self.host.dirs)
        with patch.dict('os.environ', {'OSM_ALLOW_PUBLISH': ''}):
            result = publish.inspect_release(self.release_dir)
        self.assertEqual(result['candidates'][0]['path'], stage)
        self.assertEqual(result['candidates'][0]['files']['ways.pmtiles']['actual'], 4000)
        self.assertEqual((self.host.files, self.host.dirs), before)
        self.assertEqual(self.host.uploads, [])

    def test_resume_complete_stage_does_not_reupload_payload(self):
        self.stage({'ways.pmtiles': ARCHIVE, 'LICENSE.txt': LICENSE, 'manifest.json': encoded(self.new)})
        self.publish(resume=True)
        self.assertEqual(json.loads(self.host.files[ROOT + '/current.json']), pointer(self.new))
        self.assertTrue(all('/current.' in name or '/maintenance/r-new-release.json.' in name or '/maintenance/p-new-release.json.' in name
                            for name, _, _ in self.host.uploads))
        # A rerun after promotion, say after a receipt write was interrupted, changes nothing but the receipt.
        receipt = ROOT + '/maintenance/r-new-release.json'
        self.host.files[ROOT + '/maintenance/p-new-release.json'] = self.host.files.pop(receipt)
        self.host.uploads.clear()
        sha256 = hashlib.sha256(encoded(self.new)).hexdigest()
        publish.promote_candidate('new-release', sha256, self.release_dir)
        self.assertEqual(json.loads(self.host.files[ROOT + '/current.json']), pointer(self.new))
        self.assertIn(receipt, self.host.files)
        saved, = self.release_dir.glob('rollback-*.json')
        self.assertEqual(json.loads(saved.read_bytes()), json.loads(self.previous))
        self.assertTrue(all('/maintenance/' in name for name, _, _ in self.host.uploads))
        # A failed live check after promotion: the rollback job restores the saved predecessor
        publish.rollback(saved, expected_current='new-release')
        self.assertEqual(json.loads(self.host.files[ROOT + '/current.json']), json.loads(self.previous))

    def test_resume_partial_stage_verifies_prefix_and_sends_only_missing_suffix(self):
        stage = self.stage({'ways.pmtiles': ARCHIVE[:4000]})
        self.publish(resume=True)
        self.assertIn((ROOT + '/' + stage + '/ways.pmtiles', 4000, len(ARCHIVE) - 4000), self.host.uploads)
        self.assertEqual(self.host.files[ROOT + '/releases/new-release/ways.pmtiles'], ARCHIVE)

    def test_resume_accounts_for_missing_payload_before_storage_budget(self):
        self.stage({'ways.pmtiles': ARCHIVE[:100]})
        with patch.object(publish.maintenance, 'usage', return_value=publish.maintenance.BUDGET-5000):
            with self.assertRaisesRegex(RuntimeError, 'budget'):
                self.publish(resume=True)
        self.assertEqual(self.host.uploads, [])

    def test_resume_rejects_corrupt_or_oversized_prefix_without_writing(self):
        for data in (b'wrong prefix', ARCHIVE + b'!'):
            with self.subTest(length=len(data)):
                self.stage({'ways.pmtiles': data})
                before = dict(self.host.files)
                with self.assertRaisesRegex(RuntimeError, 'prefix|larger'):
                    self.publish(resume=True)
                self.assertEqual(self.host.files, before)
                self.assertEqual(self.host.uploads, [])

    def test_resume_still_checks_complete_file_hash_before_promotion(self):
        self.stage({'ways.pmtiles': b'!' + ARCHIVE[1:], 'LICENSE.txt': LICENSE, 'manifest.json': encoded(self.new)})
        with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
            self.publish(resume=True)
        self.assertEqual(self.host.files[ROOT + '/current.json'], self.previous)
        self.assertEqual(self.host.uploads, [])

    def test_resume_existing_immutable_release_verifies_without_payload_writes(self):
        self.host.add_release(self.new)
        self.publish(resume=True)
        self.assertTrue(all('/current.' in name or '/maintenance/r-new-release.json.' in name or '/maintenance/p-new-release.json.' in name
                            for name, _, _ in self.host.uploads))

    def test_resume_refuses_missing_ambiguous_or_unsafe_staging(self):
        with self.assertRaisesRegex(RuntimeError, 'exactly one'):
            self.publish(resume=True)
        stage = self.stage({'ways.pmtiles': ARCHIVE[:4000]})
        other = ROOT + '/.upload-new-release-' + 'b' * 32
        self.host.dirs.add(other)
        with self.assertRaisesRegex(RuntimeError, 'exactly one'):
            self.publish(resume=True)
        self.host.dirs.remove(other)
        for unsafe in (ROOT + '/' + stage, ROOT + '/' + stage + '/ways.pmtiles'):
            self.host.unsafe = {unsafe}
            with self.assertRaisesRegex(RuntimeError, 'regular|directory'):
                self.publish(resume=True)
        self.assertEqual(self.host.uploads, [])

    def test_sftp_pipelines_writes_and_bounds_read_prefetch(self):
        events = []
        class File(io.BytesIO):
            def set_pipelined(self, enabled):
                events.append(('pipeline', enabled))
            def stat(self):
                return SimpleNamespace(st_size=len(ARCHIVE))
            def prefetch(self, file_size, max_concurrent_requests):
                events.append(('prefetch', file_size, max_concurrent_requests))
        class SFTP:
            def open(self, path, mode):
                return File(ARCHIVE if mode == 'rb' else b'')
        remote = publish.SFTPFiles(None, SFTP())
        remote.storbinary('STOR sample', io.BytesIO(ARCHIVE))
        chunks = []
        remote.retrbinary('RETR sample', chunks.append)
        self.assertIn(('pipeline', True), events)
        self.assertIn(('prefetch', len(ARCHIVE), 64), events)
        self.assertEqual(b''.join(chunks), ARCHIVE)

    def test_sftp_lists_a_folder_with_kinds_and_sizes_in_one_request(self):
        class SFTP:
            def listdir_attr(self, path):
                entry = lambda name, mode, size: SimpleNamespace(filename=name, st_mode=mode, st_size=size)
                return [entry('a.pmtiles', 0o100644, 5), entry('d', 0o40755, 4096), entry('link', 0o120777, 9)]
        self.assertEqual(publish.SFTPFiles(None, SFTP()).attributes('files'),
                         {'a.pmtiles': ('file', 5), 'd': ('directory', 4096), 'link': ('other', 9)})

    def test_sftp_resume_uses_nontruncating_seek_and_surfaces_close_errors(self):
        events = []
        class File(io.BytesIO):
            def set_pipelined(self, enabled):
                pass
            def close(self):
                events.append(('body', self.getvalue()))
                super().close()
                raise OSError('deferred write error')
        class SFTP:
            def lstat(self, path):
                return SimpleNamespace(st_mode=0o100600)
            def stat(self, path):
                return SimpleNamespace(st_size=4)
            def open(self, path, mode):
                events.append(('open', mode))
                return File(b'abcd')
        remote = publish.SFTPFiles(None, SFTP())
        with self.assertRaisesRegex(OSError, 'deferred write error'):
            remote.storbinary('STOR sample', io.BytesIO(b'efgh'), rest=4)
        self.assertEqual(events, [('open', 'r+b'), ('body', b'abcdefgh')])
        with self.assertRaisesRegex(ValueError, 'offset'):
            remote.storbinary('STOR sample', io.BytesIO(b'efgh'), rest=3)

    def test_host_checks_disposable_files_without_touching_release_or_pointer(self):
        original_files, original_dirs = dict(self.host.files), set(self.host.dirs)
        with patch.dict('os.environ', {'OSM_ALLOW_PUBLISH': ''}):
            result = publish.test_host()
        self.assertTrue(result['sftp_write_and_rename'])
        self.assertTrue(result['posix_overwrite'])
        self.assertTrue(result['http_range_cors_and_cache'])
        self.assertEqual(result['seconds_until_public'], {'written_after_404': 0, 'removed': 0})
        self.assertEqual(result['retention']['deleted_entries'], 3, 'a release, staging and a shared file')
        self.assertEqual(self.host.files, original_files)
        self.assertEqual(self.host.dirs, original_dirs)
        self.assertEqual(self.host.pointer_writes, 0)

    def test_settle_reads_again_until_the_host_answers_as_written_and_no_longer(self):
        answers = [HTTPError('u', 404, 'missing', {}, None), HTTPError('u', 404, 'missing', {}, None), b'x']
        with patch.object(publish, 'get', side_effect=lambda url: (lambda a: (_ for _ in ()).throw(a) if isinstance(a, Exception) else a)(answers.pop(0))), \
                patch.object(publish.time, 'sleep') as sleep, patch.object(publish.time, 'monotonic', side_effect=[0, 0, 2, 4, 4]):
            self.assertEqual(publish.settle('u', b'x', 10), 4)
        self.assertEqual(sleep.call_count, 2)
        with patch.object(publish, 'get', return_value=b'old'), patch.object(publish.time, 'sleep'), \
                patch.object(publish.time, 'monotonic', side_effect=[0, 0, 11]):
            with self.assertRaisesRegex(ValueError, 'did not answer as written within 10 s'):
                publish.settle('u', b'x', 10)

    def test_host_fails_safely_when_posix_overwrite_unsupported(self):
        original_files, original_dirs = dict(self.host.files), set(self.host.dirs)
        self.host.fail_pointer = True
        with self.assertRaisesRegex(RuntimeError, 'POSIX rename extension unsupported'):
            publish.test_host()
        self.assertEqual(self.host.files, original_files)
        self.assertEqual(self.host.dirs, original_dirs)

    def test_public_range_probe_uses_browser_user_agent(self):
        original_http = self.host.http
        def browser_only(request, **kwargs):
            if request.get_header('User-agent') != 'Mozilla/5.0':
                raise HTTPError(request.full_url, 403, 'blocked user agent', {}, None)
            return original_http(request, **kwargs)
        with patch.object(publish.urllib.request, 'urlopen', browser_only):
            body = publish.get(PUBLIC + '/releases/previous-release/ways.pmtiles',
                               (0, 10, len(ARCHIVE)), immutable=True)
        self.assertEqual(body, ARCHIVE[:10])

    def test_unsafe_root_and_missing_approval_fail_before_connect(self):
        for root in ['', '/', '/srv/example', '/srv/../polku-osm', '/srv/polku-osm\r\nDELE x']:
            with self.subTest(root=root), patch.dict('os.environ', {'OSM_FTP_ROOT': root}):
                with self.assertRaises(ValueError):
                    self.publish()
        with patch.dict('os.environ', {'OSM_ALLOW_PUBLISH': ''}):
            with self.assertRaises(ValueError):
                self.publish()
        self.assertEqual(self.host.connections, 0)

    def test_local_corruption_stops_before_connect(self):
        (self.release_dir / 'ways.pmtiles').write_bytes(b'bad')
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.host.connections, 0)

    def test_upload_hash_and_http_failures_never_switch_pointer(self):
        for failure in ['fail_upload', 'corrupt_upload', 'bad_range']:
            with self.subTest(failure=failure):
                self.host = Host()
                self.host.add_release(self.old, current=True)
                setattr(self.host, failure, True)
                with patch.object(publish, 'open_sftp', self.host.ftp), patch.object(publish.urllib.request, 'urlopen', self.host.http):
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.publish()
                self.assertEqual(self.host.files[ROOT + '/current.json'], self.previous)
                self.assertEqual(self.host.pointer_writes, 0)

    def test_existing_release_is_never_overwritten(self):
        self.host.add_release(self.new)
        before = dict(self.host.files)
        with self.assertRaises((ValueError, RuntimeError)):
            self.publish()
        self.assertEqual(self.host.files, before)

    def test_unsupported_pointer_overwrite_leaves_previous(self):
        self.host.fail_pointer = True
        with self.assertRaises((ValueError, RuntimeError)):
            self.publish()
        self.assertEqual(self.host.files[ROOT + '/current.json'], self.previous)

    def test_failed_post_switch_probe_restores_previous_pointer(self):
        self.host.bad_after_pointer = True
        with self.assertRaises((ValueError, RuntimeError)):
            self.publish()
        self.assertEqual(self.host.files[ROOT + '/current.json'], self.previous)

    def test_changed_immutable_manifest_after_promotion_restores_previous(self):
        self.host.changed_manifest_after_pointer = True
        with self.assertRaises((ValueError, RuntimeError)):
            self.publish()
        self.assertEqual(self.host.files[ROOT + '/current.json'], self.previous)

    def test_saved_rollback_roundtrip_keeps_both_releases(self):
        self.publish()
        saved = next(p for p in self.release_dir.glob('rollback*.json') if p.read_bytes() == self.previous)
        publish.rollback(saved)
        self.assertEqual(json.loads(self.host.files[ROOT + '/current.json']), json.loads(self.previous))
        self.assertIn(ROOT + '/releases/new-release/ways.pmtiles', self.host.files)
        self.assertIn(ROOT + '/releases/previous-release/ways.pmtiles', self.host.files)
        # This regression is also exercised by tools/smoke.py osmdata.
        with publish.connection(publish.settings()) as ftp:
            plan = publish.maintenance.prune(ftp, apply=True,
                       now=datetime.now(timezone.utc) + timedelta(days=40))
        self.assertEqual(plan['delete'], [])
        record = json.loads(self.host.files[ROOT + '/maintenance/r-previous-release.json'])
        self.assertEqual(record['previous_release'], 'new-release')

        # An interrupted second round trip must retain the true, pre-switch predecessor too.
        self.host.dirs.add(ROOT + '/maintenance')
        self.host.files[ROOT + '/maintenance/r-previous-release.json'] = encoded({
            'version': 1, 'release': 'previous-release', 'previous_release': 'already-pruned',
            'published_at': (datetime.now(timezone.utc)-timedelta(days=50)).isoformat(),
            'manifest_sha256': hashlib.sha256(encoded(self.old)).hexdigest()})
        self.host.add_release(self.new, current=True)
        saved = self.release_dir / 'rollback-interrupted.json'
        saved.write_bytes(self.previous)
        with patch.object(publish.maintenance, 'record_publication', side_effect=OSError('interrupted after promotion')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                publish.rollback(saved)
        before = dict(self.host.files)
        with publish.connection(publish.settings()) as ftp:
            plan = publish.maintenance.prune(ftp)
        self.assertEqual(plan['recover_receipt'], 'previous-release')
        self.assertEqual(self.host.files, before)
        with publish.connection(publish.settings()) as ftp:
            self.assertEqual(publish.maintenance.prune(ftp, apply=True)['delete'], [])
        receipt = json.loads(self.host.files[ROOT + '/maintenance/r-previous-release.json'])
        self.assertEqual(receipt['previous_release'], 'new-release')
        publish.rollback(saved)
        self.assertEqual(json.loads(self.host.files[ROOT + '/maintenance/r-previous-release.json'])['previous_release'], 'new-release')

    def test_rollback_still_requires_publication_approval(self):
        self.publish()
        saved = next(self.release_dir.glob('rollback*.json'))
        files = dict(self.host.files)
        with patch.dict('os.environ', {'OSM_ALLOW_PUBLISH': ''}):
            with self.assertRaisesRegex(ValueError, 'approval|approved'):
                publish.rollback(saved)
        self.assertEqual(self.host.files, files)

    def test_automatic_rollback_does_not_replace_another_live_release(self):
        saved = self.release_dir / 'rollback-own.json'
        saved.write_bytes(self.previous)
        before = dict(self.host.files)
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            publish.rollback(saved, expected_current='different')
        self.assertEqual(self.host.files, before)

    def test_monitor_rejects_old_data_and_invalid_public_headers(self):
        old = pointer(self.old)
        old['source']['timestamp'] = '2000-01-01T00:00:00Z'
        self.host.files[ROOT + '/current.json'] = encoded(old)
        with self.assertRaises(ValueError):
            publish.check(PUBLIC + '/current.json')

        self.host.files[ROOT + '/current.json'] = self.previous
        self.host.release_headers['Access-Control-Allow-Origin'] = 'https://other.example'
        with self.assertRaises(ValueError):
            publish.check(PUBLIC + '/current.json')

    def test_monitor_runs_without_the_sftp_dependency(self):
        original_import = __import__
        def no_sftp(name, *args, **kwargs):
            if name == 'paramiko' or name.startswith('pmtiles'):
                raise ModuleNotFoundError('SFTP and PMTiles dependencies are not installed in monitor jobs')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=no_sftp):
            spec = importlib.util.spec_from_file_location('http_monitor', publish.__file__)
            monitor = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(monitor)
            monitor.check(PUBLIC + '/current.json')

    def test_monitor_rejects_an_immutable_or_freshly_cached_current_pointer(self):
        for policy in ['public, max-age=31536000, immutable', 'public, max-age=900', '']:
            with self.subTest(policy=policy):
                self.host.pointer_cache = policy
                with self.assertRaisesRegex(ValueError, 'current.json|revalidat'):
                    publish.check(PUBLIC + '/current.json')

    def test_credentials_are_redacted_from_failure(self):
        self.host.denied_login = True
        with self.assertRaises((ValueError, RuntimeError)) as raised:
            self.publish()
        self.assertNotIn(ENV['OSM_FTP_USER'], str(raised.exception))
        self.assertNotIn(ENV['OSM_FTP_PASSWORD'], str(raised.exception))

    def test_access_check_does_not_need_publication_approval_or_write_files(self):
        files, dirs = dict(self.host.files), set(self.host.dirs)
        with patch.dict('os.environ', {'OSM_ALLOW_PUBLISH': ''}):
            result = publish.verify_access()
        self.assertTrue(result['sftp_read_access'])
        self.assertTrue(result['lists_above_root'], 'the fake host lets the login list its parent')
        self.assertEqual(result['public_status'], 200)
        self.assertEqual((self.host.files, self.host.dirs), (files, dirs))
        self.assertEqual(self.host.pointer_writes, 0)

    def test_access_check_reports_unpublished_http_without_changing_host(self):
        def forbidden(request, **kwargs):
            raise HTTPError(request.full_url, 403, 'forbidden', {}, None)
        files = dict(self.host.files)
        with patch.object(publish.urllib.request, 'urlopen', forbidden):
            result = publish.verify_access()
        self.assertTrue(result['sftp_read_access'])
        self.assertEqual(result['public_status'], 403)
        self.assertFalse(result['public_cors'])
        self.assertEqual(self.host.files, files)

    def test_host_key_mismatch_aborts_before_password_authentication(self):
        class Key:
            def asbytes(self):
                return b'an unexpected SSH host key'
        class Transport:
            authenticated = False
            closed = False
            def start_client(self, timeout=None):
                pass
            def get_remote_server_key(self):
                return Key()
            def auth_password(self, username, password):
                self.authenticated = True
            def close(self):
                self.closed = True
        transport = Transport()
        with patch('paramiko.Transport', return_value=transport):
            with self.assertRaisesRegex(ValueError, 'host key differs'):
                real_open_sftp('ftp.example', 'user', 'secret', ENV['OSM_SFTP_HOST_KEY_SHA256'])
        self.assertFalse(transport.authenticated)
        self.assertTrue(transport.closed)

    def test_missing_host_key_pin_rejects_access_without_connection(self):
        with patch.dict('os.environ', {'OSM_SFTP_HOST_KEY_SHA256': ''}):
            with self.assertRaisesRegex(ValueError, 'OSM_SFTP_HOST_KEY_SHA256'):
                publish.verify_access()
        self.assertEqual(self.host.connections, 0)

    def test_atomic_pointer_replace_uses_posix_rename_only(self):
        class Stub:
            posix_rename_calls = []
            def posix_rename(self, old, new):
                self.posix_rename_calls.append((old, new))
                raise OSError('POSIX rename extension unavailable')
        fake = Stub()
        remote = publish.SFTPFiles(None, fake)
        with self.assertRaisesRegex(OSError, 'extension unavailable'):
            remote.replace('pointer.tmp', 'current.json')
        self.assertEqual(fake.posix_rename_calls, [('pointer.tmp', 'current.json')])


def shared(data, suffix='.pmtiles'):
    return f'files/{hashlib.sha256(data).hexdigest()}{suffix}'


class SharedFilesTests(unittest.TestCase):
    """OSM_SHARED_FILES: every file among the shared files, named by its SHA-256; a release folder holds its manifest."""
    stage = PublishTests.stage

    def setUp(self):
        PublishTests.setUp(self)
        switch = patch.dict('os.environ', {'OSM_SHARED_FILES': 'true'})
        switch.start()
        self.addCleanup(switch.stop)

    def publish(self, directory=None, resume=False):
        directory = directory or self.release_dir
        publish.upload(directory, resume=resume)
        raw, hosted = publish.hosted(directory)
        publish.promote_candidate(hosted['release'], hashlib.sha256(raw).hexdigest(), directory)

    def build(self, name, archive, built_at):
        """A local release of the given archive, beside the same licence."""
        directory = Path(self.tmp.name) / name
        directory.mkdir()
        value = manifest(name, built_at)
        digest = hashlib.sha256(archive).hexdigest()
        value['files'][0].update(bytes=len(archive), sha256=digest)
        value['datasets']['ways'].update(bytes=len(archive), sha256=digest)
        for path, data in [('ways.pmtiles', archive), ('LICENSE.txt', LICENSE), ('manifest.json', encoded(value))]:
            (directory / path).write_bytes(data)
        return directory

    def sent(self):
        return sorted(name.rsplit('/', 1)[1] for name, _, _ in self.host.uploads if '/.upload-' in name)

    def test_a_rebuild_sends_only_its_manifest_and_every_release_keeps_its_files(self):
        self.publish()
        self.assertEqual(self.sent(), ['LICENSE.txt', '_upload.json', 'manifest.json', 'ways.pmtiles'])
        self.assertEqual((self.host.files[ROOT + '/' + shared(ARCHIVE)], self.host.files[ROOT + '/' + shared(LICENSE, '.txt')]),
                         (ARCHIVE, LICENSE))
        self.assertEqual([name for name in self.host.files if name.startswith(ROOT + '/releases/new-release/')],
                         [ROOT + '/releases/new-release/manifest.json'], 'a release is its manifest')
        live = json.loads(self.host.files[ROOT + '/current.json'])
        self.assertEqual(live['datasets']['ways']['url'], shared(ARCHIVE))
        publish.check(PUBLIC + '/current.json')
        # The same files built again: only the manifest goes up, and it names the files already there
        rebuilt = self.build('rebuilt-release', ARCHIVE, '2026-11-21T00:00:00Z')
        self.host.uploads.clear()
        self.publish(rebuilt)
        self.assertEqual(self.sent(), ['_upload.json', 'manifest.json'])
        self.assertEqual(json.loads(self.host.files[ROOT + '/current.json'])['datasets']['ways']['url'], shared(ARCHIVE))
        publish.check(PUBLIC + '/current.json')
        # Polku's rollback to the predecessor hashes the shared files it names in full
        saved = next(p for p in rebuilt.glob('rollback-*.json') if json.loads(p.read_bytes())['release'] == 'new-release')
        with patch.object(publish, 'verify_file', wraps=publish.verify_file) as verify:
            publish.rollback(saved, expected_current='rebuilt-release')
        self.assertEqual({call.args[1] for call in verify.call_args_list}, {shared(ARCHIVE), shared(LICENSE, '.txt')})
        self.assertEqual(json.loads(self.host.files[ROOT + '/current.json'])['release'], 'new-release')
        # A changed archive is all the next upload sends, and retention keeps what the releases that stay name
        changed = self.build('changed-release', ARCHIVE + b'!', '2026-11-22T00:00:00Z')
        self.host.uploads.clear()
        self.publish(changed)
        self.assertEqual(self.sent(), ['_upload.json', 'manifest.json', 'ways.pmtiles'])
        for data in (ARCHIVE, ARCHIVE + b'!'):
            self.assertIn(ROOT + '/' + shared(data), self.host.files)
        publish.check(PUBLIC + '/current.json')

    def test_a_resumed_upload_sends_only_what_neither_the_stage_nor_the_shared_files_hold(self):
        raw, hosted = publish.hosted(self.release_dir)
        # another upload put the same licence among the shared files after this one stopped
        self.host.dirs.add(ROOT + '/files')
        self.host.files[ROOT + '/' + shared(LICENSE, '.txt')] = LICENSE
        stage = self.stage({'ways.pmtiles': ARCHIVE[:4000], 'LICENSE.txt': LICENSE})
        with patch.dict('os.environ', {'OSM_ALLOW_PUBLISH': ''}):
            self.assertEqual(publish.inspect_release(self.release_dir)['shared'], ['LICENSE.txt'])
        self.publish(resume=True)
        self.assertEqual([(name.removeprefix(ROOT + '/'), offset, size) for name, offset, size in self.host.uploads
                          if '/.upload-' in name],
                         [(stage + '/ways.pmtiles', 4000, len(ARCHIVE) - 4000), (stage + '/manifest.json', None, len(raw))])
        self.assertEqual(self.host.files[ROOT + '/' + shared(ARCHIVE)], ARCHIVE)
        self.assertEqual([name for name in self.host.files if name.startswith(ROOT + '/releases/new-release/')],
                         [ROOT + '/releases/new-release/manifest.json'])

    def test_the_same_content_twice_in_a_release_is_sent_once(self):
        value = json.loads((self.release_dir / 'manifest.json').read_bytes())
        value['files'].append({**value['files'][0], 'path': 'lit.pmtiles'})
        value['datasets']['lit'] = {**value['datasets']['ways'], 'url': 'lit.pmtiles'}
        (self.release_dir / 'lit.pmtiles').write_bytes(ARCHIVE)
        (self.release_dir / 'manifest.json').write_bytes(encoded(value))
        self.publish()
        self.assertEqual(self.sent(), ['LICENSE.txt', '_upload.json', 'manifest.json', 'ways.pmtiles'])
        live = json.loads(self.host.files[ROOT + '/current.json'])
        self.assertEqual(live['datasets']['lit']['url'], live['datasets']['ways']['url'])
        publish.check(PUBLIC + '/current.json')

    def test_promotion_needs_every_shared_file_at_its_size(self):
        publish.upload(self.release_dir)
        raw, hosted = publish.hosted(self.release_dir)
        path = ROOT + '/' + shared(ARCHIVE)
        for change in ('short', 'missing'):
            with self.subTest(change=change):
                files = dict(self.host.files)
                if change == 'short':
                    self.host.files[path] = ARCHIVE[:-1]
                else:
                    del self.host.files[path]
                with self.assertRaisesRegex(RuntimeError, 'Shared file is missing or differs'):
                    publish.promote_candidate('new-release', hashlib.sha256(raw).hexdigest(), self.release_dir)
                self.assertEqual(self.host.pointer_writes, 0)
                self.host.files = files

    def test_a_manifest_names_a_shared_file_only_by_its_own_digest(self):
        _, hosted = publish.hosted(self.release_dir)
        ways = shared(ARCHIVE)
        self.assertEqual((hosted['datasets']['ways']['url'], publish.current(hosted)['datasets']['ways']['url']),
                         ('../../' + ways, ways))
        publish.validate(publish.current(hosted), pointed=True)
        for url, pointed in [('../../files/' + 'a' * 64 + '.pmtiles', False), ('../../' + ways[:-8] + '.json', False),
                             ('../../../' + ways, False), ('/' + ways, False), (ways, False), ('../x/../../' + ways, False),
                             ('../../' + ways, True), ('releases/../' + ways, True),
                             ('../../files/' + hashlib.sha256(ARCHIVE).hexdigest() + '.tar_gz', False)]:
            with self.subTest(url=url, pointed=pointed):
                broken = copy.deepcopy(publish.current(hosted) if pointed else hosted)
                broken['files'][0]['url'] = broken['datasets']['ways']['url'] = url
                if url.endswith('.tar_gz'):  # a suffix the shared files can't hold
                    broken['files'][0]['path'] = 'ways.tar_gz'
                with self.assertRaises(ValueError):
                    publish.validate(broken, pointed)


class CellsTests(unittest.TestCase):
    """OSM_CELLS: each archive goes up as its cells and an index naming them, among the shared files."""
    stage, sent = PublishTests.stage, SharedFilesTests.sent

    def setUp(self):
        PublishTests.setUp(self)
        for patcher in (patch.dict('os.environ', {'OSM_CELLS': 'true'}), patch.object(cells, 'TARGET', 2048)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tiles = {(14, 9100 + n % 20, 4600 + n // 20): tile(n) for n in range(300)}

    def build(self, name, tiles, built_at='2026-11-20T00:00:00Z'):
        """A local release whose ways archive is a real one of these tiles."""
        directory = Path(self.tmp.name) / name
        directory.mkdir(exist_ok=True)
        archive = write(directory / 'ways.pmtiles', tiles, {'timestamp': built_at}).read_bytes()
        value = manifest(name, built_at)
        digest = hashlib.sha256(archive).hexdigest()
        value['files'][0].update(bytes=len(archive), sha256=digest)
        value['datasets']['ways'].update(bytes=len(archive), sha256=digest, format='pmtiles-json', zoom=14)
        (directory / 'LICENSE.txt').write_bytes(LICENSE)
        (directory / 'manifest.json').write_bytes(encoded(value))
        return directory

    def publish(self, directory, resume=False):
        publish.upload(directory, resume=resume)
        raw, hosted = publish.hosted(directory)
        publish.promote_candidate(hosted['release'], hashlib.sha256(raw).hexdigest(), directory)
        return hosted

    def cells(self, hosted):
        index, = publish.indexes(hosted)
        return publish.cells_of(self.host.files[ROOT + '/' + publish.place(hosted['release'], index)], index)

    def test_an_archive_goes_up_as_its_cells_and_a_changed_tile_sends_one_cell(self):
        first = self.publish(self.build('new-release', self.tiles))
        ways = first['datasets']['ways']
        self.assertEqual(ways['format'], publish.CELLS)
        self.assertTrue(ways['url'].startswith('../../files/') and ways['url'].endswith('.json'))
        rows = self.cells(first)
        self.assertGreater(len(rows), 3)
        for entry in rows:
            self.assertEqual(hashlib.sha256(self.host.files[ROOT + '/' + publish.shared_path(entry)]).hexdigest(), entry['sha256'])
        self.assertEqual([n for n in self.host.files if n.startswith(ROOT + '/releases/new-release/')],
                         [ROOT + '/releases/new-release/manifest.json'])
        archive = hashlib.sha256((Path(self.tmp.name) / 'new-release' / 'ways.pmtiles').read_bytes()).hexdigest()
        self.assertFalse([n for n in self.host.files if archive in n], 'the whole archive never goes up')
        publish.check(PUBLIC + '/current.json')
        # A week later one tile changed: its cell, the index and the manifest go up
        self.host.uploads.clear()
        second = self.publish(self.build('next-release', self.tiles | {(14, 9100, 4600): b'changed'}, '2026-11-27T00:00:00Z'))
        new = [entry for entry in self.cells(second) if entry not in rows]
        self.assertEqual(len(new), 1)
        self.assertEqual(self.sent(), sorted(['_upload.json', 'manifest.json', 'ways.cells.json', new[0]['path']]))
        publish.check(PUBLIC + '/current.json')
        # Polku's rollback hashes every cell of the predecessor
        saved = next(p for p in (Path(self.tmp.name) / 'next-release').glob('rollback-*.json')
                     if json.loads(p.read_bytes())['release'] == 'new-release')
        with patch.object(publish, 'verify_file', wraps=publish.verify_file) as verify:
            publish.rollback(saved, expected_current='next-release')
        self.assertLessEqual({publish.shared_path(entry) for entry in rows}, {call.args[1] for call in verify.call_args_list})
        # Retention keeps every cell the releases that stay name, and stops when an index is missing
        with publish.connection(publish.settings()) as ftp:
            publish.maintenance.prune(ftp, apply=True)
        for entry in rows + new:
            self.assertIn(ROOT + '/' + publish.shared_path(entry), self.host.files)
        index, = publish.indexes(second)
        del self.host.files[ROOT + '/' + publish.place(second['release'], index)]
        before = dict(self.host.files)
        with self.assertRaisesRegex(RuntimeError, 'missing'), publish.connection(publish.settings()) as ftp:
            publish.maintenance.prune(ftp, apply=True)
        self.assertEqual(self.host.files, before)

    def test_a_cell_an_upload_reuses_stays_though_the_release_it_came_with_goes(self):
        first = self.publish(self.build('new-release', self.tiles))
        tiles = self.tiles | {(14, 9101, 4600): b'other'}
        stopped = self.build('other-release', tiles, '2026-11-21T00:00:00Z')
        with patch.object(publish, 'verify_manifest', side_effect=RuntimeError('stopped')), self.assertRaises(RuntimeError):
            publish.upload(stopped)  # after its cells moved among the shared files
        stage, = [d for d in self.host.dirs if '.upload-other-release-' in d]
        record = json.loads(self.host.files[stage + '/_upload.json'])
        self.host.files[stage + '/_upload.json'] = encoded(record | {'created_at': '2026-01-01T00:00:00+00:00'})
        _, hosted = publish.hosted(stopped)
        reused, = [entry for entry in self.cells_named(hosted, stopped) if entry not in self.cells(first)]
        # The next upload reuses that cell while its first retention removes the old staging
        third = self.publish(self.build('third-release', tiles | {(14, 9119, 4614): b'x'}, '2026-11-28T00:00:00Z'))
        self.assertIn(reused, self.cells(third))
        self.assertIn(ROOT + '/' + publish.shared_path(reused), self.host.files)
        self.assertNotIn(stage, self.host.dirs)

    def test_promotion_needs_every_cell_and_a_resume_sends_a_staged_cell_again(self):
        directory = self.build('new-release', self.tiles)
        publish.upload(directory)
        raw, hosted = publish.hosted(directory)
        entry = self.cells(hosted)[1]
        cell = self.host.files.pop(ROOT + '/' + publish.shared_path(entry))
        with self.assertRaisesRegex(RuntimeError, 'Cell is missing or differs'):
            publish.promote_candidate('new-release', hashlib.sha256(raw).hexdigest(), directory)
        self.assertEqual(self.host.pointer_writes, 0)
        self.host.files[ROOT + '/' + publish.shared_path(entry)] = cell
        # Another release stopped part way: a half-sent cell is in its staging
        other = self.build('other-release', self.tiles | {(14, 9101, 4600): b'other'}, '2026-11-21T00:00:00Z')
        _, staged = publish.hosted(other)
        changed, = [c for c in self.cells_named(staged, other) if c not in self.cells(hosted)]
        stage = '.upload-other-release-' + 'a' * 32
        self.host.dirs.add(ROOT + '/' + stage)
        self.host.files[f"{ROOT}/{stage}/{changed['path']}"] = b'half'
        self.host.uploads.clear()
        publish.upload(other, resume=True)
        self.assertIn((f"{ROOT}/{stage}/{changed['path']}", None, changed['bytes']), self.host.uploads)
        self.assertEqual(hashlib.sha256(self.host.files[ROOT + '/' + publish.shared_path(changed)]).hexdigest(), changed['sha256'])

    def cells_named(self, hosted, directory):
        index, = publish.indexes(hosted)
        return publish.cells_of((directory / index['path']).read_bytes(), index)


if __name__ == '__main__':
    unittest.main()
