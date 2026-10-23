"""Offline agenda, direct weather, local editing and native display on KT2."""
import argparse, copy, hashlib, hmac, io, json, logging, math, os, queue, re, secrets
import signal, socket, ssl, struct, subprocess, sys, threading, time
import urllib.parse, urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from renderer import ROOT, TZ, now, render, render_news_detail, render_event_detail, future_events, paginate, news_items, font, Image, ImageDraw
from store import Store, Conflict, atomic_json, initialize_data
import pages
import renderer as ui
import native_input
import updater

sys.path.insert(0,str(ROOT/'vendor'))
LOG=logging.getLogger('kindle')
PORT=8766
BUILD='0.1.0-public'
FULL_REFRESH_INTERVAL=10
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
        self.firewall=None
        self.last_frame_digest=None
        self.updates_since_full=0
        self.full_refreshes=0
        self.frame_updates=0
        self.last_refresh='none'
        self.old_prevent=command(['lipc-get-prop','com.lab126.powerd','preventScreenSaver'])[1]
        if self.old_prevent not in ('0','1'): self.old_prevent='0'
        command(['lipc-set-prop','com.lab126.powerd','preventScreenSaver','1'])

    def stop_gui(self):
        if self.gui_stopped: return
        command(['lipc-set-prop','com.lab126.appmgrd','stop','com.lab126.browser'])
        self.gui_stopped=True
        command(['initctl','stop','lab126_gui'])
        time.sleep(3)
        result=command(['initctl','status','lab126_gui'])[1]
        if 'stop/waiting' not in result: raise RuntimeError('Kindle 原界面未停止：'+result)
        # The reader/browser may have drawn since we last owned the screen.
        self.last_frame_digest=None

    def restore_gui(self):
        if self.gui_stopped:
            command(['initctl','start','lab126_gui'])
            self.gui_stopped=False

    def display(self,path):
        self.stop_gui()
        digest=hashlib.sha256(Path(path).read_bytes()).digest()
        if digest==self.last_frame_digest: return False
        full=self.last_frame_digest is None or self.updates_since_full+1>=FULL_REFRESH_INTERVAL
        args=['eips']+(['-f'] if full else [])+['-g',str(path)]
        code,output=command(args)
        if code: raise RuntimeError('Screen draw failed: '+output)
        # Only successful, changed frames count; failed draws must remain due.
        self.last_frame_digest=digest
        self.frame_updates+=1
        self.updates_since_full=0 if full else self.updates_since_full+1
        self.full_refreshes+=int(full)
        self.last_refresh='full' if full else 'normal'
        LOG.info('SCREEN_REFRESH kind=%s changes_since_full=%s total=%s',self.last_refresh,self.updates_since_full,self.frame_updates)
        return True

    def refresh_status(self):
        return dict(interval=FULL_REFRESH_INTERVAL,changes_since_full=self.updates_since_full,
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
        self.restore_gui()
        command(['lipc-set-prop','com.lab126.powerd','preventScreenSaver',self.old_prevent])
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
                        px=round((x-xmin)*599/max(1,xmax-xmin))
                        py=round((y-ymin)*799/max(1,ymax-ymin))
                        if time.monotonic()-last<(0.12 if mode=='form' else 0.9): continue
                        last=time.monotonic()
                        action=self.app.tap_action(px,py)
                        if action is None: continue
                        LOG.info('Touch action=%s x=%s y=%s',action,px,py)
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
        self.config=self.store.read()
        self.token=self.config['token']
        self.csrf=secrets.token_hex(24)
        self.lock=threading.RLock()
        self.stop=threading.Event()
        self.weather_wake=threading.Event()
        self.news_wake=threading.Event()
        self.redraw=threading.Event()
        self.requests=queue.Queue()
        self.mode='dashboard'
        self.news_item=None
        self.news_pages=[[]]
        self.news_page=0
        self.news_return='dashboard'
        self.news_list_page=0
        self.news_list_total=1
        self.event_item=None
        self.event_confirm=False
        self.event_revision=0
        self.event_message=''
        self.shown_news=[]
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
            try: self.update_weather(relocate=(count%96==0))
            except Exception as error:
                with self.lock: self.state['error']=type(error).__name__
                LOG.warning('Weather update unavailable: %s',type(error).__name__)
            finally:
                with self.lock: self.state['weather_refreshing']=False
                self.redraw.set()
            count+=1
            self.weather_wake.wait(900)
            self.weather_wake.clear()

    def update_battery(self):
        values={}
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
        if changed and self.mode=='dashboard': self.redraw.set()

    def fetch_news(self):
        errors=[]
        try:
            data=json.loads(self.fetch_text('https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=100',ua=BROWSER_UA))
            items=[]
            for raw in (data.get('data') or {}).get('items') or []:
                name=(raw.get('title') or raw.get('content_text') or '').strip()
                link=(raw.get('uri') or '').strip()
                if not name or not link or not AI_KW.search(name): continue
                items.append({'name':name,'content':raw.get('content_text') or name,'url':link,'time':raw.get('display_time') or 0,'source':'华尔街见闻'})
                if len(items)>=50: break
            if items: return items
            errors.append('华尔街见闻暂无相关资讯')
        except Exception as error:
            errors.append('华尔街见闻：'+type(error).__name__)
        try:
            param={'uid':'','keyword':'AI 金融','type':['cmsArticleWebOld'],'client':'web','clientType':'web','clientVersion':'curr','param':{'cmsArticleWebOld':{'searchScope':'default','sort':'time','pageIndex':1,'pageSize':20,'preTag':'','postTag':''}}}
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
            if items: return items[:50]
            errors.append('东方财富返回空结果')
        except Exception as error:
            errors.append('东方财富：'+type(error).__name__)
        raise ValueError('资讯获取失败（'+'，'.join(errors)+'）')

    def update_news(self):
        items=news_items(self.fetch_news())
        with self.lock:
            items=ui.merge_news_history(self.state.get('news'),items)
            self.state.update(news=items,news_at=now().isoformat(),news_error='')
            atomic_json(self.data/'news-cache.json',{'news':items,'news_at':self.state['news_at']})
            self.news_cache_dirty=False
        LOG.info('NEWS_OK count=%s',len(items))
        self.redraw.set()

    def prune_news_cache(self):
        with self.lock:
            current=ui.merge_news_history(self.state.get('news'))
            changed=current!=self.state.get('news',[])
            if changed or self.news_cache_dirty:
                atomic_json(self.data/'news-cache.json',dict(news=current,news_at=self.state.get('news_at')))
                self.state['news']=current; self.news_cache_dirty=False
        if changed and self.mode in ('dashboard','news_history'): self.redraw.set()

    def news_loop(self):
        while not self.stop.is_set():
            try: self.update_news()
            except Exception as error:
                with self.lock: self.state['news_error']='资讯更新失败：'+type(error).__name__
                LOG.warning('News update unavailable: %s',type(error).__name__)
            finally:
                with self.lock: self.state['news_refreshing']=False
                self.redraw.set()
            self.news_wake.wait(3600)
            self.news_wake.clear()

    def snapshot(self):
        config=self.store.read()
        with self.lock: state=copy.deepcopy(self.state)
        state['news']=ui.merge_news_history(state.get('news'))
        state['pomodoro']=self.pomodoro.view()
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
        draw.text((30,30),'手机管理日程与待办',font=font(30,True),fill=0,anchor='lt')
        try: address=self.connection()
        except Exception: address=None
        if address:
            qr=qrcode.QRCode(box_size=7,border=3,error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(self.url(address)); qr.make(fit=True)
            picture=qr.make_image(fill_color='black',back_color='white').convert('L')
            frame.paste(picture,((600-picture.width)//2,135))
            draw.text((30,560),'手机连同一 Wi-Fi，扫码打开。',font=font(23),fill=0,anchor='lt')
            draw.text((30,612),'设备地址：'+address+':'+str(PORT),font=font(18),fill=0,anchor='lt')
        else:
            draw.text((30,200),'请先让 Kindle 连接 Wi-Fi。',font=font(25),fill=0,anchor='lt')
        draw.text((30,653),'正在更新，请稍候…' if self.updating else '此页开启时，电脑可无线更新看板。',font=font(20),fill=0,anchor='lt')
        draw.text((30,689),'完成后自动返回看板。' if self.updating else '离开此页即关闭更新入口。',font=font(16),fill=70,anchor='lt')
        ui.icon_button(draw,(30,728,570,777),'back')
        return frame

    def tap_action(self,x,y):
        if self.drawing or self.updating or self.mode=='editor': return None
        mode=self.mode
        if mode=='form':
            action=native_input.tap(self.form,x,y)
            return ('form',action) if action else None
        if mode=='dashboard':
            if 30<=x<368 and 25<=y<130: return 'calendar_today'
            if 368<=x<570 and 55<=y<130: return 'pomodoro'
            if ui.inside(x,y,ui.MAIN_PHONE): return 'pair'
            if ui.inside(x,y,ui.MAIN_EXIT): return 'quit'
            if 30<=x<290 and 142<=y<330: return 'weather'
            if 310<=x<570 and 142<=y<188: return 'todos'
            if 310<=x<570 and 192<=y<321:
                index=(y-192)//43
                if index<len(self.shown_todos):
                    item=self.shown_todos[index]
                    if x<353: return ('todo_state',item['id'],'1',self.shown_revision)
                    return 'todos'
            if 30<=x<315 and 340<=y<380: return 'news_history'
            if 325<=x<570 and 340<=y<380: return 'refresh_news'
            if 30<=x<570 and 384<=y<540: return ('news',(y-384)//26)
            if 30<=x<570 and 554<=y<590: return 'agenda'
            if 30<=x<570 and 592<=y<764 and not (290<=x<310):
                i=(y-592)//56*2+(0 if x<300 else 1)
                if i<5 and i<len(self.shown_events): return ('event',i)
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
        elif mode=='pair' and 728<=y<777 and 30<=x<570: return 'dashboard'
        elif mode=='news' and 728<=y<777:
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
        if self.mode=='news' and self.news_item: return render_news_detail(self.news_item,self.news_page,self.news_pages)
        if self.mode=='event' and self.event_item: return render_event_detail(self.event_item,self.event_confirm,message=self.event_message)
        config,state=self.snapshot()
        current=now()
        if self.mode=='news_history':
            selected,page,total=ui.page_slice(state['news'],self.news_list_page)
            if capture:
                self.news_list_page=page; self.news_list_total=total
                self.shown_news=copy.deepcopy(selected)
            return ui.render_news_history(state['news'],page,state.get('news_refreshing',False))
        if self.mode=='weather': return ui.render_weather(state,current)
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
            return ui.render_agenda(config,page,self.agenda_day)
        try: result=render(config,state,current=current)
        except (ValueError,TypeError,KeyError,IndexError,OverflowError,AttributeError):
            LOG.exception('Feed layout failed; keeping the local agenda available')
            state.update(weather=None,news=[],news_at=None,news_error='资讯暂不可用')
            result=render(config,state,current=current)
        if capture:
            self.shown_news=copy.deepcopy(news_items(state.get('news'))[:6])
            self.shown_events=copy.deepcopy(future_events(config,current)[:5])
            self.shown_todos=copy.deepcopy(ui.todo_items(config,False)[:3])
            self.shown_revision=config['revision']
        return result

    def handle_action(self,action):
        if action=='update_restart':
            self.restart_update=True; self.stop.set(); return
        if self.updating: return
        if action=='quit': self.stop.set(); return
        if self.mode=='form' and action=='dashboard' and self.form:
            self.form.message='正在填写内容，请先保存或取消。'
            self.redraw.set()
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
            with self.lock: self.state['news_refreshing']=True
            self.redraw.set(); self.news_wake.set()
        elif isinstance(action,tuple) and action[0]=='news':
            if 0<=action[1]<len(self.shown_news):
                self.news_return=self.mode
                self.news_item=self.shown_news[action[1]]
                body=self.news_item.get('content') or self.news_item.get('name') or ''
                if self.news_item.get('source')=='东方财富':
                    body+='\n（全文较长，请用手机打开资讯列表，点进原文查看。）'
                self.news_pages=paginate(body,size=24,per_page=16)
                self.news_page=0; self.mode='news'; self.redraw.set()
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
        elif action in ('dashboard','pair','weather','todos','agenda','calendar'):
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
                operation=('todo_' if form.kind=='todo' else '')+op
                values=dict(form.values,id=form.identity)
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
                    self.mode=self.form_return; self.form=None
                except Conflict:
                    form.message='此条内容已被修改或删除；草稿保留，请核对。'
                    form.conflict=True
                except ValueError as error: form.message=str(error)
                except OSError: form.message='暂未保存，请检查存储；草稿仍保留。'
        else: form.action(action)
        self.redraw.set()

    def display_loop(self):
        previous_minute=None
        previous_timer=None
        next_cache_prune=0
        self.redraw.set()
        while not self.stop.is_set():
            if self.device and Path('/tmp/kindle-standalone-show').exists():
                Path('/tmp/kindle-standalone-show').unlink()
                self.requests.put('dashboard')
            try:
                action=self.requests.get(timeout=0.5)
                self.handle_action(action)
                if self.stop.is_set(): break
            except queue.Empty: pass
            if not (self.data/'agenda.json').exists():
                # USB mode temporarily removes user storage from the Kindle.
                if self.device: self.device.restore_gui()
                self.mode='editor'
                continue
            if self.pomodoro.tick(): self.redraw.set()
            timer=self.pomodoro.view()
            timer_stamp=(timer['state'],math.ceil(timer['remaining']/(15 if self.mode=='pomodoro' else 60)))
            if timer_stamp!=previous_timer:
                if self.mode in ('dashboard','pomodoro'): self.redraw.set()
                previous_timer=timer_stamp
            if time.monotonic()>=next_cache_prune:
                try: self.prune_news_cache()
                except OSError: LOG.warning('News cache cleanup unavailable')
                next_cache_prune=time.monotonic()+60
            moment=now()
            stamp=moment.strftime('%Y-%m-%d %H:')+str(moment.minute if self.mode=='dashboard' else moment.minute//5)
            if self.mode in ('dashboard','pair','news','news_history','pomodoro','event','weather','todos','agenda','calendar','form') and (stamp!=previous_minute or self.redraw.is_set()):
                try:
                    self.drawing=True
                    self.redraw.clear()
                    if self.device: self.device.stop_gui()
                    frame=self.frame(capture=True)
                    path=Path('/tmp/kindle-standalone.png') if self.device else self.data/'preview.png'
                    frame.save(str(path),'PNG')
                    if self.device: self.device.display(path)
                    with self.lock:
                        status=dict(display_at=now().isoformat(),weather_at=self.state.get('weather_at'),revision=self.store.read()['revision'],mode=self.mode)
                    if self.device: status['screen_refresh']=self.device.refresh_status()
                    atomic_json(self.data/'status.json',status)
                    LOG.info('DISPLAY_OK mode=%s revision=%s',self.mode,status['revision'])
                    previous_minute=stamp
                    updater.confirm(self.root)
                except Exception:
                    LOG.exception('Display failed')
                    self.stop.set()
                finally: self.drawing=False

    def network_loop(self):
        while not self.stop.is_set():
            try: self.connection()
            except Exception: pass
            if self.device: self.update_battery()
            self.stop.wait(15)

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
                self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; frame-ancestors 'none'")
                if location: self.send_header('Location',location)
                self.end_headers()
                self.wfile.write(payload)
            def valid_path(self):
                path=urllib.parse.urlsplit(self.path).path
                return path.startswith(prefix+'/')
            def do_GET(self):
                parsed=urllib.parse.urlsplit(self.path)
                if parsed.path=='/health': return self.reply(200,b'Kindle standalone','text/plain')
                if not self.valid_path(): return self.reply(404,b'Not found','text/plain')
                query=urllib.parse.parse_qs(parsed.query)
                try:
                    if parsed.path==prefix+'/status.json':
                        config=app.store.read()
                        with app.lock:
                            status=dict(native=bool(app.device),mode=app.mode,revision=config['revision'],events=len(config['events']),todos=len(config['todos']),pending_todos=len(ui.todo_items(config,False)),weather_at=app.state.get('weather_at'),weather_error=app.state.get('error',''),news_at=app.state.get('news_at'),news_count=len(news_items(app.state.get('news'))),news_error=app.state.get('news_error',''),python=sys.version.split()[0],build=BUILD)
                            status.update(battery=app.state.get('battery'),charging=app.state.get('charging'),pomodoro=app.pomodoro.view())
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
                        with app.lock:
                            feed_items=ui.merge_news_history(app.state.get('news'))
                            news_at=app.state.get('news_at')
                            news_error=app.state.get('news_error','')
                        return self.reply(200,pages.news_page(feed_items,news_at,prefix,news_error,app.csrf,query.get('device')==['kindle']))
                    if parsed.path==prefix+'/todos':
                        return self.reply(200,pages.todo_page(app.store.read(),prefix,app.csrf,query.get('device')==['kindle'],query.get('edit',[''])[0],query.get('delete',[''])[0],query.get('message',[''])[0]))
                    if parsed.path!=prefix+'/': return self.reply(404,b'Not found','text/plain')
                    body=pages.page(app.store.read(),prefix,app.csrf,query.get('device')==['kindle'],query.get('edit',[''])[0],query.get('delete',[''])[0],query.get('message',[''])[0],query.get('date',[''])[0])
                    self.reply(200,body)
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
                if not self.valid_path() or urllib.parse.urlsplit(self.path).path!=prefix+'/action': return self.reply(404,b'Not found','text/plain')
                origin=self.headers.get('Origin')
                if origin and urllib.parse.urlsplit(origin).netloc!=self.headers.get('Host'): return self.reply(403,b'Forbidden','text/plain')
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=16384: return self.reply(413,b'Request too large','text/plain')
                    raw=urllib.parse.parse_qs(self.rfile.read(size).decode('utf-8'),keep_blank_values=True)
                    values={k:v[0] for k,v in raw.items()}
                    if not secrets.compare_digest(values.get('csrf','').encode('utf-8'),app.csrf.encode('ascii')): return self.reply(403,pages.error_page('页面已过期，请重新打开。',prefix))
                    action=values.get('action','')
                    if action=='refresh_news':
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
                        self.reply(200,pages.layout('返回看板','<h1>正在返回看板…</h1>'))
                        timer=threading.Timer(1,lambda:app.requests.put('dashboard')); timer.daemon=True; timer.start()
                        return
                    app.store.change(values.get('revision','-1'),action,values)
                    app.redraw.set()
                    if action=='city': app.weather_wake.set()
                    query={'message':'已保存，Kindle 会自动更新。'}
                    if values.get('device')=='kindle': query['device']='kindle'
                    self.reply(303,b'',location=prefix+('/todos?' if action.startswith('todo_') else '/?')+urllib.parse.urlencode(query))
                except Conflict as error: self.reply(409,pages.error_page(str(error),prefix))
                except (ValueError,UnicodeError): self.reply(400,pages.error_page('请检查内容，标题最多 100 字、备注最多 200 字。日程日期为年-月-日，时间为时:分。',prefix))
                except OSError: self.reply(503,pages.error_page('Kindle 正在通过 USB 连接电脑，请拔线后重试。',prefix))
        return Handler

    def run(self,port=PORT):
        server=ThreadingHTTPServer(('0.0.0.0' if self.device else '127.0.0.1',port),self.handler())
        server.daemon_threads=True
        for target in (server.serve_forever,self.weather_loop,self.news_loop,self.network_loop):
            threading.Thread(target=target,daemon=True).start()
        if self.device:
            def touch():
                try: self.device.touch_loop()
                except Exception: LOG.exception('Touch input failed')
            threading.Thread(target=touch,daemon=True).start()
        try: self.display_loop()
        finally:
            self.stop.set(); self.weather_wake.set(); self.news_wake.set(); server.shutdown(); server.server_close()

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
        for mode in ('todos','agenda','calendar','weather','news_history','pomodoro'):
            application.mode=mode
            assert application.frame().size==(600,800)
        assert native_input.render(native_input.NativeForm('event',0)).size==(600,800)
        assert native_input.pinyin_candidates('nihao')
        import PIL, qrcode
        assert qrcode.make('Kindle standalone self-test').convert('L').width>0
        ssl.create_default_context(cafile=str(ROOT/'assets/cacert.pem'))
        print('SELF_TEST_OK Python='+sys.version.split()[0]+' Pillow='+PIL.__version__,flush=True)
        return
    application=Application(native=not args.preview)
    for signum in (signal.SIGTERM,signal.SIGINT): signal.signal(signum,lambda *unused:application.stop.set())
    try: application.run()
    finally:
        if application.device: application.device.close()
    if application.restart_update: sys.exit(75)

if __name__=='__main__': main()
