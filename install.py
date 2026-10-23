"""Install into an explicitly selected USB Kindle mount, preserving existing apps."""
import argparse,ast,hashlib,json,os,shutil,subprocess,sys,tempfile,uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parent
SOURCE=ROOT/'kindle-standalone'
sys.path.insert(0,str(SOURCE))
from store import initialize_data

FILES=('app.py','renderer.py','native_input.py','pages.py','store.py','updater.py',
       'start.sh','IndependentDashboard.sh')
IGNORE=shutil.ignore_patterns('__pycache__','*.pyc','*.pyo')

def check_source():
    for name in FILES:
        path=SOURCE/name
        if not path.is_file(): raise ValueError('程序文件缺失：'+name)
        if path.suffix=='.py': ast.parse(path.read_text(encoding='utf-8'),feature_version=(3,9))
    with tempfile.TemporaryDirectory(prefix='kindle-desk-check-') as temp:
        staged=Path(temp)
        for name in FILES: shutil.copy2(SOURCE/name,staged/name)
        shutil.copytree(SOURCE/'vendor',staged/'vendor',ignore=IGNORE)
        (staged/'assets').mkdir()
        shutil.copy2(SOURCE/'assets/cacert.pem',staged/'assets/cacert.pem')
        shutil.copytree(SOURCE/'assets/ime',staged/'assets/ime')
        # Fonts are read from SOURCE; no private device data is loaded.
        env=dict(os.environ,KINDLE_PREFLIGHT_ASSETS=str(SOURCE/'assets'))
        result=subprocess.run([sys.executable,'-B',str(staged/'app.py'),'--self-test'],
                              cwd=staged,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=60)
        if result.returncode:
            raise ValueError('程序自检失败：\n'+result.stdout.decode('utf-8',errors='replace')[-2000:])

def copy_program(target):
    target.mkdir()
    for name in FILES: shutil.copy2(SOURCE/name,target/name)
    for name in ('assets','vendor'): shutil.copytree(SOURCE/name,target/name,ignore=IGNORE)
    for path in target.rglob('*'):
        if path.is_file():
            original=SOURCE/path.relative_to(target)
            if hashlib.sha256(path.read_bytes()).digest()!=hashlib.sha256(original.read_bytes()).digest():
                raise OSError('复制校验失败：'+str(path.relative_to(target)))
    initialize_data(target/'data/agenda.json')

def install(mount):
    mount=Path(mount).resolve()
    if not (mount/'documents').is_dir(): raise ValueError('请选择含 documents 文件夹的 Kindle 磁盘根目录。')
    if not (mount/'python3/bin/python3.9').is_file():
        raise ValueError('尚未找到 Kindle Python 3.9 运行环境，请先按 docs/INSTALL.md 安装。')
    final=mount/'kindle-standalone'
    extension=mount/'extensions/kindle-desk'
    entry=mount/'documents/IndependentDashboard.sh'
    if final.exists() or extension.exists() or entry.exists():
        raise ValueError('发现已有看板。首次安装不会覆盖，请使用同步/无线更新并保留 data。')
    check_source()
    if shutil.disk_usage(mount).free<60*1024*1024: raise ValueError('需要至少 60 MB 可用空间。')
    stage=mount/('kindle-desk-install-'+uuid.uuid4().hex)
    copy_program(stage)
    os.replace(stage,final)
    shutil.copytree(ROOT/'extensions/kindle-desk',extension)
    shutil.copy2(final/'IndependentDashboard.sh',entry)
    credential=json.loads((final/'data/agenda.json').read_text(encoding='utf-8'))['token']
    (SOURCE/'wireless-device.json').write_text(json.dumps(dict(token=credential)),encoding='utf-8')
    print('安装完成。安全弹出 USB 后，打开 IndependentDashboard 或从 KUAL 启动 Kindle Desk。')
    print('首次启动后用右下角手机图标扫码添加自己的日程；示例数据不会写入设备。')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mount',nargs='?',help='Kindle USB 根目录，如 F:/ 或 /Volumes/Kindle')
    parser.add_argument('--check',action='store_true',help='只检查程序，不写入设备')
    args=parser.parse_args()
    if args.check: check_source(); print('程序检查通过。'); return
    if not args.mount: parser.error('请指定 Kindle 磁盘根目录，或使用 --check')
    install(args.mount)

if __name__=='__main__':
    try: main()
    except Exception as exc: print(str(exc)); sys.exit(1)
