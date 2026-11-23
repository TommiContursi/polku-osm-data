"""Bounded retention of publisher-owned files in the dedicated OSM directory."""
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import posixpath
import re
import uuid

from tools.osm_data import publish as p

BUDGET = 80 * 1024**3
UPLOAD = '_upload.json'
STAGED_CELL = 16 * 1024**2  # a cell holds at most 1 MiB, or one tile of at most 8 MiB


def safe(name):
    return name not in {'', '.', '..'} and '/' not in name and '\\' not in name and not any(ord(c)<32 for c in name)


def children(ftp, base='.'):
    names = [posixpath.basename(name) for name in ftp.nlst(base)]
    p.require(all(safe(name) for name in names), 'Unsafe maintenance path')
    return sorted(names)


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    p.require(result.tzinfo is not None, 'Maintenance timestamp needs timezone')
    return result


def write_record(ftp, path, record):
    body = json.dumps(record, sort_keys=True).encode()
    temporary = path + '.' + uuid.uuid4().hex + '.tmp'
    ftp.storbinary('STOR '+temporary, io.BytesIO(body))
    p.require(p.retrieve(ftp, temporary)==body, 'Maintenance record readback differs')
    ftp.replace(temporary, path)


def prepare_publication(ftp, manifest, raw, previous, now=None):
    old = p.validate(json.loads(previous), pointed=True)['release'] if previous else None
    if old == manifest['release']:
        return  # Rechecking the live release must not erase its rollback predecessor.
    if 'maintenance' not in children(ftp):
        ftp.mkd('maintenance')
    p.require(ftp.kind('maintenance')=='directory', 'Maintenance needs a real directory')
    write_record(ftp, 'maintenance/p-'+manifest['release']+'.json', {
        'version':1, 'release':manifest['release'], 'manifest_sha256':hashlib.sha256(raw).hexdigest(),
        'published_at':(now or datetime.now(timezone.utc)).isoformat(), 'previous_release':old})


def pending_publication(ftp, manifest, raw, now):
    name='p-'+manifest['release']+'.json'
    if 'maintenance' not in children(ftp): return None
    p.require(ftp.kind('maintenance')=='directory','Maintenance needs a real directory')
    if name not in children(ftp,'maintenance'): return None
    p.require(ftp.kind('maintenance/'+name)=='file','Pending intent needs a regular file')
    record=json.loads(p.retrieve(ftp,'maintenance/'+name))
    p.require(record.get('version')==1 and record.get('release')==manifest['release']
              and record.get('manifest_sha256')==hashlib.sha256(raw).hexdigest(),'Pending intent manifest differs')
    previous=record.get('previous_release')
    p.require(previous is None or isinstance(previous,str) and p.NAME.fullmatch(previous)
              and previous!=manifest['release'],'Invalid pending predecessor')
    p.require(timestamp(record['published_at'])<=now,'Pending publication is in the future')
    return record


def record_publication(ftp, manifest, raw, previous, now=None):
    now=now or datetime.now(timezone.utc)
    record=pending_publication(ftp,manifest,raw,now)
    if record is None:
        p.require(previous and json.loads(previous)['release']==manifest['release'],
                  'Missing recoverable publication intent')
        return
    p.require(p.validate(json.loads(p.retrieve(ftp,'current.json')),pointed=True)==p.current(manifest),
              'Live pointer differs before publication receipt')
    record['published_at']=now.isoformat()
    write_record(ftp,'maintenance/r-'+manifest['release']+'.json',record)
    ftp.remove('maintenance/p-'+manifest['release']+'.json')


def mark_upload(ftp, stage, manifest, raw, now=None):
    record = {'version':1, 'release':manifest['release'], 'created_at':(now or datetime.now(timezone.utc)).isoformat(),
              'files':{f['path']:f['bytes'] for f in manifest['files']} | {'manifest.json':len(raw)}}
    ftp.storbinary('STOR '+stage+'/'+UPLOAD, io.BytesIO(json.dumps(record,sort_keys=True).encode()))


def usage(ftp, base='.', depth=0):
    p.require(depth<=4, 'Unexpected nested maintenance content')
    total=0
    for name,(kind,size) in ftp.attributes(base).items():
        path=name if base=='.' else base+'/'+name
        p.require(safe(name), 'Unsafe maintenance path')
        p.require(kind in {'file','directory'}, 'Maintenance needs regular files and real directories (no symlinks)')
        total += size if kind=='file' else usage(ftp,path,depth+1)
    return total


def upload_record(ftp, base, release, now):
    p.require(ftp.kind(base+'/'+UPLOAD)=='file','Upload receipt needs a regular file')
    record=json.loads(p.retrieve(ftp,base+'/'+UPLOAD))
    p.require(record.get('version')==1 and record.get('release')==release, 'Invalid upload ownership record')
    stamp=timestamp(record['created_at'])
    p.require(stamp<=now and isinstance(record.get('files'),dict) and record['files'], 'Invalid upload metadata')
    for name,size in record['files'].items():
        p.require(p.NAME.fullmatch(name) and type(size) is int and size>=0, 'Unsafe upload file metadata')
    p.require('manifest.json' in record['files'] and 'LICENSE.txt' in record['files'], 'Incomplete upload metadata')
    return record,stamp


def own_files(manifest, raw):
    """What a release's own folder holds, by size: its manifest, and its files from before the shared files."""
    return {f['path']:f['bytes'] for f in manifest['files'] if 'url' not in f} | {'manifest.json':len(raw)}


def shared_files(ftp):
    """The shared files, {path: bytes}. Anything else among them stops the run."""
    if p.SHARED not in children(ftp): return {}
    p.require(ftp.kind(p.SHARED)=='directory', 'Shared files need a real directory')
    result={}
    for name,(kind,size) in ftp.attributes(p.SHARED).items():
        path=p.SHARED+'/'+name
        p.require(p.SHARED_NAME.fullmatch(name) and kind=='file', 'Unexpected shared file '+path)
        result[path]=size
    return result


def shared_named(ftp, release, manifest, have=None):
    """The shared files a manifest names, the cells of its indexes among them. Given have, an index the shared files
    don't hold yet names nothing: its cells go up after it."""
    return ({p.place(release,f) for f in manifest['files'] if 'url' in f}
            | {p.place(release,c) for c in p.hosted_cells(ftp,manifest,have)})


def named_by(ftp, base, release, have=None):
    """The shared files the manifest in a release's folder (or staging) names, or None when it has none; a corrupt one,
    or a missing index of a release, stops the run."""
    if 'manifest.json' not in children(ftp,base): return None
    p.require(ftp.kind(base+'/manifest.json')=='file','Release manifest needs a regular file')
    m=p.validate(json.loads(p.retrieve(ftp,base+'/manifest.json')))
    p.require(m['release']==release,'Release manifest differs from its folder')
    return shared_named(ftp,release,m,have)


def validate_files(ftp,base,expected,partial=False):
    names=children(ftp,base)
    p.require(set(names)<=set(expected), 'Unexpected files in retained release')
    p.require(partial or set(names)==set(expected), 'Retained release files differ')
    for name in names:
        path=base+'/'+name
        p.require(ftp.kind(path)=='file', 'Retained release needs regular files')
        size=ftp.size(path)
        p.require(size<=expected[name] if partial else size==expected[name], 'Retained file size differs')
    return sum(ftp.size(base+'/'+name) for name in names)



def prune(ftp, apply=False, incoming_bytes=0, now=None, incoming=None, incoming_files=(), budget=BUDGET, product='OSM'):
    """incoming is the manifest an upload is putting on the host, and incoming_files all the shared files it names,
    cells too: they all stay. budget is the folder's size limit, and product names it in the error."""
    now=now or datetime.now(timezone.utc)
    p.require(type(incoming_bytes) is int and incoming_bytes>=0,'Invalid incoming byte budget')
    incoming_release=incoming and p.validate(incoming)['release']
    p.require(ftp.kind('current.json')=='file','Live pointer needs a regular file')
    before=p.retrieve(ftp,'current.json')
    live=p.validate(json.loads(before),pointed=True)
    root=children(ftp)
    p.require('releases' in root and ftp.kind('releases')=='directory','Releases needs a real directory')
    releases=children(ftp,'releases')
    p.require(ftp.kind('releases/'+live['release'])=='directory'
              and ftp.kind('releases/'+live['release']+'/manifest.json')=='file','Live release needs regular files')
    raw=p.retrieve(ftp,'releases/'+live['release']+'/manifest.json')
    p.require(p.current(p.validate(json.loads(raw)))==live,'Live manifest differs before retention')
    pending=pending_publication(ftp,json.loads(raw),raw,now)
    records={}; manifests={}; keep={}; remove={}
    if 'maintenance' in root:
        p.require(ftp.kind('maintenance')=='directory','Maintenance needs a real directory')
        for name in children(ftp,'maintenance'):
            if not (name.startswith('r-') and name.endswith('.json')): continue
            release=name[2:-5]
            p.require(p.NAME.fullmatch(release),'Unsafe receipt release')
            if release not in releases: continue
            if release==live['release'] and pending: continue
            p.require(ftp.kind('maintenance/'+name)=='file','Receipt needs a regular file')
            record=json.loads(p.retrieve(ftp,'maintenance/'+name))
            p.require(record.get('version')==1 and record.get('release')==release,'Invalid publication receipt')
            p.require(re.fullmatch('[a-f0-9]{64}',record.get('manifest_sha256','')),'Invalid receipt digest')
            previous=record.get('previous_release')
            p.require(previous is None or isinstance(previous,str) and p.NAME.fullmatch(previous),'Invalid predecessor')
            p.require(timestamp(record['published_at'])<=now,'Publication receipt is in the future')
            p.require(ftp.kind('releases/'+release)=='directory','Release needs a real directory')
            if not children(ftp,'releases/'+release):
                records[release]=record;manifests[release]=(None,b'');continue
            body=p.retrieve(ftp,'releases/'+release+'/manifest.json')
            m=p.validate(json.loads(body))
            p.require(m['release']==release and hashlib.sha256(body).hexdigest()==record.get('manifest_sha256'),'Receipt manifest differs')
            records[release]=record;manifests[release]=(m,body)
    if pending:
        records[live['release']]=pending
        manifests[live['release']]=(json.loads(raw),raw)
    # Only the live release, its saved predecessor (rollback) and the candidate: an upload's incoming release too,
    # and the upload removes the candidate it replaces once candidates/current.json names the new one
    named=None
    if 'candidates' in root and 'current.json' in children(ftp,'candidates'):
        named=p.validate(json.loads(p.retrieve(ftp,'candidates/current.json')))['release']
    protected={live['release'],incoming_release,named}-{None}
    predecessor=None
    if live['release'] in records:
        predecessor=records[live['release']].get('previous_release')
        if predecessor:
            p.require(predecessor in releases,'Recorded predecessor is missing')
            protected.add(predecessor)
    for release in releases:
        base='releases/'+release
        p.require(p.NAME.fullmatch(release) and ftp.kind(base)=='directory','Unsafe release directory')
        if release in protected: keep[base]='live/predecessor/candidate';continue
        if live['release'] not in records: keep[base]='live receipt unavailable';continue
        if release in records:
            m,body=manifests[release]
            if m is None:
                remove[base]=0;continue
            expected=own_files(m,body)
            if UPLOAD in children(ftp,base):
                upload_record(ftp,base,release,now);expected[UPLOAD]=ftp.size(base+'/'+UPLOAD)
            remove[base]=validate_files(ftp,base,expected,partial=True)
            p.require(all(ftp.size(base+'/'+name)==expected[name] for name in children(ftp,base)),
                      'Retained file size differs')
        elif UPLOAD in children(ftp,base):
            record,_=upload_record(ftp,base,release,now)
            expected=record['files'] | {UPLOAD:ftp.size(base+'/'+UPLOAD)}
            remove[base]=validate_files(ftp,base,expected,partial=True)
        else: keep[base]='unmanaged; never delete'
    staged=[]
    for stage in root:
        match=re.fullmatch(r'\.upload-([A-Za-z0-9][A-Za-z0-9._-]{0,120})-[a-f0-9]{32}',stage)
        if not match: continue
        if match[1]==incoming_release: keep[stage]='active resume';continue
        p.require(ftp.kind(stage)=='directory','Staging needs a real directory')
        if UPLOAD not in children(ftp,stage): keep[stage]='unmanaged staging';continue
        record,stamp=upload_record(ftp,stage,match[1],now)
        if now-stamp<=timedelta(days=7):
            keep[stage]='staging within 7 days'
            # Its manifest goes up whole before any of its files move among the shared files
            if 'manifest.json' in children(ftp,stage) and ftp.size(stage+'/manifest.json')==record['files']['manifest.json']:
                staged.append((stage,match[1]))
            continue
        cells={name:STAGED_CELL for name in children(ftp,stage) if p.SHARED_NAME.fullmatch(name)}  # on its way
        remove[stage]=validate_files(ftp,stage,record['files'] | cells | {UPLOAD:ftp.size(stage+'/'+UPLOAD)},partial=True)
    # A shared file goes once no release or staging that stays, nor the incoming upload, names it; only when every
    # release that stays can be read
    have=shared_files(ftp)
    kept_files,sweep=set(),live['release'] in records
    for release in releases:
        if 'releases/'+release in remove: continue
        found=named_by(ftp,'releases/'+release,release)
        sweep=sweep and found is not None
        kept_files|=found or set()
    for stage,release in staged:  # a stopped upload kept 7 days keeps what it moved among the shared files
        kept_files|=named_by(ftp,stage,release,have) or set()
    kept_files|=set(incoming_files)
    if incoming: kept_files|=shared_named(ftp,incoming_release,incoming,have)
    for path,size in have.items():
        if path in kept_files: continue
        if sweep: remove[path]=size
        else: keep[p.SHARED]='a release that stays has no manifest to read, or the live receipt is unavailable'
    used=usage(ftp)
    after=used-sum(remove.values())
    p.require(after+incoming_bytes+4096<=budget,
              f'{product} storage budget exceeded ({budget/1024**3:g} GiB); retain live data and inspect storage')
    result={'delete':sorted(remove),'keep':keep,'bytes_before':used,'bytes_after':after,'incoming_bytes':incoming_bytes,
            'applied':apply,'recover_receipt':live['release'] if pending else None,'previous_release':predecessor}
    if apply:
        p.require(p.retrieve(ftp,'current.json')==before,'Live pointer changed before retention')
        if pending: record_publication(ftp,json.loads(raw),raw,None,now=now)
        # Releases before the shared files they named, so a stopped run leaves no release missing its files
        for base in sorted(result['delete'],key=lambda path:path.startswith(p.SHARED+'/')):
            p.require(p.retrieve(ftp,'current.json')==before,'Live pointer changed during retention')
            if base.startswith(p.SHARED+'/'):
                ftp.remove(base);continue
            # Keep ownership metadata until payload deletion finishes, so interruption is recoverable.
            last='manifest.json' if base.startswith('releases/') and base.split('/')[-1] in records else UPLOAD
            for name in sorted(children(ftp,base),key=lambda name:name==last):
                ftp.remove(base+'/'+name)
            ftp.rmd(base)
    return result


def test_retention(ftp):
    """Run only inside test_host's newly created disposable working directory."""
    now=datetime.now(timezone.utc)
    ftp.mkd('releases')
    content={'ways.pmtiles':b'PMTiles retention test','LICENSE.txt':b'OSM contributors, ODbL'}
    for release,days in [('expired',50),('previous',7),('live',0)]:
        files=[{'path':name,'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()} for name,body in content.items()]
        m=p.validate({'schema':1,'release':release,'source':{'timestamp':now.isoformat()},'files':files,
                      'datasets':{'ways':{'url':'ways.pmtiles','bytes':files[0]['bytes'],'sha256':files[0]['sha256']}}})
        if release=='live':
            m=p.validate(p.shared_manifest(m));ftp.mkd(p.SHARED)
        raw=json.dumps(m,sort_keys=True).encode();base='releases/'+release;ftp.mkd(base)
        for f in m['files']: ftp.storbinary('STOR '+p.place(release,f),io.BytesIO(content[f['path']]))
        ftp.storbinary('STOR '+base+'/manifest.json',io.BytesIO(raw))
        previous=json.dumps(p.current(dict(m,release='previous'))).encode() if release=='live' else None
        prepare_publication(ftp,m,raw,previous,now=now-timedelta(days=days))
        ftp.storbinary('STOR current.json',io.BytesIO(json.dumps(p.current(m),sort_keys=True).encode()))
        record_publication(ftp,m,raw,previous,now=now-timedelta(days=days))
        if release=='live':
            pointer=json.dumps(p.current(m),sort_keys=True).encode()
            ftp.storbinary('STOR current.json',io.BytesIO(pointer))
    stage='.upload-live-'+'a'*32;ftp.mkd(stage)
    mark_upload(ftp,stage,m,raw,now=now-timedelta(days=8))
    ftp.storbinary('STOR '+stage+'/ways.pmtiles',io.BytesIO(b'PM'))
    stray=p.SHARED+'/'+'0'*64+'.pmtiles';ftp.storbinary('STOR '+stray,io.BytesIO(b'PMTiles no release names'))
    preview=prune(ftp,now=now)
    p.require(set(preview['delete'])=={'releases/expired',stage,stray},'Disposable retention preview differs')
    p.require('expired' in children(ftp,'releases'),'Retention preview wrote files')
    applied=prune(ftp,apply=True,now=now)
    p.require(children(ftp,'releases')==['live','previous'] and p.retrieve(ftp,'current.json')==pointer
              and set(shared_files(ftp))=={p.place('live',f) for f in m['files']},
              'Disposable retention affected protected releases')
    return {'deleted_entries':len(applied['delete']),'live_and_predecessor_preserved':True,'preview_read_only':True}
