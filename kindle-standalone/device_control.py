"""Kindle WiFi controls and physical power-key input."""
import glob,logging,os,re,select,shutil,struct,subprocess,threading,time
try:
    import fcntl
except ImportError:
    fcntl=None

LOG=logging.getLogger('kindle')
IFF_UP=0x1
EV_KEY=0x01
KEY_POWER=116
KEY_MAX=0x2ff
EVENT_STRUCT=struct.Struct('@llHHi')

class DeviceControlError(RuntimeError): pass

def _command(args,timeout=30):
    result=subprocess.run(list(args),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT,timeout=timeout,shell=False)
    output=result.stdout.decode('utf-8',errors='replace') if isinstance(result.stdout,bytes) else (result.stdout or '')
    return result.returncode,output.rstrip('\r\n')

def _last_line(output):
    lines=output.splitlines()
    return lines[-1].strip() if lines else ''

def _detail(output):
    return ' '.join(output.split())[:160]

def _missing(code):
    return code in (126,127)

def _wpa_unescape(value):
    data=bytearray(); i=0
    escapes={'n':b'\n','r':b'\r','t':b'\t'}
    while i<len(value):
        if value[i]!='\\':
            data.extend(value[i].encode('utf-8',errors='replace')); i+=1; continue
        i+=1
        if i==len(value): data.append(92); break
        char=value[i]; i+=1
        if char=='x' and i+2<=len(value) and re.match(r'^[0-9a-fA-F]{2}$',value[i:i+2]):
            data.append(int(value[i:i+2],16)); i+=2
        elif char in escapes: data.extend(escapes[char])
        else: data.extend(char.encode('utf-8',errors='replace'))
    return data.decode('utf-8',errors='replace')

def _wpa_quote(value):
    return '"'+value.replace('\\','\\\\').replace('"','\\"')+'"'

def parse_wpa_status(output):
    if isinstance(output,bytes): output=output.decode('utf-8',errors='replace')
    result={}
    for line in (output or '').splitlines():
        if '=' not in line: continue
        key,value=line.split('=',1); key=key.strip()
        result[key]=_wpa_unescape(value) if key=='ssid' else value.strip()
    return result

def parse_scan_results(output,connected_ssid=None):
    if isinstance(output,bytes): output=output.decode('utf-8',errors='replace')
    if isinstance(connected_ssid,dict):
        connected_ssid=connected_ssid.get('ssid') if connected_ssid.get('connected') else None
    networks={}
    for line in (output or '').splitlines():
        fields=line.split('\t',4)
        if len(fields)!=5: continue
        try: signal=int(fields[2].strip())
        except ValueError: continue
        ssid=_wpa_unescape(fields[4])
        if not ssid or not ssid.strip('\x00') or any(ord(char)<32 or ord(char)==127 for char in ssid): continue
        flags=fields[3].upper()
        item=dict(ssid=ssid,signal=signal,secure=bool(re.search(r'WPA|WEP|RSN|SAE|OWE|DPP|OSEN',flags)),
                  connected=ssid==connected_ssid)
        old=networks.get(ssid)
        if old is None or signal>old['signal']: networks[ssid]=item
        elif signal==old['signal']:
            old['secure']=old['secure'] or item['secure']; old['connected']=old['connected'] or item['connected']
    return sorted(networks.values(),key=lambda item:(-item['signal'],item['ssid']))

class WiFiManager:
    def __init__(self,interface='wlan0',runner=None,sleep=time.sleep,monotonic=time.monotonic):
        if (not isinstance(interface,str) or not interface or interface in ('.','..') or
                interface.startswith('-') or '/' in interface or '\\' in interface or
                any(ord(char)<32 or ord(char)==127 for char in interface)):
            raise ValueError('网络接口名无效。')
        self.interface=interface
        self.runner=_command if runner is None else runner
        self.sleep=sleep; self.monotonic=monotonic
        self._default_runner=runner is None

    def _has_tool(self,name):
        return not self._default_runner or shutil.which(name) is not None

    def _require_tool(self,name):
        if not self._has_tool(name): raise DeviceControlError('不支持：缺少 '+name+'。')

    def _call(self,args,timeout=8):
        try: code,output=self.runner(list(args),timeout=timeout)
        except FileNotFoundError: raise DeviceControlError('不支持：缺少 '+args[0]+'。') from None
        except subprocess.TimeoutExpired: raise DeviceControlError(args[0]+' 执行超时。') from None
        except Exception: raise DeviceControlError(args[0]+' 执行失败。') from None
        if isinstance(output,bytes): output=output.decode('utf-8',errors='replace')
        elif output is None: output=''
        elif not isinstance(output,str): output=str(output)
        return code,output.rstrip('\r\n')

    def _wpa(self,args,timeout=8):
        self._require_tool('wpa_cli')
        return self._call(['wpa_cli','-i',self.interface]+list(args),timeout)

    def _wpa_ok(self,args,action,secret=False):
        code,output=self._wpa(args)
        if _missing(code): raise DeviceControlError('不支持：缺少 wpa_cli。')
        if code or _last_line(output).upper()!='OK':
            detail='' if secret else _detail(output)
            raise DeviceControlError(action+'失败'+(('：'+detail) if detail else '。'))

    def _read_flags(self):
        try:
            with open('/sys/class/net/'+self.interface+'/flags','r',encoding='ascii') as stream:
                return int(stream.read().strip(),0)&IFF_UP!=0
        except (OSError,ValueError,IndexError): return None

    def radio_state(self):
        """Read firmware radio switches; a down interface is not airplane mode."""
        if not self._has_tool('lipc-get-prop'): return None
        result={}
        for service,prop in (('com.lab126.cmd','wirelessEnable'),('com.lab126.wifid','enable')):
            try: code,value=self._call(['lipc-get-prop','-i',service,prop],3)
            except DeviceControlError: return None
            if code or value.strip() not in ('0','1'): return None
            result[service]=int(value.strip())
        return result

    def _set_radio(self,enabled):
        if not self._has_tool('lipc-set-prop'): return False
        previous=self.radio_state()
        if previous is None: return False
        switches=[('com.lab126.cmd','wirelessEnable'),('com.lab126.wifid','enable')]
        if not enabled: switches.reverse()
        try:
            for service,prop in switches:
                code,output=self._call(['lipc-set-prop','-i',service,prop,str(int(enabled))],3)
                LOG.info('POWER_WIFI_COMMAND service=%s property=%s requested=%s code=%s detail=%s',
                         service,prop,int(enabled),code,_detail(output))
                if code: raise DeviceControlError('Kindle 系统未能切换 Wi-Fi 开关（'+prop+'）。')
            # The firmware applies radio policy asynchronously. An immediate
            # stale read is not a failed switch and must not trigger rollback.
            deadline=self.monotonic()+6
            confirmed=None
            for attempt in range(21):
                confirmed=self.radio_state()
                if confirmed is not None and all(value==int(enabled) for value in confirmed.values()): break
                if self.monotonic()>=deadline or attempt==20: break
                self.sleep(.25)
            if confirmed is None or any(value!=int(enabled) for value in confirmed.values()):
                LOG.warning('POWER_WIFI_NOT_CONFIRMED requested=%s actual=%s',int(enabled),confirmed)
                raise DeviceControlError('Kindle 系统尚未确认 Wi-Fi 开关状态，请稍后重试。')
        except DeviceControlError as error:
            LOG.warning('POWER_WIFI_CHANGE_FAILED requested=%s detail=%s',int(enabled),error)
            # Restore both original switches after a partial failure. Never
            # report radio-off merely because ifconfig succeeded.
            for service,prop in reversed(switches):
                try: self._call(['lipc-set-prop','-i',service,prop,str(previous[service])],3)
                except DeviceControlError: pass
            raise
        LOG.info('POWER_WIFI backend=firmware enabled=%s',int(enabled))
        return True

    def status(self):
        enabled=self._read_flags(); data={}; message=''
        radio=self.radio_state()
        if radio is not None:
            enabled=all(radio.values())
            if not enabled:
                return dict(available=True,enabled=False,connected=False,ssid=None,
                            state='INTERFACE_DISABLED',address=None,message='WiFi 已关闭。')
        wpa_available=self._has_tool('wpa_cli')
        available=wpa_available and (self._has_tool('ifconfig') or self._has_tool('ip'))
        if not wpa_available: message='不支持：缺少 wpa_cli。'
        elif not available: message='不支持：缺少 ifconfig 或 ip。'
        if wpa_available:
            try: code,output=self._wpa(['status'],5)
            except DeviceControlError as error: message=str(error)
            else:
                if _missing(code): available=False; message='不支持：缺少 wpa_cli。'
                elif code: message='读取 WiFi 状态失败'+(('：'+_detail(output)) if _detail(output) else '。')
                else: data=parse_wpa_status(output)
        state=data.get('wpa_state')
        if enabled is None and state=='INTERFACE_DISABLED': enabled=False
        connected=state=='COMPLETED'
        if not message:
            if connected: message='WiFi 已连接。'
            elif enabled is False: message='WiFi 已关闭。'
            elif state is None: message='WiFi 状态未知。'
            else: message='WiFi 已启用，未连接。'
        return dict(available=available,enabled=enabled,connected=connected,
                    ssid=data.get('ssid') or None,state=state,address=data.get('ip_address') or None,
                    message=message)

    def is_enabled(self):
        return self.status()['enabled']

    def scan(self):
        self._require_tool('wpa_cli')
        self._wpa_ok(['scan'],'扫描 WiFi')
        self.sleep(2)
        code,output=self._wpa(['scan_results'],10)
        if _missing(code): raise DeviceControlError('不支持：缺少 wpa_cli。')
        if code: raise DeviceControlError('读取 WiFi 扫描结果失败'+(('：'+_detail(output)) if _detail(output) else '。'))
        current=self.status()
        return parse_scan_results(output,current)

    def _set_link(self,enabled):
        action='启用网络接口' if enabled else '禁用网络接口'; state='up' if enabled else 'down'
        if self._has_tool('ifconfig'):
            code,output=self._call(['ifconfig',self.interface,state])
            if not _missing(code):
                if code: raise DeviceControlError(action+'失败'+(('：'+_detail(output)) if _detail(output) else '。'))
                return
        if self._has_tool('ip'):
            code,output=self._call(['ip','link','set','dev',self.interface,state])
            if _missing(code): raise DeviceControlError('不支持：缺少 ifconfig 或 ip。')
            if code: raise DeviceControlError(action+'失败'+(('：'+_detail(output)) if _detail(output) else '。'))
            return
        raise DeviceControlError('不支持：缺少 ifconfig 或 ip。')

    def set_enabled(self,enabled):
        if self._set_radio(enabled): return self.status()
        if enabled:
            self._set_link(True); self._wpa_ok(['reconnect'],'启用 WiFi')
        else:
            try: self._wpa_ok(['disconnect'],'禁用 WiFi')
            except DeviceControlError: pass
            self._set_link(False)
        LOG.info('POWER_WIFI backend=interface enabled=%s',int(enabled))
        return self.status()

    def _validate(self,ssid,password):
        if not isinstance(ssid,str) or not ssid: raise DeviceControlError('SSID 不能为空。')
        if any(ord(char)<32 or 127<=ord(char)<=159 for char in ssid): raise DeviceControlError('SSID 不能包含控制字符。')
        try: size=len(ssid.encode('utf-8'))
        except UnicodeEncodeError: raise DeviceControlError('SSID 编码无效。') from None
        if size>32: raise DeviceControlError('SSID 的 UTF-8 编码不能超过 32 字节。')
        if password is None: password=''
        if not isinstance(password,str): raise DeviceControlError('WiFi 密码格式无效。')
        if password and not (8<=len(password)<=63 and all(0x20<=ord(char)<=0x7e for char in password)):
            raise DeviceControlError('WiFi 密码须为 8–63 个可打印 ASCII 字符。')
        return password

    def _cleanup(self,network,saved):
        try: self._call(['wpa_cli','-i',self.interface,'remove_network',network])
        except DeviceControlError: pass
        if saved:
            try: self._call(['wpa_cli','-i',self.interface,'save_config'])
            except DeviceControlError: pass

    def _dhcp(self):
        self._require_tool('udhcpc')
        code,output=self._call(['udhcpc','-i',self.interface,'-n','-q'],20)
        if _missing(code): raise DeviceControlError('不支持：缺少 udhcpc。')
        if code: raise DeviceControlError('已关联，但获取 IP 地址失败'+(('：'+_detail(output)) if _detail(output) else '。'))

    def _wait_connection(self,ssid):
        deadline=self.monotonic()+30
        for unused in range(35):
            current=self.status()
            if current['connected'] and current['ssid']==ssid:
                if current['address']: return current
                self._dhcp()
                current=self.status()
                if current['connected'] and current['ssid']==ssid: return current
            if current['enabled'] is False: raise DeviceControlError('WiFi 已关闭，无法连接。')
            if self.monotonic()>=deadline: break
            self.sleep(1)
        raise DeviceControlError('连接 WiFi 超时，请检查 SSID、密码或信号。')

    def connect(self,ssid,password):
        password=self._validate(ssid,password); self._require_tool('wpa_cli')
        network=None; saved=False
        try:
            code,output=self._wpa(['add_network'])
            if _missing(code): raise DeviceControlError('不支持：缺少 wpa_cli。')
            if code: raise DeviceControlError('创建 WiFi 网络配置失败'+(('：'+_detail(output)) if _detail(output) else '。'))
            ids=[line.strip() for line in output.splitlines() if line.strip().isdigit()]
            if not ids: raise DeviceControlError('创建 WiFi 网络配置失败。')
            network=str(int(ids[-1]))
            self._wpa_ok(['set_network',network,'ssid',_wpa_quote(ssid)],'设置 SSID')
            if password:
                self._wpa_ok(['set_network',network,'psk',_wpa_quote(password)],'设置 WiFi 密码',True)
                self._wpa_ok(['set_network',network,'key_mgmt','WPA-PSK'],'设置 WiFi 认证方式')
            else:
                self._wpa_ok(['set_network',network,'key_mgmt','NONE'],'设置开放网络')
            self._wpa_ok(['select_network',network],'选择 WiFi 网络')
            self._wpa_ok(['enable_network',network],'启用 WiFi 网络')
            self._wpa_ok(['save_config'],'保存 WiFi 配置')
            saved=True
            return self._wait_connection(ssid)
        except DeviceControlError:
            if network is not None: self._cleanup(network,saved)
            raise

class PowerKeyListener:
    def __init__(self,callback,stop_event,environ=None,logger=None):
        self.callback=callback; self.stop_event=stop_event
        self.environ=os.environ if environ is None else environ
        self.logger=LOG if logger is None else logger
        self.key_code=self._key_code()
        self.fds=[]; self.thread=None
        self.pressed=False; self.last_press=None
        self.stop=threading.Event(); self.closed=False

    def _key_code(self):
        value=self.environ.get('KINDLE_POWER_KEY_CODE',str(KEY_POWER))
        try: code=int(str(value),0)
        except (TypeError,ValueError):
            self.logger.warning('电源键键码无效，使用默认 KEY_POWER。'); return KEY_POWER
        if not 0<=code<=KEY_MAX:
            self.logger.warning('电源键键码无效，使用默认 KEY_POWER。'); return KEY_POWER
        return code

    def _power_capable(self,fd):
        if fcntl is None: return False
        size=max(KEY_MAX//8+1,self.key_code//8+1)
        # EVIOCGBIT(EV_KEY, size): bit 116 distinguishes power from touch keys.
        request=0x80000000|(size<<16)|(ord('E')<<8)|(0x40+EV_KEY)
        try: bits=fcntl.ioctl(fd,request,bytes(size))
        except OSError: return False
        index=self.key_code//8
        return len(bits)>index and bool(bits[index]&(1<<(self.key_code%8)))

    def start(self):
        if self.closed: return False
        if self.thread and self.thread.is_alive(): return True
        if self.stop_event.is_set():
            self.logger.warning('停止事件已设置，电源键监听未启动。'); return False
        if fcntl is None:
            self.logger.warning('不支持：当前系统无法读取 Linux 输入设备。'); return False
        self._close_fds(); self.stop.clear(); self.pressed=False; self.last_press=None
        configured=self.environ.get('KINDLE_POWER_KEY_DEVICE')
        paths=[configured] if configured else sorted(glob.glob('/dev/input/event*'))
        for path in paths:
            fd=None
            try:
                fd=os.open(path,os.O_RDONLY|getattr(os,'O_NONBLOCK',0)|getattr(os,'O_CLOEXEC',0))
                if self._power_capable(fd): self.fds.append(fd); fd=None
            except OSError:
                self.logger.warning('无法读取输入设备：'+str(path))
            finally:
                if fd is not None:
                    try: os.close(fd)
                    except OSError: pass
        if not self.fds:
            self.logger.warning('未找到具有电源键能力的输入设备。'); return False
        self.thread=threading.Thread(target=self._listen,name='kindle-power-key',daemon=True)
        try: self.thread.start()
        except RuntimeError:
            self._close_fds(); self.thread=None; self.logger.warning('电源键监听线程启动失败。'); return False
        return True

    def _drop_fd(self,fd):
        if fd in self.fds: self.fds.remove(fd)
        try: os.close(fd)
        except OSError: pass

    def _listen(self):
        buffers={fd:b'' for fd in self.fds}
        while self.fds and not self.stop.is_set() and not self.stop_event.is_set():
            try: ready,unused,unused=select.select(list(self.fds),[],[],0.5)
            except (OSError,ValueError):
                if not self.stop.is_set(): self.logger.warning('电源键输入监听中断。')
                break
            for fd in ready:
                try: chunk=os.read(fd,EVENT_STRUCT.size*32)
                except BlockingIOError: continue
                except OSError:
                    self._drop_fd(fd); buffers.pop(fd,None); continue
                if not chunk:
                    self._drop_fd(fd); buffers.pop(fd,None); continue
                data=buffers.get(fd,b'')+chunk; complete=len(data)//EVENT_STRUCT.size*EVENT_STRUCT.size
                buffers[fd]=data[complete:]
                for offset in range(0,complete,EVENT_STRUCT.size):
                    seconds,microseconds,kind,code,value=EVENT_STRUCT.unpack_from(data,offset)
                    self._handle_event(kind,code,value)

    def _handle_event(self,kind,code,value,now=None):
        if kind!=EV_KEY or code!=self.key_code: return False
        if value==0:
            self.pressed=False; return False
        if value!=1 or self.pressed: return False
        self.pressed=True
        now=time.monotonic() if now is None else now
        if self.last_press is not None and now-self.last_press<0.6: return False
        self.last_press=now
        try: self.callback()
        except Exception: self.logger.warning('电源键回调执行失败。')
        return True

    def _close_fds(self):
        for fd in self.fds:
            try: os.close(fd)
            except OSError: pass
        self.fds=[]

    def close(self):
        if self.closed: return
        self.closed=True; self.stop.set()
        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread(): self.thread.join()
        self._close_fds(); self.thread=None


class FrameworkBridge:
    """Keep powerd functional while the native reader framework is stopped.

    powerd ignores the power button if com.lab126.kaf.frameworkStarted is
    absent. This temporary service follows KOReader's no-framework contract.
    It must release the name before restarting the real reader framework.
    """
    SCRIPT='''
local module = require("liblipclua")
local api = type(module) == "table" and module or lipc
assert(api, "liblipclua unavailable")
local handle, message = api.init("com.lab126.kaf")
assert(handle, message or "framework service unavailable")
local ready = handle:register_int_property("frameworkStarted", "r")
assert(ready, "framework property unavailable")
ready.value = 1
local parent = assert(os.getenv("KINDLE_DASHBOARD_PARENT"))
while true do
    api.run_event_loop(10)
    local owner = io.open("/proc/" .. parent .. "/stat", "r")
    if not owner then break end
    owner:close()
end
handle:close()
'''
    def __init__(self,runner=None,logger=None):
        self.runner=runner or _command; self.logger=logger or LOG
        self.process=None; self.unloaded=[]; self.next_try=0

    def ready(self):
        try:
            code,value=self.runner(['lipc-get-prop','com.lab126.kaf','frameworkStarted'],timeout=2)
            return code==0 and value.strip()=='1'
        except (OSError,subprocess.TimeoutExpired): return False

    def ensure(self):
        if self.process and self.process.poll() is None: return True
        if time.monotonic()<self.next_try: return False
        self.next_try=time.monotonic()+60
        if self.ready(): return True  # Never replace another owner's service.
        interpreter=shutil.which('lua') or shutil.which('lua5.1')
        if not interpreter:
            self.logger.warning('POWER_FRAMEWORK_BRIDGE_FAILED missing Lua'); return False
        try:
            self.process=subprocess.Popen([interpreter,'-e',self.SCRIPT],stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,close_fds=True,
                env=dict(os.environ,KINDLE_DASHBOARD_PARENT=str(os.getpid())))
            for unused in range(15):
                time.sleep(.1)
                if self.process.poll() is not None: break
                if self.ready():
                    code,loaded=self.runner(['lipc-get-prop','com.lab126.blanket','load'],timeout=2)
                    if code==0:
                        for name in ('splash','screensaver'):
                            if name not in re.findall(r'[A-Za-z_]+',loaded): continue
                            code,_=self.runner(['lipc-set-prop','com.lab126.blanket','unload',name],timeout=2)
                            if code==0 and name not in self.unloaded: self.unloaded.append(name)
                    self.logger.info('POWER_FRAMEWORK_BRIDGE_READY'); return True
            detail='not ready'
            if self.process.poll() is not None:
                detail=_detail(self.process.communicate()[1].decode('utf-8',errors='replace'))
            self.logger.warning('POWER_FRAMEWORK_BRIDGE_FAILED %s',detail)
        except (OSError,subprocess.TimeoutExpired) as error:
            self.logger.warning('POWER_FRAMEWORK_BRIDGE_FAILED %s',type(error).__name__)
        self.close(); return False

    def close(self):
        process=self.process; self.process=None
        if process:
            try:
                if process.poll() is None: process.terminate()
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=2)
            except OSError: pass
            if process.stderr: process.stderr.close()
        for name in self.unloaded:
            try: self.runner(['lipc-set-prop','com.lab126.blanket','load',name],timeout=2)
            except (OSError,subprocess.TimeoutExpired): pass
        self.unloaded=[]


class KindlePower:
    """Use powerd's state events on touch Kindles; evdev is only a fallback.

    LIPC sleep/wake notifications describe states, so duplicate notifications
    must not toggle the screen twice. The OS remains responsible for suspend.
    """
    EVENTS='*'
    SLEEP_STATES=('screensaver','readytosuspend','suspended','suspend','sleep')
    def __init__(self,callback,stop_event,runner=None,logger=None):
        self.callback=callback; self.stop_event=stop_event
        self.runner=runner or _command; self.logger=logger or LOG
        self.stop=threading.Event(); self.process=None; self.thread=None; self.fallback=None
        self.backend='unavailable'; self.last_event=''; self.last_event_at=None
        self.last_maintain=0; self.protected=None; self.sleeping=False
        self.read_fd=None; self.pty_fd=None; self.transport='pipe'
        self.state=''; self.last_transition=0; self.next_check=0
        self.last_errors={}; self.command_lock=threading.RLock()
        self.recent_events=[]; self.probe_cache={}; self.probe_at=0

    def call(self,name,value):
        try:
            code,output=self.runner(['lipc-set-prop','-i','com.lab126.powerd',name,str(value)],timeout=3)
            if code and self.last_errors.get(name)!=code:
                self.logger.warning('POWER_COMMAND_FAILED property=%s code=%s detail=%s',name,code,_detail(output))
            if code: self.last_errors[name]=code
            else: self.last_errors.pop(name,None)
            return code==0
        except (OSError,subprocess.TimeoutExpired) as error:
            if self.last_errors.get(name)!=type(error).__name__:
                self.logger.warning('POWER_COMMAND_FAILED property=%s error=%s',name,type(error).__name__)
            self.last_errors[name]=type(error).__name__; return False

    def read_state(self):
        try:
            code,output=self.runner(['lipc-get-prop','-s','com.lab126.powerd','state'],timeout=3)
        except (OSError,subprocess.TimeoutExpired): return ''
        state=output.strip().strip('[]').lower() if not code else ''
        if state not in self.SLEEP_STATES+('active','ready','resuming'):
            if 'state' not in self.last_errors: self.logger.warning('POWER_STATE_UNAVAILABLE code=%s',code)
            self.last_errors['state']=code or 'unknown'; return ''
        self.last_errors.pop('state',None)
        if state!=self.state: self.logger.info('POWER_STATE %s',state)
        self.state=state
        return state

    def reconcile(self):
        # This does not schedule RTC wakeups. During CPU suspend this thread is
        # frozen too; after a real wake it repairs any missed LIPC notification.
        with self.command_lock:
            if time.monotonic()-self.last_transition<2: return
            state=self.read_state()
            if state in self.SLEEP_STATES and not self.sleeping: self._report(True)
            elif state=='active' and self.sleeping: self._report(False)

    def _report(self,sleeping):
        if self.sleeping==sleeping: return
        self.sleeping=sleeping; self.last_transition=time.monotonic()
        self.callback('system_sleep' if sleeping else 'system_wake')

    def start(self):
        if self.stop_event.is_set(): return False
        initial_state=self.read_state()
        if shutil.which('lipc-wait-event'):
            try:
                self.process=self._launch()
                time.sleep(.15)
                if self.process.poll() is not None: raise OSError('powerd listener unavailable')
                self.backend='powerd'
                self.thread=threading.Thread(target=self._listen,name='kindle-powerd',daemon=True)
                self.thread.start()
                self.logger.info('POWER_BACKEND powerd')
                return True
            except OSError:
                self._terminate(); self.logger.warning('Powerd events unavailable; trying evdev.')
        if initial_state and shutil.which('lipc-set-prop'):
            self.backend='powerd'
            self.thread=threading.Thread(target=self._listen,name='kindle-powerd',daemon=True)
            self.thread.start()
            self.logger.info('POWER_BACKEND powerd state reconciliation')
            return True
        self.fallback=PowerKeyListener(lambda:self.callback('power_toggle'),self.stop_event,logger=self.logger)
        if self.fallback.start(): self.backend='evdev'; return True
        return False

    def _launch(self):
        # A pipe can buffer C stdout until several KB accumulate. A PTY makes
        # lipc-wait-event flush each line; Python's bufsize=0 alone cannot do it.
        master=slave=None
        try:
            import pty
            master,slave=pty.openpty()
        except (ImportError,OSError): pass
        try:
            process=subprocess.Popen(['lipc-wait-event','-m','com.lab126.powerd',self.EVENTS],
                stdin=subprocess.DEVNULL,stdout=slave if slave is not None else subprocess.PIPE,
                stderr=subprocess.DEVNULL,bufsize=0,close_fds=True)
        except OSError:
            if master is not None: os.close(master)
            raise
        finally:
            if slave is not None: os.close(slave)
        self.pty_fd=master
        self.read_fd=master if master is not None else process.stdout.fileno()
        self.transport='pty' if master is not None else 'pipe'
        self.logger.info('POWER_LISTENER wildcard transport=%s',self.transport)
        return process

    def feed(self,line):
        self.recent_events.append(dict(at=time.time(),text=line.strip()[:240]))
        self.recent_events=self.recent_events[-24:]
        match=re.search(r'\b(goingToScreenSaver|outOfScreenSaver|exitingScreenSaver|readyToSuspend|suspending|resuming)\b',line)
        if not match: return
        event=match.group(1); self.last_event=event; self.last_event_at=time.time()
        self.logger.info('POWER_EVENT %s detail=%s',event,_detail(line))
        with self.command_lock:
            if event=='goingToScreenSaver': self._report(True)
            elif event in ('outOfScreenSaver','exitingScreenSaver'): self._report(False)
            elif event=='resuming': self.next_check=0
        # readyToSuspend/suspending/resuming are diagnostic only: resuming can
        # be a background RTC wake and must not dismiss the user's sleep page.

    def _listen(self):
        buffer=b''
        while not self.stop.is_set() and not self.stop_event.is_set():
            process=self.process
            try:
                if time.monotonic()>=self.next_check:
                    self.reconcile(); self.next_check=time.monotonic()+5
                if process is None or process.poll() is not None:
                    if self.stop.wait(3): break
                    self._terminate()
                    self.process=self._launch(); buffer=b''; continue
                ready,_,_=select.select([self.read_fd],[],[],1)
                if not ready: continue
                chunk=os.read(self.read_fd,4096)
                if not chunk:
                    self._terminate(); continue
                buffer=(buffer+chunk)[-16384:]
                while b'\n' in buffer:
                    line,buffer=buffer.split(b'\n',1)
                    self.feed(line.decode('utf-8',errors='replace'))
            except (OSError,ValueError):
                if self.stop.is_set(): break
                self.logger.warning('Powerd listener restarting.')
                self._terminate()
        self._terminate()

    def maintain(self,protected=False,force=False):
        with self.command_lock:
            self._maintain(protected,force)

    def _maintain(self,protected=False,force=False):
        if self.backend!='powerd' or self.stop.is_set() or self.stop_event.is_set(): return
        if self.sleeping and not protected: return
        due=force or protected!=self.protected or time.monotonic()-self.last_maintain>=60
        # A notification may still be buffered when the network thread runs.
        # Check the OS before sending simulated activity, not just our cache.
        if due and not protected and self.read_state() in self.SLEEP_STATES:
            self._report(True)
            return
        if protected!=self.protected:
            if not protected and not self.sleeping:
                self.call('touchScreenSaverTimeout',1); self.last_maintain=time.monotonic()
            if self.call('preventScreenSaver',int(protected)):
                self.protected=protected
                self.logger.info('POWER_POLICY protected=%s',int(protected))
        if self.sleeping and not protected: return
        if force or time.monotonic()-self.last_maintain>=60:
            # Another Kindle component may reset this property. Read it back
            # periodically so our cached policy cannot leave the button blocked.
            try:
                code,value=self.runner(['lipc-get-prop','-i','com.lab126.powerd','preventScreenSaver'],timeout=3)
                if not code and value.strip() in ('0','1') and int(value.strip())!=int(protected):
                    self.logger.warning('POWER_POLICY_CHANGED actual=%s expected=%s',value.strip(),int(protected))
                    self.call('preventScreenSaver',int(protected))
            except (OSError,subprocess.TimeoutExpired): pass
            self.call('touchScreenSaverTimeout',1); self.last_maintain=time.monotonic()

    def sample(self,reason):
        """Event-driven battery evidence, with no RTC alarms or sleeping polls."""
        result=dict(reason=reason,wall_time=time.time(),monotonic=time.monotonic(),
                    system_state=self.read_state(),sleeping=self.sleeping)
        try: result['boottime']=time.clock_gettime(time.CLOCK_BOOTTIME)
        except (AttributeError,OSError): result['boottime']=None
        for prop,key in (('battLevel','battery'),('isCharging','charging'),('status','powerd_status')):
            try:
                code,value=self.runner(['lipc-get-prop','com.lab126.powerd',prop],timeout=2)
                if key=='powerd_status': result[key]=value[:1200] if not code else None
                else: result[key]=int(value.strip()) if not code and value.strip().isdigit() else None
            except (OSError,subprocess.TimeoutExpired): result[key]=None
        return result

    def sleep(self):
        if self.backend!='powerd': return False
        with self.command_lock:
            if self.read_state() in self.SLEEP_STATES:
                self.sleeping=True; return True
            if not self.call('preventScreenSaver',0): return False
            self.protected=False
            if self.call('powerButton',1):
                self.sleeping=True; self.last_transition=time.monotonic(); return True
            return False

    def wake(self):
        if self.backend!='powerd': return True
        with self.command_lock:
            state=self.read_state()
            # Never toggle an already-awake system back to sleep. When state
            # cannot be read, only the non-toggle wakeUp request is safe.
            if state=='active': success=True
            elif state in self.SLEEP_STATES:
                self.call('preventScreenSaver',0); self.protected=False
                success=self.call('powerButton',1)
            else: success=self.call('wakeUp',1)
            if success:
                self.sleeping=False; self.last_transition=time.monotonic()
                self.maintain(force=True)
            return success

    def diagnostics(self):
        return dict(backend=self.backend,listener_alive=bool(self.process and self.process.poll() is None),
                    last_event=self.last_event,last_event_at=self.last_event_at,sleeping=self.sleeping,
                    system_state=self.state,transport=self.transport,command_errors=dict(self.last_errors))

    def probe(self):
        """Fixed, read-only device checks; no user-supplied commands or paths."""
        if self.probe_cache and time.monotonic()-self.probe_at<10: return self.probe_cache
        checks={
            'state':['lipc-get-prop','com.lab126.powerd','state'],
            'prevent':['lipc-get-prop','com.lab126.powerd','preventScreenSaver'],
            'status':['lipc-get-prop','com.lab126.powerd','status'],
            'powerd_test':['powerd_test','-s'],
            'properties':['lipc-probe','com.lab126.powerd'],
            'listener_help':['lipc-wait-event','-h'],
            'gui':['initctl','status','lab126_gui'],
            'framework':['initctl','status','framework'],
            'daemon':['initctl','status','powerd'],
            'framework_ready':['lipc-get-prop','com.lab126.kaf','frameworkStarted'],
            'wireless_enable':['lipc-get-prop','com.lab126.cmd','wirelessEnable'],
            'wifi_enable':['lipc-get-prop','com.lab126.wifid','enable'],
            'battery_state':['lipc-get-prop','com.lab126.powerd','battStateInfo'],
        }
        result={}
        for key,args in checks.items():
            try:
                code,output=self.runner(args,timeout=3)
                result[key]=dict(code=code,output=output[:7000])
            except (OSError,subprocess.TimeoutExpired) as error: result[key]=dict(error=type(error).__name__)
        from pathlib import Path
        for name in ('lab126_gui','powerd'):
            try: result[name+'_job']=Path('/etc/upstart/'+name+'.conf').read_text(errors='replace')[:12000]
            except OSError: pass
        result['events']=list(self.recent_events)
        result['runtime']=self.diagnostics()
        result['lua']=shutil.which('lua') or shutil.which('lua5.1')
        self.probe_cache=result; self.probe_at=time.monotonic()
        return result

    def _terminate(self):
        process=self.process; self.process=None
        fd=self.pty_fd; self.pty_fd=None; self.read_fd=None
        if fd is not None:
            try: os.close(fd)
            except OSError: pass
        if not process: return
        try:
            if process.poll() is None: process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=2)
        except OSError: pass
        if process.stdout: process.stdout.close()

    def close(self):
        self.stop.set()
        # Serialize release with any in-flight maintenance. A late network or
        # HTTP worker must not reapply inhibition after native UI is restored.
        with self.command_lock:
            if self.backend=='powerd':
                if self.call('preventScreenSaver',0): self.protected=False
        self._terminate()
        if self.thread and self.thread is not threading.current_thread(): self.thread.join(timeout=4)
        if self.fallback: self.fallback.close()
