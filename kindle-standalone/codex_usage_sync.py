"""Push quota numbers from an authenticated local Codex app-server to Kindle.

No model calls, login-token reads, account changes or persistent background jobs.
The default is one sync; --watch repeats until the user closes this helper.
"""
import argparse,ipaddress,json,os,queue,re,shutil,subprocess,threading,time,urllib.parse,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def codex_command():
    executable=shutil.which('codex.exe') or shutil.which('codex') or shutil.which('codex.cmd')
    if not executable: raise RuntimeError('请先安装 Codex CLI，并使用 ChatGPT 账号登录。')
    path=Path(executable)
    if os.name=='nt' and path.suffix.lower() in ('.cmd','.ps1','.bat',''):
        package=path.parent/'node_modules/@openai/codex'
        native=list(package.glob('node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe'))
        if native: return [str(native[0]),'app-server']
        node=shutil.which('node'); entry=package/'bin/codex.js'
        if node and entry.is_file(): return [node,str(entry),'app-server']
        raise RuntimeError('未找到 Codex 可执行程序，请重新安装 Codex CLI。')
    return [executable,'app-server']

def read_limits(command=None,timeout=35):
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    process=subprocess.Popen(command or codex_command(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL,text=True,encoding='utf-8',bufsize=1,creationflags=flags)
    inbox=queue.Queue()
    def consume():
        try:
            for line in process.stdout:
                try: message=json.loads(line)
                except ValueError: continue
                if isinstance(message,dict): inbox.put(message)
        finally: inbox.put(None)
    reader=threading.Thread(target=consume,daemon=True); reader.start()
    def send(message): process.stdin.write(json.dumps(message)+'\n'); process.stdin.flush()
    def response(identity):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try: message=inbox.get(timeout=max(.1,deadline-time.monotonic()))
            except queue.Empty: break
            if message is None: raise RuntimeError('Codex 服务已退出，请检查登录状态。')
            if message.get('id')!=identity: continue
            if 'error' in message: raise RuntimeError('Codex 尚未返回额度，请更新 CLI 并确认已登录 ChatGPT。')
            return message.get('result') or {}
        raise RuntimeError('读取 Codex 额度超时，请稍后重试。')
    try:
        send(dict(id=1,method='initialize',params=dict(clientInfo=dict(name='kindle_desk',title='Kindle Desk',version='0.2.0'))))
        response(1); send(dict(method='initialized',params={}))
        send(dict(id=2,method='account/rateLimits/read',params={}))
        raw=response(2)
        # Only send metering windows. No account identifiers or credentials.
        buckets=raw.get('rateLimitsByLimitId')
        if not isinstance(buckets,dict) or not buckets:
            single=raw.get('rateLimits')
            buckets={'codex':single} if isinstance(single,dict) else {}
        clean={}
        for key,bucket in list(buckets.items())[:12]:
            if not isinstance(bucket,dict): continue
            item={k:bucket.get(k) for k in ('limitId','limitName')}
            for slot in ('primary','secondary'):
                window=bucket.get(slot)
                item[slot]={k:window.get(k) for k in ('usedPercent','windowDurationMins','resetsAt')} if isinstance(window,dict) else None
            clean[str(key)[:80]]=item
        if not clean: raise RuntimeError('账户没有返回额度。API Key 登录不提供 ChatGPT 订阅额度。')
        return dict(rateLimitsByLimitId=clean)
    finally:
        if process.stdin: process.stdin.close()
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
        reader.join(timeout=2)
        if process.stdout: process.stdout.close()

def base_address(address):
    parsed=urllib.parse.urlsplit(address if '://' in address else 'http://'+address)
    try: local=ipaddress.ip_address(parsed.hostname or '')
    except ValueError: raise ValueError('请填写 Kindle 二维码页上的局域网 IP 地址。') from None
    if not (local.is_private or local.is_loopback) or parsed.scheme not in ('http','https') or parsed.username or parsed.password:
        raise ValueError('仅支持局域网中的 Kindle 地址。')
    parts=parsed.path.strip('/').split('/')
    token=parts[0] if parts and parts[0] else ''
    if not token:
        try: token=json.loads((ROOT/'wireless-device.json').read_text(encoding='utf-8'))['token']
        except (OSError,ValueError,KeyError): raise ValueError('请粘贴扫码后手机浏览器中的完整地址。') from None
    if not re.fullmatch(r'[a-zA-Z0-9_-]{6,128}',token): raise ValueError('连接地址中的配对码无效。')
    host='['+str(local)+']' if local.version==6 else str(local)
    return parsed.scheme+'://'+host+':'+str(parsed.port or 8766)+'/'+token

def push(base,payload):
    # Never follow redirects with the private pairing path or metering data.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs): return None
    opener=urllib.request.build_opener(NoRedirect())
    with opener.open(base+'/manager-state.json',timeout=12) as response:
        state=json.loads(response.read(32768))
    data=json.dumps(payload).encode('utf-8')
    request=urllib.request.Request(base+'/codex-usage',data=data,headers={'Content-Type':'application/json','X-CSRF-Token':state['csrf']})
    with opener.open(request,timeout=12) as response:
        if not json.loads(response.read(32768)).get('saved'): raise RuntimeError('Kindle 尚未确认保存。')

def main():
    parser=argparse.ArgumentParser(description='同步 Codex 额度到 Kindle，不传登录凭证。')
    parser.add_argument('--watch',action='store_true',help='保持窗口，每五分钟同步一次')
    parser.add_argument('--check',action='store_true',help='只检查读取，不连接 Kindle')
    args=parser.parse_args()
    if args.check:
        result=read_limits(); print('CODEX_QUOTA_OK buckets='+str(len(result['rateLimitsByLimitId']))); return
    base=base_address(input('Kindle 地址（如 192.168.1.100:8766，或扫码后的完整网址）：').strip())
    while True:
        try: push(base,read_limits()); print(time.strftime('%H:%M')+' 额度已同步到 Kindle。')
        except Exception as error:
            # urllib exceptions can contain the secret pairing URL.
            print(str(error) if isinstance(error,(RuntimeError,ValueError)) else '暂时连接失败，请确认 Kindle 已唤醒、与电脑连接同一 Wi-Fi。')
            if not args.watch: raise SystemExit(1)
        if not args.watch: break
        time.sleep(300)

if __name__=='__main__':
    try: main()
    except KeyboardInterrupt: print('\n已停止同步。')
    except (RuntimeError,ValueError) as error: print(str(error)); raise SystemExit(1)
