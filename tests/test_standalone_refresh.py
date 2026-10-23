"""Check real frame changes, refresh cadence, and successful-draw accounting."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.device = app.Device(None)

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
