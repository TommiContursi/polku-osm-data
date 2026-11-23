"""Managed retention never traverses or deletes unowned host data."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest
from unittest.mock import patch

from tools.osm_data import maintenance, publish
from tools.osm_data.test_publish import Host, ROOT, manifest, encoded, pointer, ARCHIVE, LICENSE

NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.host = Host()
        self.ftp = self.host.ftp('host', 'user-private', 'password-private', 'pin')
        self.ftp.cwd(ROOT)
        self.host.dirs.add(ROOT + '/maintenance')

    def release(self, name, days, previous=None, current=False, managed=True):
        m = manifest(name)
        self.host.add_release(m, current=current)
        if managed:
            self.host.files[f'{ROOT}/maintenance/r-{name}.json'] = encoded({
                'version': 1, 'release': name, 'published_at': (NOW-timedelta(days=days)).isoformat(),
                'previous_release': previous, 'manifest_sha256': hashlib.sha256(encoded(m)).hexdigest()})
        return m

    def test_prune_keeps_only_the_live_release_its_predecessor_and_the_candidate(self):
        self.release('live', 7, previous='rollback', current=True)
        self.release('rollback', 14)
        self.release('other-1', 1)
        self.release('other-2', 21)
        self.release('other-3', 42)
        self.release('unowned', 90, managed=False)
        self.candidate('waiting')
        before = dict(self.host.files)
        plan = maintenance.prune(self.ftp, now=NOW)
        self.assertEqual(set(plan['delete']), {'releases/other-1','releases/other-2','releases/other-3'})
        self.assertEqual(self.host.files, before)
        applied = maintenance.prune(self.ftp, apply=True, now=NOW)
        self.assertEqual(applied['delete'], plan['delete'])
        for name in ('live','rollback','waiting','unowned'):
            self.assertIn(f'{ROOT}/releases/{name}/ways.pmtiles', self.host.files)
        self.assertNotIn(f'{ROOT}/releases/other-2', self.host.dirs)
        self.assertEqual(self.host.files[ROOT+'/current.json'], before[ROOT+'/current.json'])

    def test_a_candidate_stays_until_the_pointer_names_its_replacement(self):
        self.release('live', 7, previous='rollback', current=True)
        self.release('rollback', 14)
        self.candidate('waiting')
        self.assertEqual(maintenance.prune(self.ftp, now=NOW)['delete'], [])
        self.assertEqual(maintenance.prune(self.ftp, now=NOW, incoming=manifest('next'))['delete'], [], 'before the upload')
        self.candidate('next')
        self.assertEqual(maintenance.prune(self.ftp, now=NOW, incoming=manifest('next'))['delete'], ['releases/waiting'])

    def candidate(self, name):
        """An uploaded release Polku hasn't promoted, named in candidates/current.json."""
        m = manifest(name)
        self.host.add_release(m)
        maintenance.mark_upload(self.ftp, 'releases/'+name, m, encoded(m), now=NOW)
        self.host.dirs.add(ROOT + '/candidates')
        self.host.files[ROOT + '/candidates/current.json'] = encoded(m)

    def test_missing_current_receipt_disables_deletions(self):
        self.release('legacy', 90, current=True, managed=False)
        self.release('one', 40);self.release('two', 41);self.release('three',42)
        self.assertEqual(maintenance.prune(self.ftp, now=NOW)['delete'], [])

    def test_corrupt_receipt_symlink_or_extra_file_never_deletes(self):
        self.release('live',0,previous='previous',current=True);self.release('previous',7)
        self.release('expired',50)
        for fault in ['receipt','symlink','extra']:
            files,unsafe=dict(self.host.files),set(self.host.unsafe)
            if fault=='receipt': self.host.files[ROOT+'/maintenance/r-expired.json']=b'{}'
            if fault=='symlink': self.host.unsafe.add(ROOT+'/releases/expired/ways.pmtiles')
            if fault=='extra': self.host.files[ROOT+'/releases/expired/private.txt']=b'keep'
            before=dict(self.host.files)
            with self.assertRaises(ValueError): maintenance.prune(self.ftp,apply=True,now=NOW)
            self.assertEqual(self.host.files,before)
            self.host.files,self.host.unsafe=files,unsafe

    def test_changed_pointer_aborts_before_any_deletion(self):
        self.release('live',0,previous='previous',current=True);self.release('previous',7);self.release('expired',50)
        real=self.ftp.retrbinary;calls=0
        def changed(command,callback):
            nonlocal calls
            if command=='RETR current.json':
                calls+=1
                if calls>1: callback(encoded(pointer(manifest('different'))));return
            real(command,callback)
        before=dict(self.host.files)
        with patch.object(self.ftp,'retrbinary',changed), self.assertRaisesRegex(ValueError,'pointer'):
            maintenance.prune(self.ftp,apply=True,now=NOW)
        self.assertEqual(self.host.files,before)

    def test_budget_rejection_happens_without_mutation(self):
        self.release('live',0,current=True)
        before=dict(self.host.files)
        with self.assertRaisesRegex(ValueError,'budget'):
            maintenance.prune(self.ftp,apply=True,incoming_bytes=80*1024**3,now=NOW)
        self.assertEqual(self.host.files,before)

    def test_owned_interrupted_staging_expires_after_seven_days(self):
        self.release('live',0,current=True)
        m=manifest('partial');stage='.upload-partial-'+'a'*32
        self.host.dirs.add(ROOT+'/'+stage)
        maintenance.mark_upload(self.ftp,stage,m,encoded(m),now=NOW-timedelta(days=8))
        self.host.files[ROOT+'/'+stage+'/ways.pmtiles']=ARCHIVE[:12]
        self.assertEqual(maintenance.prune(self.ftp,now=NOW)['delete'],[stage])
        maintenance.prune(self.ftp,apply=True,now=NOW)
        self.assertNotIn(ROOT+'/'+stage,self.host.dirs)

    def test_unowned_or_young_staging_is_retained(self):
        self.release('live',0,current=True)
        for char,days in [('a',6),('b',8)]:
            stage='.upload-partial-'+char*32;self.host.dirs.add(ROOT+'/'+stage)
            if char=='a': maintenance.mark_upload(self.ftp,stage,manifest('partial'),encoded(manifest('partial')),now=NOW-timedelta(days=days))
        self.assertEqual(maintenance.prune(self.ftp,now=NOW)['delete'],[])

    def test_a_resumed_upload_keeps_its_release_and_staging_and_removes_others(self):
        self.release('live',0,current=True);self.release('old',50)
        self.release('newer',1)
        stage='.upload-old-'+'a'*32;self.host.dirs.add(ROOT+'/'+stage)
        maintenance.mark_upload(self.ftp,stage,manifest('old'),encoded(manifest('old')),now=NOW-timedelta(days=8))
        self.assertEqual(maintenance.prune(self.ftp,now=NOW,incoming=manifest('old'))['delete'],['releases/newer'])

    def test_publication_record_is_updated_on_rollback(self):
        m=self.release('old',80,current=True)
        maintenance.prepare_publication(self.ftp,m,encoded(m),encoded(pointer(manifest('new'))),now=NOW)
        maintenance.record_publication(self.ftp,m,encoded(m),encoded(pointer(manifest('new'))),now=NOW)
        record=json.loads(self.host.files[ROOT+'/maintenance/r-old.json'])
        self.assertEqual(record['previous_release'],'new')
        self.assertEqual(record['published_at'],NOW.isoformat())

    def test_same_current_record_does_not_erase_predecessor(self):
        m=self.release('live',0,previous='previous',current=True)
        before=self.host.files[ROOT+'/maintenance/r-live.json']
        maintenance.record_publication(self.ftp,m,encoded(m),encoded(pointer(m)),now=NOW)
        self.assertEqual(self.host.files[ROOT+'/maintenance/r-live.json'],before)

    def test_interrupted_deletion_can_finish_without_losing_ownership(self):
        self.release('live',0,previous='previous',current=True);self.release('previous',7);self.release('expired',50)
        real=self.ftp.remove
        def fail(path):
            if path=='releases/expired/ways.pmtiles': raise OSError('interrupted')
            real(path)
        with patch.object(self.ftp,'remove',fail),self.assertRaises(OSError):
            maintenance.prune(self.ftp,apply=True,now=NOW)
        self.assertIn(ROOT+'/releases/expired/manifest.json',self.host.files)
        maintenance.prune(self.ftp,apply=True,now=NOW)
        self.assertNotIn(ROOT+'/releases/expired',self.host.dirs)

    def test_empty_owned_directory_after_interrupted_rmdir_can_finish(self):
        self.release('live',0,previous='previous',current=True);self.release('previous',7);self.release('expired',50)
        with patch.object(self.ftp,'rmd',side_effect=OSError('interrupted')),self.assertRaises(OSError):
            maintenance.prune(self.ftp,apply=True,now=NOW)
        self.assertEqual([k for k in self.host.files if k.startswith(ROOT+'/releases/expired/')],[])
        maintenance.prune(self.ftp,apply=True,now=NOW)
        self.assertNotIn(ROOT+'/releases/expired',self.host.dirs)

    def shared(self, name, days, ways, previous=None, current=False):
        """A release among the shared files: its folder holds only its manifest."""
        m = manifest(name)
        digest = hashlib.sha256(ways).hexdigest()
        m['files'][0].update(bytes=len(ways), sha256=digest)
        m['datasets']['ways'].update(bytes=len(ways), sha256=digest)
        m = publish.shared_manifest(m)
        self.host.dirs.update({f'{ROOT}/releases/{name}', ROOT + '/files'})
        self.host.files[f'{ROOT}/releases/{name}/manifest.json'] = encoded(m)
        for f in m['files']:
            self.host.files[ROOT + '/' + publish.place(name, f)] = ways if f['path'] == 'ways.pmtiles' else LICENSE
        if current:
            self.host.files[ROOT + '/current.json'] = encoded(publish.current(m))
        self.host.files[f'{ROOT}/maintenance/r-{name}.json'] = encoded({
            'version': 1, 'release': name, 'published_at': (NOW-timedelta(days=days)).isoformat(),
            'previous_release': previous, 'manifest_sha256': hashlib.sha256(encoded(m)).hexdigest()})
        return m

    def test_a_shared_file_stays_while_a_release_that_stays_names_it(self):
        self.shared('live', 7, b'PMTiles live', previous='rollback', current=True)
        self.shared('rollback', 14, b'PMTiles rollback')
        self.shared('other', 21, b'PMTiles other')
        self.shared('again', 28, b'PMTiles live')
        self.release('before', 35)  # from before the shared files: its folder holds its files
        stray = 'files/' + 'b' * 64 + '.pmtiles'
        self.host.files[ROOT + '/' + stray] = b'PMTiles stray'
        incoming = publish.shared_manifest(manifest('next'))  # an upload that stopped, whose files are already up
        for f in incoming['files']:
            self.host.files[ROOT + '/' + publish.place('next', f)] = ARCHIVE if f['path'] == 'ways.pmtiles' else LICENSE
        files = lambda *names: {'files/' + hashlib.sha256(n).hexdigest() + '.pmtiles' for n in names}
        plan = maintenance.prune(self.ftp, now=NOW, incoming=incoming)
        self.assertEqual(set(plan['delete']), {'releases/other', 'releases/again', 'releases/before', stray} | files(b'PMTiles other'))
        applied = maintenance.prune(self.ftp, apply=True, now=NOW, incoming=incoming)
        self.assertEqual(applied['delete'], plan['delete'])
        left = {name.removeprefix(ROOT + '/') for name in self.host.files if name.startswith(ROOT + '/files/')}
        self.assertEqual(left, files(b'PMTiles live', b'PMTiles rollback', ARCHIVE) | {publish.shared_path({'sha256': hashlib.sha256(LICENSE).hexdigest(), 'path': 'LICENSE.txt'})})
        # A stopped upload kept 7 days keeps what it moved among the shared files
        stopped = publish.shared_manifest(manifest('stopped'))
        stage = '.upload-stopped-' + 'c' * 32
        self.host.dirs.add(ROOT + '/' + stage)
        maintenance.mark_upload(self.ftp, stage, stopped, encoded(stopped), now=NOW - timedelta(days=1))
        self.host.files[f'{ROOT}/{stage}/manifest.json'] = encoded(stopped)
        self.host.files[ROOT + '/' + stray] = b'PMTiles stray'
        moved = 'files/' + hashlib.sha256(ARCHIVE).hexdigest() + '.pmtiles'
        self.host.files[ROOT + '/' + moved] = ARCHIVE
        plan = maintenance.prune(self.ftp, now=NOW)
        self.assertEqual((stray in plan['delete'], moved in plan['delete']), (True, False))
        self.assertNotIn(stray, maintenance.prune(self.ftp, now=NOW, incoming_files={stray})['delete'],
                         "a cell the upload under way names stays, though its index isn't up yet")
        del self.host.files[ROOT + '/' + stray]
        # A release that stays without a manifest to read keeps every shared file; anything else among them stops it
        self.host.dirs.add(ROOT + '/releases/unknown')
        self.host.files[ROOT + '/' + stray] = b'PMTiles stray'
        self.assertNotIn(stray, maintenance.prune(self.ftp, now=NOW)['delete'])
        valid = self.host.files[ROOT + '/releases/live/manifest.json']
        self.host.files[ROOT + '/releases/live/manifest.json'] = b'{"schema": 1'  # a corrupt manifest stops it too
        with self.assertRaises(ValueError):
            maintenance.prune(self.ftp, now=NOW)
        self.host.files[ROOT + '/releases/live/manifest.json'] = valid
        self.host.files[ROOT + '/files/notes.txt'] = b'unknown'
        with self.assertRaisesRegex(ValueError, 'Unexpected shared file'):
            maintenance.prune(self.ftp, now=NOW)
