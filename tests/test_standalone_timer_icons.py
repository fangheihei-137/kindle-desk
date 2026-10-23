import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'kindle-standalone'))
import app,renderer as ui
from store import atomic_json

class TimerIconTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        (self.root/'data').mkdir()
        atomic_json(self.root/'data/agenda.json',dict(token='timer-icons',revision=0,events=[],todos=[]))
        self.a=app.Application(self.root)
    def tearDown(self): self.temp.cleanup()

    def test_adjust_then_start_and_reset_never_autostarts(self):
        with patch.object(app.time,'monotonic',return_value=100):
            self.a.handle_action('pomodoro')
            self.assertEqual(self.a.pomodoro.state,'idle')
            self.a.handle_action(self.a.tap_action(430,610))
            self.assertEqual(self.a.pomodoro.duration,3000)
            self.a.handle_action(self.a.tap_action(170,610))
            self.assertEqual(self.a.pomodoro.duration,2700)
            self.a.handle_action(self.a.tap_action(300,750))
            self.assertEqual(self.a.pomodoro.view(),dict(state='running',remaining=2700,duration=2700))
            self.assertIsNone(self.a.tap_action(430,610))
            self.a.handle_action('pomodoro_more')
            self.assertEqual(self.a.pomodoro.duration,2700)
            self.a.handle_action(self.a.tap_action(480,750))
            self.assertEqual(self.a.pomodoro.state,'idle')
            self.assertEqual(app.Pomodoro(self.root/'data/pomodoro.json').state,'idle')

    def test_adjustment_limits_and_custom_duration_survive_restart(self):
        timer=self.a.pomodoro
        timer.adjust(-100); self.assertEqual(timer.duration,300)
        timer.adjust(100); self.assertEqual(timer.duration,10800)
        timer.adjust(-24); self.assertEqual(timer.duration,3600)
        self.assertEqual(app.Pomodoro(timer.path).view(),dict(state='idle',remaining=3600,duration=3600))
        with patch.object(app.time,'monotonic',return_value=100) as mono, patch.object(app.time,'time',return_value=10000) as wall:
            timer.start(); mono.return_value=1000; wall.return_value=10900
            resumed=app.Pomodoro(timer.path)
            self.assertEqual(resumed.view(),dict(state='running',remaining=2700,duration=3600))
            resumed.toggle(); resumed.adjust(1)
            self.assertEqual(resumed.duration,3600)
            self.assertEqual(app.Pomodoro(timer.path).state,'paused')

    def test_ring_erases_clockwise_from_noon_in_duration_proportion(self):
        points=[(427,223),(427,477),(173,477),(173,223)] # NE, SE, SW, NW
        for remaining,expected in ((3600,[0,0,0,0]),(2700,[255,0,0,0]),
                                   (1800,[255,255,0,0]),(900,[255,255,255,0]),(0,[255,255,255,255])):
            with self.subTest(remaining=remaining):
                frame=ui.render_pomodoro(dict(state='running' if remaining else 'done',remaining=remaining,duration=3600))
                self.assertEqual([frame.getpixel(point) for point in points],expected)
        setup=ui.render_pomodoro(dict(state='idle',remaining=3600,duration=3600))
        self.assertTrue(all(setup.getpixel(point)==255 for point in points))

    def test_icon_footers_retain_hit_targets_and_confirmation(self):
        for mode,buttons in (('agenda',('dashboard','editor','calendar')),
                             ('weather',('dashboard','refresh_weather',None)),
                             ('news_history',('dashboard',None,None))):
            self.a.handle_action(mode); self.a.frame(capture=True)
            for x,expected in zip((116,300,484),buttons):
                self.assertEqual(self.a.tap_action(x,752),expected)
        self.a.handle_action('pomodoro')
        self.assertEqual(self.a.tap_action(300,752),'pomodoro_toggle')
        self.assertIsNone(self.a.tap_action(484,752))
        self.a.handle_action('pomodoro_toggle')
        self.assertEqual(self.a.tap_action(484,752),'pomodoro_reset')

if __name__=='__main__': unittest.main(verbosity=2)
