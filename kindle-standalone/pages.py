"""Server-rendered forms work in the old Kindle browser without JavaScript."""
from datetime import datetime
from html import escape
from urllib.parse import quote
from renderer import now, fmt_news_full

CSS='''*{box-sizing:border-box}body{margin:0;background:#fff;color:#111;font:20px/1.55 sans-serif}main{max-width:650px;margin:auto;padding:20px}h1{font-size:28px;margin:0 0 6px}h2{font-size:23px;margin:24px 0 12px}p{margin:8px 0}.muted{font-size:16px;color:#555}a{color:#111}label{display:block;margin:12px 0 5px;font-weight:bold}input,textarea{width:100%;font:20px sans-serif;border:2px solid #777;background:white;color:black;padding:10px;border-radius:0}textarea{height:85px}button,.button{display:inline-block;min-height:44px;border:2px solid #111;padding:8px 17px;margin:6px 6px 6px 0;background:#111;color:#fff;text-decoration:none;font:20px sans-serif;border-radius:0;cursor:pointer}.secondary{background:#fff;color:#111}.card{border-top:2px solid #111;padding:16px 0;word-wrap:break-word}.title{font-size:22px;font-weight:bold}.inline{display:inline}.notice{border:2px solid #111;padding:12px;margin:15px 0}.split{display:inline-block;width:47%;vertical-align:top;margin-right:2%}small{font-size:16px}'''

def layout(title,body,head_extra=''):
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            +head_extra+'<title>'+escape(title)+'</title><style>'+CSS+'</style></head><body><main>'+body+'</main></body></html>').encode('utf-8')

def hidden(name,value):
    return '<input type="hidden" name="'+escape(name,quote=True)+'" value="'+escape(str(value),quote=True)+'">'

def page(data,prefix,csrf,device=False,edit_id='',delete_id='',message='',new_date=''):
    suffix='?device=kindle' if device else ''
    def form(action):
        return '<form method="post" action="'+prefix+'/action">'+hidden('csrf',csrf)+hidden('revision',data['revision'])+hidden('action',action)+hidden('device','kindle' if device else '')
    body='<h1>我的日程</h1><p class="muted">保存在 Kindle 上，电脑关机后照常显示。</p>'
    if message: body+='<p class="notice">'+escape(message)+'</p>'
    body+=form('display')+'<button type="submit">返回看板</button></form>'
    body+='<p><a class="button secondary" href="'+prefix+'/todos'+suffix+'">待办事项</a></p>'
    if not device:
        body+='<p class="muted">手机和电脑连同一 Wi-Fi，即可在这里管理。</p>'
    if delete_id:
        entry=next((e for e in data['events'] if e['id']==delete_id),None)
        if entry:
            body+='<h2>删除这条日程？</h2><p>'+escape(entry['title'])+'</p>'+form('delete')+hidden('id',delete_id)+'<button type="submit">确认删除</button><a class="button secondary" href="'+prefix+'/'+suffix+'">保留</a></form>'
            return layout('删除日程',body)
    entry=next((e for e in data['events'] if e['id']==edit_id),None)
    try: default_date=datetime.strptime(new_date,'%Y-%m-%d').date().isoformat()
    except (ValueError,TypeError): default_date=now().date().isoformat()
    entry=entry or dict(id='',date=default_date,time='',title='',note='')
    body+='<h2>'+('修改日程' if entry['id'] else '添加日程')+'</h2>'+form('save')+hidden('id',entry['id'])
    for name,label,kind in (('date','日期','date'),('time','时间 · 留空为全天','time')):
        body+='<div class="split"><label for="'+name+'">'+label+'</label><input id="'+name+'" name="'+name+'" type="'+kind+'" value="'+escape(entry[name],quote=True)+'" placeholder="'+('2026-10-22' if name=='date' else '09:00')+'"></div>'
    body+='<label for="title">内容</label><input id="title" name="title" maxlength="100" value="'+escape(entry['title'],quote=True)+'">'
    body+='<label for="note">备注 · 可选</label><textarea id="note" name="note" maxlength="200">'+escape(entry.get('note',''))+'</textarea><button type="submit">保存日程</button>'
    if entry['id']: body+='<a class="button secondary" href="'+prefix+'/'+suffix+'">取消修改</a>'
    body+='</form><h2>已保存的日程</h2>'
    if not data['events']: body+='<p class="muted">还没有日程。</p>'
    for e in sorted(data['events'],key=lambda v:(v['date'],v.get('time',''),v['id'])):
        args=('device=kindle&' if device else '')
        body+='<div class="card"><div class="muted">'+escape(e['date']+' · '+(e.get('time') or '全天'))+'</div><div class="title">'+escape(e['title'])+'</div>'
        if e.get('note'): body+='<p>'+escape(e['note'])+'</p>'
        body+='<a class="button secondary" href="'+prefix+'/?'+args+'edit='+quote(e['id'])+'">修改</a><a class="button secondary" href="'+prefix+'/?'+args+'delete='+quote(e['id'])+'">删除</a></div>'
    body+='<h2>天气城市</h2>'+form('city')+'<label for="city">留空自动估算所在城市</label><input id="city" name="city" maxlength="80" value="'+escape(data.get('city',''),quote=True)+'"><button type="submit">保存城市</button></form><p class="muted">自动定位依据网络出口 IP，可能与实际位置有偏差。天气需要联网；已保存的日程不需要。</p>'
    body+='<h2>AI 金融资讯</h2><p class="muted">每小时自动刷新，看板显示前 6 条，历史保留 48 小时，最多 300 条。</p><p><a class="button secondary" href="'+prefix+'/news'+suffix+'">打开资讯列表</a></p>'
    return layout('Kindle 日程管理',body)

def todo_page(data,prefix,csrf,device=False,edit_id='',delete_id='',message=''):
    from renderer import todo_items
    suffix='?device=kindle' if device else ''
    args='device=kindle&' if device else ''
    def form(action):
        return '<form method="post" action="'+prefix+'/action">'+hidden('csrf',csrf)+hidden('revision',data['revision'])+hidden('action',action)+hidden('device','kindle' if device else '')
    body='<h1>待办事项</h1><p class="muted">完成后保留在全部待办中；首页自动补上下一条。</p>'
    if message: body+='<p class="notice">'+escape(message)+'</p>'
    body+=form('display')+'<button type="submit">返回看板</button><a class="button secondary" href="'+prefix+'/'+suffix+'">日程管理</a></form>'
    if delete_id:
        entry=next((e for e in data.get('todos',[]) if e['id']==delete_id),None)
        if entry:
            body+='<h2>删除这条待办？</h2><p>'+escape(entry['title'])+'</p>'+form('todo_delete')+hidden('id',delete_id)+'<button type="submit">确认删除</button><a class="button secondary" href="'+prefix+'/todos'+suffix+'">保留</a></form>'
            return layout('删除待办',body)
    entry=next((e for e in data.get('todos',[]) if e['id']==edit_id),None)
    if edit_id and entry is None: body+='<p class="notice">这条待办已被删除，下面可以新建待办。</p>'
    entry=entry or dict(id='',title='',note='')
    body+='<h2>'+('修改待办' if entry['id'] else '添加待办')+'</h2>'+form('todo_save')+hidden('id',entry['id'])
    body+='<label for="title">内容</label><input id="title" name="title" maxlength="100" value="'+escape(entry['title'],quote=True)+'">'
    body+='<label for="note">备注 · 可选</label><textarea id="note" name="note" maxlength="200">'+escape(entry.get('note',''))+'</textarea><button type="submit">保存待办</button></form>'
    for done,label in ((False,'未完成'),(True,'已完成')):
        items=todo_items(data,done)
        body+='<h2>'+label+' · '+str(len(items))+'</h2>'
        if not items: body+='<p class="muted">暂无。</p>'
        for item in items:
            title=escape(item['title'])
            body+='<div class="card"><div class="title">'+('<del>'+title+'</del>' if done else title)+'</div>'
            if item.get('note'): body+='<p>'+escape(item['note'])+'</p>'
            if done and item.get('completed_at'):
                body+='<p class="muted">完成于 '+escape(item['completed_at'][:16].replace('T',' '))+'</p>'
            body+=form('todo_state')+hidden('id',item['id'])+hidden('done','0' if done else '1')+'<button type="submit">'+('恢复待办' if done else '□ 完成')+'</button></form>'
            body+='<a class="button secondary" href="'+prefix+'/todos?'+args+'edit='+quote(item['id'])+'">修改</a><a class="button secondary" href="'+prefix+'/todos?'+args+'delete='+quote(item['id'])+'">删除</a></div>'
    return layout('Kindle 待办事项',body)

def news_page(items,news_at,prefix,error='',csrf='',device=False):
    stamp='尚未更新'
    if news_at:
        try: stamp=datetime.fromisoformat(news_at).strftime('%m-%d %H:%M')
        except ValueError: pass
    suffix='?device=kindle' if device else ''
    body='<h1>AI 金融资讯</h1><p class="muted">更新于 '+stamp+' · 最近 48 小时 · 每小时自动刷新 · 点条目看全文</p>'
    body+=('<form method="post" action="'+prefix+'/action">'+hidden('csrf',csrf)+hidden('action','refresh_news')
           +hidden('device','kindle' if device else '')+'<button type="submit">立即刷新</button>'
           '<a class="button secondary" href="'+prefix+'/'+suffix+'">返回日程管理</a></form>')
    if error: body+='<p class="notice">'+escape(error)+'，以下为上次保存的内容。</p>'
    if not items: body+='<p class="muted">资讯正在连接，请稍后再看。</p>'
    for i,item in enumerate(items,1):
        when=fmt_news_full(item.get('time'))
        meta=str(i)+' · '+escape(item.get('source') or '')+((' · '+when) if when else '')
        body+='<div class="card"><div class="muted">'+meta+'</div><a class="title" href="'+escape(item.get('url') or '',quote=True)+'">'+escape(item.get('name') or '')+'</a></div>'
    body+='<p style="margin-top:20px"><a class="button secondary" href="'+prefix+'/'+suffix+'">返回日程管理</a></p>'
    return layout('AI 金融资讯',body,'<meta http-equiv="refresh" content="300">')

def error_page(message,prefix):
    return layout('请检查', '<h1>暂未保存</h1><p class="notice">'+escape(message)+'</p><a class="button" href="'+prefix+'/">重新打开日程</a>')
