import sys, tempfile, unittest, json
from pathlib import Path
from unittest.mock import Mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'kindle-standalone'))
import app,native_input as ni,renderer as ui
from store import atomic_json

class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        (self.root/'data').mkdir()
        self.path=self.root/'data/agenda.json'
        atomic_json(self.path,dict(token='c'*32,revision=0,city='',events=[],todos=[]))
        self.a=app.Application(self.root)
        self.a.device=Mock()
    def tearDown(self): self.temp.cleanup()
    def key(self,value): self.a.handle_action(('form',('key',value)))
    def do(self,op,value=None): self.a.handle_action(('form',(op,value)))
    def type_zh(self,pinyin,word):
        for char in pinyin: self.key(char)
        candidate=next(c for c in self.a.form.candidates() if c[0]==word)
        self.do('candidate',candidate)
    def test_native_add_zh_event_save_no_gui_transition(self):
        self.a.handle_action('agenda')
        self.a.handle_action(self.a.tap_action(290,748))
        self.assertEqual(self.a.mode,'form')
        self.type_zh('kaihui','开会')
        self.do('field','time')
        for key in '09:30': self.key(key)
        self.do('save')
        self.assertEqual(self.a.mode,'agenda')
        self.assertEqual(self.a.store.read()['events'][0]['title'],'开会')
        self.assertEqual(self.a.store.read()['events'][0]['time'],'09:30')
        self.a.device.editor.assert_not_called()
        self.a.device.restore_gui.assert_not_called()
    def test_native_add_todo_and_reopen(self):
        self.a.handle_action('todos'); self.a.handle_action('todo_editor')
        self.type_zh('maicai','买菜'); self.do('save')
        self.a.frame(capture=True)
        action=self.a.tap_action(160,145)
        self.a.handle_action(action)
        self.assertEqual(self.a.mode,'form'); self.assertEqual(self.a.form.values['title'],'买菜')
        self.do('field','note'); self.type_zh('mingtian','明天'); self.do('save')
        self.assertEqual(len(self.a.store.read()['todos']),1)
        self.assertEqual(self.a.store.read()['todos'][0]['note'],'明天')
        self.a.device.editor.assert_not_called()
    def test_pending_pinyin_cannot_save_or_switch_field(self):
        self.a.handle_action('todo_editor')
        for char in 'nihao': self.key(char)
        self.do('save'); self.assertEqual(self.a.store.read()['todos'],[])
        self.do('field','note'); self.assertEqual(self.a.form.field,'title')
        self.assertIn('先',self.a.form.message)
    def test_cancel_requires_confirmation_for_dirty_draft(self):
        self.a.handle_action('todos'); self.a.handle_action('todo_editor')
        self.type_zh('nihao','你好')
        self.do('cancel'); self.assertEqual(self.a.mode,'form')
        self.assertTrue(self.a.form.cancel_confirm)
        self.do('cancel'); self.assertEqual(self.a.mode,'todos')
        self.assertEqual(self.a.store.read()['todos'],[])
    def test_phone_navigation_does_not_close_draft(self):
        self.a.handle_action('todo_editor')
        self.type_zh('nihao','你好')
        self.a.handle_action('dashboard')
        self.assertEqual(self.a.mode,'form')
        self.assertEqual(self.a.form.values['title'],'你好')
        self.a.frame(capture=True)
        self.assertEqual(self.a.form.values['title'],'你好')
    def test_invalid_date_retains_draft(self):
        self.a.handle_action('editor'); self.type_zh('kaihui','开会')
        self.do('field','date')
        for key in '2026-02-30': self.key(key)
        self.do('save')
        self.assertEqual(self.a.mode,'form')
        self.assertEqual(self.a.form.values['title'],'开会')
        self.assertIn('日期无效',self.a.form.message)
        self.assertFalse(self.a.store.read()['events'])
        self.do('quick','today'); self.do('save')
        self.assertEqual(len(self.a.store.read()['events']),1)
    def make_todo(self):
        data=self.a.store.change(0,'todo_save',dict(title='原始内容',note=''))
        item=data['todos'][0]
        self.a.handle_action('todos'); self.a.frame(capture=True)
        self.a.handle_action(('todo_edit',item['id']))
        return item
    def test_same_item_conflict_keeps_phone_and_draft(self):
        item=self.make_todo()
        self.key('clear'); self.type_zh('nihao','你好')
        self.a.store.change(1,'todo_save',dict(id=item['id'],title='手机新内容',note=''))
        self.do('save')
        self.assertEqual(self.a.form.values['title'],'你好')
        self.assertEqual(self.a.store.read()['todos'][0]['title'],'手机新内容')
        self.assertEqual(self.a.mode,'form')
    def test_other_item_update_safe_retry_preserves_both(self):
        self.make_todo()
        self.key('clear'); self.type_zh('nihao','你好')
        self.a.store.change(1,'todo_save',dict(title='另一条手机待办',note=''))
        self.do('save')
        self.assertEqual([e['title'] for e in self.a.store.read()['todos']],['你好','另一条手机待办'])
    def test_delete_confirmation_and_concurrent_change(self):
        item=self.make_todo(); self.do('delete')
        self.assertEqual(len(self.a.store.read()['todos']),1)
        self.a.store.change(1,'todo_state',dict(id=item['id'],done='1'))
        self.do('delete')
        self.assertEqual(len(self.a.store.read()['todos']),1)
        self.assertEqual(self.a.mode,'form')
        self.assertTrue(self.a.form.conflict)
    def test_native_delete_and_restore_context(self):
        self.make_todo(); self.do('delete'); self.do('delete')
        self.assertEqual(self.a.mode,'todos')
        self.assertEqual(self.a.store.read()['todos'],[])
    def test_candidate_prefix_pagination_and_dictionary_cache(self):
        self.assertIn(('你好',5),ni.pinyin_candidates('nihaozzz'))
        f=ni.NativeForm('todo',0)
        for c in 'shi': f.key(c)
        first=f.candidates()[:4]; f.action(('candidate_page',1))
        self.assertNotEqual(first,f.candidates()[4:8])
        f.key('space')
        self.assertTrue(f.values['title']); self.assertFalse(f.composition)
        self.assertLessEqual(ni.pinyin_shard.cache_info().currsize,2)
    def test_cursor_limits_and_clear_composition(self):
        f=ni.NativeForm('todo',0); f.language='en'
        for c in 'abc': f.key(c)
        f.key('left'); f.key('X'); self.assertEqual(f.values['title'],'abXc')
        f.key('backspace'); self.assertEqual(f.values['title'],'abc')
        f.key('language'); f.key('n'); f.key('clear')
        self.assertEqual(f.values['title'],'abc'); self.assertEqual(f.composition,'')
        f.values['title']='字'*100; f.cursor=100; f.insert('多')
        self.assertEqual(len(f.values['title']),100)
    def test_every_keyboard_mode_renders_and_taps(self):
        for kind in ('event','todo'):
            f=ni.NativeForm(kind,0)
            for field in f.values:
                f.focus(field)
                for lang in ('zh','en','number','symbol'):
                    f.language=lang
                    self.assertEqual(ni.render(f).size,(600,800))
                    for box,label,action in ni.keyboard_buttons(f):
                        hit=ni.tap(f,(box[0]+box[2])/2,(box[1]+box[3])/2)
                        self.assertEqual(hit,action)

if __name__=='__main__':
    if '--preview' in sys.argv:
        out=ROOT/'kindle-standalone/界面预览'
        f=ni.NativeForm('event',0,day='2026-10-23')
        f.values.update(title='项目讨论',note='带上本周的工作笔记',time='09:30')
        f.cursor=len(f.values['title'])
        ni.render(f).save(out/'看板内新增日程.png')
        f=ni.NativeForm('todo',0)
        f.values['title']='整理'; f.cursor=2; f.composition='biji'
        ni.render(f).save(out/'看板内新增待办.png')
        print('NATIVE_PREVIEWS_OK')
    else: unittest.main(verbosity=2)
