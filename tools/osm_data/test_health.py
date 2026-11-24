"""The live release in a browser, and that it is the one just promoted."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from tools.osm_data import health
from tools.osm_data.test_publish import manifest,pointer,encoded


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.release=self.root/'release';self.release.mkdir()
        self.m=manifest('new');(self.release/'manifest.json').write_bytes(encoded(self.m))
        self.saved=self.release/'rollback-test.json';self.saved.write_bytes(encoded(pointer(manifest('old'))))
        self.env=patch.dict(os.environ,{'OSM_PUBLIC_URL':'https://data.example/polku-osm'});self.env.start();self.addCleanup(self.env.stop)

    def test_daily_browser_failure_reports_without_a_rollback(self):
        with patch.object(health.p,'check',return_value=pointer(self.m)),patch.object(health.subprocess,'run',side_effect=RuntimeError('browser failed')),patch.object(health.p,'rollback') as rollback:
            with self.assertRaisesRegex(RuntimeError,'browser failed'): health.verify(self.root/'out')
        rollback.assert_not_called()

    def test_post_promotion_failure_fails_without_touching_the_host(self):
        # The check runs without publishing credentials; Polku's rollback job restores the predecessor
        with patch.object(health.p,'get',return_value=encoded(pointer(self.m))),patch.object(health.subprocess,'run',side_effect=subprocess.TimeoutExpired('browser',180)),patch.object(health.p,'rollback') as rollback:
            with self.assertRaises(subprocess.TimeoutExpired): health.verify(self.root/'out',self.release)
        rollback.assert_not_called()

    def test_changed_live_release_fails_before_browser(self):
        with patch.object(health.p,'get',return_value=encoded(pointer(manifest('different')))),patch.object(health.subprocess,'run') as browser:
            with self.assertRaisesRegex(ValueError,'release'): health.verify(self.root/'out',self.release)
        browser.assert_not_called()

    def test_success_requires_all_current_archives_and_32_browser_cases(self):
        live=pointer(self.m)
        live['datasets']={str(i):{'url':'releases/new/'+str(i)+'.pmtiles'} for i in range(28)}
        def browser(*args,**kwargs):
            products={'network':[{'url':'https://data.example/polku-osm/'+d['url']} for d in live['datasets'].values()]}
            row=dict(products,lighting={'network':[]},details={})
            json.dump({'cases':{'desktop':[row]*16,'phone_emulated':[row]*16}},kwargs['stdout'])
            self.assertEqual(kwargs['timeout'],180)
        with patch.object(health.p,'check',return_value=live),patch.object(health.subprocess,'run',side_effect=browser):
            result=health.verify(self.root/'out')
        self.assertEqual(result['release'],'new');self.assertEqual(result['browser_cases'],32)

    def test_a_release_with_reedbeds_needs_their_four_archives_too(self):
        for read in [32,28]:
            with self.subTest(read=read):
                live=pointer(self.m)
                live['datasets']={str(i):{'url':'releases/new/'+str(i)+'.pmtiles'} for i in range(28)}
                live['datasets'].update({k:{'url':'releases/new/'+k+'.pmtiles'} for k in ['wetlands','sweden-wetlands','norway-wetlands','estonia-wetlands']})
                def browser(*args,**kwargs):
                    products={'network':[{'url':'https://data.example/polku-osm/'+d['url']} for d in list(live['datasets'].values())[:read]]}
                    json.dump({'cases':{'desktop':[dict(products,lighting={'network':[]},details={})]*16,'phone_emulated':[dict(products,lighting={'network':[]},details={})]*16}},kwargs['stdout'])
                with patch.object(health.p,'check',return_value=live),patch.object(health.subprocess,'run',side_effect=browser):
                    if read==32:self.assertEqual(health.verify(self.root/('out'+str(read)))['archive_count'],32)
                    else:
                        with self.assertRaisesRegex(ValueError,'archives'):health.verify(self.root/('out'+str(read)))

    def test_a_release_with_points_needs_their_four_archives_too(self):
        for read in [36,32]:
            with self.subTest(read=read):
                live=pointer(self.m)
                live['datasets']={str(i):{'url':'releases/new/'+str(i)+'.pmtiles'} for i in range(28)}
                live['datasets'].update({k:{'url':'releases/new/'+k+'.pmtiles'} for c in ['','sweden-','norway-','estonia-'] for k in [c+'wetlands',c+'points']})
                def browser(*args,**kwargs):
                    products={'network':[{'url':'https://data.example/polku-osm/'+d['url']} for d in list(live['datasets'].values())[:read]]}
                    json.dump({'cases':{'desktop':[dict(products,lighting={'network':[]},details={})]*16,'phone_emulated':[dict(products,lighting={'network':[]},details={})]*16}},kwargs['stdout'])
                with patch.object(health.p,'check',return_value=live),patch.object(health.subprocess,'run',side_effect=browser):
                    if read==36:self.assertEqual(health.verify(self.root/('points'+str(read)))['archive_count'],36)
                    else:
                        with self.assertRaisesRegex(ValueError,'archives'):health.verify(self.root/('points'+str(read)))

    def test_a_dataset_in_cells_is_read_through_its_cells(self):
        cell={'path':'c.pmtiles','bytes':10,'sha256':'c'*64}
        body=json.dumps({'schema':1,'zoom':14,'cells':[[14,1,1,10,'c'*64]]}).encode()
        for mode in ['ok','index','outside']:
            with self.subTest(mode=mode):
                live=pointer(self.m)
                live['datasets']={str(i):{'url':'files/'+str(i)*64+'.pmtiles'} for i in range(28)}
                live['datasets']['0']={'url':'files/'+'0'*64+'.json','format':health.p.CELLS,'bytes':len(body),
                                       'sha256':hashlib.sha256(body).hexdigest()}
                def browser(*args,**kwargs):
                    urls=['https://data.example/polku-osm/'+d['url'] for k,d in live['datasets'].items() if k!='0']
                    urls.append('https://data.example/polku-osm/'+({'ok':'files/'+'c'*64,'index':live['datasets']['0']['url'][:-5],'outside':'files/'+'d'*64}[mode])+'.pmtiles')
                    row=dict({'network':[{'url':u} for u in urls]},lighting={'network':[]},details={})
                    json.dump({'cases':{'desktop':[row]*16,'phone_emulated':[row]*16}},kwargs['stdout'])
                with patch.object(health.p,'check',return_value=live),patch.object(health.p,'get',return_value=body),\
                        patch.object(health.subprocess,'run',side_effect=browser):
                    if mode=='ok':self.assertEqual(health.verify(self.root/mode)['archive_count'],28)
                    else:
                        with self.assertRaisesRegex(ValueError,'archives'):health.verify(self.root/mode)

    def test_extended_release_requires_geometry_url_and_both_profiles_area_checks(self):
        for mode in ['ok','missing-url','missing-cases','missing-consumers']:
            with self.subTest(mode=mode):
                live=pointer(self.m)
                live['datasets']={str(i):{'url':'releases/new/'+str(i)+'.pmtiles'} for i in range(28)}
                live['datasets']['geometry']={'url':'releases/new/geometry.pmtiles'}
                def browser(*args,**kwargs):
                    products={'network':[{'url':'https://data.example/polku-osm/'+d['url']} for k,d in live['datasets'].items() if mode!='missing-url' or k!='geometry']}
                    row=dict(products,lighting={'network':[]},details={})
                    checks=[{'complete':True,'consumers_verified':mode!='missing-consumers'}]*6 if mode!='missing-cases' else []
                    json.dump({'cases':{'desktop':[row]*16,'phone_emulated':[row]*16},'geometry_checks':{'desktop':checks,'phone_emulated':checks}},kwargs['stdout'])
                with patch.object(health.p,'check',return_value=live),patch.object(health.subprocess,'run',side_effect=browser):
                    if mode=='ok':self.assertEqual(health.verify(self.root/mode)['archive_count'],29)
                    else:
                        with self.assertRaisesRegex(ValueError,'archives|geometry'):health.verify(self.root/mode)
