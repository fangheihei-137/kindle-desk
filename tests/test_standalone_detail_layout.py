"""Account migration, touch routes, photo framing and passive Wi-Fi status."""
import io,json,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from PIL import Image,ImageOps,ImageChops
ROOT=Path(__file__).resolve().parents[1]/'kindle-standalone'
sys.path.insert(0,str(ROOT))
import app,modules,phone,renderer as ui
from store import atomic_json


class DetailLayout(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        (self.root/'data').mkdir()
        atomic_json(self.root/'data/agenda.json',dict(token='detail-test',revision=0,events=[],todos=[]))
        self.a=app.Application(self.root)
    def tearDown(self): self.temp.cleanup()
    def settings(self,**values):
        old=self.a.store.read()
        new=self.a.store.change(old['revision'],'settings',dict(settings=values))
        self.a.settings_changed(old,new)
        return new
    def photo(self,number=0):
        image=Image.new('L',(240,320),110+number*5)
        output=io.BytesIO(); image.save(output,'PNG')
        return self.a.modules.add_photo(output.getvalue(),'照片'+str(number)+'.png')

    def test_legacy_account_selection_migrates_on_read_without_rewriting(self):
        for legacy in ('codex','deepseek'):
            raw=dict(token='detail-test',revision=23,events=[],todos=[],settings=dict(panel_module=legacy))
            atomic_json(self.root/'data/agenda.json',raw)
            before=(self.root/'data/agenda.json').read_bytes()
            restored=app.Application(self.root)
            self.assertEqual(restored.store.read()['settings']['panel_module'],'usage')
            self.assertEqual(restored.panel_view()['kind'],'usage')
            self.assertEqual(before,(self.root/'data/agenda.json').read_bytes())

    def test_combined_accounts_keep_independent_timestamps_and_errors(self):
        self.settings(panel_module='usage')
        self.a.modules._save('deepseek',dict(checked_at=time.time()-10000,error='网络不可用',balances=[dict(currency='CNY',total='12.34',granted='0.00',topped_up='12.34')]))
        self.a.modules.import_codex(dict(rateLimits=dict(primary=dict(usedPercent=25,windowDurationMins=300))))
        restored=app.Application(self.root); view=restored.panel_view()
        self.assertTrue(view['accounts']['deepseek']['stale'])
        self.assertFalse(view['accounts']['codex']['stale'])
        self.assertEqual(view['accounts']['codex']['windows'][0]['remaining'],75)
        self.assertEqual(view['accounts']['deepseek']['balances'][0]['total'],'12.34')
        self.assertEqual(restored.frame().size,(600,800))
        self.assertEqual(modules.detail_image(view)[0].size,(600,800))
        self.assertNotIn('网络不可用',str(view['accounts']['codex']))
        self.a.module_wake.clear(); self.a.handle_action('panel_refresh'); self.assertTrue(self.a.module_wake.is_set())
        self.a.modules.save_key('sk-test')
        with patch.object(self.a.modules,'refresh',side_effect=lambda *args:self.a.stop.set()) as refresh:
            self.a.module_loop()
        refresh.assert_called_once_with('deepseek',self.a)

    def test_weibo_ten_headlines_open_the_correct_item(self):
        self.settings(panel_module='weibo')
        self.a.modules._save('weibo',dict(checked_at=time.time(),items=[dict(name='热搜'+str(i),time=time.time(),content='完整详情'+str(i),source='微博',rank=i) for i in range(1,11)]))
        for index in range(10):
            self.a.mode='dashboard'; self.a.frame(capture=True)
            action=self.a.tap_action(100 if index<5 else 390,397+(index%5)*27)
            self.a.handle_action(action)
            self.assertEqual(self.a.mode,'news')
            self.assertEqual(self.a.news_item['name'],'热搜'+str(index+1))
        self.a.mode='dashboard'; self.assertIsNone(self.a.tap_action(300,400))

    def test_wallpaper_crops_in_proportion_and_has_no_footer(self):
        for size in ((900,400),(400,900),(600,800)):
            source=Image.new('L',size,150)
            draw=ui.ImageDraw.Draw(source)
            draw.ellipse((size[0]//2-120,size[1]//2-120,size[0]//2+120,size[1]//2+120),fill=40)
            path=self.root/'wallpaper.png'; source.save(path)
            expected=ImageOps.fit(source,(600,800),method=Image.Resampling.LANCZOS,centering=(.5,.5))
            for system in (True,False):
                actual=ui.render_sleep_photo(path,system)
                self.assertIsNone(ImageChops.difference(actual,expected).getbbox())

    def test_native_tabs_module_return_and_cycle_settings(self):
        self.a.handle_action('settings')
        self.a.handle_action(self.a.tap_action(120,210)); self.assertEqual(self.a.mode,'module_picker')
        self.a.handle_action(self.a.tap_action(100,420)); self.assertEqual(self.a.mode,'settings')
        self.assertEqual(self.a.store.read()['settings']['panel_module'],'usage')
        while not self.a.requests.empty(): self.a.handle_action(self.a.requests.get_nowait())
        self.assertEqual(self.a.mode,'settings')
        self.a.handle_action(self.a.tap_action(300,138)); self.assertEqual(self.a.settings_page,'display')
        self.a.handle_action(self.a.tap_action(400,210)); self.assertEqual(self.a.store.read()['settings']['time_format'],'12')
        self.a.handle_action(self.a.tap_action(400,370)); self.assertEqual(self.a.mode,'photo_picker')
        self.assertEqual(self.a.store.read()['settings']['sleep_display'],'calendar')
        self.a.handle_action('settings'); self.a.handle_action(self.a.tap_action(480,138))
        self.assertEqual(self.a.settings_page,'network')
        self.a.handle_action(self.a.tap_action(400,370)); self.assertEqual(self.a.mode,'form')
        self.assertEqual(self.a.form_return,'settings')
        for tab in ui.SETTINGS_TABS:
            self.a.mode='settings'; self.a.settings_page=tab
            self.assertEqual(self.a.frame().size,(600,800))

    def test_photo_picker_pagination_and_deleted_photo_does_not_save(self):
        identities=[self.photo(i) for i in range(5)]
        self.a.handle_action(('photos','standby')); self.a.frame(capture=True)
        self.a.handle_action(self.a.tap_action(490,690)); self.a.frame(capture=True)
        self.assertEqual(self.a.photo_page,1)
        self.assertEqual(self.a.shown_photos[0]['id'],identities[4])
        self.a.modules.delete_photo(identities[4],self.a.store.read())
        self.a.handle_action(self.a.tap_action(100,220))
        self.assertEqual(self.a.mode,'photo_picker'); self.assertTrue(self.a.photo_message)
        self.assertEqual(self.a.store.read()['settings']['standby_photo'],'')
        self.a.frame(capture=True); self.assertEqual(self.a.photo_page,0)
        self.a.handle_action(self.a.tap_action(100,220))
        self.assertEqual(self.a.mode,'settings')
        self.assertEqual(self.a.store.read()['settings']['standby_photo'],identities[0])
        self.assertEqual(self.a.store.read()['settings']['sleep_display'],'photo')

    def test_passive_wifi_status_is_escaped_and_never_runs_in_standby(self):
        status=dict(connected=True,enabled=True,ssid='<家里&WiFi>',address='192.168.1.2')
        self.a.wifi=Mock(); self.a.wifi.status.return_value=status; self.a.mode='pair'
        self.a.refresh_wifi_status(); self.assertEqual(self.a.wifi_view()[0]['ssid'],status['ssid'])
        self.assertTrue(self.a.redraw.is_set())
        doc=phone.home(self.a,'/test').decode()
        self.assertIn('&lt;家里&amp;WiFi&gt;',doc); self.assertNotIn('<家里&WiFi>',doc)
        self.a.standby.set(); self.a.refresh_wifi_status(); self.a.wifi.status.assert_called_once()
        with patch.object(self.a,'connection',return_value='192.168.1.2'):
            self.assertEqual(self.a.pair_image().size,(600,800))

if __name__=='__main__': unittest.main()
