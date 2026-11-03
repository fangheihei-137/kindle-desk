"""Standby calendar, local battery refresh and foreground focus regression tests."""
import os,queue,shutil,subprocess,sys,tempfile,unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch,Mock
ROOT=Path(__file__).resolve().parents[1]/'kindle-standalone'
sys.path.insert(0,str(ROOT))
import app,renderer as ui
from store import atomic_json

class StandbyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        (self.root/'data').mkdir()
        atomic_json(self.root/'data/agenda.json',dict(token='c'*48,revision=0,events=[],todos=[]))
        self.a=app.Application(self.root)
        self.moment=datetime(2026,10,26,12,0,tzinfo=ui.TZ)
    def tearDown(self): self.temp.cleanup()
    def power(self,battery=76,charging=False):
        return dict(battery=battery,battery_at=self.moment.isoformat(),charging=charging,charging_at=self.moment.isoformat())

    def test_static_calendar_does_not_change_with_minutes_or_sample_timestamps(self):
        config=self.a.store.read(); state=self.power()
        first=ui.render_screensaver(config,state,self.moment)
        later=self.moment+timedelta(hours=3,minutes=27)
        state.update(battery_at=later.isoformat(),charging_at=later.isoformat())
        self.assertEqual(first.tobytes(),ui.render_screensaver(config,state,later).tobytes())

    def test_battery_and_charging_changes_only_affect_header(self):
        config=self.a.store.read()
        first=ui.render_screensaver(config,self.power(),self.moment)
        for state in (self.power(75),self.power(76,True),self.power(None)):
            other=ui.render_screensaver(config,state,self.moment)
            self.assertNotEqual(first.crop((350,0,600,110)).tobytes(),other.crop((350,0,600,110)).tobytes())
            self.assertEqual(first.crop((0,116,600,800)).tobytes(),other.crop((0,116,600,800)).tobytes())

    def test_stale_reading_not_presented_as_live_percentage(self):
        config=self.a.store.read(); state=self.power()
        state['battery_at']=(self.moment-timedelta(minutes=3)).isoformat()
        self.assertEqual(ui.render_screensaver(config,state,self.moment).tobytes(),ui.render_screensaver(config,self.power(None),self.moment).tobytes())

    def test_new_day_and_new_agenda_change_calendar_without_waking(self):
        self.a.enter_standby()
        with patch.object(app,'now',return_value=self.moment): first=self.a.frame()
        with patch.object(app,'now',return_value=self.moment+timedelta(days=1)): next_day=self.a.frame()
        self.assertNotEqual(first.tobytes(),next_day.tobytes())
        self.a.store.change(0,'save',dict(date='2026-10-26',time='15:00',title='讨论计划',note=''))
        with patch.object(app,'now',return_value=self.moment): changed=self.a.frame()
        self.assertNotEqual(first.tobytes(),changed.tobytes())
        self.assertEqual(self.a.mode,'screensaver'); self.assertTrue(self.a.standby.is_set())
        self.assertEqual(self.a.display_stamp(self.moment),self.a.display_stamp(self.moment+timedelta(hours=5)))
        self.assertNotEqual(self.a.display_stamp(self.moment),self.a.display_stamp(self.moment+timedelta(days=1)))

    def test_battery_poll_wakes_render_only_on_change_and_never_leaves_standby(self):
        self.a.enter_standby(); self.a.redraw.clear()
        values={'battLevel':(0,'76'),'isCharging':(0,'0')}
        with patch.object(app,'command',side_effect=lambda args,**kwargs:values[args[-1]]):
            self.a.update_battery(); self.assertEqual(self.a.requests.get_nowait(),'standby_refresh')
            self.a.redraw.clear(); self.a.update_battery()
            self.assertFalse(self.a.redraw.is_set()); self.assertTrue(self.a.requests.empty())
            values['battLevel']=(1,'unavailable'); self.a.update_battery()
            self.assertIsNone(self.a.state['battery'])
            self.a.handle_action(self.a.requests.get_nowait())
            self.assertEqual(self.a.mode,'screensaver'); self.assertTrue(self.a.standby.is_set())

    def test_standby_network_worker_reads_local_power_without_network_polling(self):
        self.a.enter_standby(); self.a.device=Mock()
        with patch.object(self.a,'update_battery') as battery, patch.object(self.a,'connection') as connection:
            self.a.network_wake.wait=Mock(side_effect=lambda seconds:self.a.stop.set())
            self.a.network_loop()
            battery.assert_called_once(); connection.assert_not_called()
            self.a.network_wake.wait.assert_called_once_with(30)

    def test_standby_feed_workers_block_without_periodic_wake_or_fetch(self):
        for worker,event,update in (('weather_loop','weather_wake','update_weather'),('news_loop','news_wake','update_news')):
            self.a.stop.clear(); self.a.enter_standby()
            wake=getattr(self.a,event); wake.wait=Mock(side_effect=lambda:self.a.stop.set())
            with patch.object(self.a,update) as fetch:
                getattr(self.a,worker)(); fetch.assert_not_called(); wake.wait.assert_called_once_with()

    def test_running_timer_stays_foreground_and_prevents_all_standby(self):
        with patch.object(app.time,'monotonic',return_value=100) as clock:
            self.a.pomodoro.start(); self.a.last_activity=0
            clock.return_value=1100
            self.a.maybe_standby(); self.assertFalse(self.a.standby.is_set())
            for action in ('standby','power_toggle','dashboard','pair'):
                self.a.handle_action(action)
                self.assertEqual(self.a.mode,'pomodoro'); self.assertFalse(self.a.standby.is_set())
            self.a.handle_action('pomodoro_toggle')
            self.a.handle_action('dashboard'); self.assertEqual(self.a.mode,'dashboard')
            self.a.enter_standby(); self.assertTrue(self.a.standby.is_set())

    def test_restored_running_timer_opens_in_foreground(self):
        self.a.pomodoro.start()
        restored=app.Application(self.root)
        self.assertEqual(restored.mode,'pomodoro')

    def test_boot_self_check_preserves_an_active_timer(self):
        self.a.pomodoro.start()
        before=(self.root/'data/pomodoro.json').read_bytes()
        for name in tuple(app.updater.CORE)+('updater.py',):
            shutil.copy2(ROOT/name,self.root/name)
        shutil.copytree(ROOT/'assets/ime',self.root/'assets/ime')
        shutil.copy2(ROOT/'assets/cacert.pem',self.root/'assets/cacert.pem')
        shutil.copytree(ROOT/'vendor',self.root/'vendor',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        result=subprocess.run([sys.executable,'-B',str(self.root/'app.py'),'--self-test'],cwd=self.root,
                              env=dict(os.environ,KINDLE_PREFLIGHT_ASSETS=str(ROOT/'assets')),
                              capture_output=True,timeout=55)
        self.assertEqual(result.returncode,0,result.stdout.decode(errors='replace')+result.stderr.decode(errors='replace'))
        self.assertEqual((self.root/'data/pomodoro.json').read_bytes(),before)

    def test_shutdown_wakes_blocked_standby_workers_and_display(self):
        self.a.enter_standby()
        for event in (self.a.weather_wake,self.a.news_wake,self.a.network_wake): event.clear()
        self.a.request_stop()
        self.assertTrue(self.a.stop.is_set()); self.assertEqual(self.a.requests.get_nowait(),'quit')
        self.assertTrue(all(event.is_set() for event in (self.a.weather_wake,self.a.news_wake,self.a.network_wake)))

    def test_completion_resets_idle_countdown_before_auto_standby(self):
        with patch.object(app.time,'monotonic',return_value=100) as clock:
            self.a.pomodoro.start(); self.a.mode='pomodoro'; self.a.last_activity=0
            clock.return_value=3000
            def wait(**kwargs): self.a.stop.set(); raise queue.Empty
            self.a.requests.get=wait
            self.a.display_loop()
            self.assertEqual(self.a.pomodoro.state,'done')
            self.assertEqual(self.a.mode,'pomodoro'); self.assertFalse(self.a.standby.is_set())
            self.assertEqual(self.a.last_activity,3000)
            clock.return_value=3901; self.a.maybe_standby(); self.assertTrue(self.a.standby.is_set())

    def test_dense_six_week_month_and_empty_leap_february_render(self):
        config=self.a.store.read()
        config['events']=[dict(id=str(i),date='2026-11-30',time='09:00',title='较长的日程标题需要自动截断'+str(i),note='') for i in range(5)]
        for current in (datetime(2026,11,30,tzinfo=ui.TZ),datetime(2028,2,29,tzinfo=ui.TZ)):
            image=ui.render_screensaver(config,self.power(),current)
            self.assertEqual(image.size,(600,800)); self.assertEqual(image.mode,'L')

if __name__=='__main__': unittest.main()
