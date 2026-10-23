import copy, http.client, json, sys, tempfile, threading, unittest, urllib.parse
from datetime import timedelta, datetime
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'kindle-standalone'))
import app, renderer as ui, pages
from store import Store, Conflict, atomic_json

def fixture():
    today=ui.now().date()
    titles=['整理本周阅读笔记','确认周末出行安排','给绿植浇水','寄出包裹','整理书桌','备份照片','买咖啡豆','归还图书']
    return dict(token='b'*32,revision=0,city='',events=[
        dict(id='e'+str(i),date=(today+timedelta(days=i)).isoformat(),time='23:59',title=t,note='测试数据')
        for i,t in enumerate(['项目讨论','阅读分享','产品评审','和朋友吃饭','整理下周计划','家庭聚会','运动训练','月末复盘'])],
        todos=[dict(id='t'+str(i),title=t,note='',done=False,completed_at='') for i,t in enumerate(titles)])

def weather():
    today=ui.now().date()
    return dict(location=dict(name='北京',source='网络定位'),weather_at=ui.now().isoformat(),
        weather=dict(current=dict(temperature_2m=18,apparent_temperature=17,relative_humidity_2m=56,weather_code=2,wind_speed_10m=9),
        daily=dict(time=[(today+timedelta(days=i)).isoformat() for i in range(7)],weather_code=[2,0,3,61,2,0,1],
        temperature_2m_min=[11,10,9,8,9,10,11],temperature_2m_max=[19,20,18,16,18,19,20],
        precipitation_probability_max=[10,0,25,85,20,5,10])))

class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name); (self.root/'data').mkdir()
        self.path=self.root/'data/agenda.json'
        atomic_json(self.path,fixture()); self.a=app.Application(self.root)
    def tearDown(self): self.temp.cleanup()
    def draw(self,mode=None):
        if mode: self.a.handle_action(mode)
        return self.a.frame(capture=True)
    def test_completion_archive_refill_and_restart(self):
        self.draw()
        self.assertEqual([e['id'] for e in self.a.shown_todos],['t0','t1','t2'])
        action=self.a.tap_action(330,216)
        self.a.handle_action(action)
        self.assertTrue(self.a.redraw.is_set())
        self.draw()
        self.assertEqual([e['id'] for e in self.a.shown_todos],['t1','t2','t3'])
        reread=app.Application(self.root).store.read()
        self.assertTrue(reread['todos'][0]['done'])
        self.assertTrue(reread['todos'][0]['completed_at'])
        self.assertEqual(reread['events'],fixture()['events'])
        self.assertEqual(len(ui.todo_items(reread,True)),1)
        self.a.store.change(reread['revision'],'todo_state',dict(id='t0',done='0'))
        self.draw()
        self.assertEqual(self.a.shown_todos[0]['id'],'t0')
    def test_stale_checkbox_never_completes_replacement(self):
        self.draw(); action=self.a.tap_action(330,216)
        self.a.store.change(0,'todo_save',dict(id='t0',title='手机已修改',note=''))
        with self.assertLogs('kindle',level='WARNING'): self.a.handle_action(action)
        config=self.a.store.read()
        self.assertEqual(config['todos'][0]['title'],'手机已修改')
        self.assertFalse(any(e['done'] for e in config['todos']))
        self.assertIn('更新',self.a.todo_message)
    def test_double_queued_completion_does_not_mark_next(self):
        self.draw(); action=self.a.tap_action(330,216)
        self.a.handle_action(action); self.draw()
        with self.assertLogs('kindle',level='WARNING'): self.a.handle_action(action)
        self.assertEqual([e['id'] for e in ui.todo_items(self.a.store.read(),True)],['t0'])
    def test_todo_pagination_done_strikethrough(self):
        self.draw('todos')
        self.assertEqual(len(self.a.shown_todos),6)
        self.a.handle_action(self.a.tap_action(490,690)); self.draw()
        self.assertEqual([e['id'] for e in self.a.shown_todos],['t6','t7'])
        self.a.handle_action(self.a.tap_action(48,145)); self.draw()
        self.assertTrue(self.a.store.read()['todos'][6]['done'])
        self.assertEqual(ui.todo_items(self.a.store.read())[-1]['id'],'t6')
        image=ui.render_todos(self.a.store.read(),1)
        self.assertEqual(image.size,(600,800))
    def test_dashboard_regions_five_events_and_icons(self):
        self.draw()
        self.assertEqual(len(self.a.shown_events),5)
        for point,action in [((150,210),'weather'),((430,165),'todos'),((90,570),'agenda'),((370,735),'pair'),((510,735),'quit')]:
            self.assertEqual(self.a.tap_action(*point),action)
        self.assertIsNone(self.a.tap_action(20,740))
        self.assertNotEqual(self.a.tap_action(100,740),'editor')
        self.a.drawing=True
        self.assertIsNone(self.a.tap_action(330,216))
    def test_calendar_leap_year_and_day_selection(self):
        self.draw('calendar'); self.a.calendar_year=2024; self.a.calendar_month=2
        self.draw()
        flat=sum(ui.month_days(2024,2),[])
        self.assertIn(29,flat)
        self.assertIsNone(self.a.tap_action(45,190))
        index=flat.index(29); action=self.a.tap_action(30+(index%7)*77+35,165+(index//7)*76+30)
        self.a.handle_action(action); self.draw()
        self.assertEqual(self.a.agenda_day,'2024-02-29')
        self.assertEqual(self.a.shown_events,[])
        self.a.handle_action(self.a.tap_action(70,745)); self.assertEqual(self.a.mode,'calendar')
        self.a.calendar_year,self.a.calendar_month=2026,12
        self.a.handle_action('month_next')
        self.assertEqual((self.a.calendar_year,self.a.calendar_month),(2027,1))
        self.a.handle_action('month_prev')
        self.assertEqual((self.a.calendar_year,self.a.calendar_month),(2026,12))
    def test_all_agenda_includes_past_and_return_context(self):
        config=self.a.store.read()
        config['events'][0]['date']='2020-01-01'; atomic_json(self.path,config)
        self.draw('agenda'); self.assertEqual(self.a.shown_events[0]['date'],'2020-01-01')
        self.a.handle_action(self.a.tap_action(150,169)); self.draw()
        self.assertEqual(self.a.mode,'event')
        self.a.handle_action('event_back'); self.assertEqual(self.a.mode,'agenda')
        self.a.frame(capture=True)
        self.a.handle_action('page_next'); self.draw()
        self.assertEqual(len(self.a.shown_events),2)
    def test_legacy_agenda_untouched_until_first_change(self):
        config=fixture(); del config['todos']; atomic_json(self.path,config)
        original=self.path.read_bytes(); value=Store(self.path).read()
        self.assertEqual(value['todos'],[]); self.assertEqual(self.path.read_bytes(),original)
        value=Store(self.path).change(0,'todo_save',dict(title='新增',note=''))
        self.assertEqual(value['events'],config['events'])
        self.assertEqual(len(value['todos']),1)
    def test_seven_day_weather_and_bad_cache(self):
        state=weather(); rows=ui.weather_rows(state)
        self.assertEqual(len(rows),7)
        self.assertEqual(ui.render_weather(state).size,(600,800))
        for bad in ({},dict(weather=[]),dict(weather={'daily':{'time':['bad']}})):
            self.assertEqual(ui.render_weather(bad).size,(600,800))
        state['weather']['daily']['temperature_2m_max']=[None]
        self.assertEqual(ui.render_weather(state).size,(600,800))
    def test_browser_todo_save_complete_restore_and_conflict(self):
        server=app.ThreadingHTTPServer(('127.0.0.1',0),self.a.handler())
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        prefix='/'+self.a.token
        def req(method,path,values=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            conn.request(method,path,urllib.parse.urlencode(values) if values else None)
            response=conn.getresponse(); result=response.status,response.read(),dict(response.getheaders()); conn.close(); return result
        def post(action,revision,**values):
            return req('POST',prefix+'/action',dict(csrf=self.a.csrf,action=action,revision=revision,device='kindle',**values))
        try:
            result=post('todo_save',0,title='<script>测试</script>',note='多行\n内容')
            self.assertEqual(result[0],303); self.assertIn('/todos?',result[2]['Location'])
            html=req('GET',prefix+'/todos?device=kindle')[1].decode()
            self.assertIn('&lt;script&gt;',html); self.assertNotIn('<script>',html)
            self.assertIn('返回看板',html)
            identity=self.a.store.read()['todos'][-1]['id']
            self.assertEqual(post('todo_state',1,id=identity,done='1')[0],303)
            self.assertEqual(post('todo_state',1,id=identity,done='0')[0],409)
            self.assertEqual(post('todo_state',2,id=identity,done='0')[0],303)
            self.assertEqual(post('todo_delete',3,id=identity)[0],303)
            self.assertEqual(req('GET',prefix+'/?date=2028-02-29')[0],200)
        finally: server.shutdown(); server.server_close(); thread.join()

if __name__=='__main__':
    if '--preview' in sys.argv:
        out=ROOT/'kindle-standalone/界面预览'; out.mkdir(exist_ok=True)
        config=fixture(); state=weather()
        state.update(battery=76,battery_at=ui.now().isoformat(),news_at=ui.now().isoformat(),news=[
            dict(name=t,time=int(ui.now().timestamp())) for t in ['人工智能应用加速落地，产业关注持续升温','全球市场观察：科技与金融动态','新一代模型能力提升，开发者生态持续扩展','算力需求增长，基础设施建设稳步推进','金融机构探索智能助手与风险管理应用','更多行业开始部署人工智能工具']])
        ui.render(config,state).save(out/'首页.png')
        config['todos'][0]['done']=True
        ui.render_todos(config,1).save(out/'全部待办.png')
        ui.render_weather(state).save(out/'天气预报.png')
        ui.render_calendar(config,ui.now().year,ui.now().month).save(out/'日历.png')
        ui.render_agenda(config).save(out/'全部日程.png')
        print('PREVIEWS_OK')
    else: unittest.main(verbosity=2)
