"""Build an allowlisted archive; runtime data and pairing files are never included."""
import hashlib,json,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'kindle-standalone'
VERSION='2.0.0'

def release_files():
    names=['README.md','CHANGELOG.md','LICENSE','THIRD_PARTY_NOTICES.md','requirements.txt','install.py','.gitignore','.gitattributes']
    for path in APP.iterdir():
        if path.is_file() and path.suffix in ('.py','.pyw','.sh','.cmd'): names.append(path.relative_to(ROOT).as_posix())
    for folder in ('kindle-standalone/assets','kindle-standalone/vendor','extensions','docs','scripts','tests','.github'):
        for path in (ROOT/folder).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc','.pyo'):
                names.append(path.relative_to(ROOT).as_posix())
    for name in names:
        if any(part in ('data','backups','updates') for part in Path(name).parts) or 'wireless-device' in name:
            raise ValueError('Private file in release manifest')
        if name.endswith('.ttc') or Path(name).name.startswith(('msyh','arial')):
            raise ValueError('Proprietary font in release manifest')
    return sorted(set(names))

def build():
    output=ROOT/'dist'; output.mkdir(exist_ok=True)
    path=output/f'kindle-desk-{VERSION}.zip'
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name in release_files(): archive.write(ROOT/name,'kindle-desk/'+name)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    (output/'SHA256SUMS.txt').write_text(digest+'  '+path.name+'\n',encoding='utf-8')
    print(path.name, path.stat().st_size, 'bytes')
    print('SHA256',digest)

if __name__=='__main__': build()
