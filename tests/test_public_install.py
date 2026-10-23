import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(ROOT/'kindle-standalone'))
import install
from store import initialize_data,Store

class PublicInstallTests(unittest.TestCase):
    def test_first_boot_unique_empty_and_repeated_boot_preserves_data(self):
        with tempfile.TemporaryDirectory() as temp:
            paths=[Path(temp)/name/'agenda.json' for name in ('one','two')]
            for path in paths: self.assertTrue(initialize_data(path))
            first,second=[Store(path).read() for path in paths]
            self.assertNotEqual(first['token'],second['token'])
            self.assertGreaterEqual(len(first['token']),32)
            self.assertEqual((first['events'],first['todos']),( [],[]))
            Store(paths[0]).change(0,'todo_save',dict(title='保留我',note=''))
            before=paths[0].read_bytes()
            self.assertFalse(initialize_data(paths[0]))
            self.assertEqual(paths[0].read_bytes(),before)

    def test_invalid_existing_config_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'agenda.json'; path.write_text('{broken',encoding='utf-8')
            with self.assertRaises(ValueError): initialize_data(path)
            self.assertEqual(path.read_text(encoding='utf-8'),'{broken')

    def test_installer_rejects_existing_install_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            mount=Path(temp); (mount/'documents').mkdir(); (mount/'python3/bin').mkdir(parents=True)
            (mount/'python3/bin/python3.9').write_text('runtime')
            (mount/'kindle-standalone').mkdir(); marker=mount/'kindle-standalone/keep.txt'
            marker.write_text('user data')
            with self.assertRaisesRegex(ValueError,'已有看板'): install.install(mount)
            self.assertEqual(marker.read_text(),'user data')

    def test_program_copy_excludes_host_credentials_and_private_data(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'copy'
            install.copy_program(target)
            self.assertFalse((target/'wireless-device.json').exists())
            self.assertEqual(Store(target/'data/agenda.json').read()['events'],[])
            self.assertTrue((target/'assets/SourceHanSansCN-Regular.otf').is_file())
            self.assertFalse(list(target.rglob('*.ttc')))

if __name__=='__main__': unittest.main()
