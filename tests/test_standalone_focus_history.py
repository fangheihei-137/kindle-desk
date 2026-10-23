import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'kindle-standalone'))
import app, renderer as ui
from store import atomic_json


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name); (self.root/'data').mkdir()
        atomic_json(self.root/'data/agenda.json',dict(token='new-feature-test',revision=0,events=[],todos=[]))
        self.a=app.Application(self.root)
        self.current=ui.now()

    def tearDown(self): self.temp.cleanup()

    def news(self,index,age=0):
        return dict(name='测试资讯 '+str(index),content='完整内容 '+str(index),
                    url='https://example.invalid/'+str(index),source='测试',
                    time=int((self.current-timedelta(hours=age)).timestamp()))

    def test_history_merges_deduplicates_expires_and_bounds(self):
        old=[self.news(1,47),self.news(2,49)]
        fresh=[dict(self.news(1,47),content='更新的正文'),self.news(3,1)]
        history=ui.merge_news_history(old,fresh,self.current)
        self.assertEqual([x['name'] for x in history],['测试资讯 3','测试资讯 1'])
        self.assertEqual(history[-1]['content'],'更新的正文')
        for batch in range(10):
            items=[self.news(100+batch*50+i,5-batch/10) for i in range(50)]
            history=ui.merge_news_history(history,items,self.current)
        self.assertEqual(len(history),300)
        self.assertEqual(len({x['url'] for x in history}),300)
        self.assertTrue(all(x['time']>=(self.current-timedelta(hours=48)).timestamp() for x in history))

    def test_unknown_timestamp_expires_from_first_seen(self):
        item=dict(name='没有日期的资讯',url='https://example.invalid/no-date')
        first=ui.merge_news_history([], [item],self.current)
        later=ui.merge_news_history(first,[item],self.current+timedelta(hours=47))
        self.assertEqual(later[0]['seen_at'],first[0]['seen_at'])
        self.assertEqual(ui.merge_news_history(later,current=self.current+timedelta(hours=49)),[])

    def test_offline_cache_cleanup_and_read_only_preflight(self):
        payload=dict(news=[self.news(1,49),self.news(2,1)],news_at=self.current.isoformat())
        path=self.root/'data/news-cache.json'; atomic_json(path,payload)
        original=path.read_bytes()
        application=app.Application(self.root)
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(len(application.state['news']),1)
        application.prune_news_cache()
        self.assertEqual(len(json.loads(path.read_text(encoding='utf-8'))['news']),1)
        with patch.object(ui,'now',return_value=self.current+timedelta(hours=49)):
            application.prune_news_cache()
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['news'],[])

    def test_fetch_preserves_previous_history_and_newer_content(self):
        self.a.state['news']=[self.news(1,2)]
        with patch.object(self.a,'fetch_news',return_value=[self.news(2,0)]):
            self.a.update_news()
        self.assertEqual(len(self.a.state['news']),2)
        self.assertEqual(len(app.Application(self.root).state['news']),2)

    def test_history_tap_pagination_detail_and_return(self):
        self.a.state['news']=[self.news(i,i/10) for i in range(14)]
        self.a.frame(capture=True)
        self.assertEqual(self.a.tap_action(130,360),'news_history')
        self.assertEqual(self.a.tap_action(480,360),'refresh_news')
        self.a.handle_action('news_history'); self.a.frame(capture=True)
        self.assertFalse(self.a.news_wake.is_set())
        self.assertEqual(self.a.news_list_total,3)
        self.a.handle_action(self.a.tap_action(490,690)); self.a.frame(capture=True)
        chosen=self.a.shown_news[0].copy()
        self.a.state['news']=[self.news(99)]
        self.a.handle_action(self.a.tap_action(250,150))
        self.assertEqual(self.a.news_item,chosen)
        self.assertEqual(self.a.news_return,'news_history')
        self.a.handle_action(self.a.tap_action(90,747))
        self.assertEqual(self.a.mode,'news_history')
        self.a.frame(capture=True)
        self.assertEqual(self.a.news_list_page,0)

    def test_date_opens_current_month_and_clock_opens_timer_setup(self):
        self.a.calendar_year,self.a.calendar_month=2020,1
        self.a.handle_action(self.a.tap_action(120,90))
        self.assertEqual(self.a.mode,'calendar')
        self.assertEqual((self.a.calendar_year,self.a.calendar_month),(ui.now().year,ui.now().month))
        self.a.handle_action('dashboard')
        self.a.handle_action(self.a.tap_action(480,90))
        self.assertEqual(self.a.mode,'pomodoro')
        self.assertEqual(self.a.pomodoro.state,'idle')
        self.assertEqual(self.a.pomodoro.view()['remaining'],2700)
        self.assertFalse((self.root/'data/pomodoro.json').exists())
        self.assertEqual(self.a.frame().size,(600,800))

    def test_timer_pause_resume_background_finish_and_restart(self):
        with patch.object(app.time,'monotonic',return_value=100) as mono, patch.object(app.time,'time',return_value=10000) as wall:
            timer=app.Pomodoro(self.root/'data/pomodoro.json'); timer.start()
            mono.return_value=160; wall.return_value=10060
            timer.toggle(); self.assertEqual(timer.view(),dict(state='paused',remaining=2640,duration=2700))
            mono.return_value=1000; wall.return_value=10900
            self.assertEqual(app.Pomodoro(timer.path).view(),dict(state='paused',remaining=2640,duration=2700))
            timer.toggle()
            mono.return_value=1060; wall.return_value=10960
            self.a.pomodoro=timer; self.a.handle_action('dashboard')
            self.a.handle_action('pomodoro')
            self.assertEqual(timer.view()['remaining'],2580)
            resumed=app.Pomodoro(timer.path)
            self.assertEqual(resumed.view()['remaining'],2580)
            mono.return_value=3640; wall.return_value=13540
            self.assertTrue(timer.tick()); self.assertFalse(timer.tick())
            self.assertEqual(timer.view(),dict(state='done',remaining=0,duration=2700))
            self.assertEqual(app.Pomodoro(timer.path).state,'done')
            self.a.handle_action('pomodoro_reset')
            self.assertEqual(timer.view()['remaining'],2700)
            self.assertEqual(timer.state,'idle')

    def test_timer_uses_monotonic_time_and_handles_bad_cache(self):
        path=self.root/'data/pomodoro.json'
        atomic_json(path,dict(state='running',until='broken'))
        timer=app.Pomodoro(path); self.assertEqual(timer.state,'idle')
        with patch.object(app.time,'monotonic',return_value=10) as mono, patch.object(app.time,'time',return_value=1000) as wall:
            timer.start()
            mono.return_value=70; wall.return_value=999999
            self.assertEqual(timer.view()['remaining'],2640)

    def test_charging_changes_redraw_and_failed_probe_clears_state(self):
        values={'battLevel':(0,'76'),'isCharging':(0,'1')}
        with patch.object(app,'command',side_effect=lambda args,**kwargs:values[args[-1]]):
            self.a.update_battery()
            self.assertTrue(self.a.state['charging']); self.assertTrue(self.a.redraw.is_set())
            charging=ui.render(self.a.store.read(),self.a.state,current=ui.now()).crop((390,20,580,50)).tobytes()
            self.a.redraw.clear(); self.a.update_battery(); self.assertFalse(self.a.redraw.is_set())
            values['isCharging']=(0,'0'); self.a.update_battery()
            self.assertFalse(self.a.state['charging']); self.assertTrue(self.a.redraw.is_set())
            plain=ui.render(self.a.store.read(),self.a.state,current=ui.now()).crop((390,20,580,50)).tobytes()
            self.assertTrue(charging!=plain,'Charging indicator must visibly change')
            values['isCharging']=(1,'unknown'); self.a.update_battery()
            self.assertIsNone(self.a.state['charging'])


if __name__=='__main__': unittest.main(verbosity=2)
