"""Bounded LAN packages and recoverable updates. This bootstrap stays USB-managed."""
import ast,errno,hashlib,io,json,os,re,shutil,signal,subprocess,sys,time,uuid,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent
CORE=('app.py','renderer.py','pages.py','store.py','native_input.py','device_control.py','modules.py','phone.py')
MAX_PACKAGE=4*1024*1024
MAX_EXPANDED=8*1024*1024

def write_json(path,value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.new')
    with temp.open('w',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False); f.flush(); os.fsync(f.fileno())
    os.replace(str(temp),str(path))

def allowed(name):
    return name in CORE or bool(re.fullmatch(r'assets/ime/(?:[a-z]\.json|LICENSE|AUTHORS|NOTICE\.txt)',name))

def validate(payload):
    if len(payload)>MAX_PACKAGE: raise ValueError('更新包过大。')
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            names=z.namelist()
            if len(names)!=len(set(names)) or len(names)>48: raise ValueError('文件列表不正确。')
            if 'manifest.json' not in names or any(n!='manifest.json' and not allowed(n) for n in names):
                raise ValueError('更新包包含不允许修改的文件。')
            if any(i.file_size>2*1024*1024 for i in z.infolist()) or sum(i.file_size for i in z.infolist())>MAX_EXPANDED:
                raise ValueError('解压内容过大。')
            manifest=json.loads(z.read('manifest.json'))
            if manifest.get('format')!=1 or not re.fullmatch(r'[a-zA-Z0-9._-]{1,80}',manifest.get('build','')):
                raise ValueError('更新包版本无效。')
            hashes=manifest.get('files',{})
            if set(hashes)!=set(names)-{'manifest.json'} or not set(CORE)<=set(hashes):
                raise ValueError('更新包缺少程序文件。')
            files={}
            for name,digest in hashes.items():
                data=z.read(name)
                if hashlib.sha256(data).hexdigest()!=digest: raise ValueError('文件校验失败。')
                if name.endswith('.py'): ast.parse(data.decode('utf-8'),filename=name,feature_version=(3,9))
                elif name.endswith('.json'): json.loads(data.decode('utf-8'))
                files[name]=data
            return manifest,files
    except (zipfile.BadZipFile,KeyError,TypeError,SyntaxError,UnicodeError) as e:
        raise ValueError('更新包不完整或不兼容。') from e

def package(root=ROOT):
    root=Path(root)
    names=list(CORE)+[p.relative_to(root).as_posix() for p in sorted((root/'assets/ime').iterdir()) if p.is_file()]
    files={name:(root/name).read_bytes() for name in names}
    build=re.search(r"^BUILD=['\"]([^'\"]+)",files['app.py'].decode('utf-8'),re.M).group(1)
    manifest=dict(format=1,build=build,files={name:hashlib.sha256(data).hexdigest() for name,data in files.items()})
    result=io.BytesIO()
    with zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json',json.dumps(manifest))
        for name,data in files.items(): z.writestr(name,data)
    payload=result.getvalue(); validate(payload)
    return payload,build

def stage(payload,root=ROOT):
    root=Path(root)
    if (root/'data/update-pending.json').exists(): raise ValueError('已有更新正在处理。')
    manifest,files=validate(payload)
    folder=root/'data/updates'/uuid.uuid4().hex
    candidate=folder/'candidate'; candidate.mkdir(parents=True)
    if shutil.disk_usage(root).free<MAX_EXPANDED*4: raise ValueError('可用空间不足，更新未开始。')
    for name,data in files.items():
        target=candidate/name; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(data)
    # Preflight uses current data but cannot write to it.
    (candidate/'assets').mkdir(exist_ok=True)
    shutil.copy2(root/'updater.py',candidate/'updater.py')
    # Kindle user storage is FAT: do not depend on symbolic or hard links.
    shutil.copytree(root/'vendor',candidate/'vendor')
    shutil.copy2(root/'assets/cacert.pem',candidate/'assets/cacert.pem')
    (candidate/'data').mkdir()
    for name in ('agenda.json','weather-cache.json','news-cache.json'):
        if (root/'data'/name).exists(): shutil.copy2(root/'data'/name,candidate/'data'/name)
    environment=dict(os.environ,KINDLE_PREFLIGHT_ASSETS=str(root/'assets'))
    check=subprocess.run([sys.executable,'-B',str(candidate/'app.py'),'--self-test'],cwd=str(candidate),env=environment,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=55)
    if check.returncode:
        (folder/'check.log').write_bytes(check.stdout[-24000:])
        raise ValueError('新程序启动检查失败，当前版本保持不变。')
    state=dict(id=folder.name,build=manifest['build'],files=manifest['files'],phase='staged')
    write_json(root/'data/update-pending.json',state)
    return state

def state(root=ROOT):
    path=Path(root)/'data/update-pending.json'
    try: return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError: return None
    except OSError as error:
        # USB export invalidates the filesystem while we still supervise the
        # child. Keep supervising; do not crash and orphan the dashboard.
        if error.errno in (errno.EIO,errno.ENODEV,getattr(errno,'ESTALE',116)): return None
        raise

def folder_for(root,s):
    if not re.fullmatch(r'[a-f0-9]{32}',s['id']): raise ValueError('Invalid update id')
    return root/'data/updates'/s['id']

def replace_file(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    tmp=target.with_name(target.name+'.update')
    with tmp.open('wb') as f:
        f.write(source.read_bytes()); f.flush(); os.fsync(f.fileno())
    os.replace(str(tmp),str(target))

def apply(root=ROOT):
    root=Path(root); s=state(root)
    if not s or s['phase']!='staged': return
    folder=folder_for(root,s); existing=[]
    for name,digest in s['files'].items():
        if not allowed(name): raise ValueError('Invalid target')
        if hashlib.sha256((folder/'candidate'/name).read_bytes()).hexdigest()!=digest: raise ValueError('Staged file changed')
        old=root/name
        if old.exists():
            saved=folder/'backup'/name; saved.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(old,saved); existing.append(name)
    s.update(phase='applying',existing=existing)
    write_json(root/'data/update-pending.json',s)
    try:
        for name in s['files']: replace_file(folder/'candidate'/name,root/name)
        s['phase']='trial'; write_json(root/'data/update-pending.json',s)
    except Exception:
        recover(root); raise

def recover(root=ROOT):
    root=Path(root); s=state(root)
    if not s: return False
    folder=folder_for(root,s)
    if s['phase'] in ('applying','trial'):
        for name in s['files']:
            if not allowed(name): raise ValueError('Invalid recovery target')
            if name in s.get('existing',[]): replace_file(folder/'backup'/name,root/name)
            elif (root/name).is_file(): (root/name).unlink()
    write_json(root/'data/update-result.json',dict(id=s['id'],build=s['build'],result='rolled_back',at=time.time()))
    (root/'data/update-pending.json').unlink()
    return True

def confirm(root=ROOT):
    root=Path(root); s=state(root)
    if s and s['phase']=='trial':
        write_json(root/'data/update-result.json',dict(id=s['id'],build=s['build'],result='installed',at=time.time()))
        (root/'data/update-pending.json').unlink()

def supervise(root=ROOT):
    root=Path(root); child=None; stopping=False
    def stop(signum,frame):
        nonlocal stopping
        stopping=True
        if child and child.poll() is None: child.terminate()
    for sig in (signal.SIGTERM,signal.SIGINT): signal.signal(sig,stop)
    recover(root)
    while not stopping:
        try:
            check=subprocess.run([sys.executable,'-B',str(root/'app.py'),'--self-test'],cwd=str(root),timeout=60)
            result=check.returncode
        except subprocess.TimeoutExpired: result=6
        if result:
            if recover(root): continue
            return result
        child=subprocess.Popen([sys.executable,'-B','-u',str(root/'app.py')],cwd=str(root))
        started=time.monotonic(); stop_at=None
        try:
            while child.poll() is None:
                if time.monotonic()-started>65:
                    pending=state(root)
                    if pending and pending['phase']=='trial' and not stop_at: child.terminate(); stop_at=time.monotonic()
                if stopping and stop_at is None: stop_at=time.monotonic()
                if stop_at and time.monotonic()-stop_at>5: child.kill()
                time.sleep(0.25)
        finally:
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait(timeout=3)
        code=child.returncode
        if stopping: return 0
        if code==75:
            try: apply(root)
            except Exception:
                recover(root)
            continue
        if recover(root): continue
        return code
    return 0

if __name__=='__main__':
    sys.exit(supervise())
