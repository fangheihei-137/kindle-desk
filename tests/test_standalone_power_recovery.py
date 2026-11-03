"""Regressions for missed/buffered power events and unsupported wake properties."""
import os,sys,tempfile,threading,time,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1]/'kindle-standalone'
sys.path.insert(0,str(ROOT))
import app,device_control
from store import atomic_json

class PowerRecovery(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        (self.root/'data').mkdir()
        atomic_json(self.root/'data/agenda.json',dict(token='power-test',revision=0,events=[],todos=[]))
        self.a=app.Application(self.root)
        self.state='active'; self.commands=[]; self.reject=set()
        def run(args,timeout=3):
            self.commands.append(args)
            if args[0]=='lipc-get-prop': return (0,self.state) if self.state else (1,'unavailable')
            name=args[-2]
            if name in self.reject: return 1,'unsupported'
            if name=='powerButton': self.state='active' if self.state!='active' else 'screenSaver'
            return 0,''
        self.p=device_control.KindlePower(self.a.handle_action,self.a.stop,runner=run)
        self.p.backend='powerd'; self.a.power=self.p
    def tearDown(self): self.temp.cleanup()
    def writes(self,name): return [x for x in self.commands if x[0]=='lipc-set-prop' and x[-2]==name]

    def test_missing_sleep_and_wake_events_recover_from_actual_state(self):
        self.state='screenSaver'; self.p.reconcile()
        self.assertEqual(self.a.mode,'screensaver'); self.assertTrue(self.a.system_sleeping)
        self.p.last_transition=0; self.state='active'; self.p.reconcile()
        self.assertEqual(self.a.mode,'dashboard'); self.assertFalse(self.a.standby.is_set())
        self.assertEqual(self.writes('powerButton'),[])

    def test_already_awake_touch_does_not_toggle_back_to_sleep(self):
        self.a.enter_standby(system=True); self.p.sleeping=True; self.reject.add('wakeUp')
        self.a.handle_action('power_toggle')
        self.assertEqual(self.a.mode,'dashboard'); self.assertFalse(self.p.sleeping)
        self.assertEqual(self.writes('powerButton'),[]); self.assertEqual(self.writes('wakeUp'),[])

    def test_known_sleep_uses_state_aware_button_even_without_wakeup_property(self):
        self.a.enter_standby(system=True); self.p.sleeping=True
        self.state='screenSaver'; self.reject.add('wakeUp')
        self.a.handle_action('power_toggle')
        self.assertEqual(self.state,'active'); self.assertEqual(self.a.mode,'dashboard')
        self.assertEqual(len(self.writes('powerButton')),1)
        self.assertEqual(self.writes('wakeUp'),[])

    def test_unreadable_state_never_blindly_toggles_and_does_not_trap_ui(self):
        self.a.enter_standby(system=True); self.p.sleeping=True
        self.state=''; self.reject.add('wakeUp')
        self.a.handle_action('power_toggle')
        self.assertEqual(self.a.mode,'dashboard'); self.assertFalse(self.a.system_sleeping)
        self.assertFalse(self.a.standby.is_set()); self.assertEqual(self.writes('powerButton'),[])

    def test_sleep_does_not_toggle_an_already_sleeping_os(self):
        self.state='screenSaver'; self.assertTrue(self.p.sleep())
        self.assertTrue(self.p.sleeping); self.assertEqual(self.writes('powerButton'),[])

    def test_background_resume_and_unknown_states_do_not_dismiss_calendar(self):
        self.state='screenSaver'; self.p.reconcile(); self.p.last_transition=0
        self.p.feed('resuming 0'); self.p.reconcile()
        self.assertEqual(self.a.mode,'screensaver')
        self.state='resuming'; self.p.reconcile(); self.assertEqual(self.a.mode,'screensaver')
        self.state=''; self.p.reconcile(); self.assertEqual(self.a.mode,'screensaver')

    def test_exiting_event_recovers_once_without_duplicate_redraw(self):
        self.p.feed('goingToScreenSaver 2')
        self.p.feed('exitingScreenSaver 1'); self.assertEqual(self.a.mode,'dashboard')
        self.a.redraw.clear(); self.p.feed('outOfScreenSaver 1')
        self.assertFalse(self.a.redraw.is_set())

    def test_transient_stale_active_state_after_sleep_event_is_ignored(self):
        self.p.feed('goingToScreenSaver 2'); self.p.reconcile()
        self.assertEqual(self.a.mode,'screensaver')

    def test_protected_timer_recovers_when_events_are_missing(self):
        self.a.pomodoro.start(); self.state='screenSaver'; self.p.reconcile()
        self.assertEqual(self.a.mode,'pomodoro'); self.assertFalse(self.a.standby.is_set())
        self.assertEqual(self.state,'active'); self.assertTrue(self.p.protected)

    def test_listener_uses_wildcard_and_terminal_output_not_buffered_pipe(self):
        fakepty=types.SimpleNamespace(openpty=lambda:(111,112))
        with patch.dict(sys.modules,{'pty':fakepty}), patch.object(device_control.os,'close') as close, patch.object(device_control.subprocess,'Popen') as launch:
            process=self.p._launch()
            args=launch.call_args
            self.assertEqual(args.args[0],['lipc-wait-event','-m','com.lab126.powerd','*'])
            self.assertEqual(args.kwargs['stdout'],112)
            self.assertEqual(self.p.read_fd,111); self.assertEqual(self.p.transport,'pty')
            close.assert_called_once_with(112)
            self.p.process=process; self.p._terminate()
            self.assertIn(((111,),{}),close.call_args_list)

    def test_failed_launch_releases_both_terminal_descriptors(self):
        fakepty=types.SimpleNamespace(openpty=lambda:(111,112))
        with patch.dict(sys.modules,{'pty':fakepty}), patch.object(device_control.os,'close') as close, patch.object(device_control.subprocess,'Popen',side_effect=OSError):
            with self.assertRaises(OSError): self.p._launch()
            self.assertEqual({c.args[0] for c in close.call_args_list},{111,112})

class FrameworkRecovery(unittest.TestCase):
    def test_bridge_owns_only_missing_framework_name_and_restores_blanket(self):
        started=[False]; commands=[]
        def runner(args,timeout=2):
            commands.append(args)
            if args[-1]=='frameworkStarted': return (0,'1') if started[0] else (3,'missing')
            if args[-1]=='load': return 0,'splash screensaver ad_screensaver'
            return 0,''
        process=Mock(); process.poll.return_value=None
        def launch(*args,**kw): started[0]=True; return process
        bridge=device_control.FrameworkBridge(runner=runner)
        with patch.object(device_control.shutil,'which',return_value='/usr/bin/lua'), patch.object(device_control.subprocess,'Popen',side_effect=launch) as popen, patch.object(device_control.time,'sleep'):
            self.assertTrue(bridge.ensure()); self.assertTrue(bridge.ensure())
            popen.assert_called_once()
            self.assertEqual(bridge.unloaded,['splash','screensaver'])
            bridge.close(); process.terminate.assert_called_once()
        self.assertIn(['lipc-set-prop','com.lab126.blanket','load','screensaver'],commands)
        self.assertNotIn(['lipc-set-prop','com.lab126.blanket','unload','ad_screensaver'],commands)

    def test_existing_framework_service_is_never_replaced_or_terminated(self):
        bridge=device_control.FrameworkBridge(runner=Mock(return_value=(0,'1')))
        with patch.object(device_control.subprocess,'Popen') as launch:
            self.assertTrue(bridge.ensure()); bridge.close(); launch.assert_not_called()

    def test_failed_lua_start_does_not_claim_power_recovered(self):
        bridge=device_control.FrameworkBridge(runner=Mock(return_value=(3,'missing')))
        process=Mock(); process.poll.return_value=1; process.communicate.return_value=(b'',b'no liblipclua')
        with patch.object(device_control.shutil,'which',return_value='/usr/bin/lua'), patch.object(device_control.subprocess,'Popen',return_value=process), patch.object(device_control.time,'sleep'):
            self.assertFalse(bridge.ensure()); self.assertIsNone(bridge.process)

    def test_restore_gui_releases_service_before_starting_real_framework(self):
        device=app.Device.__new__(app.Device); device.gui_stopped=True
        history=[]; device.framework_bridge=Mock()
        device.framework_bridge.close.side_effect=lambda:history.append('release')
        with patch.object(app,'command',side_effect=lambda args:history.append('start')):
            device.restore_gui()
        self.assertEqual(history,['release','start']); self.assertFalse(device.gui_stopped)

if __name__=='__main__': unittest.main()
