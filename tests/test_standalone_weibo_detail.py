"""Text-only related news, bounded caches and nonblocking Kindle navigation."""
import copy,json,queue,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]/'kindle-standalone'
sys.path.insert(0,str(ROOT))
import app,modules,renderer as ui
from store import atomic_json

TOPIC='城市图书馆开放夜间阅读'
def item(name=TOPIC): return dict(name=name,content=name+'\n热度：12000',source='微博热搜',time=time.time())
def response(topic=TOPIC):
    return dict(code=0,data=dict(list=[dict(title='<em>'+topic+'</em>',intro='本周起，城市图书馆延长开放时间，为市民提供晚间自习、借阅和阅读活动。',url='https://news.sina.com.cn/example',media_show='城市日报',ctime=time.time()-3600)]))


class WeiboDetail(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name); (self.root/'data').mkdir()
        atomic_json(self.root/'data/agenda.json',dict(token='weibo-test-token',revision=0,events=[],todos=[]))
        self.a=app.Application(self.root)
    def tearDown(self):
        self.a.stop.set()
        worker=self.a.detail_worker
        if worker: worker.join(timeout=3)
        self.temp.cleanup()
    def consume(self):
        worker=self.a.detail_worker
        if worker: worker.join(timeout=3)
        while not self.a.requests.empty(): self.a.handle_action(self.a.requests.get_nowait())

    def test_html_is_text_only_and_no_scripts_images_or_control_characters(self):
        raw='<p>正文&nbsp;<em>关键词</em><img src="x" onerror="alert(1)"></p><script>攻击指令</script><style>CSS</style><p>第二段\u202e</p>'
        self.assertEqual(modules.plain_text(raw),'正文 关键词\n\n第二段')
        self.assertEqual(modules.plain_text(None),'')

    def test_summary_filter_keeps_related_recent_reports_and_deduplicates(self):
        raw=response(); duplicate=copy.deepcopy(raw['data']['list'][0]); duplicate['url']+='?duplicate=1'
        old=copy.deepcopy(duplicate); old['ctime']=time.time()-8*86400; old['intro']='一篇已经超过一周的旧报道，不能冒充当前热搜的具体内容。'
        unrelated=copy.deepcopy(duplicate); unrelated.update(title='周末天气晴朗',intro='各地天气转晴，市民在公园散步，与城市公共设施开放时间没有相关内容。')
        raw['data']['list'] += [duplicate,old,unrelated]
        rows=modules.news_summaries(raw,TOPIC)
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['source'],'城市日报')
        self.assertEqual(rows[0]['title'],TOPIC); self.assertNotIn('<',rows[0]['text'])
        with self.assertRaises(ValueError): modules.news_summaries({'code':403,'data':{}},TOPIC)
        with self.assertRaises(ValueError): modules.news_summaries({'code':0,'data':{'list':[unrelated]}},TOPIC)

    def test_detail_cache_survives_restart_is_bounded_and_expires_on_disk(self):
        with patch.object(self.a.modules,'request_json',return_value=response()) as fetch:
            saved=self.a.modules.load_weibo_detail(item())
        self.assertEqual(fetch.call_count,1)
        self.assertIn('search.sina.com.cn/api/news?',fetch.call_args.args[0])
        restored=modules.ModuleManager(self.root)
        self.assertEqual(restored.weibo_detail(item()),saved)
        with restored.lock:
            for i in range(105): restored.details[str(i)]=dict(saved,checked_at=time.time()-i)
            restored._prune_details()
        self.assertLessEqual(len(restored.details),100)
        with patch.object(modules.time,'time',return_value=saved['checked_at']+48*3600+1):
            self.assertIsNone(restored.weibo_detail(item()))
        self.assertEqual(json.loads((self.root/'data/weibo-detail-cache.json').read_text()),{})

    def test_corrupt_cache_is_ignored_without_breaking_reader(self):
        self.a.modules.details={self.a.modules.detail_key(item()):dict(checked_at='bad',articles=[None])}
        self.assertIsNone(self.a.modules.weibo_detail(item()))

    def test_click_loads_without_blocking_and_finishes_with_pages(self):
        entered=threading.Event(); release=threading.Event()
        raw=response(); raw['data']['list'][0]['intro']+='图书馆提供安静舒适的阅读环境。'*40
        def fetch(url): entered.set(); release.wait(2); return raw
        self.a.shown_news=[item()]
        with patch.object(self.a.modules,'request_json',side_effect=fetch):
            started=time.monotonic(); self.a.handle_action(('news',0))
            self.assertLess(time.monotonic()-started,.5)
            self.assertTrue(entered.wait(1)); self.assertTrue(self.a.news_item['detail_loading'])
            self.assertIn('正在加载',self.a.news_item['content']); release.set(); self.consume()
        self.assertFalse(self.a.news_item['detail_loading'])
        self.assertIn('来源：城市日报',self.a.news_item['content'])
        self.assertIn('非微博原文',self.a.news_item['content'])
        self.assertGreater(len(self.a.news_pages),1)
        self.assertEqual(self.a.frame().size,(600,800))
        self.a.handle_action('news_next'); self.assertEqual(self.a.news_page,1)
        self.a.handle_action('news_back'); self.assertEqual(self.a.mode,'dashboard')

    def test_late_response_never_replaces_another_article_or_wakes_standby(self):
        old=item(); self.a.mode='news'; self.a.news_item=dict(name='金融快讯',source='华尔街见闻',content='原文')
        self.a.handle_action(('weibo_detail_result',0,dict(old,content='已加载')))
        self.assertEqual(self.a.news_item['content'],'原文')
        self.a.mode='screensaver'; self.a.standby.set(); self.a.redraw.clear()
        self.a.handle_action(('weibo_detail_result',0,dict(old,content='已加载')))
        self.assertEqual(self.a.mode,'screensaver'); self.assertFalse(self.a.redraw.is_set())

    def test_fresh_cache_avoids_network_and_failed_refresh_keeps_cache(self):
        with patch.object(self.a.modules,'request_json',return_value=response()): saved=self.a.modules.load_weibo_detail(item())
        with patch.object(self.a.modules,'request_json',side_effect=OSError('private connection details')) as fetch:
            self.a.open_weibo_detail(item()); self.assertFalse(self.a.news_item['detail_loading']); fetch.assert_not_called()
            self.assertEqual(self.a.tap_action(550,35),'weibo_detail_refresh')
            self.a.handle_action('weibo_detail_refresh'); self.consume()
        self.assertIn('显示已保存摘要',self.a.news_item['content'])
        self.assertNotIn('private connection',self.a.news_item['content'])
        self.assertEqual(self.a.modules.weibo_detail(item()),saved)

    def test_fast_switches_only_one_active_fetch_and_latest_pending(self):
        entered=threading.Event(); release=threading.Event(); calls=[]
        def load(article):
            calls.append(article['name']); entered.set(); release.wait(2)
            return dict(checked_at=time.time(),articles=modules.news_summaries(response(),TOPIC))
        with patch.object(self.a.modules,'load_weibo_detail',side_effect=load):
            self.a.open_weibo_detail(item('话题一')); self.assertTrue(entered.wait(1))
            self.a.open_weibo_detail(item('话题二')); self.a.open_weibo_detail(item('话题三'))
            release.set(); self.consume()
        self.assertEqual(calls,['话题一','话题三'])
        self.assertEqual(self.a.news_item['name'],'话题三')
        self.assertFalse(self.a.news_item['detail_loading'])

if __name__=='__main__': unittest.main()
