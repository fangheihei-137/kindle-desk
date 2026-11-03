"""Power-saving regressions: native radio control, transitions and evidence."""
import errno,json,queue,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]/'kindle-standalone'
sys.path.insert(0,str(ROOT))
import app,device_control
from store import atomic_json

class RadioControl(unittest.TestCase):
    def setUp(self):
        self.radio={'com.lab126.cmd':1,'com.lab126.wifid':1}
        self.calls=[]; self.fail=None
        def runner(args,timeout=3):
            self.calls.append(args)
            if args[0]=='lipc-get-prop':
                return (0,str(self.radio[args[-2]])) if self.radio else (1,'unsupported')
            if args[0]=='lipc-set-prop':
                if (args[-3],args[-1])==self.fail: return 1,'failed'
                self.radio[args[-3]]=int(args[-1]); return 0,''
            if args[0]=='wpa_cli':
                return (0,'wpa_state=COMPLETED\nssid=test\nip_address=192.0.2.1') if args[-1]=='status' else (0,'OK')
            return 0,''
        self.wifi=device_control.WiFiManager(runner=runner)
        self.wifi._read_flags=Mock(return_value=True)
    def writes(self): return [x for x in self.calls if x[0]=='lipc-set-prop']

    def test_off_disables_firmware_radio_and_never_calls_interface_tools(self):
        status=self.wifi.set_enabled(False)
        self.assertFalse(status['enabled']); self.assertFalse(status['connected'])
        self.assertEqual([c[-3:] for c in self.writes()],
                         [['com.lab126.wifid','enable','0'],['com.lab126.cmd','wirelessEnable','0']])
        self.assertFalse(any(c[0] in ('wpa_cli','ifconfig','ip') for c in self.calls))

    def test_wake_restores_wireless_before_wifi(self):
        self.radio={key:0 for key in self.radio}
        status=self.wifi.set_enabled(True)
        self.assertTrue(status['enabled']); self.assertTrue(status['connected'])
        self.assertEqual([c[-3:] for c in self.writes()],
                         [['com.lab126.cmd','wirelessEnable','1'],['com.lab126.wifid','enable','1']])

    def test_partial_failure_restores_original_radio_instead_of_fake_success(self):
        self.fail=('com.lab126.cmd','0')
        with self.assertRaises(device_control.DeviceControlError): self.wifi.set_enabled(False)
        self.assertEqual(set(self.radio.values()),{1})
        self.assertFalse(any(c[0] in ('ifconfig','ip') for c in self.calls))

    def test_interface_down_does_not_masquerade_as_radio_off(self):
        self.wifi._read_flags.return_value=False
        self.assertTrue(self.wifi.is_enabled())

    def test_unsupported_firmware_preserves_legacy_interface_control(self):
        self.radio={}
        self.wifi.set_enabled(False)
        self.assertIn(['wpa_cli','-i','wlan0','disconnect'],self.calls)
        self.assertIn(['ifconfig','wlan0','down'],self.calls)

    def test_async_radio_change_can_settle_without_rollback(self):
        enabled={'com.lab126.cmd':1,'com.lab126.wifid':1}
        mixed={'com.lab126.cmd':1,'com.lab126.wifid':0}
        disabled={key:0 for key in enabled}
        with patch.object(self.wifi,'radio_state',side_effect=[enabled,mixed,mixed,disabled]),patch.object(self.wifi,'sleep') as wait:
            self.assertTrue(self.wifi._set_radio(False))
        self.assertEqual(wait.call_count,2)
        self.assertEqual(len(self.writes()),2)
        self.assertEqual(set(self.radio.values()),{0})

    def test_radio_confirmation_timeout_is_bounded_and_restores_switches(self):
        enabled=dict(self.radio)
        with patch.object(self.wifi,'radio_state',return_value=enabled),patch.object(self.wifi,'sleep') as wait,patch.object(self.wifi,'monotonic',return_value=0):
            with self.assertRaises(device_control.DeviceControlError): self.wifi._set_radio(False)
        self.assertEqual(wait.call_count,20)
        self.assertEqual(set(self.radio.values()),{1})

class SleepMaintenance(unittest.TestCase):
    def setUp(self):
        self.calls=[]; self.events=[]; self.state='active'
        def runner(args,timeout=3):
            self.calls.append(args)
            if args[0]=='lipc-get-prop':
                return 0,{'state':self.state,'battLevel':'62','isCharging':'0','status':'Powerd state: Screen Saver'}.get(args[-1],'0')
            if args[-2]=='powerButton': self.state='screenSaver'
            return 0,''
        self.p=device_control.KindlePower(self.events.append,threading.Event(),runner=runner)
        self.p.backend='powerd'
    def test_missed_sleep_event_does_not_send_fake_activity(self):
        for state in ('screenSaver','readyToSuspend','suspended'):
            self.calls.clear(); self.p.sleeping=False; self.state=state
            self.p.maintain(False,force=True)
            self.assertTrue(self.p.sleeping)
            self.assertFalse(any(c[0]=='lipc-set-prop' for c in self.calls))
        self.assertEqual(self.events,['system_sleep']*3)

    def test_sleeping_has_no_periodic_lipc_work(self):
        self.p.sleeping=True
        self.p.maintain(False,force=True)
        self.assertEqual(self.calls,[])

    def test_sleep_request_and_maintenance_are_serialized(self):
        started=threading.Event()
        def maintain(): started.set(); self.p.maintain(False,force=True)
        with self.p.command_lock:
            thread=threading.Thread(target=maintain); thread.start()
            self.assertTrue(started.wait(1))
            self.assertTrue(self.p.sleep())
        thread.join(2); self.assertFalse(thread.is_alive())
        self.assertFalse(any(c[0]=='lipc-set-prop' and c[-2]=='touchScreenSaverTimeout' for c in self.calls))

    def test_battery_sample_is_read_only_and_handles_unsupported_boottime(self):
        with patch.object(device_control.time,'clock_gettime',create=True,side_effect=OSError):
            sample=self.p.sample('wake_system')
        self.assertEqual(sample['battery'],62); self.assertEqual(sample['charging'],0)
        self.assertIsNone(sample['boottime'])
        self.assertTrue(all(c[0]=='lipc-get-prop' for c in self.calls))

class AppPowerSaving(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        (self.root/'data').mkdir()
        atomic_json(self.root/'data/agenda.json',dict(token='never-include-secret',revision=0,events=[],todos=[]))
        self.a=app.Application(self.root); self.a.wifi=Mock()
        self.a.wifi.is_enabled.return_value=True
        self.a.wifi.set_enabled.return_value=dict(enabled=False)
    def tearDown(self): self.temp.cleanup()

    def test_queued_restore_cannot_reenable_radio_after_new_sleep(self):
        self.a.standby_restore_wifi=True
        with patch.object(app.threading,'Thread') as thread:
            self.a.restore_standby_wifi()
            worker=thread.call_args.kwargs['target']
        self.a.enter_standby(system=True)
        worker()
        self.a.wifi.set_enabled.assert_called_once_with(False)
        self.assertTrue(self.a.standby_restore_wifi)
        self.a.standby.clear(); worker()
        self.assertEqual(self.a.wifi.set_enabled.call_args.args,(True,))
        self.assertFalse(self.a.standby_restore_wifi)

    def test_reentering_standby_keeps_pending_radio_restore(self):
        self.a.standby_restore_wifi=True; self.a.wifi.is_enabled.return_value=False
        self.a.enter_standby(system=True)
        self.assertTrue(self.a.standby_restore_wifi)

    def test_shutdown_restores_radio_even_when_sleep_page_was_open(self):
        self.a.standby.set(); self.a.standby_restore_wifi=True; self.a.stop.set()
        self.a.restore_standby_wifi(background=False)
        self.a.wifi.set_enabled.assert_called_once_with(True)

    def test_system_sleep_blocks_network_without_polling_battery(self):
        self.a.enter_standby(system=True); self.a.device=Mock()
        self.a.network_wake.wait=Mock(side_effect=lambda:self.a.stop.set())
        with patch.object(self.a,'update_battery') as battery,patch.object(self.a,'connection') as network:
            self.a.network_loop()
        battery.assert_not_called(); network.assert_not_called()
        self.a.network_wake.wait.assert_called_once_with()

    def test_power_history_is_bounded_and_excludes_private_config(self):
        self.a.device=Mock(); self.a.power=Mock()
        self.a.power.sample.return_value=dict(battery=62,charging=0,system_state='screensaver')
        self.a.wifi.radio_state.return_value={'com.lab126.cmd':0,'com.lab126.wifid':0}
        self.a.record_power('standby_system')
        path=self.root/'data/power-history.jsonl'
        self.assertNotIn('never-include-secret',path.read_text())
        data=json.loads(path.read_text()); self.assertEqual(data['battery'],62)
        path.write_text('x'*(128*1024))
        self.a.record_power('wake_system')
        self.assertTrue(path.with_name(path.name+'.1').exists())
        self.assertLess(path.stat().st_size,2048)

    def test_usb_stale_handle_is_temporary_unavailable_storage(self):
        with patch.object(Path,'exists',side_effect=OSError(getattr(errno,'ESTALE',116),'Stale file handle')):
            self.assertFalse(self.a.storage_available())
        self.assertTrue(self.a.storage_available())

    def test_usb_transition_stops_dashboard_for_full_cleanup(self):
        self.a.device=Mock()
        original=Path.exists
        def exists(path):
            if path==self.a.data/'agenda.json': raise OSError(getattr(errno,'ESTALE',116),'Stale file handle')
            return original(path)
        with patch.object(Path,'exists',exists),patch.object(self.a.requests,'get',side_effect=[queue.Empty,'quit']):
            self.a.display_loop()
        # run/main cleanup releases the power listener before restoring GUI.
        self.a.device.restore_gui.assert_not_called()
        self.assertTrue(self.a.stop.is_set())
        self.assertNotEqual(self.a.mode,'editor')
        self.a.device.display.assert_not_called()

    def test_storage_permission_error_is_not_silently_treated_as_usb(self):
        with patch.object(Path,'exists',side_effect=PermissionError(errno.EACCES,'denied')):
            with self.assertRaises(PermissionError): self.a.storage_available()

if __name__=='__main__': unittest.main()
