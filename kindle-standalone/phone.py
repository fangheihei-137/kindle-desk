"""Responsive, dependency-free phone control panel. Mutations use CSRF forms."""
import json,time
from html import escape as e
from urllib.parse import quote
from datetime import datetime
import pages,modules
from renderer import TZ

CSS='''
:root{color-scheme:light;--ink:#182521;--muted:#68756e;--green:#255b47;--line:#dce2d9;--paper:#f4f5ef}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif}main{max-width:860px;margin:auto;padding:24px 20px 108px}a{color:var(--green);text-underline-offset:3px}h1{font-size:30px;line-height:1.2;letter-spacing:-.8px;margin:0 0 10px}h2{font-size:20px;margin:0 0 12px}h3{font-size:17px;margin:0 0 8px}p{margin:8px 0}.muted,small{font-size:13px;color:var(--muted)}.eyebrow{font-size:11px;letter-spacing:2px;font-weight:700;color:var(--green);margin-bottom:10px}.masthead{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px}.brand{font-size:15px;font-weight:800;text-decoration:none;letter-spacing:.5px}.badge{font-size:12px;padding:5px 9px;border-radius:20px;background:#e4ebdf}.intro{margin:0 0 24px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.card{background:#fff;border:1px solid var(--line);border-radius:18px;padding:20px;margin:14px 0;word-wrap:break-word;overflow-wrap:anywhere}.grid>.card{margin:0}.metric{font-size:30px;line-height:1.2;font-weight:700}.preview{display:flex;gap:22px;align-items:center}.preview img{width:132px;max-width:42%;border:8px solid #26312d;border-radius:9px;box-shadow:0 7px 16px #18252120}.preview>div{min-width:0}.section{margin:26px 0 13px}.title{font-size:18px;font-weight:700}.notice{background:#e4edde;border:1px solid #c9d8c3;border-radius:12px;padding:13px 15px;margin:16px 0}.error{background:#fff0ed;border-color:#e7c7bd;color:#7b382c}.button,button{display:inline-flex;justify-content:center;align-items:center;gap:8px;min-height:46px;padding:11px 17px;margin:8px 6px 0 0;border:1px solid var(--green);border-radius:11px;background:var(--green);color:white;text-decoration:none;font-family:inherit;font-size:15px;font-weight:600;line-height:1.4;cursor:pointer}.secondary{background:white;color:var(--green);border-color:var(--line)}.danger{background:#fff;color:#9d4031;border-color:#dfc6bd}label{display:block;font-weight:600;font-size:14px;margin:16px 0 7px}input,select,textarea{display:block;width:100%;padding:12px;border:1px solid #cbd4c8;border-radius:10px;background:#fff;color:var(--ink);font-family:inherit;font-size:16px;line-height:1.45;min-width:0}textarea{min-height:90px;resize:vertical}input[type=radio]{display:inline-block;width:18px;height:18px;margin:0 10px 0 0;accent-color:var(--green)}input[type=hidden]{display:none}.choice{display:flex;gap:12px;align-items:flex-start;border:1px solid var(--line);border-radius:13px;background:#fff;padding:15px;margin:10px 0;cursor:pointer}.choice:has(input:checked){border-color:var(--green);background:#f0f5ec;box-shadow:0 0 0 1px var(--green)}.choice input{margin-top:4px;flex:none}.choice span{min-width:0}.choice strong{display:block;font-size:16px}.choice small{display:block;font-weight:400;margin-top:5px}.inline{display:inline}.split{display:inline-block;width:47%;margin-right:2%;vertical-align:top}.nav{position:fixed;bottom:0;left:0;right:0;z-index:10;display:flex;justify-content:center;gap:2px;background:#fffffff5;border-top:1px solid var(--line);padding:10px 10px calc(10px + env(safe-area-inset-bottom));backdrop-filter:blur(10px)}.nav a{display:flex;align-items:center;justify-content:center;flex-direction:column;gap:4px;width:86px;max-width:20%;font-size:11px;text-decoration:none;color:var(--muted);padding:6px 0;border-radius:10px}.nav a.active{color:var(--green);background:#edf3e9;font-weight:700}svg{width:21px;height:21px;vertical-align:middle;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}.photo{width:100%;height:150px;object-fit:contain;background:#fafafa;border-radius:10px}.photo-card .button,.photo-card button{width:100%;margin-right:0;padding:9px;font-size:13px}.progress{background:#e4e9df;height:9px;border-radius:8px;overflow:hidden;margin:8px 0 16px}.progress span{display:block;background:var(--green);height:100%}.code{overflow-wrap:anywhere;font:13px/1.5 monospace;background:#eef1e9;padding:10px;border-radius:8px}details summary{cursor:pointer;font-weight:600}details[open] summary{margin-bottom:12px}.row{display:flex;align-items:center;justify-content:space-between;gap:14px}.text-link{font-size:13px}.footer-note{margin-top:25px;font-size:12px;color:var(--muted)}@media(min-width:700px){main{padding:30px 28px 110px}.preview img{width:170px}.modules-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.choice{margin:0}}@media(max-width:360px){main{padding:20px 14px 100px}.card{padding:16px}.preview{gap:14px}.grid{gap:10px}.button,button{padding:10px 12px}h1{font-size:27px}}
'''
ICONS={
 'home':'<path d="M3 10 12 3l9 7v10H3Z"/><path d="M9 20v-7h6v7"/>',
 'modules':'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
 'calendar':'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v5m10-5v5M3 11h18m-13 5h2m4 0h2"/>',
 'todo':'<path d="m4 6 2 2 3-4m3 3h8M4 13h4m4 0h8M4 19h4m4 0h8"/>',
 'settings':'<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="8" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="10" cy="18" r="2"/>',
}
def icon(name): return '<svg viewBox="0 0 24 24" aria-hidden="true">'+ICONS.get(name,ICONS['modules'])+'</svg>'
def nav(prefix,active):
    return '<nav class="nav" aria-label="主导航">'+''.join('<a '+('class="active" aria-current="page" ' if key==active else '')+'href="'+prefix+path+'">'+icon(key)+'<span>'+label+'</span></a>' for key,path,label in [('home','/manage','面板'),('modules','/modules','模块'),('calendar','/','日程'),('todo','/todos','待办'),('settings','/settings','设置')])+'</nav>'
def shell(title,body,prefix,active='home',message=''):
    head='<div class="masthead"><a class="brand" href="'+prefix+'/manage">Kindle Desk</a><span class="badge">本地管理</span></div>'
    if message: head+='<div class="notice" role="status">'+e(message)+'</div>'
    doc=pages.layout(title,head+body+nav(prefix,active),'<script src="'+prefix+'/manager.js" defer></script>')
    return doc.decode('utf-8').replace('</head>','<style>'+CSS+'</style></head>').encode('utf-8')
def wrap_legacy(payload,prefix,active):
    text=payload.decode('utf-8').replace('</head>','<style>'+CSS+'</style></head>')
    text=text.replace('<main>','<main><div class="masthead"><a class="brand" href="'+prefix+'/manage">Kindle Desk</a><span class="badge">本地管理</span></div>',1)
    return text.replace('</main>',nav(prefix,active)+'</main>').encode('utf-8')
def form(prefix,csrf,revision,action):
    return '<form method="post" action="'+prefix+'/manage-action">'+pages.hidden('csrf',csrf)+pages.hidden('revision',revision)+pages.hidden('action',action)
def field(name,label,value,kind='text',extra=''):
    return '<label for="'+name+'">'+label+'</label><input id="'+name+'" name="'+name+'" type="'+kind+'" value="'+e(str(value),quote=True)+'" '+extra+'>'
def select(name,label,value,options):
    return '<label for="'+name+'">'+label+'</label><select id="'+name+'" name="'+name+'">'+''.join('<option value="'+e(str(key),quote=True)+'"'+(' selected' if str(key)==str(value) else '')+'>'+e(title)+'</option>' for key,title in options)+'</select>'
def dated(value):
    value=modules.stamp(value)
    return datetime.fromtimestamp(value,TZ).strftime('%m-%d %H:%M') if value else '尚未同步'

def home(app,prefix,message=''):
    config=app.store.read(); panel=app.panel_view(config); battery=app.state.get('battery')
    wifi,_,_,_=app.wifi_view()
    body='<div class="intro"><div class="eyebrow">YOUR KINDLE, AT A GLANCE</div><h1>我的桌面看板</h1><p class="muted">管理日程与待办，选择你想看的内容。</p></div>'
    body+='<section class="card preview"><img src="'+prefix+'/frame.png" alt="Kindle 当前画面"><div><span class="badge">'+e('待机中' if app.standby.is_set() else '正在显示')+'</span><h2 style="margin-top:15px">'+e(panel['title'])+'</h2><p class="muted">'+(str(battery)+'% 电量' if battery is not None else '电量暂未读取')+'</p><a class="button" href="'+prefix+'/modules">更换模块</a><a class="text-link" href="'+prefix+'/frame.png">查看大图</a></div></section>'
    pending=sum(not row.get('done') for row in config['todos'])
    body+='<div class="grid"><a class="card" href="'+prefix+'/" style="text-decoration:none"><div class="muted">已保存日程</div><div class="metric">'+str(len(config['events']))+'</div></a><a class="card" href="'+prefix+'/todos" style="text-decoration:none"><div class="muted">未完成待办</div><div class="metric">'+str(pending)+'</div></a></div>'
    body+='<section class="card"><div class="row"><div><h2>一张图片，一种心情</h2><p class="muted">照片、插画、信息卡，都可以留在墨水屏上。</p></div>'+icon('modules')+'</div><a class="button secondary" href="'+prefix+'/photos">管理图片</a></section>'
    body+='<section class="card"><h2>显示与休眠</h2><p class="muted">天气城市、刷新频率、自动待机和 Wi-Fi。</p><a class="button secondary" href="'+prefix+'/settings">打开设置</a></section>'
    body+='<section class="card"><div class="row"><h2>当前 Wi-Fi</h2><span class="badge">'+('已连接' if wifi.get('connected') else '未连接' if wifi.get('enabled') else '已关闭' if wifi.get('enabled') is False else '读取中')+'</span></div><p class="metric network-name">'+e(wifi.get('ssid') or '网络名称暂未读取')+'</p><p class="muted">设备地址 · '+e(app.address or wifi.get('address') or '暂未读取')+'</p><a class="button secondary" href="'+prefix+'/settings#wifi">调整无线网络</a></section>'
    body+='<p class="footer-note">修改保存在 Kindle 本机。同一 Wi-Fi 下可管理；设备休眠断网或连接 USB 时，请先唤醒并拔线。</p>'
    return shell('Kindle Desk · 我的面板',body,prefix,'home',message)

def module_page(app,prefix,message=''):
    config=app.store.read(); settings=config['settings']; panel=app.panel_view(config)
    body='<div class="intro"><div class="eyebrow">MAKE IT YOURS</div><h1>选择中间模块</h1><p class="muted">更换中间区域，天气、待办和日程照常保留。</p></div>'
    body+=form(prefix,app.csrf,config['revision'],'module')+'<div class="modules-grid">'
    for key,title,description,_ in modules.CATALOG:
        body+='<label class="choice"><input type="radio" name="panel_module" value="'+key+'"'+(' checked' if key==modules.selected(config) else '')+'><span><strong>'+e(title)+'</strong><small>'+e(description)+'</small></span></label>'
    body+='</div>'+select('module_interval','联网模块自动更新',settings['module_interval'],[(15,'每 15 分钟'),(30,'每 30 分钟'),(60,'每小时')])+'<p class="muted">仅更新选中的模块；金融资讯每小时更新。Codex 由电脑推送，图片不联网。</p><button type="submit">保存模块</button></form>'
    body+='<section class="card"><div class="row"><h2>当前模块</h2><span class="badge">'+e(panel['title'])+'</span></div><p class="muted">数据时间 · '+dated(panel.get('checked_at'))+'</p>'
    if panel.get('error'): body+='<p class="notice error">'+e(panel['error'])+'</p>'
    if panel['kind']=='usage':
        for key,title in (('deepseek','DeepSeek'),('codex','Codex')):
            account=panel.get('accounts',{}).get(key,{})
            body+='<p class="muted">'+title+' · '+dated(account.get('checked_at'))+'</p>'
            if account.get('error'): body+='<p class="notice error">'+e(account['error'])+'</p>'
    if panel['kind']!='photo':
        body+=form(prefix,app.csrf,config['revision'],'refresh')+'<button class="secondary">'+('更新 DeepSeek 余额' if panel['kind']=='usage' else '立即更新')+'</button></form>'
    body+='</section><section class="card"><h2>Codex 额度</h2><p class="muted">电脑读取已登录账号的额度，并把用量数字同步到 Kindle。电脑离线时显示最后同步时间，不会假装实时更新。</p>'
    for row in app.modules.cache.get('codex',{}).get('windows',[]):
        body+='<p>'+e(row['label']+' · '+row['period'])+'<br><strong>剩余 '+str(row['remaining'])+'%</strong> · 已用 '+str(row['used'])+'%</p><div class="progress"><span style="width:'+str(row['remaining'])+'%"></span></div>'
    body+='<p class="muted">同步助手：codex_usage_sync.py。只传额度，不传登录凭证。</p><details><summary>查看电脑连接地址</summary><p class="muted">在助手中填写当前浏览器地址即可。每次只同步需点击一次，连续同步可保持助手运行。</p></details></section>'
    body+='<section class="card"><h2>DeepSeek API 余额</h2><p class="muted">读取可用余额、赠送余额和充值余额。此接口不提供 Token 消耗总量。</p>'
    has_key=bool(app.modules.credentials().get('deepseek_key'))
    body+='<p><span class="badge">'+('已配置密钥' if has_key else '尚未配置')+'</span></p>'+form(prefix,app.csrf,config['revision'],'deepseek_key')
    body+=field('api_key','API Key · 仅保存在 Kindle', '', 'password','autocomplete="new-password" maxlength="256" required placeholder="'+('填写新密钥以替换' if has_key else 'sk-…')+'"')+'<button>保存密钥</button></form>'
    if has_key: body+=form(prefix,app.csrf,config['revision'],'remove_deepseek_key')+'<button class="danger">移除密钥</button></form>'
    body+='</section><a class="button secondary" href="'+prefix+'/photos">管理模块图片</a>'
    return shell('选择模块 · Kindle Desk',body,prefix,'modules',message)

def settings_page(app,prefix,message=''):
    config=app.store.read(); settings=config['settings']; wifi,networks,busy,wifi_message=app.wifi_view()
    body='<div class="intro"><div class="eyebrow">A QUIETER DESK</div><h1>面板设置</h1><p class="muted">保存后自动应用，重启依然保留。</p></div>'+form(prefix,app.csrf,config['revision'],'settings')
    body+='<section class="card"><h2>天气与时间</h2>'+field('city','天气城市 · 留空按网络定位',config.get('city',''),extra='maxlength="80" placeholder="自动定位"')+select('time_format','时间格式',settings['time_format'],[('24','24 小时'),('12','12 小时')])+select('weather_interval','天气更新',settings['weather_interval'],[(15,'每 15 分钟'),(30,'每 30 分钟'),(60,'每小时')])+'</section>'
    body+='<section class="card"><h2>墨水屏与待机</h2>'+select('full_refresh','消除残影 · 完整刷新',settings['full_refresh'],[(5,'每 5 次变化'),(10,'每 10 次变化'),(20,'每 20 次变化'),(50,'每 50 次变化')])+select('idle_minutes','无操作自动待机',settings['idle_minutes'],[(0,'关闭自动待机'),(5,'5 分钟'),(15,'15 分钟'),(30,'30 分钟'),(60,'60 分钟')])+select('standby_wifi','待机时 Wi-Fi',int(settings['standby_wifi']),[(1,'关闭，唤醒时恢复'),(0,'保持当前设置')])+select('sleep_display','待机画面',settings['sleep_display'],[('calendar','大日历与日程'),('photo','选中的图片')])+'<p class="muted">番茄钟计时中保持唤醒。系统深度休眠期间画面静止，按电源键唤醒后更新。</p><a href="'+prefix+'/photos">选择待机图片</a></section>'
    body+='<section class="card"><h2>新闻来源</h2>'+select('news_wallstreetcn','华尔街见闻',int(settings['news_wallstreetcn']),[(1,'开启'),(0,'关闭')])+select('news_eastmoney','东方财富',int(settings['news_eastmoney']),[(1,'开启'),(0,'关闭')])+'</section><button type="submit">保存设置</button></form>'
    body+='<section class="card" id="wifi"><h2>Wi-Fi</h2><p>当前网络 · '+e(wifi.get('ssid') or '未连接')+'</p><p class="muted">'+e(wifi_message or wifi.get('message') or '')+'</p>'
    if app.wifi:
        body+=form(prefix,app.csrf,config['revision'],'wifi_scan')+'<button class="secondary"'+(' disabled' if busy else '')+'>扫描网络</button><a class="button secondary" href="'+prefix+'/settings">刷新状态</a></form>'
        if networks: body+='<p class="muted">附近网络：'+e(' / '.join(row.get('ssid','') for row in networks[:8]))+'</p>'
        body+=form(prefix,app.csrf,config['revision'],'wifi_connect')+field('ssid','网络名称 SSID','',extra='required maxlength="32" autocomplete="off"')+field('password','Wi-Fi 密码 · 开放网络留空','','password','autocomplete="new-password" maxlength="63"')+'<p class="muted">切换网络会断开本页，请让手机连接新网络后重新扫码。</p><button'+(' disabled' if busy else '')+'>连接网络</button></form>'
    else: body+='<p class="muted">电脑预览时不操作无线网络。</p>'
    body+='</section><section class="card"><h2>设备状态</h2><p class="muted">电源键通道：'+e(app.power.diagnostics()['backend'] if app.power else '待设备启动')+'</p>'+form(prefix,app.csrf,config['revision'],'standby')+'<button class="secondary">立即待机</button></form><p class="muted">待机会断开连接，之后按 Kindle 电源键唤醒。</p></section>'
    return shell('设置 · Kindle Desk',body,prefix,'settings',message)

def photos_page(app,prefix,message=''):
    config=app.store.read(); photos=app.modules.photos(); settings=config['settings']
    body='<div class="intro"><div class="eyebrow">MEMORIES IN INK</div><h1>我的图片库</h1><p class="muted">最多 12 张。上传后自动转灰度并适配屏幕。</p></div>'
    body+='<section class="card"><h2>添加图片</h2><form id="photo-upload" data-endpoint="'+prefix+'/photo-upload" data-csrf="'+app.csrf+'" data-return="'+prefix+'/photos"><label for="photo-file">选择 JPG、PNG 或 WebP</label><input id="photo-file" type="file" accept="image/jpeg,image/png,image/webp" required><p class="muted">手机会先缩小图片；原图不会被修改。</p><button type="submit" disabled>上传图片</button><p id="upload-status" role="status" class="muted"></p></form><noscript>图片上传需要启用浏览器 JavaScript。</noscript></section>'
    body+='<div class="row section"><h2>我的图片</h2><span class="muted">'+str(len(photos))+' / 12</span></div><div class="grid">'
    for item in photos:
        identity=item['id']; using=[]
        if settings['panel_photo']==identity: using.append('面板图片')
        if settings['standby_photo']==identity: using.append('待机图片')
        body+='<section class="card photo-card"><img class="photo" src="'+prefix+'/photos/'+identity+'.png" alt="'+e(item['name'],quote=True)+'"><h3>'+e(item['name'])+'</h3><p class="muted">'+e(' · '.join(using) or '未使用')+'</p>'
        for target,label in [('panel','用于中间区域'),('standby','用于待机')]:
            body+=form(prefix,app.csrf,config['revision'],'photo_select')+pages.hidden('id',identity)+pages.hidden('target',target)+'<button class="secondary">'+label+'</button></form>'
        if not using:
            body+='<details><summary class="muted">删除图片</summary>'+form(prefix,app.csrf,config['revision'],'photo_delete')+pages.hidden('id',identity)+'<button class="danger">确认删除</button></form></details>'
        body+='</section>'
    body+='</div>'
    if not photos: body+='<p class="muted">还没有图片，从上方添加第一张吧。</p>'
    body+='<a class="button secondary" href="'+prefix+'/modules">返回模块设置</a>'
    return shell('图片库 · Kindle Desk',body,prefix,'modules',message)

SCRIPT=r'''"use strict";
const upload=document.getElementById('photo-upload');
if(upload) upload.addEventListener('submit',async event=>{
 event.preventDefault(); const button=upload.querySelector('button');const status=document.getElementById('upload-status');
 const file=document.getElementById('photo-file').files[0];if(!file)return;
 button.disabled=true;status.textContent='正在处理图片…';let url;
 try{
  if(file.size>30*1024*1024)throw Error('原图太大，请选择小于 30 MB 的图片。');
  if(!['image/png','image/jpeg','image/webp'].includes(file.type))throw Error('请选择 JPG、PNG 或 WebP。');
  url=URL.createObjectURL(file);const image=new Image();image.src=url;await image.decode();
  const ratio=Math.min(1,1200/image.width,1600/image.height);const canvas=document.createElement('canvas');
  canvas.width=Math.max(1,Math.round(image.width*ratio));canvas.height=Math.max(1,Math.round(image.height*ratio));
  const ctx=canvas.getContext('2d');ctx.fillStyle='white';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(image,0,0,canvas.width,canvas.height);
  const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.9));if(!blob)throw Error('无法处理这张图片。');
  status.textContent='正在传到 Kindle…';
  const response=await fetch(upload.dataset.endpoint,{method:'POST',headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':upload.dataset.csrf,'X-Photo-Name':encodeURIComponent(file.name)},body:blob});
  const result=await response.json();if(!response.ok)throw Error(result.error||'上传未完成。');
  location.assign(upload.dataset.return+'?message='+encodeURIComponent('图片已保存，选择用途即可显示。'));
 }catch(error){status.textContent=error.message||'连接已断开，请唤醒 Kindle 后重试。';status.className='notice error';}
 finally{button.disabled=false;if(url)URL.revokeObjectURL(url);}
});
if(upload) upload.querySelector('button').disabled=false;
'''
