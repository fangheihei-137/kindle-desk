"""USB/native handback must not leave an awake editor or orphaned process."""
import errno,json,queue,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1]/'kindle-standalone'
sys.path.insert(0,str(ROOT))
import app,device_control,updater
from store import atomic_json

class NativeExit(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        (self.root/'data').mkdir()
        atomic_json(self.root/'data/agenda.json',dict(token='exit-test',revision=0,events=[],todos=[]))
        self.a=app.Application(self.root)
    def tearDown(self): self.temp.cleanup()

    def test_stopped_app_cannot_request_editor_inhibition(self):
        self.a.mode='editor'; self.a.power=Mock(); self.a.request_stop()
        self.a.maintain_power(); self.a.power.maintain.assert_not_called()

    def test_close_releases_inhibition_and_late_maintenance_is_inert(self):
        runner=Mock(return_value=(0,'0'))
        power=device_control.KindlePower(lambda action:None,self.a.stop,runner=runner)
        power.backend='powerd'; power.protected=True
        self.a.stop.set(); power.maintain(True,force=True)
        runner.assert_not_called()
        power.close()
        self.assertEqual(runner.call_args.args[0][-2:],['preventScreenSaver','0'])
        count=runner.call_count
        power.maintain(True,force=True)
        self.assertEqual(runner.call_count,count)

    def test_close_waits_out_in_flight_inhibition_then_releases(self):
        writing=threading.Event(); finish=threading.Event(); commands=[]
        def runner(args,timeout=3):
            commands.append(args)
            if args[0]=='lipc-set-prop' and args[-2:]==['preventScreenSaver','1']:
                writing.set(); self.assertTrue(finish.wait(2))
            return 0,'0'
        power=device_control.KindlePower(lambda action:None,self.a.stop,runner=runner)
        power.backend='powerd'
        worker=threading.Thread(target=lambda:power.maintain(True,force=True)); worker.start()
        self.assertTrue(writing.wait(2))
        closer=threading.Thread(target=power.close); closer.start()
        self.assertTrue(power.stop.wait(2)); finish.set()
        worker.join(2); closer.join(2)
        self.assertFalse(worker.is_alive() or closer.is_alive())
        writes=[args[-1] for args in commands if args[0]=='lipc-set-prop' and args[-2]=='preventScreenSaver']
        self.assertEqual(writes[-1],'0')
        self.assertTrue(writes[:-1])
        self.assertTrue(all(value=='1' for value in writes[:-1]))

    def test_native_gui_handoff_does_not_restore_stale_inhibit_value(self):
        device=app.Device.__new__(app.Device)
        device.old_prevent='1'; device.gui_stopped=True; device.firewall=None
        history=[]; device.framework_bridge=Mock()
        device.framework_bridge.close.side_effect=lambda:history.append('bridge_closed')
        with patch.object(app,'command',side_effect=lambda args:history.append(args)):
            device.close()
        self.assertEqual(history,[['lipc-set-prop','com.lab126.powerd','preventScreenSaver','0'],
                                  'bridge_closed',['initctl','start','lab126_gui']])

    def test_usb_exit_closes_power_and_server_before_native_handoff(self):
        history=[]; self.a.device=Mock(); power=Mock(); server=Mock()
        power.start.return_value=True
        power.close.side_effect=lambda:history.append('power_released')
        server.shutdown.side_effect=lambda:history.append('server_stopped')
        self.a.device.close.side_effect=lambda:history.append('native_gui')
        with patch.object(app,'ThreadingHTTPServer',return_value=server),patch.object(app.threading,'Thread'),patch.object(app,'KindlePower',return_value=power),patch.object(self.a,'record_power'),patch.object(self.a,'capture_system_power_log'),patch.object(self.a,'storage_available',return_value=False),patch.object(self.a.requests,'get',side_effect=queue.Empty):
            try: self.a.run()
            finally: self.a.device.close()
        self.assertEqual(history,['power_released','server_stopped','native_gui'])
        self.assertTrue(self.a.stop.is_set())
        self.assertNotEqual(self.a.mode,'editor')
        self.a.device.restore_gui.assert_not_called()

    def test_update_state_tolerates_usb_unmount_without_modifying_pending_file(self):
        path=self.root/'data/update-pending.json'
        value=dict(phase='trial',id='test'); path.write_text(json.dumps(value))
        with patch.object(Path,'read_text',side_effect=OSError(getattr(errno,'ESTALE',116),'stale')):
            self.assertIsNone(updater.state(self.root))
        self.assertEqual(updater.state(self.root),value)

    def test_update_state_does_not_hide_permission_or_json_errors(self):
        with patch.object(Path,'read_text',side_effect=PermissionError(errno.EACCES,'denied')):
            with self.assertRaises(PermissionError): updater.state(self.root)
        with patch.object(Path,'read_text',return_value='broken'):
            with self.assertRaises(ValueError): updater.state(self.root)

    def test_supervisor_error_stops_child_instead_of_leaving_it_running(self):
        child=Mock(); child.poll.return_value=None
        with patch.object(updater,'recover',return_value=False),patch.object(updater.signal,'signal'),patch.object(updater.subprocess,'run',return_value=Mock(returncode=0)),patch.object(updater.subprocess,'Popen',return_value=child),patch.object(updater.time,'monotonic',side_effect=[0,100]),patch.object(updater,'state',side_effect=ValueError('bad update state')):
            with self.assertRaisesRegex(ValueError,'bad update state'): updater.supervise(self.root)
        child.terminate.assert_called_once(); child.wait.assert_called_once_with(timeout=6)

if __name__=='__main__': unittest.main()
