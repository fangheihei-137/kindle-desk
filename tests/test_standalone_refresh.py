"""Check real frame changes, refresh cadence, and successful-draw accounting."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1] / 'kindle-standalone'
sys.path.insert(0, str(ROOT))
import app


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'frame.png'
        self.calls = []
        self.fail_next_draw = False
        def command(args, timeout=30):
            self.calls.append(args)
            if args[0] == 'eips' and self.fail_next_draw:
                self.fail_next_draw = False
                return 1, 'simulated failure'
            if args[:2] == ['initctl', 'status']:
                return 0, 'lab126_gui stop/waiting'
            return 0, '0'
        self.commands = patch.object(app, 'command', side_effect=command)
        self.sleep = patch.object(app.time, 'sleep')
        self.commands.start()
        self.sleep.start()
        owner = Mock()
        owner.mode = 'dashboard'
        owner.preferences.return_value = {'full_refresh': 10}
        self.device = app.Device(owner)

    def tearDown(self):
        self.commands.stop()
        self.sleep.stop()
        self.temp.cleanup()

    def draw(self, value):
        app.Image.new('L', (600, 800), value).save(self.path)
        return self.device.display(self.path)

    def test_full_refresh_every_ten_changes_and_skip_identical(self):
        for value in range(21):
            self.assertTrue(self.draw(value))
            for _ in range(3):
                self.assertFalse(self.draw(value))
        draws = [args for args in self.calls if args[0] == 'eips']
        self.assertEqual(len(draws), 21)
        self.assertEqual([i for i, args in enumerate(draws) if '-f' in args], [0, 10, 20])
        self.assertEqual(self.device.refresh_status(), dict(
            interval=10, changes_since_full=0, full_refreshes=3,
            frame_updates=21, last='full'))

    def test_failed_tenth_change_does_not_reset_count_or_skip_retry(self):
        for value in range(10):
            self.draw(value)
        before = self.device.refresh_status()
        self.assertEqual(before['changes_since_full'], 9)
        self.fail_next_draw = True
        with self.assertRaises(RuntimeError):
            self.draw(10)
        self.assertEqual(self.device.refresh_status(), before)
        self.assertTrue(self.draw(10))
        draws = [args for args in self.calls if args[0] == 'eips']
        self.assertIn('-f', draws[-2])
        self.assertIn('-f', draws[-1])
        self.assertEqual(self.device.refresh_status()['changes_since_full'], 0)
        self.assertEqual(self.device.refresh_status()['frame_updates'], 11)

    def test_reader_return_forces_full_even_for_same_frame(self):
        self.draw(255)
        self.draw(254)
        self.device.restore_gui()
        # Application normally stops the reader before preparing the next PNG.
        self.device.stop_gui()
        self.assertTrue(self.draw(254))
        self.assertIn('-f', self.calls[-1])
        self.assertEqual(self.device.refresh_status()['full_refreshes'], 2)
        self.assertEqual(self.device.refresh_status()['changes_since_full'], 0)

    def test_failed_first_frame_is_not_reported_as_displayed(self):
        self.fail_next_draw = True
        with self.assertRaises(RuntimeError):
            self.draw(255)
        self.assertEqual(self.device.refresh_status()['frame_updates'], 0)
        self.assertEqual(self.device.refresh_status()['full_refreshes'], 0)
        self.assertTrue(self.draw(255))
        self.assertIn('-f', self.calls[-1])

    def test_standby_clears_white_then_full_draws_wallpaper_only_once(self):
        self.draw(80)
        self.device.app.mode='screensaver'
        self.device.prepare_standby_refresh()
        self.calls.clear()
        self.assertTrue(self.draw(80))  # Identical to last frame still needs cleaning.
        draws=[args for args in self.calls if args[0]=='eips']
        self.assertEqual(len(draws),2)
        self.assertEqual(Path(draws[0][-1]).name,'kindle-standalone-clear.png')
        with app.Image.open(draws[0][-1]) as white:
            self.assertEqual(white.getextrema(),(255,255))
        self.assertEqual(draws[1],['eips','-f','-g',str(self.path)])
        self.assertFalse(self.device.standby_clear_pending)
        self.assertFalse(self.draw(80))
        self.assertTrue(self.draw(79))
        self.assertEqual(sum(Path(args[-1]).name=='kindle-standalone-clear.png' for args in self.calls if args[0]=='eips'),1)

    def test_failed_standby_clear_remains_due_and_does_not_claim_wallpaper(self):
        self.draw(80)
        before=self.device.refresh_status()
        self.device.app.mode='screensaver'; self.device.prepare_standby_refresh()
        self.fail_next_draw=True
        with self.assertRaisesRegex(RuntimeError,'Standby clear failed'): self.draw(79)
        self.assertTrue(self.device.standby_clear_pending)
        self.assertEqual(self.device.refresh_status(),before)
        self.assertTrue(self.draw(79))
        self.assertFalse(self.device.standby_clear_pending)

    def test_failed_wallpaper_after_clear_cannot_skip_retry_as_identical(self):
        self.draw(80)
        before=self.device.refresh_status()
        self.device.app.mode='screensaver'; self.device.prepare_standby_refresh()
        def failed_wallpaper(args,**kwargs):
            return (1,'wallpaper failed') if args[0]=='eips' and args[-1]==str(self.path) else (0,'')
        with patch.object(app,'command',side_effect=failed_wallpaper):
            with self.assertRaisesRegex(RuntimeError,'Screen draw failed'): self.draw(80)
        self.assertTrue(self.device.standby_clear_pending)
        self.assertIsNone(self.device.last_frame_digest)
        self.assertEqual(self.device.refresh_status(),before)
        self.assertTrue(self.draw(80))

    def test_wake_cancels_pending_clear(self):
        self.draw(80)
        self.device.prepare_standby_refresh(); self.device.cancel_standby_refresh()
        self.calls.clear(); self.draw(79)
        self.assertEqual([args for args in self.calls if args[0]=='eips'],[['eips','-f','-g',str(self.path)]])


class StandbyDrawingOrder(unittest.TestCase):
    def test_menu_sleep_precedes_clear_and_wallpaper(self):
        from store import atomic_json
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'data').mkdir()
            atomic_json(root/'data/agenda.json',dict(token='test',revision=0,events=[],todos=[]))
            a=app.Application(root); actions=[]
            a.device=Mock(); a.device.refresh_status.return_value={}
            a.device.stop_gui.side_effect=lambda:actions.append('gui_ready')
            a.device.prepare_standby_refresh.side_effect=lambda:actions.append('prepare_clear')
            a.device.display.side_effect=lambda path:actions.append('display')
            a.power=Mock(); a.power.backend='powerd'; a.power.diagnostics.return_value={}
            def sleep(): actions.append('system_sleep'); return True
            a.power.sleep.side_effect=sleep
            with patch.object(a,'update_battery'),patch.object(a,'record_power'),patch.object(a,'frame',return_value=app.Image.new('L',(600,800),255)),patch.object(a.requests,'get',side_effect=['standby','quit']),patch.object(app.Image.Image,'save'):
                a.display_loop()
            self.assertLess(actions.index('gui_ready'),actions.index('system_sleep'))
            self.assertLess(actions.index('system_sleep'),actions.index('display'))
            self.assertEqual(actions.count('display'),1)
            self.assertTrue(a.system_sleeping)
            a.power.sleep.assert_called_once()

    def test_system_sleep_never_toggles_power_again(self):
        from store import atomic_json
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'data').mkdir()
            atomic_json(root/'data/agenda.json',dict(token='test',revision=0,events=[],todos=[]))
            a=app.Application(root); a.power=Mock(); a.power.backend='powerd'
            a.enter_standby(system=True); a.request_pending_suspend()
            a.power.sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
