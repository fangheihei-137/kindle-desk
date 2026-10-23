import hashlib,http.client,io,json,shutil,sys,tempfile,threading,unittest,zipfile,subprocess
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]/'kindle-standalone'
sys.path.insert(0,str(ROOT))
import app,updater
from store import atomic_json

class WirelessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        (self.root/'data').mkdir()
        atomic_json(self.root/'data/agenda.json',dict(token='d'*32,revision=0,events=[],todos=[]))
    def tearDown(self): self.temp.cleanup()
    def zipdata(self,files):
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:
            for name,data in files.items(): z.writestr(name,data)
        return b.getvalue()
    def test_real_package_integrity_and_no_user_data(self):
        payload,build=updater.package(ROOT); manifest,files=updater.validate(payload)
        self.assertEqual(build,app.BUILD)
        self.assertFalse(any('data/' in n or 'updater.py'==n or n.endswith('.sh') for n in files))
        self.assertTrue(set(updater.CORE)<=set(files))
    def test_traversal_and_checksum_rejected(self):
        for name in ('../app.py','data/agenda.json','start.sh','updater.py','/tmp/file'):
            with self.assertRaises(ValueError): updater.validate(self.zipdata({'manifest.json':'{}',name:'x'}))
        payload,build=updater.package(ROOT)
        with zipfile.ZipFile(io.BytesIO(payload)) as z: files={n:z.read(n) for n in z.namelist()}
        files['app.py']+=b'\n# corrupt'
        with self.assertRaises(ValueError): updater.validate(self.zipdata(files))
    def test_stage_apply_confirm_and_preserve_agenda(self):
        for name in updater.CORE+('updater.py',): shutil.copy2(ROOT/name,self.root/name)
        shutil.copytree(ROOT/'assets',self.root/'assets'); shutil.copytree(ROOT/'vendor',self.root/'vendor')
        original=(self.root/'data/agenda.json').read_bytes()
        payload,build=updater.package(ROOT)
        s=updater.stage(payload,self.root)
        self.assertEqual(s['phase'],'staged')
        updater.apply(self.root); self.assertEqual(updater.state(self.root)['phase'],'trial')
        updater.confirm(self.root); self.assertIsNone(updater.state(self.root))
        self.assertEqual((self.root/'data/agenda.json').read_bytes(),original)
        self.assertEqual(json.loads((self.root/'data/update-result.json').read_text())['result'],'installed')
    def trial_fixture(self):
        identity='a'*32; folder=self.root/'data/updates'/identity
        (folder/'candidate').mkdir(parents=True)
        (self.root/'app.py').write_bytes(b'OLD')
        (folder/'candidate/app.py').write_bytes(b'NEW')
        (folder/'candidate/pages.py').write_bytes(b'NEW PAGE')
        updater.write_json(self.root/'data/update-pending.json',dict(id=identity,phase='staged',build='test',files={n:hashlib.sha256((folder/'candidate'/n).read_bytes()).hexdigest() for n in ('app.py','pages.py')}))
    def test_failed_boot_or_power_interruption_rolls_back(self):
        self.trial_fixture(); updater.apply(self.root)
        self.assertEqual((self.root/'app.py').read_bytes(),b'NEW')
        updater.recover(self.root)
        self.assertEqual((self.root/'app.py').read_bytes(),b'OLD')
        self.assertFalse((self.root/'pages.py').exists())
    def test_partial_copy_failure_rolls_back(self):
        self.trial_fixture()
        original=updater.replace_file
        def replace(source,target):
            if 'candidate' in str(source) and target.name=='pages.py': raise OSError('interrupted')
            original(source,target)
        with patch.object(updater,'replace_file',side_effect=replace):
            with self.assertRaises(OSError): updater.apply(self.root)
        self.assertEqual((self.root/'app.py').read_bytes(),b'OLD')
        self.assertFalse((self.root/'pages.py').exists())
    def test_wireless_endpoint_pair_nonce_and_origin(self):
        application=app.Application(self.root)
        server=app.ThreadingHTTPServer(('127.0.0.1',0),application.handler())
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        prefix='/'+application.token
        def request(method,path,body=None,headers=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            conn.request(method,path,body,headers or {})
            response=conn.getresponse(); result=response.status,response.read(); conn.close(); return result
        try:
            self.assertEqual(request('GET','/wrong/update-info.json')[0],404)
            self.assertEqual(request('GET',prefix+'/update-info.json')[0],409)
            application.mode='pair'
            info=json.loads(request('GET',prefix+'/update-info.json')[1])
            self.assertEqual(request('POST',prefix+'/update',b'bad',{'X-Update-Nonce':'bad'})[0],403)
            headers={'X-Update-Nonce':info['nonce'],'Origin':'http://evil.invalid'}
            self.assertEqual(request('POST',prefix+'/update',b'bad',headers)[0],403)
            headers.pop('Origin')
            with self.assertLogs('kindle',level='WARNING'):
                self.assertEqual(request('POST',prefix+'/update',b'bad',headers)[0],400)
            self.assertFalse(application.updating)
            self.assertEqual(request('POST',prefix+'/update',b'bad',headers)[0],403)
            self.assertFalse((self.root/'data/update-pending.json').exists())
        finally: server.shutdown(); server.server_close(); thread.join()
    def test_supervisor_restart_and_bad_boot_recovery(self):
        for good in (True,False):
            root=self.root/('good' if good else 'bad'); (root/'data').mkdir(parents=True)
            shutil.copy2(ROOT/'updater.py',root/'updater.py')
            identity='b'*32; folder=root/'data/updates'/identity/'candidate'
            folder.mkdir(parents=True)
            new="import sys,updater\nif '--self-test' in sys.argv: sys.exit(0)\n"
            new+=("updater.confirm()\n" if good else "sys.exit(3)\n")
            (folder/'app.py').write_bytes(new.encode('utf-8'))
            pending=dict(id=identity,phase='staged',build='test',files={'app.py':hashlib.sha256(new.encode()).hexdigest()})
            old="import sys,json,pathlib\nr=pathlib.Path(__file__).parent\nif '--self-test' in sys.argv: sys.exit(0)\n"
            old+="if not (r/'once').exists():\n (r/'once').touch()\n (r/'data/update-pending.json').write_text("+repr(json.dumps(pending))+")\n sys.exit(75)\n"
            (root/'app.py').write_text(old,encoding='utf-8')
            result=subprocess.run([sys.executable,str(root/'updater.py')],cwd=root,timeout=15,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            status=json.loads((root/'data/update-result.json').read_text())
            self.assertEqual(status['result'],'installed' if good else 'rolled_back')
            self.assertEqual((root/'app.py').read_text(),new if good else old)

if __name__=='__main__': unittest.main(verbosity=2)
