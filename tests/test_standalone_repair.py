import copy,json,sys,tempfile,unittest
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'kindle-standalone'))
import app,renderer,sync_standalone
from store import atomic_json

class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name); (self.root/'data').mkdir()
        self.event=dict(id='test-event',date=(renderer.now().date()+timedelta(days=1)).isoformat(),time='09:00',title='测试日程',note='')
        atomic_json(self.root/'data/agenda.json',dict(token='f'*32,revision=0,city='',events=[self.event]))
    def tearDown(self): self.temp.cleanup()
    def test_actual_crashing_cache(self):
        cache=dict(news=[dict(name='示例标题\n第二行 '+str(i),time=int(renderer.now().timestamp())) for i in range(6)],news_at=renderer.now().isoformat())
        self.assertTrue(any('\n' in v['name'] for v in cache['news']))
        atomic_json(self.root/'data/news-cache.json',cache)
        application=app.Application(self.root)
        frame=application.frame(capture=True)
        self.assertEqual((frame.size,frame.mode),((600,800),'L'))
        self.assertEqual(len(application.shown_news),6)
    def test_long_multiline_headline_is_bounded(self):
        draw=renderer.ImageDraw.Draw(renderer.Image.new('L',(600,800)))
        with patch.object(draw,'textlength',wraps=draw.textlength) as measure:
            value=renderer.fit_text(draw,'人工智能\r\n金融资讯\t'*1000,16,445)
            self.assertLess(measure.call_count,25)
        self.assertNotIn('\n',value); self.assertNotIn('\r',value)
        self.assertLessEqual(draw.textlength(value,font=renderer.font(16)),445)
    def test_details_preserve_paragraphs_and_page_width(self):
        pages=renderer.paginate('第一段\r\n第二段\u2028第三段\n'+('很长的资讯内容'*150),size=24,per_page=16)
        self.assertGreater(len(pages),1)
        draw=renderer.ImageDraw.Draw(renderer.Image.new('L',(1,1)))
        for page in pages:
            self.assertLessEqual(len(page),16)
            for line in page:
                self.assertFalse(any(c in line for c in '\r\n\u2028'))
                self.assertLessEqual(draw.textlength(line,font=renderer.font(24)),540)
        self.assertEqual(pages[0][:3],['第一段','第二段','第三段'])
    def test_invalid_optional_cache_keeps_agenda_available(self):
        atomic_json(self.root/'data/news-cache.json',dict(news=[None,12,{},dict(name='有效资讯',time=10**100)],news_at='broken date'))
        atomic_json(self.root/'data/weather-cache.json',dict(weather=['invalid weather']))
        application=app.Application(self.root)
        with self.assertLogs('kindle',level='ERROR'):
            frame=application.frame(capture=True)
        self.assertEqual(frame.size,(600,800))
        self.assertEqual(application.shown_events[0]['id'],'test-event')
    def test_long_event_cannot_cover_buttons(self):
        short=renderer.render_event_detail(self.event,True)
        long=renderer.render_event_detail(dict(self.event,title='测\n'*50,note='长\n'*100),True)
        self.assertEqual(short.crop((0,570,600,800)).tobytes(),long.crop((0,570,600,800)).tobytes())
    def test_touch_holds_do_not_double_submit(self):
        tap=app.TouchTap()
        seq=[(1,330,1),(3,0,450),(3,1,750),(0,0,0)]
        seq+= [(3,0,451),(0,0,0)]*50
        seq+= [(1,330,0),(0,0,0),(0,0,0)]
        points=[p for p in (tap.feed(*event) for event in seq) if p is not None]
        self.assertEqual(points,[(451,750)])
    def test_type_b_release_without_coordinates(self):
        tap=app.TouchTap()
        seq=[(3,47,0),(3,57,19),(3,53,300),(3,54,750),(0,0,0),(3,57,-1),(0,0,0)]
        self.assertEqual([p for p in (tap.feed(*e) for e in seq) if p],[(300,750)])
    def test_type_a_release_and_drag(self):
        tap=app.TouchTap()
        seq=[(3,53,300),(3,54,750),(0,2,0),(0,0,0),(0,2,0),(0,0,0)]
        self.assertEqual([p for p in (tap.feed(*e) for e in seq) if p],[(300,750)])
        tap=app.TouchTap()
        seq=[(1,330,1),(3,0,10),(3,1,10),(0,0,0),(3,0,450),(3,1,750),(0,0,0),(1,330,0),(0,0,0)]
        self.assertFalse(any(tap.feed(*e) for e in seq))
    def run_delete_sequence(self,concurrent=False):
        application=app.Application(self.root)
        application.frame(capture=True)
        actions=iter([('event',0),'event_delete','event_delete','quit'])
        count=0
        def get(timeout):
            nonlocal count
            count+=1
            if concurrent and count==3:
                application.store.change(0,'save',dict(self.event,title='手机刚修改的内容'))
            return next(actions)
        with patch.object(application.requests,'get',side_effect=get): application.display_loop()
        return application
    def test_delete_rejects_concurrent_phone_edit(self):
        with self.assertLogs('kindle',level='WARNING'):
            application=self.run_delete_sequence(True)
        self.assertEqual(application.store.read()['events'][0]['title'],'手机刚修改的内容')
        self.assertIn('未删除',application.event_message)
    def test_confirmed_delete_uses_selected_event(self):
        application=self.run_delete_sequence()
        self.assertEqual(application.store.read()['events'],[])
    def test_news_click_uses_visible_snapshot(self):
        application=app.Application(self.root)
        application.state['news']=[dict(name='屏幕上的标题',content='屏幕上的完整内容',source='华尔街见闻')]
        application.frame(capture=True)
        application.state['news']=[dict(name='后台刚刷新的另一条')]
        actions=iter([('news',0),'quit'])
        with patch.object(application.requests,'get',side_effect=lambda **kw:next(actions)):
            application.display_loop()
        self.assertEqual(application.news_item['name'],'屏幕上的标题')
        self.assertIn('完整内容',''.join(application.news_pages[0]))
    def test_updater_preserves_device_data(self):
        kindle=self.root/'kindle'; base=kindle/'kindle-standalone'
        base.mkdir(parents=True); (kindle/'documents').mkdir(); (base/'data').mkdir()
        for folder in ('assets','vendor'): (base/folder).mkdir()
        for name in sync_standalone.PROGRAM: (base/name).write_text('previous '+name,encoding='utf-8')
        agenda=(self.root/'data/agenda.json').read_bytes()
        (base/'data/agenda.json').write_bytes(agenda)
        with patch.object(sync_standalone,'find_kindle',return_value=kindle): sync_standalone.sync()
        self.assertEqual((base/'data/agenda.json').read_bytes(),agenda)
        self.assertEqual((base/'renderer.py').read_bytes(),(sync_standalone.ROOT/'renderer.py').read_bytes())
        self.assertEqual(len(list((base/'backups').glob('*/agenda.json'))),1)

if __name__=='__main__': unittest.main(verbosity=2)
