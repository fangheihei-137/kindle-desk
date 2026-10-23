"""Validate against device caches, back up programs, then update over USB.

Kindle data is authoritative and is never overwritten by this updater.
"""
from pathlib import Path
from datetime import datetime
import ast, hashlib, os, shutil, sys, json

ROOT=Path(__file__).resolve().parent
PROGRAM=['app.py','pages.py','renderer.py','store.py','native_input.py','updater.py','start.sh']
LIBRARY_ENTRY='IndependentDashboard.sh'

def find_kindle():
    matches=[]
    for letter in 'DEFGHIJKLMNOPQRSTUVWXYZ':
        root=Path(letter+':/')
        try:
            if (root/'kindle-standalone/start.sh').is_file(): matches.append(root)
        except OSError: continue
    if len(matches)>1: raise RuntimeError('发现多个 Kindle，请只连接需要更新的那一台。')
    if not matches: raise RuntimeError('未找到 Kindle。请先回到书库，再连接 USB。')
    return matches[0]

def verified_copy(source,target,note):
    payload=source.read_bytes()
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(target.name+'.new')
    with temporary.open('wb') as output:
        output.write(payload); output.flush(); os.fsync(output.fileno())
    if hashlib.sha256(temporary.read_bytes()).digest()!=hashlib.sha256(payload).digest():
        raise RuntimeError('写入校验失败：'+str(target))
    os.replace(temporary,target)
    if hashlib.sha256(target.read_bytes()).digest()!=hashlib.sha256(payload).digest():
        raise RuntimeError('替换后校验失败：'+str(target))
    print('  已更新 '+note)

def preflight(base):
    for name in PROGRAM:
        if name.endswith('.py'): ast.parse((ROOT/name).read_text(encoding='utf-8'),filename=name,feature_version=(3,9))
    if not (base/'data/agenda.json').is_file():
        raise RuntimeError('Kindle 日程文件缺失，请先从备份恢复；程序尚未同步。')
    from app import Application
    application=Application(root=base,native=False)
    frame=application.frame(capture=True)
    if frame.size!=(600,800) or frame.mode!='L': raise RuntimeError('画面检查失败，程序尚未同步。')
    for mode in ('todos','agenda','calendar','weather','news_history','pomodoro'):
        application.mode=mode
        frame=application.frame(capture=True)
        if frame.size!=(600,800) or frame.mode!='L': raise RuntimeError(mode+' 画面检查失败，程序尚未同步。')
    import native_input
    if not native_input.pinyin_candidates('nihao'): raise RuntimeError('拼音词库缺失，程序尚未同步。')
    form=native_input.NativeForm('event',application.store.read()['revision'])
    if native_input.render(form).size!=(600,800): raise RuntimeError('新增界面检查失败。')
    print('  已用 Kindle 当前数据检查首页、待办、日程、日历及天气详情。')

def sync():
    kindle=find_kindle()
    base=kindle/'kindle-standalone'
    if not (kindle/'documents').is_dir(): raise RuntimeError('未发现 Kindle 书库目录。')
    print('找到 Kindle：'+str(kindle))
    preflight(base)
    files=[(ROOT/name,base/name) for name in PROGRAM]
    files.extend((path,base/'assets/ime'/path.name) for path in sorted((ROOT/'assets/ime').iterdir()) if path.is_file())
    files.append((ROOT/LIBRARY_ENTRY,kindle/'documents'/LIBRARY_ENTRY))
    backup=base/'backups'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup.mkdir(parents=True)
    previous={}
    for source,target in files:
        if target.exists():
            saved=backup/target.relative_to(kindle)
            saved.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(target,saved)
            previous[target]=saved
    shutil.copy2(base/'data/agenda.json',backup/'agenda.json')
    agenda_before=(base/'data/agenda.json').read_bytes()
    try:
        for source,target in files: verified_copy(source,target,str(target.relative_to(kindle)))
        for folder in ('assets','vendor'):
            if not (base/folder).exists(): shutil.copytree(ROOT/folder,base/folder)
        if (base/'data/agenda.json').read_bytes()!=agenda_before: raise RuntimeError('日程在同步期间发生变化，请检查后重试。')
        config=json.loads(agenda_before)
        if kindle.parent==kindle:
            (ROOT/'wireless-device.json').write_text(json.dumps(dict(token=config['token'])),encoding='utf-8')
        (base/'data/restart-required').write_text('USB program update\n',encoding='utf-8')
    except Exception:
        for target,saved in previous.items():
            try: verified_copy(saved,target,'恢复 '+target.name)
            except OSError: pass
        raise
    print('  旧程序已备份；Kindle 日程和资讯缓存均已保留。')
    return kindle

if __name__=='__main__':
    try:
        sync()
        print('\n同步完成。请安全弹出并拔线，在书库打开 IndependentDashboard。')
    except Exception as error:
        print('\n同步未完成：'+str(error)); sys.exit(1)
