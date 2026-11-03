"""Offline agenda, direct weather, local editing and native display on KT2."""
import argparse, copy, errno, hashlib, hmac, io, json, logging, math, os, queue, re, secrets
import signal, socket, ssl, struct, subprocess, sys, threading, time
import urllib.parse, urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from renderer import ROOT, TZ, now, render, render_news_detail, render_event_detail, future_events, paginate, news_items, font, Image, ImageDraw
from store import Store, Conflict, atomic_json, CHOICE_SETTINGS, initialize_data
import pages
import renderer as ui
import native_input
import updater
import modules
import phone
from device_control import WiFiManager, PowerKeyListener, KindlePower, FrameworkBridge, DeviceControlError

sys.path.insert(0,str(ROOT/'vendor'))
LOG=logging.getLogger('kindle')
PORT=8766
BUILD='2.0.0'
STANDBY_BATTERY_SECONDS=30
BROWSER_UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
AI_KW=re.compile(r'\bAI\b|AIGC|AGI\b|人工智能|大模型|OpenAI|DeepSeek|ChatGPT|智能体|算力|英伟达|NVIDIA|机器人|Anthropic|黄仁勋|具身智能|商汤|智谱|月之暗面|Kimi|豆包|通义|文心|Sora|Copilot',re.I)

class TouchTap:
    """Emit once on finger release, never repeatedly while holding a button."""
    def __init__(self):
        self.x=self.y=None
        self.slot=0
        self.tracking={}
        self.button=None
        self.pressure=None
        self.changed=False
        self.type_a=False
        self.down=False
        self.start=None
        self.cancelled=False

    def feed(self,kind,code,value):
        if kind==0 and code==2: self.type_a=True
        if kind==1 and code==330: self.button=bool(value)
        elif kind==3:
            if code==47: self.slot=value
            elif code==57: self.tracking[self.slot]=value
            elif code==24: self.pressure=value
            elif code==0 or (code==53 and self.slot==0): self.x=value; self.changed=True
            elif code==1 or (code==54 and self.slot==0): self.y=value; self.changed=True
        if kind!=0 or code!=0: return None
        contacts=sum(value>=0 for value in self.tracking.values())
        if self.button is not None: active=self.button
        elif self.type_a: active=self.changed
        elif self.tracking: active=contacts>0
        elif self.pressure is not None: active=self.pressure>0
        else: active=self.changed
        self.changed=False
        point=(self.x,self.y) if self.x is not None and self.y is not None else None
        result=None
        if active:
            if not self.down:
                self.start=point
                self.cancelled=False
            if contacts>1: self.cancelled=True
            if point and self.start and max(abs(point[i]-self.start[i]) for i in (0,1))>35:
                self.cancelled=True
        elif self.down and point and not self.cancelled:
            result=point
        self.down=active
        return result

def command(args,timeout=30):
    result=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout)
    return result.returncode,result.stdout.decode('utf-8',errors='replace').strip()

def local_network():
    if os.name=='nt': return '127.0.0.1', '255.0.0.0'
    import fcntl
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        name=struct.pack('256s',b'wlan0')
        address=socket.inet_ntoa(fcntl.ioctl(sock.fileno(),0x8915,name)[20:24])
        mask=socket.inet_ntoa(fcntl.ioctl(sock.fileno(),0x891b,name)[20:24])
        return address,mask

class Device:
    def __init__(self,app):
        self.app=app
        self.gui_stopped=False
        self.framework_bridge=FrameworkBridge(logger=LOG)
        self.firewall=None
        self.last_frame_digest=None
        self.updates_since_full=0
        self.full_refreshes=0
        self.frame_updates=0
        self.last_refresh='none'
        self.standby_clear_pending=False
        self.standby_not_before=0
        command(['lipc-set-prop','com.lab126.powerd','preventScreenSaver','1'])

    def stop_gui(self):
        if self.gui_stopped:
            self.framework_bridge.ensure(); return
        command(['lipc-set-prop','com.lab126.appmgrd','stop','com.lab126.browser'])
        self.gui_stopped=True
        command(['initctl','stop','lab126_gui'])
        time.sleep(3)
        result=command(['initctl','status','lab126_gui'])[1]
        if 'stop/waiting' not in result: raise RuntimeError('Kindle 原界面未停止：'+result)
        self.framework_bridge.ensure()
        # The reader/browser may have drawn since we last owned the screen.
        self.last_frame_digest=None

    def restore_gui(self):
        if self.gui_stopped:
            self.framework_bridge.close()
            command(['initctl','start','lab126_gui'])
            self.gui_stopped=False

    def prepare_standby_refresh(self):
        # Give the OS screensaver transition time to finish before taking
        # ownership of the panel. This delay happens only on entry to standby.
        self.standby_clear_pending=True
        self.standby_not_before=time.monotonic()+1.0
        self.force_full=True

    def cancel_standby_refresh(self):
        self.standby_clear_pending=False
        self.standby_not_before=0

    def display(self,path):
        self.stop_gui()
        digest=hashlib.sha256(Path(path).read_bytes()).digest()
        clear=self.standby_clear_pending and self.app.mode=='screensaver'
        if digest==self.last_frame_digest and not getattr(self,'force_full',False) and not clear: return False
        if clear:
            delay=max(0,self.standby_not_before-time.monotonic())
            if delay: time.sleep(delay)
            # A separate all-white full refresh removes the system wallpaper
            # before the dashboard wallpaper's own full refresh is submitted.
            blank=Path(path).with_name('kindle-standalone-clear.png')
            Image.new('L',(600,800),255).save(str(blank),'PNG')
            code,output=command(['eips','-f','-g',str(blank)])
            if code: raise RuntimeError('Standby clear failed: '+output)
            self.last_frame_digest=None
            self.force_full=True
            LOG.info('STANDBY_CLEAR_OK')
            time.sleep(.25)
        interval=self.app.preferences().get('full_refresh',10)
        full=getattr(self,'force_full',False) or self.last_frame_digest is None or self.updates_since_full+1>=interval
        args=['eips']+(['-f'] if full else [])+['-g',str(path)]
        code,output=command(args)
        if code: raise RuntimeError('Screen draw failed: '+output)
        # Only successful, changed frames count; failed draws must remain due.
        self.last_frame_digest=digest
        self.force_full=False
        self.frame_updates+=1
        self.updates_since_full=0 if full else self.updates_since_full+1
        self.full_refreshes+=int(full)
        self.last_refresh='full' if full else 'normal'
        if clear: self.cancel_standby_refresh()
        LOG.info('SCREEN_REFRESH kind=%s changes_since_full=%s total=%s',self.last_refresh,self.updates_since_full,self.frame_updates)
        return True

    def refresh_status(self):
        return dict(interval=self.app.preferences().get('full_refresh',10),changes_since_full=self.updates_since_full,
                    full_refreshes=self.full_refreshes,frame_updates=self.frame_updates,last=self.last_refresh)

    def editor(self,url):
        self.restore_gui()
        time.sleep(3)
        code,_=command(['lipc-set-prop','com.lab126.appmgrd','start','app://com.lab126.browser'])
        if code: raise RuntimeError('日程编辑器未能打开。')
        time.sleep(2)
        code,_=command(['lipc-set-prop','com.lab126.browser','gotoURL',url])
        if code:
            code,_=command(['lipc-set-prop','com.lab126.appmgrd','start','app://com.lab126.browser#going?url='+url])
            if code: raise RuntimeError('日程编辑器未能打开。')

    def allow_lan(self,address,mask):
        import ipaddress
        network=str(ipaddress.ip_network(address+'/'+mask,strict=False))
        rule=['INPUT','-i','wlan0','-p','tcp','--dport',str(PORT),'-s',network,'-j','ACCEPT']
        if self.firewall==rule: return
        if self.firewall: command(['iptables','-D']+self.firewall)
        code,_=command(['iptables','-I']+rule)
        self.firewall=rule if code==0 else None
        if code: LOG.warning('Could not open the Wi-Fi editing port')

    def close(self):
        # Our editing/timer protection belongs to this dashboard session.
        # Reinstating a stale startup value of 1 would block the native reader.
        command(['lipc-set-prop','com.lab126.powerd','preventScreenSaver','0'])
        self.restore_gui()
        if self.firewall:
            command(['iptables','-D']+self.firewall)
            self.firewall=None

    def touch_loop(self):
        import fcntl,select
        # KT2's touchscreen is event1, as in KOReader's KindleBasic profile.
        fd=os.open('/dev/input/event1',os.O_RDONLY|os.O_NONBLOCK)
        def limits(code,fallback):
            try:
                raw=fcntl.ioctl(fd,0x80000000|(24<<16)|(ord('E')<<8)|(0x40+code),bytes(24))
                values=struct.unpack('6i',raw)
                if values[2]>values[1]: return values[1],values[2]
            except OSError: pass
            return 0,fallback
        xmin,xmax=limits(53,599)
        ymin,ymax=limits(54,799)
        LOG.info('Touch ranges x=%s:%s y=%s:%s',xmin,xmax,ymin,ymax)
        tap=TouchTap()
        last=0
        packet=struct.Struct('<IIHHi')
        try:
            while not self.app.stop.is_set():
                ready,_,_=select.select([fd],[],[],0.5)
                if not ready: continue
                raw=os.read(fd,packet.size*32)
                for offset in range(0,len(raw)-packet.size+1,packet.size):
                    _,_,kind,code,value=packet.unpack_from(raw,offset)
                    point=tap.feed(kind,code,value)
                    if point is not None:
                        x,y=point
                        mode=self.app.mode
                        if mode=='editor': continue
                        self.app.note_activity()
                        px=round((x-xmin)*599/max(1,xmax-xmin))
                        py=round((y-ymin)*799/max(1,ymax-ymin))
                        if time.monotonic()-last<(0.12 if mode=='form' else 0.9): continue
                        last=time.monotonic()
                        action=self.app.tap_action(px,py)
                        if action is None: continue
                        LOG.info('Touch mode=%s x=%s y=%s',mode,px,py)
                        self.app.requests.put(action)
        finally: os.close(fd)

class Pomodoro:
    DURATION=45*60
    MIN_DURATION=5*60
    MAX_DURATION=180*60
    STEP=5*60

    def __init__(self,path):
        self.path=Path(path)
        self.state='idle'; self.duration=self.DURATION; self.remaining=self.duration; self.deadline=0
        try:
            value=json.loads(self.path.read_text(encoding='utf-8'))
            duration=int(value.get('duration',self.DURATION))
            if not self.MIN_DURATION<=duration<=self.MAX_DURATION or duration%self.STEP:
                raise ValueError('Invalid duration')
            self.duration=duration; self.remaining=duration
            state=value.get('state')
            if state=='running':
                left=float(value['until'])-time.time()
                if not math.isfinite(left): raise ValueError('Invalid timer')
                self.remaining=max(0,min(self.duration,left))
                self.state='running' if self.remaining else 'done'
                self.deadline=time.monotonic()+self.remaining
            elif state in ('paused','done'):
                left=float(value.get('remaining',0))
                if not math.isfinite(left): raise ValueError('Invalid timer')
                self.remaining=0 if state=='done' else max(0,min(self.duration,left))
                self.state=state if self.remaining or state=='done' else 'done'
        except (OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError): pass

    def view(self):
        left=max(0,self.deadline-time.monotonic()) if self.state=='running' else self.remaining
        return dict(state='done' if self.state=='running' and left<=0 else self.state,remaining=left,duration=self.duration)

    def save(self):
        view=self.view()
        try: atomic_json(self.path,dict(view,until=time.time()+view['remaining'] if view['state']=='running' else None))
        except OSError: LOG.warning('Timer persistence unavailable; continuing in memory',exc_info=True)

    def start(self):
        self.state='running'; self.remaining=self.duration
        self.deadline=time.monotonic()+self.remaining
        self.save()

    def adjust(self,steps):
        if self.state!='idle': return
        self.duration=max(self.MIN_DURATION,min(self.MAX_DURATION,self.duration+steps*self.STEP))
        self.remaining=self.duration; self.save()

    def reset(self):
        self.state='idle'; self.remaining=self.duration; self.deadline=0; self.save()

    def toggle(self):
        if self.state=='running':
            self.remaining=self.view()['remaining']; self.state='paused' if self.remaining else 'done'
        elif self.state=='paused':
            self.deadline=time.monotonic()+self.remaining; self.state='running'
        elif self.state=='idle': self.start(); return
        self.save()

    def tick(self):
        if self.state=='running' and self.view()['remaining']<=0:
            self.state='done'; self.remaining=0; self.save(); return True
        return False


class Application:
    def __init__(self,root=ROOT,native=False):
        self.root=Path(root)
        self.data=self.root/'data'
        self.store=Store(self.data/'agenda.json')
        self.modules=modules.ModuleManager(self.root)
        self.module_wake=threading.Event()
        self.module_page=0
        self.module_pages=1
        self.system_sleeping=False
        self.system_suspend_pending=False
        self.config=self.store.read()
        self.token=self.config['token']
        self.csrf=secrets.token_hex(24)
        self.lock=threading.RLock()
        self.stop=threading.Event()
        self.weather_wake=threading.Event()
        self.news_wake=threading.Event()
        self.network_wake=threading.Event()
        self.standby=threading.Event()
        self.redraw=threading.Event()
        self.requests=queue.Queue()
        self.mode='dashboard'
        self.last_activity=time.monotonic()
        self.standby_moment=None
        self.standby_restore_wifi=False
        self.power=None
        self.power_available=False
        self.wifi=WiFiManager() if native else None
        self.wifi_lock=threading.Lock()
        self.wifi_busy=False
        self.wifi_message=''
        self.wifi_networks=[]
        self.wifi_status=dict(available=native,enabled=None,connected=False,ssid='',state='',address='',message='')
        self.news_item=None
        self.news_pages=[[]]
        self.detail_lock=threading.Lock()
        self.detail_pending=None
        self.detail_worker=None
        self.detail_generation=0
        self.news_page=0
        self.news_return='dashboard'
        self.news_list_page=0
        self.news_list_total=1
        self.event_item=None
        self.event_confirm=False
        self.event_revision=0
        self.event_message=''
        self.shown_news=[]
        self.module_return='dashboard'
        self.settings_page='content'
        self.photo_target='panel'
        self.photo_page=0
        self.photo_pages=1
        self.shown_photos=[]
        self.photo_message=''
        self.shown_events=[]
        self.shown_revision=0
        self.shown_todos=[]
        self.list_page=0
        self.list_total=1
        self.agenda_day=None
        self.calendar_year=now().year
        self.calendar_month=now().month
        self.todo_message=''
        self.event_return='dashboard'
        self.drawing=False
        self.form=None
        self.form_return='dashboard'
        self.updating=False
        self.restart_update=False
        self.update_nonce=''
        self.update_until=0
        self.address=None
        self.pomodoro=Pomodoro(self.data/'pomodoro.json')
        if self.pomodoro.view()['state']=='running': self.mode='pomodoro'
        self.news_cache_dirty=False
        cache=self.data/'weather-cache.json'
        try: self.state=json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else {}
        except (ValueError,OSError): self.state={}
        if not isinstance(self.state,dict): self.state={}
        self.state.setdefault('error','')
        self.state.setdefault('requested_city',self.config.get('city',''))
        news_cache=self.data/'news-cache.json'
        try:
            if news_cache.exists():
                cached=json.loads(news_cache.read_text(encoding='utf-8'))
                if isinstance(cached,dict):
                    cached_at=ui.parsed_time(cached.get('news_at'))
                    self.state['news']=ui.merge_news_history(cached.get('news'),fallback=cached_at.timestamp() if cached_at else None)
                    self.news_cache_dirty=self.state['news']!=cached.get('news')
                    if cached.get('news_at'): self.state['news_at']=cached['news_at']
        except (ValueError,OSError): pass
        self.state.setdefault('news_error','')
        self.device=Device(self) if native else None

    def preferences(self):
        return self.config.get('settings',{})

    def news_sources_enabled(self,config=None):
        settings=(config or self.config).get('settings',{})
        return settings.get('news_wallstreetcn',True) or settings.get('news_eastmoney',True)

    def note_activity(self):
        self.last_activity=time.monotonic()

    def request_stop(self):
        self.stop.set()
        self.requests.put('quit')
        self.weather_wake.set(); self.news_wake.set(); self.network_wake.set()
        self.module_wake.set()

    def change_setting(self,key,value):
        current=self.store.read()
        updated=self.store.change(current['revision'],'setting',dict(key=key,value=value))
        self.settings_changed(current,updated)

    def fetch_text(self,url,ua='KindleStandaloneDashboard/1.0'):
        context=ssl.create_default_context(cafile=str(self.root/'assets/cacert.pem'))
        request=urllib.request.Request(url,headers={'User-Agent':ua})
        with urllib.request.urlopen(request,context=context,timeout=20) as response:
            data=response.read(2*1024*1024+1)
            if len(data)>2*1024*1024: raise ValueError('Response too large')
            return data.decode('utf-8')

    def fetch_json(self,url):
        return json.loads(self.fetch_text(url))

    def city(self,name):
        query=urllib.parse.urlencode(dict(name=name,count=1,language='zh',format='json'))
        values=self.fetch_json('https://geocoding-api.open-meteo.com/v1/search?'+query).get('results',[])
        if not values: raise ValueError('未找到城市，可试用拼音或英文名。')
        return values[0]

    def update_weather(self,relocate=False):
        config=self.store.read()
        city=config.get('city','')
        with self.lock:
            location=copy.deepcopy(self.state.get('location'))
            old_city=self.state.get('requested_city')
        if not location or old_city!=city or (not city and relocate):
            if city:
                location=self.city(city)
                location['source']='手动城市'
            else:
                result=self.fetch_json('https://ipwho.is/')
                if not result.get('success'): raise ValueError('网络定位暂不可用。')
                name=result.get('city') or result.get('region') or '未知城市'
                try:
                    translated=self.city(name)
                    if abs(translated['latitude']-result['latitude'])<0.5 and abs(translated['longitude']-result['longitude'])<0.5: name=translated['name']
                except Exception: pass
                location=dict(name=name,latitude=result['latitude'],longitude=result['longitude'],source='网络定位')
        params=dict(latitude=location['latitude'],longitude=location['longitude'],current='temperature_2m,apparent_temperature,weather_code,relative_humidity_2m,wind_speed_10m',daily='weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max',timezone='Asia/Shanghai',forecast_days=7)
        weather=self.fetch_json('https://api.open-meteo.com/v1/forecast?'+urllib.parse.urlencode(params))
        if 'current' not in weather or 'daily' not in weather: raise ValueError('天气数据不完整。')
        if self.store.read().get('city','')!=city: return
        with self.lock:
            self.state.update(weather=weather,weather_at=now().isoformat(),location=location,error='',requested_city=city)
            atomic_json(self.data/'weather-cache.json',{key:self.state.get(key) for key in ('weather','weather_at','location','error','requested_city')})
        LOG.info('WEATHER_OK direct_from_kindle=%s',int(bool(self.device)))
        self.redraw.set()

    def weather_loop(self):
        count=0
        while not self.stop.is_set():
            if self.standby.is_set():
                self.weather_wake.wait(); self.weather_wake.clear(); continue
            interval=self.preferences().get('weather_interval',15)
            try: self.update_weather(relocate=(count%max(1,1440//interval)==0))
            except Exception as error:
                with self.lock: self.state['error']=type(error).__name__
                LOG.warning('Weather update unavailable: %s',type(error).__name__)
            finally:
                with self.lock: self.state['weather_refreshing']=False
                if not self.standby.is_set(): self.redraw.set()
            count+=1
            self.weather_wake.wait(interval*60)
            self.weather_wake.clear()

    def update_battery(self):
        values={'battery':None,'charging':None}
        for prop,key in (('battLevel','battery'),('isCharging','charging')):
            try:
                code,output=command(['lipc-get-prop','com.lab126.powerd',prop],timeout=2)
                output=output.strip()
                if code==0 and output.isdigit() and (key=='battery' or output in ('0','1')):
                    values[key]=max(0,min(100,int(output))) if key=='battery' else output=='1'
                    values[key+'_at']=now().isoformat()
                elif key=='charging': values[key]=None
            except (OSError,subprocess.TimeoutExpired):
                if key=='charging': values[key]=None
        with self.lock:
            changed=any(self.state.get(k)!=v for k,v in values.items() if not k.endswith('_at'))
            self.state.update(values)
        if changed and self.mode in ('dashboard','screensaver'):
            self.redraw.set()
            if self.mode=='screensaver': self.requests.put('standby_refresh')
        return changed

    def fetch_wallstreetcn(self,ai_only=True):
        data=json.loads(self.fetch_text('https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=100',ua=BROWSER_UA))
        items=[]
        for raw in (data.get('data') or {}).get('items') or []:
            name=(raw.get('title') or raw.get('content_text') or '').strip()
            link=(raw.get('uri') or '').strip()
            if not name or not link or (ai_only and not AI_KW.search(name)): continue
            items.append({'name':name,'content':raw.get('content_text') or name,'url':link,'time':raw.get('display_time') or 0,'source':'华尔街见闻'})
            if len(items)>=50: break
        return items

    def fetch_eastmoney(self,ai_only=True):
        param={'uid':'','keyword':'AI 金融','type':['cmsArticleWebOld'],'client':'web','clientType':'web','clientVersion':'curr','param':{'cmsArticleWebOld':{'searchScope':'default','sort':'time','pageIndex':1,'pageSize':20,'preTag':'','postTag':''}}}
        if not ai_only: param['keyword']='新闻'
        raw_text=self.fetch_text('https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param='+urllib.parse.quote(json.dumps(param,ensure_ascii=False)),ua=BROWSER_UA)
        data=json.loads(raw_text[raw_text.index('(')+1:raw_text.rindex(')')])
        items=[]
        for raw in (data.get('result') or {}).get('cmsArticleWebOld') or []:
            name=(raw.get('title') or '').strip()
            link=(raw.get('url') or '').strip()
            if not name or not link: continue
            try: stamp=int(datetime.strptime(raw.get('date',''),'%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ).timestamp())
            except ValueError: stamp=0
            items.append({'name':name,'url':link,'time':stamp,'source':'东方财富'})
        return items[:50]

    def fetch_news(self,ai_only=True):
        config=self.store.read()
        settings=config.get('settings',{})
        errors=[]
        for key,label,fetcher in (('news_wallstreetcn','华尔街见闻',self.fetch_wallstreetcn),('news_eastmoney','东方财富',self.fetch_eastmoney)):
            if not settings.get(key,True): continue
            try:
                items=fetcher() if ai_only else fetcher(ai_only=False)
                if items: return items
                errors.append(label+'暂无相关资讯')
            except Exception as error:
                errors.append(label+'：'+type(error).__name__)
        if not errors: return []
        raise ValueError('资讯获取失败（'+'，'.join(errors)+'）')

    def update_news(self):
        config=self.store.read()
        if not self.news_sources_enabled(config):
            with self.lock: self.state.update(news_at=None,news_error='',news_refreshing=False)
            return
        items=news_items(self.fetch_news())
        with self.lock:
            items=ui.merge_news_history(self.state.get('news'),items)
            self.state.update(news=items,news_at=now().isoformat(),news_error='')
            atomic_json(self.data/'news-cache.json',{'news':items,'news_at':self.state['news_at']})
            self.news_cache_dirty=False
        LOG.info('NEWS_OK count=%s',len(items))
        self.redraw.set()

    def prune_news_cache(self):
        if self.modules.prune(): self.redraw.set()
        with self.lock:
            current=ui.merge_news_history(self.state.get('news'))
            changed=current!=self.state.get('news',[])
            if changed or self.news_cache_dirty:
                atomic_json(self.data/'news-cache.json',dict(news=current,news_at=self.state.get('news_at')))
                self.state['news']=current; self.news_cache_dirty=False
        if changed and self.mode in ('dashboard','news_history'): self.redraw.set()

    def news_loop(self):
        while not self.stop.is_set():
            if self.standby.is_set() or modules.selected(self.store.read())!='finance':
                self.news_wake.wait(); self.news_wake.clear(); continue
            try: self.update_news()
            except Exception as error:
                with self.lock: self.state['news_error']='资讯更新失败：'+type(error).__name__
                LOG.warning('News update unavailable: %s',type(error).__name__)
            finally:
                with self.lock: self.state['news_refreshing']=False
                if not self.standby.is_set(): self.redraw.set()
            self.news_wake.wait(3600)
            self.news_wake.clear()

    def module_loop(self):
        while not self.stop.is_set():
            config=self.store.read(); kind=modules.selected(config)
            if self.standby.is_set() or kind not in ('news','weibo','usage'):
                self.module_wake.wait(); self.module_wake.clear(); continue
            if kind!='usage' or self.modules.credentials().get('deepseek_key'):
                self.modules.refresh('deepseek' if kind=='usage' else kind,self)
            if not self.standby.is_set(): self.redraw.set()
            self.module_wake.wait(config['settings'].get('module_interval',30)*60)
            self.module_wake.clear()

    def panel_view(self,config=None):
        config=config or self.store.read()
        panel=self.modules.view(config)
        if panel['kind']=='finance':
            checked=ui.parsed_time(self.state.get('news_at'))
            panel.update(checked_at=checked.timestamp() if checked else None,error=self.state.get('news_error',''))
        if panel['kind']=='photo' and panel.get('photo'):
            try: panel['photo_path']=str(self.modules.photo_path(panel['photo']))
            except ValueError: pass
        return panel

    def open_weibo_detail(self,item,force=False):
        item=copy.deepcopy(item)
        cached=self.modules.weibo_detail(item)
        loading=force or not cached or time.time()-cached['checked_at']>=modules.DETAIL_FRESH
        item['content']=modules.detail_body(item,cached,loading=loading)
        item['detail_loading']=loading
        self.news_item=item
        self.news_pages=paginate(item['content'],size=24,per_page=16)
        self.news_page=0; self.mode='news'; self.redraw.set()
        with self.detail_lock:
            self.detail_generation+=1
            self.detail_pending=(self.detail_generation,item) if loading else None
            if loading and not self.detail_worker:
                self.detail_worker=threading.Thread(target=self.detail_loop,daemon=True,name='weibo-detail')
                self.detail_worker.start()

    def detail_loop(self):
        while True:
            with self.detail_lock:
                task=self.detail_pending; self.detail_pending=None
                if not task or self.stop.is_set(): self.detail_worker=None; return
            generation,item=task
            if self.standby.is_set() or self.updating: continue
            detail=None; error=''
            try: detail=self.modules.load_weibo_detail(item)
            except Exception as failure:
                try: detail=self.modules.weibo_detail(item)
                except (OSError,ValueError): pass
                error=str(failure) if isinstance(failure,ValueError) and not isinstance(failure,json.JSONDecodeError) else '新闻来源暂不可用，请稍后重试。'
                LOG.warning('Weibo related-news detail unavailable: %s',type(failure).__name__)
            item.update(content=modules.detail_body(item,detail,error=error),detail_loading=False)
            if not self.stop.is_set(): self.requests.put(('weibo_detail_result',generation,item))

    def protected_mode(self):
        return self.updating or self.mode in ('form','editor') or self.pomodoro.view()['state']=='running'

    def maintain_power(self):
        if self.power and not self.stop.is_set(): self.power.maintain(self.protected_mode())

    def record_power(self,reason):
        if not self.device or not self.power: return
        try:
            sample=self.power.sample(reason)
            sample.update(at=now().isoformat(),build=BUILD,mode=self.mode,
                          idle_minutes=self.preferences().get('idle_minutes'),
                          radio=self.wifi.radio_state() if self.wifi else None)
            path=self.data/'power-history.jsonl'
            if path.exists() and path.stat().st_size>=128*1024:
                os.replace(path,path.with_name(path.name+'.1'))
            with path.open('a',encoding='utf-8') as output:
                output.write(json.dumps(sample,ensure_ascii=False)+'\n')
            LOG.info('POWER_SAMPLE reason=%s mode=%s state=%s battery=%s charging=%s',
                     reason,self.mode,sample.get('system_state'),sample.get('battery'),sample.get('charging'))
        except (OSError,ValueError,DeviceControlError):
            LOG.warning('POWER_SAMPLE_UNAVAILABLE reason=%s',reason)

    def capture_system_power_log(self):
        # A bounded, read-only copy at launch and exit. This never wakes the
        # device to collect logs, and exports neither agenda nor credentials.
        path=Path('/var/log/messages')
        try:
            with path.open('rb') as source:
                source.seek(0,os.SEEK_END); size=source.tell()
                source.seek(max(0,size-256*1024))
                lines=source.read(256*1024).decode('utf-8',errors='replace').splitlines()
            lines=[line for line in lines if re.search(r'\b(powerd|suspend|resume|wakeup)\b',line,re.I)]
            (self.data/'power-system.log').write_text('\n'.join(lines[-200:])+'\n',encoding='utf-8')
        except OSError: pass

    def settings_changed(self,old,new):
        self.config=new; self.note_activity()
        before=old.get('settings',{}); after=new['settings']
        if old.get('city')!=new.get('city') or before.get('weather_interval')!=after['weather_interval']: self.weather_wake.set()
        if any(before.get(k)!=after.get(k) for k in ('panel_module','news_wallstreetcn','news_eastmoney')): self.news_wake.set()
        if any(before.get(k)!=after.get(k) for k in ('panel_module','module_interval','news_wallstreetcn','news_eastmoney')): self.module_wake.set()
        if self.mode in ('module','news_history','news','module_picker'): self.requests.put('dashboard')
        self.redraw.set()
        if self.standby.is_set(): self.requests.put('standby_refresh')

    def snapshot(self):
        config=self.store.read()
        self.config=config
        with self.lock: state=copy.deepcopy(self.state)
        state['news']=ui.enabled_news(config,ui.merge_news_history(state.get('news')))
        state['pomodoro']=self.pomodoro.view()
        state['panel']=self.panel_view(config)
        return config,state

    def connection(self):
        address,mask=local_network()
        if self.device: self.device.allow_lan(address,mask)
        if address!=self.address:
            self.address=address
            atomic_json(self.data/'connection.json',dict(address=address,port=PORT,url=self.url(address)))
        return address

    def url(self,address='127.0.0.1'):
        return 'http://'+address+':'+str(PORT)+'/'+self.token+'/'

    def pair_image(self):
        import qrcode
        frame=Image.new('L',(600,800),255)
        draw=ImageDraw.Draw(frame)
        draw.text((30,30),'手机管理面板',font=font(30,True),fill=0,anchor='lt')
        try: address=self.connection()
        except Exception: address=None
        if address:
            qr=qrcode.QRCode(box_size=7,border=3,error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(self.url(address)+'manage'); qr.make(fit=True)
            picture=qr.make_image(fill_color='black',back_color='white').convert('L')
            frame.paste(picture,((600-picture.width)//2,135))
            status,_,_,_=self.wifi_view()
            network=status.get('ssid') or '正在读取网络名称'
            draw.text((30,555),ui.fit_text(draw,'Wi-Fi：'+network,20,540),font=font(20,True),fill=0,anchor='lt')
            draw.text((30,586),'手机连同一 Wi-Fi，扫码打开。',font=font(18),fill=0,anchor='lt')
            draw.text((30,612),'设备地址：'+address+':'+str(PORT),font=font(18),fill=0,anchor='lt')
        else:
            draw.text((30,200),'请先让 Kindle 连接 Wi-Fi。',font=font(25),fill=0,anchor='lt')
        draw.text((30,653),'正在更新，请稍候…' if self.updating else '此页开启时，电脑可无线更新看板。',font=font(20),fill=0,anchor='lt')
        draw.text((30,689),'完成后自动返回看板。' if self.updating else '离开此页即关闭更新入口。',font=font(16),fill=70,anchor='lt')
        ui.icon_button(draw,(30,728,570,777),'back')
        return frame

    def wifi_view(self):
        with self.lock:
            return (copy.deepcopy(self.wifi_status),copy.deepcopy(self.wifi_networks),
                    self.wifi_busy,self.wifi_message)

    def start_wifi_operation(self,operation,*args):
        if not self.wifi:
            with self.lock: self.wifi_message='电脑预览不控制本机 Wi-Fi。'
            self.redraw.set(); return
        with self.lock:
            if self.wifi_busy: return
            self.wifi_busy=True
            self.wifi_message={'scan':'正在扫描附近网络…','toggle':'正在切换 Wi-Fi…','connect':'正在连接，请稍候…','status':'正在读取 Wi-Fi 状态…'}.get(operation,'请稍候…')
        self.redraw.set()
        def work():
            status=None; networks=None; message=''
            try:
                with self.wifi_lock:
                    if operation=='scan':
                        status=self.wifi.status()
                        if status.get('enabled') is False: raise DeviceControlError('Wi-Fi 已关闭，请先开启。')
                        networks=self.wifi.scan()
                        status=self.wifi.status()
                        message='扫描完成，找到 '+str(len(networks))+' 个网络。'
                    elif operation=='toggle':
                        enabled=self.wifi.is_enabled()
                        if enabled is None: raise DeviceControlError('无法确认 Wi-Fi 开关状态。')
                        status=self.wifi.set_enabled(not enabled)
                        networks=[] if enabled else self.wifi_networks
                        message='Wi-Fi 已关闭。' if enabled else 'Wi-Fi 已开启，可扫描网络。'
                    elif operation=='connect':
                        status=self.wifi.connect(*args)
                        message='已连接 '+(status.get('ssid') or args[0])+'。'
                    else:
                        status=self.wifi.status(); message='Wi-Fi 状态已更新。'
            except DeviceControlError as error:
                message=str(error)
            except Exception as error:
                LOG.warning('Wi-Fi operation failed: %s',type(error).__name__)
                message='Wi-Fi 操作未完成（'+type(error).__name__+'），请重试。'
            with self.lock:
                if status is not None: self.wifi_status=status
                if networks is not None: self.wifi_networks=copy.deepcopy(networks)
                self.wifi_busy=False; self.wifi_message=message
            self.requests.put('wifi_changed')
        threading.Thread(target=work,daemon=True).start()

    def restore_standby_wifi(self,background=True):
        with self.lock:
            if not self.standby_restore_wifi: return
        if not self.wifi: return
        def work():
            try:
                with self.wifi_lock:
                    # A queued wake may be followed immediately by sleep.
                    # Keep the restore request for the next genuine wake;
                    # never switch the radio on after a new sleep has begun.
                    if self.standby.is_set() and not self.stop.is_set(): return
                    with self.lock:
                        if not self.standby_restore_wifi: return
                    status=self.wifi.set_enabled(True)
                    with self.lock:
                        self.standby_restore_wifi=False
                        self.wifi_status=status
                        self.wifi_message='Wi-Fi 已恢复。'
            except Exception as error:
                LOG.warning('Wi-Fi resume failed: %s',type(error).__name__)
                with self.lock: self.wifi_message='Wi-Fi 尚未恢复，请在设置中手动开启。'
            if self.standby.is_set(): return
            self.weather_wake.set(); self.news_wake.set(); self.network_wake.set()
            self.requests.put('wifi_changed')
        if background: threading.Thread(target=work,daemon=True).start()
        else: work()

    def enter_standby(self,system=False):
        if self.mode=='screensaver' or self.updating: return
        if self.pomodoro.view()['state']=='running':
            self.mode='pomodoro'; self.redraw.set(); return
        if self.mode=='form' and self.form:
            self.form.message='请先保存或取消，再进入待机。'
            self.redraw.set(); return
        self.note_activity()
        if self.device: self.update_battery()
        self.mode='screensaver'; self.standby_moment=now(); self.standby.set()
        self.system_sleeping=system
        self.system_suspend_pending=not system and bool(self.power and self.power.backend=='powerd')
        self.weather_wake.set(); self.news_wake.set(); self.network_wake.set()
        self.module_wake.set()
        if self.device: self.device.force_full=True
        if self.device: self.device.prepare_standby_refresh()
        if self.wifi and self.preferences().get('standby_wifi',True) and not self.wifi_busy:
            try:
                with self.wifi_lock:
                    if self.wifi.is_enabled():
                        status=self.wifi.set_enabled(False)
                        self.standby_restore_wifi=True
                        with self.lock: self.wifi_status=status
            except Exception as error:
                LOG.warning('Could not disable Wi-Fi for standby: %s',type(error).__name__)
        self.record_power('standby_system' if system else 'standby_request')
        self.redraw.set()

    def leave_standby(self,system=False):
        if self.mode!='screensaver': return
        if self.system_sleeping and not system and self.power and not self.power.wake():
            LOG.warning('POWER_WAKE_REQUEST_FAILED; restoring dashboard after user input')
        self.system_sleeping=False; self.system_suspend_pending=False
        self.mode='dashboard'; self.standby_moment=None; self.standby.clear()
        self.note_activity()
        if self.device:
            self.device.force_full=True
            self.device.cancel_standby_refresh()
        self.redraw.set()
        self.record_power('wake_system' if system else 'wake_touch')
        self.restore_standby_wifi()
        self.weather_wake.set(); self.news_wake.set(); self.network_wake.set()
        self.module_wake.set()
        if self.device: self.update_battery()

    def toggle_standby(self):
        if self.mode=='screensaver': self.leave_standby()
        else: self.enter_standby()

    def maybe_standby(self):
        if self.mode in ('screensaver','editor','form') or self.updating: return
        if self.pomodoro.view()['state']=='running': return
        minutes=self.preferences().get('idle_minutes',15)
        if minutes and time.monotonic()-self.last_activity>=minutes*60:
            self.enter_standby()

    def tap_action(self,x,y):
        if self.drawing or self.updating or self.mode=='editor': return None
        mode=self.mode
        if mode=='screensaver': return 'power_toggle'
        if mode=='form':
            action=native_input.tap(self.form,x,y)
            return ('form',action) if action else None
        if mode=='dashboard':
            if 30<=x<368 and 25<=y<130: return 'calendar_today'
            if 368<=x<570 and 55<=y<130: return 'pomodoro'
            if ui.inside(x,y,ui.MAIN_PHONE): return 'pair'
            if ui.inside(x,y,ui.MAIN_SETTINGS): return 'settings'
            if ui.inside(x,y,ui.MAIN_EXIT): return 'quit'
            if 30<=x<290 and 142<=y<330: return 'weather'
            if 310<=x<570 and 142<=y<188: return 'todos'
            if 310<=x<570 and 192<=y<321:
                index=(y-192)//43
                if index<len(self.shown_todos):
                    item=self.shown_todos[index]
                    if x<353: return ('todo_state',item['id'],'1',self.shown_revision)
                    return 'todos'
            if 30<=x<445 and 340<=y<380: return 'panel_detail'
            if 445<=x<515 and 340<=y<380: return 'module_picker'
            if 515<=x<580 and 340<=y<380: return 'panel_refresh'
            if 30<=x<570 and 384<=y<540:
                kind=modules.selected(self.config)
                if kind=='finance': return ('news',(y-384)//26)
                if kind in ('news','weibo'):
                    if y>=519: return 'panel_detail'
                    if kind=='weibo' and 290<=x<312: return None
                    index=(y-384)//27+(5 if kind=='weibo' and x>=312 else 0)
                    return ('news',index) if index<len(self.shown_news) else None
                return 'panel_detail'
            if 30<=x<570 and 554<=y<590: return 'agenda'
            if 30<=x<570 and 592<=y<764 and not (290<=x<310):
                i=(y-592)//56*2+(0 if x<300 else 1)
                if i<5 and i<len(self.shown_events): return ('event',i)
        elif mode=='module_picker':
            if 30<=x<570 and 120<=y<660:
                index=(y-120)//90
                if index<len(modules.CATALOG): return ('select_module',modules.CATALOG[index][0])
            if 30<=x<202 and 728<=y<777: return self.module_return
            if 214<=x<386 and 728<=y<777: return 'pair'
        elif mode=='photo_picker':
            for i,box in enumerate(ui.PHOTO_BOXES):
                if ui.inside(x,y,box) and i<len(self.shown_photos): return ('photo_select',self.shown_photos[i]['id'])
            if 670<=y<710:
                if 30<=x<182: return 'photo_prev'
                if 418<=x<570: return 'photo_next'
            if 728<=y<777:
                if 30<=x<202: return 'settings'
                if 214<=x<386: return 'pair'
        elif mode=='module':
            if 670<=y<710:
                if 30<=x<182: return 'module_prev'
                if 418<=x<570: return 'module_next'
            if 728<=y<777:
                if 30<=x<202: return 'dashboard'
                if 214<=x<386: return 'pair'
                if 398<=x<570: return 'panel_refresh'
        elif mode=='news_history':
            if 30<=x<570 and 125<=y<635:
                index=(y-125)//85
                if index<len(self.shown_news): return ('news',index)
            if 670<=y<710:
                if 30<=x<182 and self.news_list_page>0: return 'history_prev'
                if 418<=x<570 and self.news_list_page+1<self.news_list_total: return 'history_next'
            if 30<=x<202 and 728<=y<777: return 'dashboard'
        elif mode=='pomodoro':
            if self.pomodoro.state=='idle':
                if ui.inside(x,y,ui.POMO_MINUS): return 'pomodoro_less'
                if ui.inside(x,y,ui.POMO_PLUS): return 'pomodoro_more'
            if 728<=y<777:
                if 30<=x<202: return 'dashboard'
                if 214<=x<386 and self.pomodoro.state!='done': return 'pomodoro_toggle'
                if 398<=x<570 and self.pomodoro.state!='idle': return 'pomodoro_reset'
        elif mode in ('todos','agenda'):
            if 30<=x<570 and 125<=y<635:
                index=(y-125)//85
                if mode=='todos' and index<len(self.shown_todos):
                    item=self.shown_todos[index]
                    if x<80: return ('todo_state',item['id'],'0' if item.get('done') else '1',self.shown_revision)
                    return ('todo_edit',item['id'])
                if mode=='agenda' and index<len(self.shown_events): return ('event',index)
            if 670<=y<710:
                if 30<=x<182 and self.list_page>0: return 'page_prev'
                if 418<=x<570 and self.list_page+1<self.list_total: return 'page_next'
            if 728<=y<777:
                if 30<=x<202: return 'calendar' if mode=='agenda' and self.agenda_day else 'dashboard'
                if 214<=x<386: return 'todo_editor' if mode=='todos' else 'editor'
                if 398<=x<570 and mode=='agenda': return 'calendar'
        elif mode=='calendar':
            if 30<=x<569 and 165<=y<621:
                day=ui.month_days(self.calendar_year,self.calendar_month)[(y-165)//76][(x-30)//77]
                if day: return ('day',f'{self.calendar_year:04d}-{self.calendar_month:02d}-{day:02d}')
            if 660<=y<707:
                if 30<=x<202: return 'month_prev'
                if 214<=x<386: return 'month_now'
                if 398<=x<570: return 'month_next'
            if 728<=y<777:
                if 30<=x<202: return 'dashboard'
                if 214<=x<386: return 'editor'
                if 398<=x<570: return 'agenda'
        elif mode=='weather' and 728<=y<777:
            if 30<=x<202: return 'dashboard'
            if 214<=x<386: return 'refresh_weather'
        elif mode=='settings':
            for box,action in ui.settings_hitboxes(self.settings_page):
                if ui.inside(x,y,box): return action
            if 30<=x<202 and 728<=y<777: return 'dashboard'
        elif mode=='wifi':
            if 30<=x<202 and 728<=y<777: return 'settings'
            if self.wifi_busy: return None
            if ui.inside(x,y,ui.WIFI_SCAN): return 'wifi_scan'
            if ui.inside(x,y,ui.WIFI_TOGGLE): return 'wifi_toggle'
            if ui.inside(x,y,ui.WIFI_MANUAL): return 'wifi_manual'
            for i,box in enumerate(ui.WIFI_LIST_BOXES):
                if ui.inside(x,y,box) and i<len(self.wifi_networks): return ('wifi_select',i)
        elif mode=='pair' and 728<=y<777 and 30<=x<570: return 'dashboard'
        elif mode=='news':
            if 516<=x<580 and 8<=y<62 and self.news_item and self.news_item.get('source')=='微博热搜': return 'weibo_detail_refresh'
            if 728<=y<777:
                if 30<=x<202: return 'news_back'
                if 214<=x<386: return 'news_prev'
                if 398<=x<570: return 'news_next'
        elif mode=='event' and 728<=y<777:
            if 30<=x<202: return 'event_back'
            if 214<=x<386: return 'event_edit'
            if 398<=x<570: return 'event_delete'
        return None

    def frame(self,capture=False):
        if self.mode=='form' and self.form: return native_input.render(self.form)
        if self.mode=='pair': return self.pair_image()
        if self.mode=='pomodoro': return ui.render_pomodoro(self.pomodoro.view())
        config,state=self.snapshot()
        current=now()
        time_format=self.preferences().get('time_format','24')
        if self.mode=='screensaver':
            if config['settings'].get('sleep_display')=='photo':
                try: photo=self.modules.photo_path(config['settings'].get('standby_photo',''))
                except ValueError: photo=None
                if photo: return ui.render_sleep_photo(photo,self.system_sleeping or self.system_suspend_pending)
            state['system_sleep']=self.system_sleeping or self.system_suspend_pending
            return ui.render_screensaver(config,state,self.standby_moment if self.system_sleeping else current,power_available=self.power_available)
        if self.mode=='module_picker': return ui.render_module_picker(config)
        if self.mode=='photo_picker':
            photos=self.modules.photos()
            total=max(1,(len(photos)+3)//4); page=min(self.photo_page,total-1)
            selected=photos[page*4:page*4+4]
            paths={}
            for row in selected:
                try: paths[row['id']]=self.modules.photo_path(row['id'])
                except ValueError: pass
            if capture:
                self.photo_page=page; self.photo_pages=total; self.shown_photos=copy.deepcopy(selected)
            return ui.render_photo_picker(selected,paths,config,self.photo_target,page,total,self.photo_message)
        if self.mode=='module':
            image,total=modules.detail_image(state['panel'],self.module_page)
            if capture: self.module_pages=total
            return image
        if self.mode=='settings':
            status,_,_,_=self.wifi_view()
            return ui.render_settings(config,state,status,power_available=self.power_available,page=self.settings_page,photos=self.modules.photos())
        if self.mode=='wifi':
            status,networks,busy,message=self.wifi_view()
            return ui.render_wifi(status,networks,busy,message)
        if self.mode=='news' and self.news_item:
            return render_news_detail(self.news_item,self.news_page,self.news_pages,time_format=time_format)
        if self.mode=='event' and self.event_item:
            return render_event_detail(self.event_item,self.event_confirm,message=self.event_message,time_format=time_format)
        if self.mode=='news_history':
            feed=state['panel'].get('items',[]) if state['panel']['kind'] in ('news','weibo') else state['news']
            selected,page,total=ui.page_slice(feed,self.news_list_page)
            if capture:
                self.news_list_page=page; self.news_list_total=total
                self.shown_news=copy.deepcopy(selected)
            return ui.render_news_history(feed,page,state.get('news_refreshing',False),time_format=time_format,title=state['panel']['title'],history=state['panel']['kind']!='weibo')
        if self.mode=='weather': return ui.render_weather(state,current,time_format=time_format)
        if self.mode=='calendar': return ui.render_calendar(config,self.calendar_year,self.calendar_month,current)
        if self.mode in ('todos','agenda'):
            items=ui.todo_items(config) if self.mode=='todos' else ui.all_events(config,self.agenda_day)
            selected,page,total=ui.page_slice(items,self.list_page)
            if capture:
                self.list_page=page; self.list_total=total
                self.shown_revision=config['revision']
                if self.mode=='todos': self.shown_todos=copy.deepcopy(selected)
                else: self.shown_events=copy.deepcopy(selected)
            if self.mode=='todos': return ui.render_todos(config,page,self.todo_message)
            return ui.render_agenda(config,page,self.agenda_day,time_format=time_format)
        try: result=render(config,state,current=current)
        except (ValueError,TypeError,KeyError,IndexError,OverflowError,AttributeError):
            LOG.exception('Feed layout failed; keeping the local agenda available')
            state.update(weather=None,news=[],news_at=None,news_error='资讯暂不可用')
            result=render(config,state,current=current)
        if capture:
            feed=state['panel'].get('items',[])[:10 if state['panel']['kind']=='weibo' else 5] if state['panel']['kind'] in ('news','weibo') else news_items(state.get('news'))[:6]
            self.shown_news=copy.deepcopy(feed)
            self.shown_events=copy.deepcopy(future_events(config,current)[:5])
            self.shown_todos=copy.deepcopy(ui.todo_items(config,False)[:3])
            self.shown_revision=config['revision']
        return result

    def handle_action(self,action):
        if isinstance(action,tuple) and action[0]=='weibo_detail_result':
            if (not self.updating and self.mode=='news' and action[1]==self.detail_generation
                    and self.news_item and self.news_item.get('source')=='微博热搜'
                    and self.modules.detail_key(self.news_item)==self.modules.detail_key(action[2])):
                self.news_item=action[2]
                self.news_pages=paginate(self.news_item['content'],size=24,per_page=16)
                self.news_page=min(self.news_page,len(self.news_pages)-1)
                self.redraw.set()
            return
        if action=='update_restart':
            self.restart_update=True; self.stop.set(); return
        if self.updating: return
        if action=='quit': self.stop.set(); return
        if action=='standby_refresh':
            if self.mode=='screensaver': self.redraw.set()
            return
        if action=='system_sleep':
            if self.protected_mode():
                if self.power: self.power.wake(); self.power.maintain(True,force=True)
                if self.pomodoro.view()['state']=='running': self.mode='pomodoro'
                if self.device: self.device.force_full=True
                self.redraw.set(); return
            if self.mode=='screensaver': self.system_sleeping=True; self.system_suspend_pending=False
            else: self.enter_standby(system=True)
            self.network_wake.set(); return
        if action=='system_wake':
            self.leave_standby(system=True)
            if self.device: self.device.force_full=True
            self.redraw.set(); return
        if (self.pomodoro.view()['state']=='running' and action not in
                ('pomodoro','pomodoro_toggle','pomodoro_reset','pomodoro_less','pomodoro_more','wifi_changed')):
            self.mode='pomodoro'; self.redraw.set(); return
        if action=='power_toggle': self.toggle_standby(); return
        if self.mode=='screensaver': return
        if action=='wifi_changed': self.redraw.set(); return
        if action=='standby': self.enter_standby(); return
        self.note_activity()
        if isinstance(action,tuple) and action[0]=='select_module':
            old=self.store.read()
            try: new=self.store.change(old['revision'],'setting',dict(key='panel_module',value=action[1]))
            except (ValueError,Conflict): return
            self.mode=self.module_return; self.settings_changed(old,new); return
        if action=='module_picker':
            self.module_return='settings' if self.mode=='settings' else 'dashboard'
            self.mode='module_picker'; self.redraw.set(); return
        if isinstance(action,tuple) and action[0]=='settings_tab':
            if action[1] in ui.SETTINGS_TABS: self.settings_page=action[1]
            self.redraw.set(); return
        if isinstance(action,tuple) and action[0]=='photos':
            self.photo_target=action[1]; self.photo_page=0; self.photo_message=''
            self.mode='photo_picker'; self.redraw.set(); return
        if action in ('photo_prev','photo_next'):
            self.photo_page=max(0,min(self.photo_pages-1,self.photo_page+(-1 if action=='photo_prev' else 1)))
            self.photo_message=''; self.redraw.set(); return
        if isinstance(action,tuple) and action[0]=='photo_select':
            old=self.store.read()
            try:
                with self.modules.lock:
                    self.modules.photo_path(action[1])
                    changes={'panel_photo':action[1],'panel_module':'photo'} if self.photo_target=='panel' else {'standby_photo':action[1],'sleep_display':'photo'}
                    new=self.store.change(old['revision'],'settings',dict(settings=changes))
                self.mode='settings'; self.settings_changed(old,new)
            except (ValueError,Conflict):
                self.photo_message='图片或设置已更新，请重新选择。'; self.redraw.set()
            return
        if action=='panel_detail':
            kind=modules.selected(self.store.read())
            self.news_list_page=0; self.module_page=0
            self.mode='news_history' if kind in ('finance','news','weibo') else 'module'
            self.redraw.set(); return
        if action in ('module_prev','module_next'):
            self.module_page=max(0,min(self.module_pages-1,self.module_page+(-1 if action=='module_prev' else 1)))
            self.redraw.set(); return
        if action=='panel_refresh':
            kind=modules.selected(self.store.read())
            if kind=='finance': action='refresh_news'
            elif kind in ('news','weibo','usage'): self.module_wake.set(); self.redraw.set(); return
            elif kind=='photo': self.mode='module'; self.redraw.set(); return
            else: self.mode='module'; self.redraw.set(); return
        if self.mode=='form' and action=='dashboard' and self.form:
            self.form.message='正在填写内容，请先保存或取消。'
            self.redraw.set()
            return
        if action=='city_editor':
            config=self.store.read()
            self.form_return='settings'
            self.form=native_input.NativeForm('city',config['revision'],{'title':config.get('city','')})
            self.mode='form'; self.redraw.set(); return
        if action=='wifi':
            self.mode='wifi'; self.start_wifi_operation('scan'); self.redraw.set(); return
        if action in ('wifi_scan','wifi_toggle'):
            self.start_wifi_operation('scan' if action=='wifi_scan' else 'toggle'); return
        if action=='wifi_manual' or (isinstance(action,tuple) and action[0]=='wifi_select'):
            if self.wifi_busy: return
            entry=None
            if isinstance(action,tuple):
                if action[1]>=len(self.wifi_networks): return
                entry={'title':self.wifi_networks[action[1]].get('ssid',''),'note':''}
            self.form_return='wifi'
            self.form=native_input.NativeForm('wifi',0,entry)
            self.mode='form'; self.redraw.set(); return
        if isinstance(action,tuple) and action[0] in ('setting','setting_value','setting_cycle'):
            key=action[1]
            current=self.store.read()
            settings=current.get('settings',{})
            if action[0]=='setting': value=not settings.get(key,False)
            elif action[0]=='setting_value': value=action[2]
            else:
                choices=CHOICE_SETTINGS.get(key,())
                if not choices: return
                try: index=choices.index(settings.get(key))
                except ValueError: index=-1
                value=choices[(index+1)%len(choices)]
            if key=='sleep_display' and value=='photo':
                try: self.modules.photo_path(settings.get('standby_photo',''))
                except ValueError: self.handle_action(('photos','standby')); return
            try: self.change_setting(key,value)
            except (Conflict,ValueError):
                self.config=self.store.read(); self.redraw.set()
            return
        if action=='calendar_today':
            self.calendar_year,self.calendar_month=now().year,now().month
            self.agenda_day=None; self.mode='calendar'; self.redraw.set(); return
        if action in ('pomodoro','pomodoro_toggle','pomodoro_reset','pomodoro_less','pomodoro_more'):
            if action=='pomodoro':
                self.mode='pomodoro'
            elif action=='pomodoro_toggle': self.pomodoro.toggle()
            elif action=='pomodoro_reset': self.pomodoro.reset()
            else: self.pomodoro.adjust(-1 if action=='pomodoro_less' else 1)
            if self.pomodoro.view()['state']=='running': self.mode='pomodoro'
            self.redraw.set(); return
        if action=='news_history':
            self.news_list_page=0; self.mode='news_history'; self.redraw.set(); return
        if action in ('history_prev','history_next') and self.mode=='news_history':
            self.news_list_page=max(0,min(self.news_list_total-1,self.news_list_page+(-1 if action=='history_prev' else 1)))
            self.redraw.set(); return
        if action=='news_back' and self.mode=='news':
            self.mode=self.news_return; self.redraw.set(); return
        if action in ('editor','todo_editor','event_edit') or (isinstance(action,tuple) and action[0]=='todo_edit'):
            kind='todo' if action=='todo_editor' or isinstance(action,tuple) else 'event'
            entry=None
            config=self.store.read()
            revision=config['revision']
            if action=='event_edit':
                if self.event_confirm or self.event_message or not self.event_item: return
                entry=self.event_item
                revision=self.event_revision
            elif isinstance(action,tuple):
                entry=next((item for item in self.shown_todos if item['id']==action[1]),None)
                if entry is None: return
                revision=self.shown_revision
            self.form_return=self.event_return if self.mode=='event' else self.mode
            self.form=native_input.NativeForm(kind,revision,entry,self.agenda_day)
            self.mode='form'
            self.redraw.set()
        elif isinstance(action,tuple) and action[0]=='form' and self.mode=='form':
            self.form_action(action[1])
        elif isinstance(action,tuple) and action[0]=='todo_state':
            try:
                self.store.change(action[3],'todo_state',dict(id=action[1],done=action[2]))
                self.todo_message=''
            except (Conflict,ValueError,OSError):
                LOG.warning('Todo change rejected',exc_info=True)
                self.mode='todos'
                self.todo_message='内容有更新或暂不能保存，请核对后再点。'
            self.redraw.set()
        elif action=='refresh_weather':
            with self.lock: self.state['weather_refreshing']=True
            self.weather_wake.set()
            self.redraw.set()
        elif action=='refresh_news':
            if not self.news_sources_enabled(self.store.read()):
                with self.lock: self.state.update(news_refreshing=False,news_at=None,news_error='')
            else:
                with self.lock: self.state['news_refreshing']=True
                self.news_wake.set()
            self.redraw.set()
        elif isinstance(action,tuple) and action[0]=='news':
            if 0<=action[1]<len(self.shown_news):
                self.news_return=self.mode
                self.news_item=self.shown_news[action[1]]
                if self.news_item.get('source')=='微博热搜':
                    self.open_weibo_detail(self.news_item); return
                body=self.news_item.get('content') or self.news_item.get('name') or ''
                if self.news_item.get('source')=='东方财富':
                    body+='\n（全文较长，请用手机打开资讯列表，点进原文查看。）'
                self.news_pages=paginate(body,size=24,per_page=16)
                self.news_page=0; self.mode='news'; self.redraw.set()
        elif action=='weibo_detail_refresh' and self.mode=='news':
            if self.news_item and not self.news_item.get('detail_loading'):
                self.open_weibo_detail(self.news_item,force=True)
        elif action=='news_prev' and self.mode=='news':
            if self.news_page>0: self.news_page-=1; self.redraw.set()
        elif action=='news_next' and self.mode=='news':
            if self.news_page<len(self.news_pages)-1: self.news_page+=1; self.redraw.set()
        elif isinstance(action,tuple) and action[0]=='event':
            if 0<=action[1]<len(self.shown_events):
                self.event_return=self.mode
                self.event_item=self.shown_events[action[1]]
                self.event_confirm=False; self.event_revision=self.shown_revision; self.event_message=''
                self.mode='event'; self.redraw.set()
        elif action=='event_back' and self.mode=='event':
            if self.event_confirm: self.event_confirm=False
            else: self.mode=self.event_return
            self.redraw.set()
        elif action=='event_delete' and self.mode=='event':
            if self.event_message: return
            if not self.event_confirm: self.event_confirm=True
            else:
                try:
                    self.store.change(self.event_revision,'delete',{'id':self.event_item.get('id','')})
                    self.mode=self.event_return
                except (Conflict,ValueError,OSError):
                    LOG.warning('Event delete failed',exc_info=True)
                    self.event_confirm=False
                    self.event_message='未删除：日程可能已在其他入口更新，请返回后重新打开。'
            self.redraw.set()
        elif isinstance(action,tuple) and action[0]=='day':
            self.agenda_day=action[1]; self.list_page=0; self.mode='agenda'; self.redraw.set()
        elif action in ('page_prev','page_next') and self.mode in ('todos','agenda'):
            self.list_page=max(0,min(self.list_total-1,self.list_page+(-1 if action=='page_prev' else 1)))
            self.redraw.set()
        elif action in ('month_prev','month_next','month_now'):
            self.agenda_day=None
            if action=='month_now': self.calendar_year,self.calendar_month=now().year,now().month
            else:
                index=self.calendar_year*12+self.calendar_month-1+(-1 if action=='month_prev' else 1)
                if 12<=index<120000: self.calendar_year,self.calendar_month=index//12,index%12+1
            self.redraw.set()
        elif action in ('dashboard','pair','weather','todos','agenda','calendar','settings'):
            self.mode=action
            if action in ('todos','agenda'):
                self.list_page=0; self.todo_message=''
            if action=='agenda': self.agenda_day=None
            self.redraw.set()

    def form_action(self,action):
        form=self.form
        op,value=action
        if op=='cancel':
            if form.dirty and not form.cancel_confirm:
                form.cancel_confirm=True; form.delete_confirm=False
                form.message='内容未保存；再点左下角叉号放弃。'
            else: self.mode=self.form_return; self.form=None
        elif op in ('save','delete'):
            if op=='save' and form.composition:
                form.message='请先点候选字，完成拼音输入后保存。'
            elif op=='delete' and not form.delete_confirm:
                form.delete_confirm=True; form.cancel_confirm=False
                form.message='再点右上角勾号，确认删除这条内容。'
            else:
                if form.kind=='wifi' and op=='save':
                    try:
                        form.validate()
                        ssid=form.values['title']; password=form.values['note']
                        self.mode='wifi'; self.form=None
                        self.start_wifi_operation('connect',ssid,password)
                    except ValueError as error: form.message=str(error)
                    self.redraw.set(); return
                operation='city' if form.kind=='city' else ('todo_' if form.kind=='todo' else '')+op
                values={'city':form.values['title'].strip()} if form.kind=='city' else dict(form.values,id=form.identity)
                try:
                    if op=='save': form.validate()
                    try: self.store.change(form.revision,operation,values)
                    except Conflict:
                        # Another item may have changed while this draft was open.
                        # Retry only when this item's original fields still match.
                        current=self.store.read()
                        entries=current['todos'] if form.kind=='todo' else current['events']
                        existing=next((e for e in entries if e['id']==form.identity),None)
                        unchanged=existing is not None and all(str(existing.get(k,''))==v for k,v in form.original.items())
                        if not form.identity or (op=='save' and unchanged):
                            self.store.change(current['revision'],operation,values)
                        else: raise
                    self.config=self.store.read()
                    if form.kind=='city': self.weather_wake.set()
                    self.mode=self.form_return; self.form=None
                except Conflict:
                    form.message='此条内容已被修改或删除；草稿保留，请核对。'
                    form.conflict=True
                except ValueError as error: form.message=str(error)
                except OSError: form.message='暂未保存，请检查存储；草稿仍保留。'
        else: form.action(action)
        self.redraw.set()

    def storage_available(self):
        try: return (self.data/'agenda.json').exists()
        except OSError as error:
            # USB export may invalidate the mounted filesystem before it
            # disappears. Treat this like missing storage, not an app crash.
            if error.errno in (errno.EIO,errno.ENODEV,getattr(errno,'ESTALE',116)): return False
            raise

    def request_pending_suspend(self):
        if not self.system_suspend_pending or not self.power: return
        self.system_suspend_pending=False
        self.system_sleeping=self.power.sleep()
        if self.system_sleeping:
            if self.device: self.device.prepare_standby_refresh()
        else:
            LOG.warning('POWER_SUSPEND_REQUEST_FAILED; touch standby remains available')
            self.redraw.set()
        self.network_wake.set()

    def display_loop(self):
        previous_minute=None
        previous_timer=None
        next_cache_prune=0
        next_idle_check=0
        self.redraw.set()
        while not self.stop.is_set():
            if self.device and Path('/tmp/kindle-standalone-show').exists():
                Path('/tmp/kindle-standalone-show').unlink()
                if self.mode=='screensaver': self.leave_standby()
                self.requests.put('dashboard')
            try:
                action=self.requests.get(timeout=STANDBY_BATTERY_SECONDS if self.standby.is_set() else 0.5)
                self.handle_action(action)
                self.maintain_power()
                if self.stop.is_set(): break
            except queue.Empty: pass
            if not self.storage_available():
                # Hand the device back completely. Leaving an 'editor' session
                # alive here makes background power maintenance block sleep in
                # the native library even after USB is unplugged.
                LOG.info('USB_STORAGE_UNAVAILABLE exiting dashboard')
                self.request_stop()
                break
            if not self.standby.is_set():
                if self.pomodoro.tick():
                    self.note_activity()  # Show completion before restarting the idle countdown.
                    self.redraw.set()
                timer=self.pomodoro.view()
                timer_stamp=(timer['state'],math.ceil(timer['remaining']/(15 if self.mode=='pomodoro' else 60)))
                if timer_stamp!=previous_timer:
                    if self.mode in ('dashboard','pomodoro'): self.redraw.set()
                    previous_timer=timer_stamp
                if time.monotonic()>=next_cache_prune:
                    try: self.prune_news_cache()
                    except OSError: LOG.warning('News cache cleanup unavailable')
                    next_cache_prune=time.monotonic()+60
                if time.monotonic()>=next_idle_check:
                    self.maybe_standby()
                    next_idle_check=time.monotonic()+1
            moment=now()
            stamp=self.display_stamp(moment)
            modes=('dashboard','pair','news','news_history','pomodoro','event','weather','todos','agenda','calendar','form','settings','wifi','screensaver','module','module_picker','photo_picker')
            if self.mode in modes and (stamp!=previous_minute or self.redraw.is_set()):
                try:
                    self.drawing=True
                    self.redraw.clear()
                    if self.device: self.device.stop_gui()
                    # Menu/automatic standby must let the system wallpaper
                    # switch first, just like physical power-button standby.
                    self.request_pending_suspend()
                    frame=self.frame(capture=True)
                    path=Path('/tmp/kindle-standalone.png') if self.device else self.data/'preview.png'
                    frame.save(str(path),'PNG')
                    if self.device: self.device.display(path)
                    with self.lock:
                        status=dict(display_at=now().isoformat(),weather_at=self.state.get('weather_at'),revision=self.store.read()['revision'],mode=self.mode)
                    if self.device: status['screen_refresh']=self.device.refresh_status()
                    if self.power: status['power']=self.power.diagnostics()
                    atomic_json(self.data/'status.json',status)
                    LOG.info('DISPLAY_OK mode=%s revision=%s',self.mode,status['revision'])
                    previous_minute=stamp
                    updater.confirm(self.root)
                except Exception:
                    LOG.exception('Display failed')
                    self.stop.set()
                finally: self.drawing=False

    def display_stamp(self,moment):
        if self.mode=='screensaver': return 'calendar:'+moment.date().isoformat()
        return moment.strftime('%Y-%m-%d %H:')+str(moment.minute if self.mode=='dashboard' else moment.minute//5)

    def network_loop(self):
        next_wifi_status=0
        while not self.stop.is_set():
            self.maintain_power()
            if self.standby.is_set():
                if self.system_sleeping:
                    self.network_wake.wait(); self.network_wake.clear(); continue
                # Local power status only: never reconnect Wi-Fi or fetch feeds in standby.
                if self.device: self.update_battery()
                self.network_wake.wait(STANDBY_BATTERY_SECONDS); self.network_wake.clear(); continue
            try: self.connection()
            except Exception: pass
            if self.wifi and time.monotonic()>=next_wifi_status:
                self.refresh_wifi_status()
                next_wifi_status=time.monotonic()+30
            if self.device: self.update_battery()
            self.network_wake.wait(15); self.network_wake.clear()

    def refresh_wifi_status(self):
        if not self.wifi or self.standby.is_set() or self.wifi_busy: return
        if not self.wifi_lock.acquire(blocking=False): return
        try:
            status=self.wifi.status()
            with self.lock:
                changed=status!=self.wifi_status
                self.wifi_status=status
            if changed and self.mode in ('pair','settings','wifi'): self.redraw.set()
        except (DeviceControlError,OSError): pass
        finally: self.wifi_lock.release()

    def handler(self):
        app=self
        prefix='/'+self.token
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(15)
            def log_message(self,*args): pass
            def reply(self,code,payload,kind='text/html; charset=utf-8',location=None):
                self.send_response(code)
                self.send_header('Content-Type',kind)
                self.send_header('Content-Length',str(len(payload)))
                self.send_header('Cache-Control','no-store')
                # no-referrer turns ordinary browser form POST Origin into null,
                # which fails our same-origin check. Keep same-device forms
                # working while withholding the private URL from external links.
                self.send_header('Referrer-Policy','same-origin')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; img-src 'self' blob:; script-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
                if location: self.send_header('Location',location)
                self.end_headers()
                self.wfile.write(payload)
            def valid_path(self):
                path=urllib.parse.urlsplit(self.path).path
                return path.startswith(prefix+'/')
            def reject_post(self,code,payload,kind='text/plain'):
                # Drain small, already-sent requests before closing. Otherwise
                # Windows/phone clients may see a reset instead of the error.
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if 0<size<=modules.MAX_UPLOAD:
                        self.connection.settimeout(2); self.rfile.read(size)
                except (ValueError,OSError): pass
                return self.reply(code,payload,kind)
            def do_GET(self):
                parsed=urllib.parse.urlsplit(self.path)
                if parsed.path=='/health': return self.reply(200,b'Kindle standalone','text/plain')
                if not self.valid_path(): return self.reply(404,b'Not found','text/plain')
                query=urllib.parse.parse_qs(parsed.query)
                try:
                    path=parsed.path
                    manager_pages={'/manage':phone.home,'/modules':phone.module_page,'/settings':phone.settings_page,'/photos':phone.photos_page}
                    for route,view in manager_pages.items():
                        if path==prefix+route: return self.reply(200,view(app,prefix,query.get('message',[''])[0][:180]))
                    if path==prefix+'/manager.js': return self.reply(200,phone.SCRIPT.encode('utf-8'),'text/javascript; charset=utf-8')
                    if path==prefix+'/manager-state.json':
                        info=dict(csrf=app.csrf,build=BUILD)
                        return self.reply(200,json.dumps(info).encode(),'application/json')
                    if path==prefix+'/power-diagnostics.json':
                        info=app.power.probe() if app.power else {'backend':'unavailable'}
                        info=dict(info,build=BUILD,mode=app.mode,protected=app.protected_mode())
                        return self.reply(200,json.dumps(info).encode(),'application/json')
                    if path.startswith(prefix+'/photos/'):
                        identity=path[len(prefix+'/photos/'):]
                        if not re.fullmatch(r'[a-f0-9]{24}\.png',identity): return self.reply(404,b'Not found','text/plain')
                        try: photo=app.modules.photo_path(identity[:-4])
                        except ValueError: return self.reply(404,b'Not found','text/plain')
                        return self.reply(200,photo.read_bytes(),'image/png')
                    if parsed.path==prefix+'/status.json':
                        config=app.store.read()
                        with app.lock:
                            status=dict(native=bool(app.device),mode=app.mode,revision=config['revision'],events=len(config['events']),todos=len(config['todos']),pending_todos=len(ui.todo_items(config,False)),weather_at=app.state.get('weather_at'),weather_error=app.state.get('error',''),news_at=app.state.get('news_at'),news_count=len(ui.enabled_news(config,news_items(app.state.get('news')))),news_error=app.state.get('news_error',''),python=sys.version.split()[0],build=BUILD)
                            status.update(battery=app.state.get('battery'),charging=app.state.get('charging'),pomodoro=app.pomodoro.view(),settings=config['settings'],standby=app.standby.is_set(),power_key=app.power_available)
                            status['panel']=app.modules.view(config)
                            status['power']=app.power.diagnostics() if app.power else {'backend':'unavailable'}
                        display=app.data/'status.json'
                        update_result=app.data/'update-result.json'
                        if update_result.exists(): status['update_result']=json.loads(update_result.read_text(encoding='utf-8'))
                        if display.exists():
                            drawn=json.loads(display.read_text(encoding='utf-8'))
                            status['display_at']=drawn.get('display_at')
                            if 'screen_refresh' in drawn: status['screen_refresh']=drawn['screen_refresh']
                        return self.reply(200,json.dumps(status,ensure_ascii=False).encode('utf-8'),'application/json; charset=utf-8')
                    if parsed.path==prefix+'/update-info.json':
                        with app.lock:
                            if app.mode!='pair' or app.updating:
                                return self.reply(409,b'Open phone connection on Kindle first','text/plain')
                            app.update_nonce=secrets.token_hex(24)
                            app.update_until=time.monotonic()+120
                            info=dict(nonce=app.update_nonce,build=BUILD,protocol=1)
                        return self.reply(200,json.dumps(info).encode(),'application/json')
                    if parsed.path==prefix+'/frame.png':
                        buffer=io.BytesIO(); app.frame().save(buffer,'PNG')
                        return self.reply(200,buffer.getvalue(),'image/png')
                    if parsed.path==prefix+'/news':
                        config=app.store.read()
                        with app.lock:
                            feed_items=ui.enabled_news(config,ui.merge_news_history(app.state.get('news')))
                            news_at=app.state.get('news_at')
                            news_error=app.state.get('news_error','')
                        if not app.news_sources_enabled(config): news_at=None; news_error=''
                        return self.reply(200,pages.news_page(feed_items,news_at,prefix,news_error,app.csrf,query.get('device')==['kindle']))
                    if parsed.path==prefix+'/todos':
                        body=pages.todo_page(app.store.read(),prefix,app.csrf,query.get('device')==['kindle'],query.get('edit',[''])[0],query.get('delete',[''])[0],query.get('message',[''])[0])
                        return self.reply(200,phone.wrap_legacy(body,prefix,'todo'))
                    if parsed.path!=prefix+'/': return self.reply(404,b'Not found','text/plain')
                    body=pages.page(app.store.read(),prefix,app.csrf,query.get('device')==['kindle'],query.get('edit',[''])[0],query.get('delete',[''])[0],query.get('message',[''])[0],query.get('date',[''])[0])
                    self.reply(200,phone.wrap_legacy(body,prefix,'calendar'))
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError): self.close_connection=True
                except (OSError,ValueError): self.reply(503,pages.error_page('Kindle 正在通过 USB 连接电脑，请拔线后重试。',prefix))
            def receive_update(self):
                def reject(code,message):
                    # Drain bounded rejected uploads so Windows clients receive
                    # the HTTP error instead of a reset caused by unread bytes.
                    try:
                        size=int(self.headers.get('Content-Length','0'))
                        if 0<size<=updater.MAX_PACKAGE:
                            self.connection.settimeout(2)
                            self.rfile.read(size)
                    except (ValueError,OSError): pass
                    return self.reply(code,message,'text/plain')
                if not self.valid_path(): return reject(404,b'Not found')
                origin=self.headers.get('Origin')
                if origin and urllib.parse.urlsplit(origin).netloc!=self.headers.get('Host'):
                    return reject(403,b'Forbidden')
                with app.lock:
                    nonce=self.headers.get('X-Update-Nonce','')
                    if app.mode!='pair' or app.updating or time.monotonic()>app.update_until or not nonce or not secrets.compare_digest(nonce,app.update_nonce):
                        return reject(403,b'Update unavailable')
                    app.updating=True; app.update_nonce=''; app.redraw.set()
                app.maintain_power()
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=updater.MAX_PACKAGE: raise ValueError('更新包过大或为空。')
                    self.connection.settimeout(60)
                    payload=self.rfile.read(size)
                    if len(payload)!=size: raise ValueError('上传未完成。')
                    staged=updater.stage(payload,app.root)
                except Exception as error:
                    LOG.warning('Wireless stage failed',exc_info=True)
                    with app.lock: app.updating=False
                    app.redraw.set()
                    message=str(error) if isinstance(error,ValueError) else '更新未完成，当前版本保持不变。'
                    return self.reply(400,json.dumps(dict(error=message),ensure_ascii=False).encode(),'application/json')
                try:
                    self.reply(202,json.dumps(dict(id=staged['id'],build=staged['build'],status='restarting')).encode(),'application/json')
                finally: app.requests.put('update_restart')

            def do_POST(self):
                if urllib.parse.urlsplit(self.path).path==prefix+'/update':
                    return self.receive_update()
                if app.updating: return self.reply(503,b'Updating','text/plain')
                if urllib.parse.urlsplit(self.path).path in (prefix+'/manage-action',prefix+'/photo-upload',prefix+'/codex-usage'):
                    return self.receive_manager()
                if not self.valid_path() or urllib.parse.urlsplit(self.path).path!=prefix+'/action': return self.reply(404,b'Not found','text/plain')
                origin=self.headers.get('Origin')
                if origin and urllib.parse.urlsplit(origin).netloc!=self.headers.get('Host'): return self.reject_post(403,b'Forbidden')
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=16384: return self.reply(413,b'Request too large','text/plain')
                    raw=urllib.parse.parse_qs(self.rfile.read(size).decode('utf-8'),keep_blank_values=True)
                    values={k:v[0] for k,v in raw.items()}
                    if not secrets.compare_digest(values.get('csrf','').encode('utf-8'),app.csrf.encode('ascii')): return self.reply(403,pages.error_page('页面已过期，请重新打开。',prefix))
                    action=values.get('action','')
                    app.note_activity()
                    if action=='refresh_news':
                        if app.news_sources_enabled(app.store.read()) and not app.standby.is_set():
                            with app.lock: old_at=app.state.get('news_at')
                            app.news_wake.set()
                            for _ in range(16):
                                time.sleep(0.5)
                                with app.lock: changed=app.state.get('news_at')!=old_at
                                if changed or app.stop.is_set(): break
                        location=prefix+'/news'
                        if values.get('device')=='kindle': location+='?device=kindle'
                        self.reply(303,b'',location=location)
                        return
                    if action=='display':
                        if app.standby.is_set(): return self.reply(409,pages.error_page('看板正在待机，请先轻触 Kindle 屏幕唤醒。',prefix))
                        self.reply(200,pages.layout('返回看板','<h1>正在返回看板…</h1>'))
                        timer=threading.Timer(1,lambda:app.requests.put('dashboard')); timer.daemon=True; timer.start()
                        return
                    app.config=app.store.change(values.get('revision','-1'),action,values)
                    app.redraw.set()
                    if app.standby.is_set(): app.requests.put('standby_refresh')
                    if action=='city': app.weather_wake.set()
                    if action=='setting' and values.get('key','').startswith('news_'): app.news_wake.set()
                    if action=='setting' and values.get('key')=='weather_interval': app.weather_wake.set()
                    query={'message':'已保存，Kindle 会自动更新。'}
                    if values.get('device')=='kindle': query['device']='kindle'
                    self.reply(303,b'',location=prefix+('/todos?' if action.startswith('todo_') else '/?')+urllib.parse.urlencode(query))
                except Conflict as error: self.reply(409,pages.error_page(str(error),prefix))
                except (ValueError,UnicodeError): self.reply(400,pages.error_page('请检查内容，标题最多 100 字、备注最多 200 字。日程日期为年-月-日，时间为时:分。',prefix))
                except OSError: self.reply(503,pages.error_page('Kindle 正在通过 USB 连接电脑，请拔线后重试。',prefix))

            def receive_manager(self):
                def fail(code,message):
                    return self.reply(code,json.dumps(dict(error=message),ensure_ascii=False).encode('utf-8'),'application/json; charset=utf-8')
                origin=self.headers.get('Origin')
                if origin and urllib.parse.urlsplit(origin).netloc!=self.headers.get('Host'): return self.reject_post(403,b'{"error":"Origin rejected"}','application/json')
                path=urllib.parse.urlsplit(self.path).path
                try:
                    maximum=modules.MAX_UPLOAD if path.endswith('/photo-upload') else 32768
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=maximum: return fail(413,'上传内容过大或为空。')
                    binary=path!=prefix+'/manage-action'
                    if binary and not hmac.compare_digest(self.headers.get('X-CSRF-Token','').encode(),app.csrf.encode()): return self.reject_post(403,json.dumps(dict(error='页面已过期，请重新打开。'),ensure_ascii=False).encode('utf-8'),'application/json; charset=utf-8')
                    body=self.rfile.read(size)
                    if len(body)!=size: return fail(400,'上传不完整。')
                    if path==prefix+'/photo-upload':
                        identity=app.modules.add_photo(body,urllib.parse.unquote(self.headers.get('X-Photo-Name','图片')))
                        return self.reply(201,json.dumps(dict(id=identity)).encode(),'application/json')
                    if path==prefix+'/codex-usage':
                        app.modules.import_codex(json.loads(body.decode('utf-8')))
                        app.redraw.set()
                        return self.reply(200,b'{"saved":true}','application/json')
                    values={k:v[0] for k,v in urllib.parse.parse_qs(body.decode('utf-8'),keep_blank_values=True).items()}
                    if not hmac.compare_digest(values.get('csrf','').encode(),app.csrf.encode()): return fail(403,'页面已过期，请重新打开。')
                    old=app.store.read(); action=values.get('action'); updates={}; target='/modules'; message='已保存，Kindle 会自动更新。'
                    if action=='module': updates={k:values.get(k) for k in ('panel_module','module_interval')}
                    elif action=='settings':
                        updates={k:values.get(k) for k in ('time_format','weather_interval','full_refresh','idle_minutes','standby_wifi','sleep_display','news_wallstreetcn','news_eastmoney')}
                        target='/settings'
                        if updates['sleep_display']=='photo' and not old['settings'].get('standby_photo'): raise ValueError('请先在图片库选一张待机图片。')
                    elif action=='photo_select':
                        identity=values.get('id',''); app.modules.photo_path(identity)
                        if values.get('target')=='panel': updates=dict(panel_photo=identity,panel_module='photo')
                        elif values.get('target')=='standby': updates=dict(standby_photo=identity,sleep_display='photo')
                        else: raise ValueError('图片用途无效。')
                        target='/photos'
                    elif action=='photo_delete':
                        if int(values.get('revision',-1))!=old['revision']: raise Conflict('设置已更新，请刷新页面。')
                        app.modules.delete_photo(values.get('id',''),old); target='/photos'
                    elif action=='deepseek_key': app.modules.save_key(values.get('api_key','')); app.module_wake.set()
                    elif action=='remove_deepseek_key': app.modules.save_key('',remove=True); app.redraw.set()
                    elif action=='refresh':
                        if not app.standby.is_set():
                            if modules.selected(old)=='finance': app.news_wake.set()
                            else: app.module_wake.set()
                        message='已请求更新，请稍后刷新本页。'
                    elif action in ('wifi_scan','wifi_connect'):
                        if not app.wifi: raise ValueError('此设备不支持 Wi-Fi 配置。')
                        if app.wifi_busy: raise ValueError('正在处理 Wi-Fi，请稍后再试。')
                        if action=='wifi_connect':
                            ssid=values.get('ssid',''); password=values.get('password','')
                            app.wifi._validate(ssid,password)
                            app.start_wifi_operation('connect',ssid,password)
                        else: app.start_wifi_operation('scan')
                        target='/settings'; message='正在处理 Wi-Fi，稍后刷新状态查看结果。'
                    elif action=='standby':
                        if app.protected_mode(): raise ValueError('请先结束或暂停番茄钟，并保存正在填写的内容。')
                        app.requests.put('standby'); target='/settings'; message='已请求待机，按 Kindle 电源键可唤醒。'
                    else: raise ValueError('未知操作。')
                    if updates:
                        payload=dict(settings=updates)
                        if action=='settings': payload['city']=values.get('city','')
                        new=app.store.change(values.get('revision',-1),'settings',payload)
                        app.settings_changed(old,new)
                    location=prefix+target+'?'+urllib.parse.urlencode(dict(message=message))
                    return self.reply(303,b'',location=location)
                except Conflict as error: return self.reply(409,phone.shell('内容已更新','<h1>请刷新后重试</h1><p>'+pages.escape(str(error))+'</p><a href="'+prefix+'/manage">返回面板</a>',prefix))
                except (ValueError,UnicodeError,DeviceControlError) as error:
                    message=str(error) if isinstance(error,(ValueError,DeviceControlError)) else '内容格式不正确。'
                    if path==prefix+'/manage-action':
                        return self.reply(400,phone.shell('暂未保存','<h1>暂未保存</h1><p>'+pages.escape(message)+'</p><a href="'+prefix+'/manage">返回面板</a>',prefix))
                    return fail(400,message)
                except OSError: return fail(503,'设备存储暂不可用，请拔下 USB 后重试。')
        return Handler

    def run(self,port=PORT):
        server=ThreadingHTTPServer(('0.0.0.0' if self.device else '127.0.0.1',port),self.handler())
        server.daemon_threads=True
        for target in (server.serve_forever,self.weather_loop,self.news_loop,self.module_loop,self.network_loop):
            threading.Thread(target=target,daemon=True).start()
        if self.device:
            def touch():
                try: self.device.touch_loop()
                except Exception: LOG.exception('Touch input failed')
            threading.Thread(target=touch,daemon=True).start()
            try:
                self.power=KindlePower(lambda action:self.requests.put(action),self.stop,logger=LOG)
                self.power_available=self.power.start()
                self.maintain_power()
                self.record_power('startup')
                self.capture_system_power_log()
            except Exception:
                LOG.exception('Power key input failed')
                self.power_available=False
        try: self.display_loop()
        finally:
            self.stop.set(); self.weather_wake.set(); self.news_wake.set(); self.network_wake.set()
            self.module_wake.set()
            if self.system_sleeping and self.power: self.power.wake()
            if self.power: self.power.close()
            self.restore_standby_wifi(background=False)
            server.shutdown(); server.server_close()
            if self.device: self.capture_system_power_log()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--preview',action='store_true')
    parser.add_argument('--self-test',action='store_true')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    initialize_data(ROOT/'data/agenda.json')
    if args.self_test:
        application=Application(native=False)
        image=application.frame()
        assert image.size==(600,800) and image.mode=='L'
        for mode in ('todos','agenda','calendar','weather','news_history','pomodoro','settings','wifi','screensaver','module_picker','module','photo_picker'):
            application.mode=mode
            assert application.frame().size==(600,800) and application.frame().mode=='L'
        for page in ui.SETTINGS_TABS:
            application.mode='settings'; application.settings_page=page
            assert application.frame().size==(600,800)
        for kind in ('event','city','wifi'):
            assert native_input.render(native_input.NativeForm(kind,0)).size==(600,800)
        assert native_input.pinyin_candidates('nihao')
        assert ui.clock_text(datetime(2026,10,24,14,30,tzinfo=TZ),'12')=='02:30 PM'
        assert ui.event_time('14:30','12')=='02:30 PM'
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            photo_root=Path(folder)/'photo-check'; (photo_root/'data').mkdir(parents=True)
            photo_manager=modules.ModuleManager(photo_root)
            pixels=io.BytesIO(); Image.new('RGB',(90,120),'white').save(pixels,'PNG')
            identity=photo_manager.add_photo(pixels.getvalue(),'check.png')
            assert ui.render_sleep_photo(photo_manager.photo_path(identity),True).size==(600,800)
            photo_manager.delete_photo(identity,{'settings':{}})
            # Boot preflight must not conflict with or change a persisted active timer.
            application.pomodoro=Pomodoro(Path(folder)/'pomodoro.json')
            application.mode='dashboard'; application.handle_action('standby')
            assert application.mode=='screensaver' and application.standby.is_set()
            assert application.frame().size==(600,800)
            application.handle_action('power_toggle')
            assert application.mode=='dashboard' and not application.standby.is_set()
            path=Path(folder)/'agenda.json'
            atomic_json(path,dict(token='self-test',revision=0,events=[],todos=[],city=''))
            store=Store(path)
            changed=store.change(0,'setting',dict(key='time_format',value='12'))
            assert changed['settings']['time_format']=='12'
            assert store.read()['settings']['time_format']=='12'
            store.change(1,'setting',dict(key='news_wallstreetcn',value=False))
            store.change(2,'setting',dict(key='news_eastmoney',value=False))
            assert not ui.enabled_news(store.read(),[dict(name='AI',source='华尔街见闻'),dict(name='AI',source='东方财富')])
        import PIL, qrcode
        assert qrcode.make('Kindle standalone self-test').convert('L').width>0
        ssl.create_default_context(cafile=str(ROOT/'assets/cacert.pem'))
        print('SELF_TEST_OK Python='+sys.version.split()[0]+' Pillow='+PIL.__version__,flush=True)
        return
    application=Application(native=not args.preview)
    for signum in (signal.SIGTERM,signal.SIGINT): signal.signal(signum,lambda *unused:application.request_stop())
    try: application.run()
    finally:
        if application.device: application.device.close()
    if application.restart_update: sys.exit(75)

if __name__=='__main__': main()
