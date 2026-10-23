"""Send a verified dashboard package to the user's paired Kindle over LAN."""
import ipaddress,json,sys,time,urllib.error,urllib.request
from pathlib import Path
import updater
ROOT=Path(__file__).resolve().parent

def run(address):
    address=address.removeprefix('http://').rstrip('/').split(':')[0]
    ip=ipaddress.ip_address(address)
    if ip.version!=4 or not any(ip in ipaddress.ip_network(n) for n in ('192.168.0.0/16','10.0.0.0/8','172.16.0.0/12')):
        raise ValueError('请输入 Kindle 手机连接页显示的局域网地址。')
    config=ROOT/'wireless-device.json'
    if not config.exists(): config=ROOT/'data/agenda.json'
    token=json.loads(config.read_text(encoding='utf-8'))['token']
    base='http://'+address+':8766/'+token
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base+'/update-info.json',timeout=10) as response: info=json.load(response)
    payload,build=updater.package(ROOT)
    request=urllib.request.Request(base+'/update',data=payload,headers={'Content-Type':'application/zip','X-Update-Nonce':info['nonce']},method='POST')
    print('正在发送新版并检查…')
    with opener.open(request,timeout=90) as response:
        if response.status!=202: raise ValueError('更新未被接受。')
        update_id=json.load(response)['id']
    print('上传完成，等待 Kindle 重新打开看板…')
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        time.sleep(3)
        try:
            with opener.open(base+'/status.json',timeout=4) as response: status=json.load(response)
            result=status.get('update_result') or {}
            if result.get('id')==update_id and result.get('result')=='rolled_back':
                raise ValueError('新版启动未通过，已恢复旧版。')
            if status.get('build')==build and result.get('id')==update_id and result.get('result')=='installed':
                print('无线更新完成，日程和待办已保留。'); return
        except (urllib.error.URLError,TimeoutError,OSError): pass
    raise ValueError('尚未收到完成确认，请查看 Kindle 画面后再操作。')

if __name__=='__main__':
    try:
        address=sys.argv[1] if len(sys.argv)>1 else input('先在 Kindle 打开手机连接页，再输入设备地址：').strip()
        run(address)
    except urllib.error.HTTPError as error:
        print('连接未通过（'+str(error.code)+'）。请确认 Kindle 已打开手机连接页，再试一次。')
        sys.exit(1)
    except Exception as error:
        print(str(error) if isinstance(error,ValueError) else '连接未完成，请检查两台设备是否在同一 Wi-Fi。')
        sys.exit(1)
