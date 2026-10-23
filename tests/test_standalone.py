from pathlib import Path
import concurrent.futures, http.client, json, sys, tempfile, threading, unittest, urllib.parse
from datetime import datetime, timedelta
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'kindle-standalone'))
import app, pages, renderer
from store import Store, Conflict, atomic_json

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name); (self.root/'data').mkdir()
        self.path=self.root/'data/agenda.json'
        atomic_json(self.path,dict(token='a'*32,revision=0,city='',events=[]))
        self.store=Store(self.path)
        self.event=dict(date='2026-10-22',time='09:30',title='测试日程 <script>',note='跨设备编辑')
    def tearDown(self): self.temp.cleanup()
    def test_edits_deletion_and_backup(self):
        first=self.store.change(0,'save',self.event)
        identity=first['events'][0]['id']
        second=self.store.change(1,'save',dict(self.event,id=identity,title='已修改'))
        self.assertEqual(len(second['events']),1)
        self.assertEqual(second['events'][0]['title'],'已修改')
        self.assertEqual(json.loads(self.path.with_suffix('.backup.json').read_text(encoding='utf-8')),first)
        self.assertEqual(self.store.change(2,'delete',{'id':identity})['events'],[])
    def test_stale_editor_cannot_overwrite(self):
        self.store.change(0,'save',self.event)
        with self.assertRaises(Conflict): self.store.change(0,'city',{'city':'上海'})
        self.assertEqual(self.store.read()['city'],'')
    def test_simultaneous_editors(self):
        def save(_):
            try: Store(self.path).change(0,'save',self.event); return 'saved'
            except Conflict: return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertCountEqual(list(pool.map(save,[1,2])),['saved','conflict'])
        self.assertEqual(len(self.store.read()['events']),1)
    def test_invalid_input_preserves_file(self):
        original=self.path.read_bytes()
        for changes in ({'date':'2026-02-30'},{'time':'25:00'},{'title':''},{'title':'a'*101}):
            with self.assertRaises(ValueError): self.store.change(0,'save',dict(self.event,**changes))
        self.assertEqual(self.path.read_bytes(),original)
    def test_browser_auth_csrf_conflict_escape(self):
        application=app.Application(self.root)
        server=app.ThreadingHTTPServer(('127.0.0.1',0),application.handler())
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        def req(method,path,values=None,headers=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            body=urllib.parse.urlencode(values) if values else None
            conn.request(method,path,body=body,headers=headers or {})
            response=conn.getresponse(); result=(response.status,response.read(),dict(response.getheaders()))
            conn.close(); return result
        prefix='/'+application.token
        try:
            self.assertEqual(req('GET','/')[0],404)
            self.assertEqual(req('GET','/bad/')[0],404)
            self.assertEqual(req('GET',prefix+'/')[0],200)
            refresh=dict(interval=10,changes_since_full=9,full_refreshes=1,frame_updates=10,last='normal')
            atomic_json(self.root/'data/status.json',dict(display_at='2026-10-23T10:00:00+08:00',screen_refresh=refresh))
            status=req('GET',prefix+'/status.json')
            self.assertEqual(status[0],200)
            self.assertEqual(json.loads(status[1])['news_count'],0)
            self.assertEqual(json.loads(status[1])['build'],app.BUILD)
            self.assertEqual(json.loads(status[1])['screen_refresh'],refresh)
            self.assertEqual(req('POST',prefix+'/action',dict(csrf='中文'))[0],403)
            values=dict(self.event,csrf=application.csrf,revision=0,action='save',device='kindle')
            self.assertEqual(req('POST',prefix+'/action',values,{'Origin':'http://evil.invalid'})[0],403)
            result=req('POST',prefix+'/action',values)
            self.assertEqual(result[0],303); self.assertIn('device=kindle',result[2]['Location'])
            self.assertEqual(req('POST',prefix+'/action',values)[0],409)
            html=req('GET',prefix+'/?device=kindle')[1].decode()
            self.assertIn('&lt;script&gt;',html); self.assertNotIn('<script>',html)
            self.assertIn('返回看板',html)
            self.assertEqual(req('GET',prefix+'/frame.png')[1][:8],b'\x89PNG\r\n\x1a\n')
        finally: server.shutdown(); server.server_close(); thread.join()
    def test_offline_and_day_rollover(self):
        config=self.store.change(0,'save',self.event)
        before=datetime(2026,10,22,9,0,tzinfo=renderer.TZ)
        first=renderer.render(config,{},before)
        second=renderer.render(config,{},before+timedelta(days=1))
        self.assertEqual(first.size,(600,800)); self.assertEqual(first.mode,'L')
        self.assertNotEqual(first.crop((0,500,600,720)).tobytes(),second.crop((0,500,600,720)).tobytes())
    def test_native_controls(self):
        calls=[]
        def command(args,timeout=30):
            calls.append(args)
            if args[:2]==['initctl','status']: return 0,'lab126_gui stop/waiting'
            return 0,'0'
        with patch.object(app,'command',command),patch.object(app.time,'sleep'):
            frame=self.root/'frame.png'; renderer.Image.new('L',(600,800),255).save(frame)
            device=app.Device(None); device.display(frame)
            self.assertLess(calls.index(['initctl','stop','lab126_gui']),calls.index(['eips','-f','-g',str(frame)]))
            device.editor('http://127.0.0.1:8766/test/?device=kindle')
            self.assertIn(['lipc-set-prop','com.lab126.browser','gotoURL','http://127.0.0.1:8766/test/?device=kindle'],calls)
            device.close()
    def test_failed_gui_stop_does_not_draw(self):
        calls=[]
        def command(args,timeout=30):
            calls.append(args); return 0,'lab126_gui start/running'
        with patch.object(app,'command',command),patch.object(app.time,'sleep'):
            device=app.Device(None)
            with self.assertRaises(RuntimeError): device.display('/tmp/frame.png')
            device.close()
        self.assertNotIn(['eips','-g','/tmp/frame.png'],calls)
        self.assertIn(['initctl','start','lab126_gui'],calls)

if __name__=='__main__':
    unittest.main(verbosity=2)
