"""Bounded panel providers, private account configuration and local photo library."""
import copy,hashlib,io,json,math,os,re,ssl,threading,time,urllib.parse,urllib.request
from datetime import datetime,timezone,timedelta
from decimal import Decimal,InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from PIL import Image,ImageOps
from store import atomic_json

CATALOG=(
    ('finance','AI 金融资讯','AI 与金融快讯，历史保留 48 小时。','news'),
    ('news','综合新闻','关注最新快讯，不限 AI 主题。','news'),
    ('weibo','微博热搜','微博实时榜单；无法获取时显示上次内容和时间。','trend'),
    ('usage','AI 用量与余额','DeepSeek 余额与 Codex 用量，在同一块查看。','chart'),
    ('photo','我的图片','上传照片或信息卡，适配墨水屏。','image'),
)
TITLES={row[0]:row[1] for row in CATALOG}
MAX_UPLOAD=6*1024*1024
MAX_PHOTOS=12
MAX_PIXELS=2_000_000
MAX_REMOTE=2*1024*1024
DETAIL_LIFETIME=48*3600
DETAIL_FRESH=30*60
MAX_DETAILS=100
DETAIL_TZ=timezone(timedelta(hours=8))


def plain_text(value,limit=1200):
    """Render public search snippets as text, never execute or display HTML."""
    if not isinstance(value,str): return ''
    class TextOnly(HTMLParser):
        def __init__(self): super().__init__(convert_charrefs=True); self.parts=[]; self.hidden=[]
        def handle_starttag(self,tag,attrs):
            if tag in ('script','style','noscript','iframe'): self.hidden.append(tag)
            elif not self.hidden and tag in ('br','p','div','li'): self.parts.append('\n')
        def handle_endtag(self,tag):
            if self.hidden:
                if tag==self.hidden[-1]: self.hidden.pop()
            elif tag in ('p','div','li'): self.parts.append('\n')
        def handle_data(self,data):
            if not self.hidden: self.parts.append(data)
    parser=TextOnly(); parser.feed(value[:24000]); parser.close()
    text=''.join(parser.parts)
    text=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]','',text)
    text='\n'.join(re.sub(r'\s+',' ',line).strip() for line in text.splitlines())
    return re.sub(r'\n{3,}','\n\n',text).strip()[:limit]


def topic_matches(topic,text):
    normalize=lambda value:re.sub(r'[^a-z0-9\u4e00-\u9fff]','',value.lower())
    topic=normalize(topic); text=normalize(text)
    if len(topic)<2: return False
    if topic in text: return True
    pairs={topic[i:i+2] for i in range(len(topic)-1)}
    return len(pairs)>=3 and sum(pair in text for pair in pairs)/len(pairs)>=.6


def news_summaries(raw,topic,current=None):
    current=current or time.time()
    if not isinstance(raw,dict) or raw.get('code')!=0 or not isinstance(raw.get('data'),dict):
        raise ValueError('新闻来源暂不可用，请稍后重试。')
    rows=raw['data'].get('list')
    if not isinstance(rows,list): raise ValueError('新闻来源暂不可用，请稍后重试。')
    result=[]; seen=set()
    for row in rows[:10]:
        if not isinstance(row,dict): continue
        title=plain_text(row.get('title'),180)
        summary=plain_text(row.get('intro') or row.get('searchSummary'),700)
        published=stamp(row.get('ctime')); url=safe_url(row.get('url'))
        if not published or not current-7*86400<=published<=current+300: continue
        if not title or len(summary)<20 or not url or not topic_matches(topic,title+' '+summary): continue
        fingerprint=re.sub(r'\W','',summary).lower()
        if fingerprint in seen or url in seen: continue
        seen.update((fingerprint,url))
        result.append(dict(title=title,text=summary,url=url,published_at=published,
                           source=plain_text(row.get('media_show') or row.get('media'),60) or '新浪新闻'))
        if len(result)==3: break
    if not result: raise ValueError('暂未找到足够相关的公开新闻摘要，请稍后重试。')
    return result


def detail_body(item,detail=None,loading=False,error=''):
    title=plain_text(item.get('name'),150)
    lines=[title,'','相关新闻摘要']
    if detail and detail.get('articles'):
        for index,article in enumerate(detail['articles'],1):
            when=datetime.fromtimestamp(article['published_at'],DETAIL_TZ).strftime('%m-%d %H:%M')
            lines+=['',str(index)+'. '+article['title'],'来源：'+article['source']+' · '+when,article['text']]
        checked=datetime.fromtimestamp(detail['checked_at'],DETAIL_TZ).strftime('%m-%d %H:%M')
        lines+=['','摘要保存于 '+checked,'来自公开新闻检索，非微博原文。']
        if error: lines+=['更新暂不可用，显示已保存摘要。']
        elif loading: lines+=['正在检查更新…']
    elif loading: lines+=['','正在加载文字内容…','加载期间可以返回。']
    else: lines+=['',error or '暂未找到相关报道。','点右上角刷新图标重试。']
    return '\n'.join(lines)

def selected(config):
    kind=config.get('settings',{}).get('panel_module','finance')
    return 'usage' if kind in ('codex','deepseek') else kind
def safe_url(value):
    value=str(value or '')
    return value if urllib.parse.urlsplit(value).scheme in ('http','https') else ''
def stamp(value):
    try:
        if isinstance(value,bool): return None
        result=float(value)
        return result if math.isfinite(result) and 0<result<4102444800 else None
    except (TypeError,ValueError): return None
def money(value):
    try:
        result=Decimal(str(value))
        if not result.is_finite() or abs(result)>Decimal('1000000000'): raise ValueError()
        return format(result.quantize(Decimal('.01')),'f')
    except (InvalidOperation,ValueError,TypeError): raise ValueError('余额数据格式不正确。') from None

def codex_windows(payload):
    if not isinstance(payload,dict): raise ValueError('额度数据格式不正确。')
    buckets=payload.get('rateLimitsByLimitId')
    if not isinstance(buckets,dict) or not buckets:
        single=payload.get('rateLimits')
        buckets={str((single or {}).get('limitId') or 'codex'):single} if isinstance(single,dict) else {}
    result=[]
    for identity,bucket in list(buckets.items())[:12]:
        if not isinstance(bucket,dict): continue
        for slot in ('primary','secondary'):
            item=bucket.get(slot)
            if not isinstance(item,dict): continue
            used=item.get('usedPercent'); duration=item.get('windowDurationMins')
            if used is None: continue
            try:
                used=float(used)
                if not math.isfinite(used) or not 0<=used<=10000: continue
                duration=int(duration) if duration is not None else None
                if duration is not None and not 0<duration<=525600: duration=None
            except (ValueError,TypeError,OverflowError): continue
            label=str(bucket.get('limitName') or identity)[:40]
            period=(str(duration//1440)+' 天' if duration and duration%1440==0 else
                    str(duration//60)+' 小时' if duration and duration%60==0 else
                    str(duration)+' 分钟' if duration else '窗口 '+('1' if slot=='primary' else '2'))
            result.append(dict(label=label,period=period,used=round(used,2),remaining=max(0,round(100-used,2)),resets_at=stamp(item.get('resetsAt'))))
    if not result: raise ValueError('账户尚未返回可用额度；请确认电脑已登录 ChatGPT。')
    return result[:12]

class ModuleManager:
    def __init__(self,root):
        self.root=Path(root); self.data=self.root/'data'; self.lock=threading.RLock()
        self.cache=self._read('modules-cache.json',{})
        if not isinstance(self.cache,dict): self.cache={}
        self.refreshing=False
        details=self._read('weibo-detail-cache.json',{})
        self.details=details if isinstance(details,dict) else {}

    def detail_key(self,item):
        return hashlib.sha256(str(item.get('name','')).strip().encode('utf-8')).hexdigest()

    def _prune_details(self):
        current=time.time()
        valid={key:value for key,value in self.details.items() if isinstance(value,dict)
               and isinstance(value.get('checked_at'),(float,int)) and stamp(value.get('checked_at')) and 0<=current-value['checked_at']<DETAIL_LIFETIME
               and isinstance(value.get('articles'),list) and 1<=len(value['articles'])<=3
               and all(isinstance(row,dict) and isinstance(row.get('published_at'),(float,int)) and stamp(row.get('published_at'))
                       and all(isinstance(row.get(field),str) and len(row[field])<=1200 for field in ('title','text','source','url'))
                       for row in value['articles'])}
        valid=dict(sorted(valid.items(),key=lambda pair:pair[1]['checked_at'],reverse=True)[:MAX_DETAILS])
        changed=valid!=self.details; self.details=valid
        return changed

    def weibo_detail(self,item):
        with self.lock:
            if self._prune_details(): atomic_json(self.data/'weibo-detail-cache.json',self.details)
            return copy.deepcopy(self.details.get(self.detail_key(item)))

    def load_weibo_detail(self,item):
        topic=plain_text(item.get('name'),150)
        query=urllib.parse.urlencode(dict(q=topic,tp='news',sort=0,page=1,size=10,time='w',**{'from':'search_result'}))
        raw=self.request_json('https://search.sina.com.cn/api/news?'+query)
        articles=news_summaries(raw,topic)
        value=dict(title=topic,articles=articles,checked_at=time.time())
        with self.lock:
            self.details[self.detail_key(item)]=value
            self._prune_details()
            atomic_json(self.data/'weibo-detail-cache.json',self.details)
        return copy.deepcopy(value)

    def _read(self,name,default):
        try: return json.loads((self.data/name).read_text(encoding='utf-8'))
        except (ValueError,OSError): return copy.deepcopy(default)

    def _save(self,kind,value):
        with self.lock:
            self.cache[kind]=value
            atomic_json(self.data/'modules-cache.json',self.cache)

    def view(self,config):
        kind=selected(config)
        with self.lock: data=copy.deepcopy(self.cache.get(kind,{}) or {})
        if kind=='usage':
            with self.lock: accounts={name:copy.deepcopy(self.cache.get(name,{}) or {}) for name in ('codex','deepseek')}
            for item in accounts.values():
                checked=stamp(item.get('checked_at'))
                item['stale']=not checked or time.time()-checked>max(900,config.get('settings',{}).get('module_interval',30)*120)
            times=[stamp(item.get('checked_at')) for item in accounts.values() if stamp(item.get('checked_at'))]
            data=dict(accounts=accounts,windows=accounts['codex'].get('windows',[]),balances=accounts['deepseek'].get('balances',[]),
                      checked_at=min(times) if times else None)
        checked=stamp(data.get('checked_at'))
        data.update(kind=kind,title=TITLES.get(kind,TITLES['finance']),refreshing=self.refreshing,
                    stale=not checked or time.time()-checked>max(900,config.get('settings',{}).get('module_interval',30)*120))
        if kind in ('news','weibo'):
            data['items']=[item for item in data.get('items',[]) if stamp(item.get('time')) and time.time()-item['time']<48*3600][:300]
        if kind=='photo': data['photo']=config.get('settings',{}).get('panel_photo','')
        return data

    def prune(self):
        with self.lock:
            changed=False
            for kind in ('news','weibo'):
                value=self.cache.get(kind)
                if not isinstance(value,dict): continue
                old=value.get('items',[])
                current=[item for item in old if stamp(item.get('time')) and time.time()-item['time']<48*3600][:300]
                if current!=old: value['items']=current; changed=True
            if changed: atomic_json(self.data/'modules-cache.json',self.cache)
            if self._prune_details():
                atomic_json(self.data/'weibo-detail-cache.json',self.details); changed=True
            return changed

    def credentials(self):
        with self.lock:
            value=self._read('integrations.json',{})
            return value if isinstance(value,dict) else {}

    def save_key(self,key,remove=False):
        key=str(key).strip()
        if not remove and (not key or len(key)>256 or not re.fullmatch(r'[A-Za-z0-9_.-]+',key)):
            raise ValueError('请填写有效的 DeepSeek API Key。')
        with self.lock:
            value=self.credentials()
            if remove: value.pop('deepseek_key',None)
            else: value['deepseek_key']=key
            atomic_json(self.data/'integrations.json',value)
            try: os.chmod(self.data/'integrations.json',0o600)
            except OSError: pass
            self.cache.pop('deepseek',None)
            atomic_json(self.data/'modules-cache.json',self.cache)

    def request_json(self,url,headers=None):
        context=ssl.create_default_context(cafile=str(self.root/'assets/cacert.pem'))
        request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Accept':'application/json',**(headers or {})})
        # A redirected API request must never forward a bearer token to another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs): return None
        opener=urllib.request.build_opener(urllib.request.HTTPSHandler(context=context),NoRedirect())
        with opener.open(request,timeout=18) as response:
            payload=response.read(MAX_REMOTE+1)
        if len(payload)>MAX_REMOTE: raise ValueError('数据过大。')
        return json.loads(payload.decode('utf-8'))

    def import_codex(self,payload):
        windows=codex_windows(payload)
        self._save('codex',dict(windows=windows,checked_at=time.time(),error='',source='Codex app-server'))

    def refresh(self,kind,app):
        if kind not in ('news','weibo','deepseek'): return
        with self.lock: self.refreshing=True
        try:
            if kind=='news':
                items=app.fetch_news(ai_only=False)
                import renderer as ui
                with self.lock: old=self.cache.get(kind,{}).get('items',[])
                value=dict(items=ui.merge_news_history(old,items),source='新闻快讯')
            elif kind=='weibo':
                raw=self.request_json('https://weibo.com/ajax/side/hotSearch',{
                    'User-Agent':app.BROWSER_UA if hasattr(app,'BROWSER_UA') else 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36',
                    'Referer':'https://weibo.com/'})
                rows=(raw.get('data') or {}).get('realtime') or []
                items=[]
                for item in rows[:50]:
                    if not isinstance(item,dict) or item.get('is_ad'): continue
                    title=str(item.get('word') or item.get('note') or '').strip()[:150]
                    if not title: continue
                    items.append(dict(name=title,content=title+'\n热度：'+str(item.get('num') or '—')[:30],
                        url='https://s.weibo.com/weibo?q='+urllib.parse.quote('#'+title+'#'),time=int(time.time()),source='微博热搜',rank=len(items)+1))
                if not items: raise ValueError('微博暂未返回公开榜单，请稍后重试。')
                value=dict(items=items,source='微博')
            else:
                key=self.credentials().get('deepseek_key')
                if not key: raise ValueError('请在手机后台配置 DeepSeek API Key。')
                raw=self.request_json('https://api.deepseek.com/user/balance',{'Authorization':'Bearer '+key})
                rows=[]
                for item in (raw.get('balance_infos') or [])[:4]:
                    if item.get('currency') not in ('CNY','USD'): continue
                    rows.append(dict(currency=item['currency'],total=money(item.get('total_balance')),
                        granted=money(item.get('granted_balance')),topped_up=money(item.get('topped_up_balance'))))
                if not rows: raise ValueError('账户没有返回余额。')
                value=dict(balances=rows,available=raw.get('is_available') is True,source='DeepSeek 官方 API')
                with self.lock:
                    if self.credentials().get('deepseek_key')!=key: return
                    self._save(kind,dict(value,checked_at=time.time(),error=''))
                return
            self._save(kind,dict(value,checked_at=time.time(),error=''))
        except Exception as error:
            # Never persist request URLs, headers or exception text containing credentials.
            message=str(error) if isinstance(error,ValueError) and not isinstance(error,json.JSONDecodeError) else '连接暂不可用，请稍后重试。'
            with self.lock: old=copy.deepcopy(self.cache.get(kind,{}) or {})
            old.update(error=message[:120]); self._save(kind,old)
        finally:
            with self.lock: self.refreshing=False

    def photos(self):
        with self.lock:
            value=self._read('photos.json',[])
            return [row for row in value if isinstance(row,dict) and re.fullmatch(r'[a-f0-9]{24}',str(row.get('id','')))][:MAX_PHOTOS] if isinstance(value,list) else []

    def photo_path(self,identity):
        if not re.fullmatch(r'[a-f0-9]{24}',str(identity)): raise ValueError('图片不存在。')
        if not any(row['id']==identity for row in self.photos()): raise ValueError('图片不存在。')
        return self.data/'photos'/(identity+'.png')

    def add_photo(self,payload,name):
        if not 0<len(payload)<=MAX_UPLOAD: raise ValueError('图片须小于 6 MB。')
        with self.lock:
            rows=self.photos()
            if len(rows)>=MAX_PHOTOS: raise ValueError('最多保存 12 张图片，请先删除不用的图片。')
            try:
                with Image.open(io.BytesIO(payload)) as source:
                    if source.format not in ('PNG','JPEG','WEBP') or source.width*source.height>MAX_PIXELS:
                        raise ValueError('请用 JPG、PNG 或 WebP 图片，分辨率不超过 200 万像素；手机上传会自动缩小。')
                    source.seek(0)
                    photo=ImageOps.exif_transpose(source).convert('RGBA')
                    background=Image.new('RGBA',photo.size,'white'); background.alpha_composite(photo)
                    photo=ImageOps.autocontrast(background.convert('L'))
                    photo.thumbnail((600,800),Image.Resampling.LANCZOS if hasattr(Image,'Resampling') else Image.LANCZOS)
                    output=io.BytesIO(); photo.save(output,'PNG'); normalized=output.getvalue()
            except (OSError,Image.DecompressionBombError,Image.DecompressionBombWarning):
                raise ValueError('图片无法读取，请换一张 JPG 或 PNG。') from None
            identity=hashlib.sha256(normalized).hexdigest()[:24]
            if any(row['id']==identity for row in rows): return identity
            folder=self.data/'photos'; folder.mkdir(exist_ok=True)
            target=folder/(identity+'.png'); temporary=target.with_suffix('.tmp')
            with temporary.open('wb') as stream: stream.write(normalized); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary,target)
            label=Path(str(name).replace('\\','/')).stem.strip()[:60] or '我的图片'
            rows.append(dict(id=identity,name=label,added_at=time.time(),width=photo.width,height=photo.height))
            atomic_json(self.data/'photos.json',rows)
            return identity

    def delete_photo(self,identity,config):
        with self.lock:
            path=self.photo_path(identity)
            settings=config.get('settings',{})
            if identity in (settings.get('panel_photo'),settings.get('standby_photo')):
                raise ValueError('这张图片正在使用，请先更换展示图片再删除。')
            atomic_json(self.data/'photos.json',[row for row in self.photos() if row['id']!=identity])
            path.unlink(missing_ok=True)

def paint_panel(image,panel,current):
    import renderer as ui
    d=ui.ImageDraw.Draw(image); kind=panel['kind']
    d.rectangle((30,340,570,548),fill=255)
    d.text((30,346),panel['title'],font=ui.font(23,True),fill=0,anchor='lt')
    ui.action_icon(d,'settings',482,360,26)
    ui.action_icon(d,'refresh' if kind!='photo' else 'next',552,360,26)
    checked=stamp(panel.get('checked_at'))
    note=('更新中' if panel.get('refreshing') else '离线缓存' if panel.get('error') else '上次同步' if panel.get('stale') else '更新于')
    if kind=='photo':
        path=panel.get('photo_path')
        if path:
            try:
                with Image.open(path) as source:
                    thumb=ImageOps.contain(source.convert('L'),(540,156))
                image.paste(thumb,(30+(540-thumb.width)//2,384+(156-thumb.height)//2)); return
            except OSError: pass
        lines=['还没有选择图片','扫码进入手机后台上传并选用']
    elif kind=='weibo':
        items=panel.get('items',[])
        if items:
            d.line((300,386,300,516),fill=210,width=1)
            for i,item in enumerate(items[:10]):
                x=30+(i//5)*282; y=386+(i%5)*27
                d.text((x,y),str(i+1),font=ui.font(15,True),fill=0,anchor='lt')
                d.text((x+29,y),ui.fit_text(d,item['name'],16,229),font=ui.font(16),fill=0,anchor='lt')
            lines=[]
        else: lines=[panel.get('error') or '等待首次更新','可在手机后台配置或重试']
    elif kind=='news':
        items=panel.get('items',[])
        if items:
            for i,item in enumerate(items[:5]):
                d.text((30,386+i*27),str(i+1),font=ui.font(16,True),fill=0,anchor='lt')
                d.text((58,386+i*27),ui.fit_text(d,item['name'],17,505),font=ui.font(17),fill=0,anchor='lt')
            lines=[]
        else: lines=[panel.get('error') or '等待首次更新','可在手机后台配置或重试']
    elif kind=='usage':
        accounts=panel.get('accounts',{})
        ds=accounts.get('deepseek',{}); codex=accounts.get('codex',{})
        d.line((299,386,299,539),fill=195,width=1)
        d.text((30,384),'DeepSeek',font=ui.font(16,True),fill=0,anchor='lt')
        d.text((316,384),'Codex',font=ui.font(16,True),fill=0,anchor='lt')
        balances=ds.get('balances',[])
        if balances:
            row=balances[0]; unit='¥' if row['currency']=='CNY' else '$'
            d.text((30,410),ui.fit_text(d,unit+row['total'],34,254,True),font=ui.font(34,True,True),fill=0,anchor='lt')
            d.text((30,454),'可用余额 · '+row['currency'],font=ui.font(15),fill=45,anchor='lt')
            d.text((30,480),'赠送 '+row['granted'],font=ui.font(14),fill=75,anchor='lt')
            d.text((30,502),'充值 '+row['topped_up'],font=ui.font(14),fill=75,anchor='lt')
        else:
            d.text((30,423),'尚未连接',font=ui.font(24,True),fill=75,anchor='lt')
            d.text((30,465),'手机后台配置 API Key',font=ui.font(14),fill=80,anchor='lt')
        windows=codex.get('windows',[])
        for i,row in enumerate(windows[:2]):
            y=413+i*54
            label=row['period']+' · 剩余 '+f"{row['remaining']:g}%"
            d.text((316,y),ui.fit_text(d,label,16,254,True),font=ui.font(16,True),fill=0,anchor='lt')
            d.rectangle((316,y+25,570,y+33),outline=0,width=1)
            end=316+round(254*min(100,max(0,row['remaining']))/100)
            if end>316: d.rectangle((316,y+25,end,y+33),fill=0)
            d.text((570,y+37),'已用 '+f"{row['used']:g}%",font=ui.font(11),fill=80,anchor='rt')
        if not windows:
            d.text((316,423),'等待同步',font=ui.font(24,True),fill=75,anchor='lt')
            d.text((316,465),'在电脑运行额度同步助手',font=ui.font(14),fill=80,anchor='lt')
        for x,item in ((30,ds),(316,codex)):
            checked_at=stamp(item.get('checked_at'))
            if checked_at:
                note='缓存 ' if item.get('error') or item.get('stale') else '更新 '
                d.text((x,533),note+datetime.fromtimestamp(checked_at,ui.TZ).strftime('%m-%d %H:%M'),font=ui.font(11),fill=80,anchor='lt')
        return
    elif kind=='codex':
        rows=panel.get('windows',[])
        for i,row in enumerate(rows[:2]):
            y=387+i*66
            label=row['period']+' · 剩余 '+f"{row['remaining']:g}%"+' / 已用 '+f"{row['used']:g}%"
            if len(rows)>2: label=row['label']+' '+label
            d.text((30,y),ui.fit_text(d,label,17,540),font=ui.font(17,True),fill=0,anchor='lt')
            d.rectangle((30,y+30,570,y+43),outline=0,width=1)
            end=30+int(540*row['remaining']/100)
            if end>30: d.rectangle((30,y+30,end,y+43),fill=0)
        lines=[] if rows else ['等待同步 Codex 额度','在登录 Codex 的电脑上运行同步助手']
    else:
        rows=panel.get('balances',[])
        if rows:
            row=rows[0]; unit='¥' if row['currency']=='CNY' else '$'
            d.text((30,391),unit+row['total'],font=ui.font(43,True,True),fill=0,anchor='lt')
            d.text((32,448),'API 可用余额 · '+row['currency'],font=ui.font(17),fill=0,anchor='lt')
            d.text((32,481),'赠送 '+row['granted']+' · 充值 '+row['topped_up'],font=ui.font(16),fill=70,anchor='lt')
        lines=[] if rows else [panel.get('error') or '尚未连接 DeepSeek','扫码进入手机后台配置 API Key']
    for i,line in enumerate(lines[:2]): d.text((30,402+i*38),ui.fit_text(d,line,19 if i==0 else 16,540),font=ui.font(19 if i==0 else 16),fill=0 if i==0 else 90,anchor='lt')
    if checked:
        when=datetime.fromtimestamp(checked,ui.TZ).strftime('%m-%d %H:%M')
        d.text((570,532),note+' '+when,font=ui.font(12),fill=80,anchor='rt')

def detail_image(panel,page=0):
    import renderer as ui
    image,d=ui.section_frame(panel['title'],'手机后台可切换模块与管理数据')
    kind=panel['kind']
    lines=[]
    if kind=='usage':
        accounts=panel.get('accounts',{})
        ds=accounts.get('deepseek',{}); codex=accounts.get('codex',{})
        lines=['DeepSeek · API 余额']
        for row in ds.get('balances',[]):
            lines.extend([row['currency']+' '+row['total'],'赠送 '+row['granted']+' · 充值 '+row['topped_up']])
        if not ds.get('balances'): lines.append('尚未连接，请在手机后台配置 API Key。')
        if ds.get('error'): lines.append(ds['error'])
        if stamp(ds.get('checked_at')): lines.append('更新 '+datetime.fromtimestamp(ds['checked_at'],ui.TZ).strftime('%m-%d %H:%M'))
        lines+=['','Codex · 用量与余量']
        for row in codex.get('windows',[]):
            lines.extend([row['label']+' · '+row['period'],f"已用 {row['used']:g}% · 剩余 {row['remaining']:g}%"])
            if stamp(row.get('resets_at')): lines.append('重置 '+datetime.fromtimestamp(row['resets_at'],ui.TZ).strftime('%m-%d %H:%M'))
        if not codex.get('windows'): lines.append('等待电脑额度同步助手传入。')
        if stamp(codex.get('checked_at')): lines.append('同步 '+datetime.fromtimestamp(codex['checked_at'],ui.TZ).strftime('%m-%d %H:%M'))
        lines+=['','余额与账户额度分别计量。']
    elif kind=='codex':
        for row in panel.get('windows',[]):
            lines.extend([row['label']+' · '+row['period'],f"已用 {row['used']:g}% · 剩余 {row['remaining']:g}%"])
            if row.get('resets_at'): lines.append('重置 '+datetime.fromtimestamp(row['resets_at'],ui.TZ).strftime('%m-%d %H:%M'))
            lines.append('')
        if not lines: lines=['还没有收到额度。','请在登录 Codex 的电脑上运行同步助手。']
    elif kind=='deepseek':
        for row in panel.get('balances',[]): lines.extend([row['currency']+' API 余额',row['total'],'赠送 '+row['granted'],'充值 '+row['topped_up'],''])
        lines+=['此接口提供余额，不提供已消耗 Token 总数。']
    elif kind=='photo':
        if panel.get('photo_path'):
            try:
                with Image.open(panel['photo_path']) as source: photo=ImageOps.contain(source.convert('L'),(540,545))
                image.paste(photo,(30+(540-photo.width)//2,120+(545-photo.height)//2))
            except OSError: lines=['图片暂不可用，请在手机后台重新选择。']
        else: lines=['请在手机后台上传并选择图片。']
    wrapped=[]
    for line in lines: wrapped.extend(ui.wrap_lines(line,21,540) or [''])
    total=max(1,math.ceil(len(wrapped)/14)); page=max(0,min(page,total-1))
    for i,line in enumerate(wrapped[page*14:(page+1)*14]): d.text((30,125+i*34),line,font=ui.font(21),fill=0,anchor='lt')
    if panel.get('error'): d.text((30,622),ui.fit_text(d,panel['error'],16,540),font=ui.font(16),fill=60,anchor='lt')
    checked=stamp(panel.get('checked_at'))
    if checked and kind!='usage': d.text((30,654),'数据时间 '+datetime.fromtimestamp(checked,ui.TZ).strftime('%m-%d %H:%M'),font=ui.font(16),fill=75,anchor='lt')
    ui.pager(d,page,total)
    for x,name in ((30,'back'),(214,'phone'),(398,'refresh')):
        if kind=='photo' and x==398: continue
        ui.icon_button(d,(x,728,x+172,777),name)
    return image,total
