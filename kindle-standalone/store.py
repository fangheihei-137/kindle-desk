"""Shared, versioned agenda storage for Kindle, browsers and USB editing."""
import copy, json, os, secrets, threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager

@contextmanager
def disk_lock(path):
    with Path(path).open('a+b') as handle:
        if os.name=='nt':
            import msvcrt
            if handle.tell()==0: handle.write(b'0'); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
        try: yield
        finally:
            if os.name=='nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

class Conflict(ValueError):
    pass

def event_fields(data):
    date=datetime.strptime(str(data.get('date','')).strip(),'%Y-%m-%d').date().isoformat()
    clock=str(data.get('time','')).strip()
    if clock: clock=datetime.strptime(clock,'%H:%M').strftime('%H:%M')
    title=str(data.get('title','')).strip()
    note=str(data.get('note','')).strip()
    if not title: raise ValueError('请填写日程内容。')
    if len(title)>100 or len(note)>200: raise ValueError('内容最多 100 字，备注最多 200 字。')
    return dict(date=date,time=clock,title=title,note=note)

def atomic_json(path, value):
    path=Path(path)
    temporary=path.with_suffix('.tmp')
    with temporary.open('w',encoding='utf-8',newline='\n') as output:
        json.dump(value,output,ensure_ascii=False,indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(str(temporary),str(path))

class Store:
    def __init__(self,path):
        self.path=Path(path)
        self.lock=threading.RLock()

    def read(self):
        with self.lock:
            result=json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(result.get('events'),list): raise ValueError('日程文件格式不正确。')
            result.setdefault('revision',0)
            result.setdefault('todos',[])
            if not isinstance(result['todos'],list): raise ValueError('待办文件格式不正确。')
            return result

    def change(self,revision,action,data):
        with self.lock, disk_lock(self.path.with_suffix('.lock')):
            value=self.read()
            if int(revision)!=value['revision']:
                raise Conflict('内容已在另一入口更新，请重新打开页面后再修改。')
            if action in ('save','delete'):
                identity=str(data.get('id',''))
                old=next((e for e in value['events'] if e['id']==identity),None)
                if identity and old is None: raise Conflict('这条日程已被删除，请刷新列表。')
                if action=='delete':
                    if not identity: raise ValueError('未指定日程。')
                    value['events']=[e for e in value['events'] if e['id']!=identity]
                else:
                    entry=event_fields(data)
                    entry['id']=identity or secrets.token_hex(6)
                    value['events']=[e for e in value['events'] if e['id']!=identity]+[entry]
            elif action in ('todo_save','todo_state','todo_delete'):
                identity=str(data.get('id',''))
                old=next((e for e in value['todos'] if e['id']==identity),None)
                if identity and old is None: raise Conflict('这条待办已被删除，请刷新列表。')
                if action=='todo_save':
                    title=str(data.get('title','')).strip()
                    note=str(data.get('note','')).strip()
                    if not title or len(title)>100 or len(note)>200:
                        raise ValueError('请填写待办内容，最多 100 字；备注最多 200 字。')
                    entry=dict(old) if old else dict(id=secrets.token_hex(6),done=False,completed_at='')
                    entry.update(title=title,note=note)
                    if old: value['todos']=[entry if e['id']==identity else e for e in value['todos']]
                    else: value['todos'].append(entry)
                elif not old: raise ValueError('未指定待办。')
                elif action=='todo_delete': value['todos']=[e for e in value['todos'] if e['id']!=identity]
                else:
                    if str(data.get('done','')) not in ('0','1'): raise ValueError('待办状态无效。')
                    done=str(data['done'])=='1'
                    if old.get('done',False)==done: return copy.deepcopy(value)
                    old['done']=done
                    old['completed_at']=datetime.now(timezone(timedelta(hours=8))).isoformat() if done else ''
            elif action=='city':
                city=str(data.get('city','')).strip()
                if len(city)>80: raise ValueError('城市名称太长。')
                value['city']=city
            else: raise ValueError('不支持的操作。')
            value['revision']+=1
            atomic_json(self.path.with_suffix('.backup.json'),self.read())
            atomic_json(self.path,value)
            return copy.deepcopy(value)

def initialize_data(path):
    """First boot only: each installation gets its own random pairing credential."""
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with disk_lock(path.with_suffix('.lock')):
        if path.exists():
            value=Store(path).read()
            if not isinstance(value.get('token'),str) or len(value['token'])<32:
                raise ValueError('已有配置的连接凭据无效，请从备份恢复；不会覆盖原数据。')
            return False
        atomic_json(path,dict(revision=0,token=secrets.token_hex(24),city='',events=[],todos=[]))
        return True
