"""Module isolation, HTTP authorization, image limits and power state transitions."""
import copy,http.client,io,json,os,queue,sys,tempfile,threading,time,unittest,urllib.parse
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1]/'kindle-standalone'
sys.path.insert(0,str(ROOT))
import app,modules,phone,device_control,renderer as ui
from store import Store,atomic_json,Conflict

def rates(used=30): return {'rateLimitsByLimitId':{'codex':{'primary':dict(usedPercent=used,windowDurationMins=300,resetsAt=int(time.time())+1800),'secondary':dict(usedPercent=45,windowDurationMins=10080,resetsAt=int(time.time())+80000)}}}
def picture(size=(400,500),color=180):
    output=io.BytesIO(); ui.Image.new('L',size,color).save(output,'PNG'); return output.getvalue()

class Modules(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name); (self.root/'data').mkdir()
        atomic_json(self.root/'data/agenda.json',dict(token='module-test-only',revision=0,events=[dict(id='e1',date='2026-11-01',time='12:00',title='测试日程')],todos=[dict(id='t1',title='测试待办',done=False)]))
        self.a=app.Application(self.root)
    def tearDown(self): self.temp.cleanup()
    def setting(self,**settings):
        old=self.a.store.read(); new=self.a.store.change(old['revision'],'settings',dict(settings=settings))
        self.a.settings_changed(old,new); return new

    def test_defaults_do_not_rewrite_user_data_and_bulk_settings_are_atomic(self):
        before=(self.root/'data/agenda.json').read_bytes(); value=self.a.store.read()
        self.assertEqual(value['settings']['panel_module'],'finance')
        self.assertEqual(before,(self.root/'data/agenda.json').read_bytes())
        with self.assertRaises(ValueError): self.setting(panel_module='codex',full_refresh=999)
        self.assertEqual(before,(self.root/'data/agenda.json').read_bytes())
        updated=self.setting(panel_module='codex',full_refresh=20)
        self.assertEqual(updated['events'],value['events']); self.assertEqual(updated['todos'],value['todos'])
        with self.assertRaises(Conflict): self.a.store.change(0,'setting',dict(key='panel_module',value='weibo'))

    def test_modules_render_and_route_without_crossing_feed_caches(self):
        self.a.modules.import_codex(rates())
        for key,_,_,_ in modules.CATALOG:
            self.setting(panel_module=key); self.a.mode='dashboard'; frame=self.a.frame(capture=True)
            self.assertEqual(frame.size,(600,800)); self.assertEqual(frame.mode,'L')
            self.a.handle_action('panel_detail'); self.assertEqual(self.a.frame(capture=True).size,(600,800))
        self.a.handle_action('module_picker'); self.assertEqual(self.a.mode,'module_picker')
        self.assertEqual(self.a.frame().size,(600,800))
        self.a.handle_action(self.a.tap_action(100,330))
        self.assertEqual(self.a.config['settings']['panel_module'],'weibo')

    def test_codex_uses_correct_buckets_and_unknown_is_not_zero(self):
        payload=rates(); payload['rateLimits']=dict(primary=dict(usedPercent=99))
        windows=modules.codex_windows(payload)
        self.assertEqual(windows[0]['remaining'],70)
        self.assertEqual(windows[1]['period'],'7 天')
        self.assertEqual(modules.codex_windows(rates(120))[0]['remaining'],0)
        with self.assertRaises(ValueError): modules.codex_windows({'rateLimits':{'primary':{'usedPercent':None}}})
        for bad in (float('nan'),float('inf'),-5):
            payload=rates(bad); payload['rateLimitsByLimitId']['codex']['secondary']=None
            with self.assertRaises(ValueError): modules.codex_windows(payload)
        payload=rates(); payload['rateLimitsByLimitId']['codex']['primary']['resetsAt']=1e50
        self.assertIsNone(modules.codex_windows(payload)[0]['resets_at'])

    def test_codex_cache_is_persistent_and_time_does_not_falsely_refresh(self):
        self.a.modules.import_codex(rates()); before=self.a.modules.cache['codex']['checked_at']
        self.setting(panel_module='codex'); restored=app.Application(self.root)
        with patch.object(modules.time,'time',return_value=before+10000):
            view=restored.panel_view(); self.assertTrue(view['stale']); self.assertEqual(view['checked_at'],before)

    def test_deepseek_private_key_and_currency_values(self):
        secret='sk-private-test-only'; self.a.modules.save_key(secret)
        response=dict(is_available=True,balance_infos=[dict(currency='CNY',total_balance='12.34',granted_balance='2.00',topped_up_balance='10.34')])
        with patch.object(self.a.modules,'request_json',return_value=response) as get:
            self.a.modules.refresh('deepseek',self.a)
        self.assertEqual(get.call_args.args[1]['Authorization'],'Bearer '+secret)
        self.assertEqual(self.a.modules.cache['deepseek']['balances'][0]['total'],'12.34')
        self.assertNotIn(secret,(self.root/'data/modules-cache.json').read_text())
        self.assertNotIn(secret,(self.root/'data/agenda.json').read_text())
        self.assertNotIn(secret,phone.module_page(self.a,'/test').decode())
        self.a.modules.save_key('',remove=True); self.assertNotIn('deepseek',self.a.modules.cache)

    def test_deepseek_failure_does_not_overwrite_successful_balance(self):
        self.a.modules.save_key('sk-test'); self.a.modules._save('deepseek',dict(checked_at=100,balances=[dict(total='12.00')]))
        with patch.object(self.a.modules,'request_json',side_effect=OSError('secret URL')): self.a.modules.refresh('deepseek',self.a)
        row=self.a.modules.cache['deepseek']; self.assertEqual(row['checked_at'],100); self.assertEqual(row['balances'][0]['total'],'12.00')
        self.assertNotIn('secret URL',row['error'])

    def test_weibo_ranking_and_history_expiry(self):
        raw=dict(data=dict(realtime=[dict(word='测试热搜',num=123),dict(word='广告',is_ad=True)]))
        with patch.object(self.a.modules,'request_json',return_value=raw): self.a.modules.refresh('weibo',self.a)
        self.setting(panel_module='weibo'); view=self.a.panel_view(); self.assertEqual(len(view['items']),1)
        self.assertEqual(view['items'][0]['rank'],1); self.assertTrue(view['items'][0]['url'].startswith('https://s.weibo.com/'))
        with patch.object(modules.time,'time',return_value=time.time()+48*3600+1): self.assertEqual(self.a.panel_view()['items'],[])

    def test_image_validation_normalization_use_and_delete(self):
        identity=self.a.modules.add_photo(picture((1200,1600)),'../../图片.png')
        path=self.a.modules.photo_path(identity)
        with ui.Image.open(path) as photo: self.assertEqual(photo.mode,'L'); self.assertEqual(photo.size,(600,800)); self.assertNotIn('exif',photo.info)
        self.assertEqual(self.a.modules.photos()[0]['name'],'图片')
        self.setting(panel_module='photo',panel_photo=identity,sleep_display='photo',standby_photo=identity)
        self.assertEqual(self.a.frame().size,(600,800)); self.a.enter_standby(); self.assertEqual(self.a.frame().size,(600,800))
        with self.assertRaises(ValueError): self.a.modules.delete_photo(identity,self.a.store.read())
        self.setting(panel_photo='',standby_photo='',sleep_display='calendar')
        self.a.modules.delete_photo(identity,self.a.store.read()); self.assertFalse(path.exists())
        for identity in ('../agenda','x'*24,'../../token.json'):
            with self.assertRaises(ValueError): self.a.modules.photo_path(identity)
        with self.assertRaises(ValueError): self.a.modules.add_photo(b'not an image','a.png')
        with self.assertRaises(ValueError): self.a.modules.add_photo(picture((2000,2000)),'large.png')

    def test_module_worker_blocks_during_standby(self):
        self.setting(panel_module='weibo'); self.a.enter_standby(); self.a.module_wake.clear()
        worker=threading.Thread(target=self.a.module_loop); fetch=Mock()
        with patch.object(self.a.modules,'refresh',fetch):
            worker.start(); time.sleep(.08); self.a.request_stop(); worker.join(timeout=2)
        self.assertFalse(worker.is_alive()); fetch.assert_not_called()

    def test_powerd_notifications_are_idempotent_not_toggles(self):
        power=device_control.KindlePower(self.a.handle_action,self.a.stop,runner=Mock(return_value=(0,'')))
        power.backend='powerd'; self.a.power=power
        power.feed('goingToScreenSaver 2'); power.feed('goingToScreenSaver 2')
        self.assertTrue(self.a.standby.is_set()); self.assertTrue(self.a.system_sleeping)
        power.feed('resuming 0'); self.assertTrue(self.a.standby.is_set())
        power.feed('outOfScreenSaver 1'); power.feed('outOfScreenSaver 1')
        self.assertFalse(self.a.standby.is_set()); self.assertEqual(self.a.mode,'dashboard')

    def test_system_power_sleep_is_blocked_for_timer_and_form(self):
        power=device_control.KindlePower(self.a.handle_action,self.a.stop,runner=Mock(return_value=(0,'')))
        power.backend='powerd'; self.a.power=power
        self.a.pomodoro.start(); power.feed('goingToScreenSaver 2')
        self.assertEqual(self.a.mode,'pomodoro'); self.assertFalse(self.a.standby.is_set()); self.assertFalse(power.sleeping)
        self.assertTrue(any(call.args[0][-2:]==['wakeUp','1'] for call in power.runner.call_args_list))
        self.a.pomodoro.reset(); self.a.mode='form'; power.feed('goingToScreenSaver 2')
        self.assertEqual(self.a.mode,'form'); self.assertFalse(self.a.standby.is_set())

    def test_protection_released_after_timer_and_keeps_normal_button_working(self):
        runner=Mock(return_value=(0,'')); power=device_control.KindlePower(lambda event:None,self.a.stop,runner=runner); power.backend='powerd'
        power.maintain(True); power.maintain(False)
        commands=[call.args[0] for call in runner.call_args_list]
        self.assertIn(['lipc-set-prop','-i','com.lab126.powerd','preventScreenSaver','0'],commands)
        self.assertTrue(power.sleep()); self.assertTrue(power.sleeping)
        self.assertTrue(power.wake()); self.assertFalse(power.sleeping)

class HTTP(Modules):
    def setUp(self):
        super().setUp()
        self.server=app.ThreadingHTTPServer(('127.0.0.1',0),self.a.handler())
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.prefix='/'+self.a.token
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); super().tearDown()
    def request(self,path,body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        connection.request('GET' if body is None else 'POST',self.prefix+path,body=body,headers=headers or {})
        response=connection.getresponse(); data=response.read(); result=(response.status,data,dict(response.getheaders())); connection.close(); return result
    def action(self,**values):
        payload=dict(csrf=self.a.csrf,revision=self.a.store.read()['revision'],**values)
        return self.request('/manage-action',urllib.parse.urlencode(payload),{'Content-Type':'application/x-www-form-urlencoded'})
    def test_manager_forms_validate_csrf_origin_and_revisions(self):
        response=self.action(action='module',panel_module='codex',module_interval=15); self.assertEqual(response[0],303)
        self.assertEqual(self.a.store.read()['settings']['panel_module'],'usage')
        payload=urllib.parse.urlencode(dict(csrf=self.a.csrf,revision=0,action='module',panel_module='weibo',module_interval=15))
        self.assertEqual(self.request('/manage-action',payload)[0],409)
        self.assertEqual(self.request('/manage-action',payload,{'Origin':'http://evil.invalid'})[0],403)
        self.assertEqual(self.request('/manage-action','csrf=no&action=module')[0],403)
        for path in ('/manage','/modules','/settings','/photos','/manager.js'):
            self.assertEqual(self.request(path)[0],200)

    def test_photo_upload_authorized_and_paths_bound(self):
        self.assertEqual(self.request('/photo-upload',picture())[0],403)
        code,body,_=self.request('/photo-upload',picture(),{'X-CSRF-Token':self.a.csrf,'X-Photo-Name':'%E5%9B%BE%E7%89%87.png'})
        self.assertEqual(code,201); identity=json.loads(body)['id']
        self.assertEqual(self.request('/photos/'+identity+'.png')[0],200)
        self.assertEqual(self.request('/photos/../../agenda.json')[0],404)
        self.assertEqual(self.action(action='photo_select',id=identity,target='panel')[0],303)
        self.assertEqual(self.a.store.read()['settings']['panel_photo'],identity)

    def test_codex_companion_push_end_to_end(self):
        import codex_usage_sync
        base='http://127.0.0.1:'+str(self.server.server_port)+self.prefix
        codex_usage_sync.push(base,rates(44))
        self.assertEqual(self.a.modules.cache['codex']['windows'][0]['remaining'],56)
        self.assertEqual(self.request('/codex-usage',json.dumps(rates()))[0],403)

if __name__=='__main__': unittest.main()
