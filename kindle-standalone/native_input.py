"""Offline touch forms; no browser, system keyboard or GUI lifecycle changes."""
import copy,json
from datetime import datetime,timedelta
from functools import lru_cache
from pathlib import Path
import renderer as ui

@lru_cache(maxsize=2)
def pinyin_shard(initial):
    try:
        return json.loads((ui.ROOT/'assets/ime'/(initial+'.json')).read_text(encoding='utf-8'))
    except (OSError,ValueError): return {}

def pinyin_candidates(code):
    if not code: return []
    data=pinyin_shard(code[0])
    # Exact phrases first. If the phrase is absent, commit its longest known
    # prefix and keep the remainder in the composition buffer.
    result=[]
    for end in range(len(code),0,-1):
        if code[:end] in data:
            result.extend((word,end) for word in data[code[:end]])
            if len(result)>=128: break
    return result[:256]

class NativeForm:
    def __init__(self,kind,revision,entry=None,day=None):
        self.kind=kind
        self.revision=revision
        self.identity=(entry or {}).get('id','')
        self.values=dict(title='',note='')
        if kind=='event': self.values.update(date=day or ui.now().date().isoformat(),time='')
        for key in self.values:
            self.values[key]=str((entry or {}).get(key,self.values[key]))
        self.original=copy.deepcopy(self.values)
        self.field='title'
        self.cursor=len(self.values['title'])
        self.selected=False
        self.language='zh'
        self.upper=False
        self.composition=''
        self.candidate_page=0
        self.message=''
        self.cancel_confirm=False
        self.delete_confirm=False
        self.conflict=False

    @property
    def dirty(self): return self.values!=self.original or bool(self.composition)

    def candidates(self): return pinyin_candidates(self.composition)

    def choose(self,word,used):
        if (word,used) not in self.candidates(): return
        if self.insert(word):
            self.composition=self.composition[used:]
            self.candidate_page=0

    def insert(self,text):
        value=self.values[self.field]
        base='' if self.selected else value
        cursor=0 if self.selected else self.cursor
        limit={'title':100,'note':200,'date':10,'time':5}[self.field]
        if len(base)+len(text)>limit:
            self.message='内容已到字数上限。'; return False
        self.values[self.field]=base[:cursor]+text+base[cursor:]
        self.cursor=cursor+len(text); self.selected=False
        return True

    def focus(self,name):
        if self.composition:
            self.message='请先点候选字，或退格清除拼音。'; return
        self.field=name; self.cursor=len(self.values[name])
        self.selected=name in ('date','time')
        self.message=''

    def key(self,value):
        self.cancel_confirm=self.delete_confirm=False
        self.message=''
        numeric=self.field in ('date','time')
        if value=='backspace':
            if self.composition: self.composition=self.composition[:-1]; self.candidate_page=0
            elif self.selected: self.values[self.field]=''; self.cursor=0; self.selected=False
            elif self.cursor:
                text=self.values[self.field]
                self.values[self.field]=text[:self.cursor-1]+text[self.cursor:]
                self.cursor-=1
        elif value=='clear':
            if self.composition: self.composition=''; self.candidate_page=0
            else: self.values[self.field]=''; self.cursor=0; self.selected=False
        elif value in ('left','right'):
            if self.composition: self.message='请先选字，再移动光标。'; return
            self.selected=False
            self.cursor=max(0,min(len(self.values[self.field]),self.cursor+(-1 if value=='left' else 1)))
        elif value in ('language','numbers','symbols','upper'):
            if self.composition: self.message='请先选字，或退格清除拼音。'; return
            if value=='language': self.language='en' if self.language=='zh' else 'zh'
            elif value=='numbers': self.language='number' if self.language!='number' else 'zh'
            elif value=='symbols': self.language='symbol' if self.language!='symbol' else 'zh'
            elif value=='upper': self.upper=not self.upper
        elif value=='space':
            if self.composition:
                candidates=self.candidates()
                if candidates: self.choose(*candidates[self.candidate_page*4])
                else: self.message='暂无候选，请分词输入拼音。'
            elif not numeric: self.insert(' ')
        elif numeric:
            allowed='0123456789-' if self.field=='date' else '0123456789:'
            if value in allowed: self.insert(value)
        elif self.language=='zh' and len(value)==1 and value.isascii() and value.isalpha():
            if len(self.composition)<40: self.composition+=value.lower(); self.candidate_page=0
        else: self.insert(value)

    def action(self,action):
        self.cancel_confirm=self.delete_confirm=False
        op,value=action
        if op=='field': self.cancel_confirm=self.delete_confirm=False; self.focus(value)
        elif op=='key': self.key(value)
        elif op=='candidate': self.choose(*value)
        elif op=='candidate_page':
            self.candidate_page=max(0,min((len(self.candidates())-1)//4,self.candidate_page+value))
        elif op=='quick':
            if self.field=='date':
                if value=='today': date=ui.now().date()
                else:
                    try: date=datetime.strptime(self.values['date'],'%Y-%m-%d').date()
                    except ValueError: date=ui.now().date()
                    try: date+=timedelta(days=value)
                    except OverflowError: return
                self.values['date']=date.isoformat()
            else: self.values['time']=value
            self.cursor=len(self.values[self.field]); self.selected=True
        elif op=='next':
            fields=['title','note']+(['date','time'] if self.kind=='event' else [])
            self.focus(fields[(fields.index(self.field)+1)%len(fields)])

    def validate(self):
        if not self.values['title'].strip(): raise ValueError('请填写内容后再保存。')
        if self.kind=='event':
            try: datetime.strptime(self.values['date'],'%Y-%m-%d')
            except ValueError: raise ValueError('日期无效，请按 2026-10-22 的格式填写。')
            if self.values['time']:
                try: datetime.strptime(self.values['time'],'%H:%M')
                except ValueError: raise ValueError('时间无效，请按 09:30 填写，或选择全天。')

def field_boxes(form):
    if form.kind=='event':
        return [('date',(30,82,344,142)),('time',(362,82,570,142)),
                ('title',(30,161,570,237)),('note',(30,255,570,331))]
    return [('title',(30,90,570,185)),('note',(30,207,570,331))]

def keyboard_buttons(form):
    buttons=[]
    def row(values,y,x=25,width=55):
        for i,value in enumerate(values):
            buttons.append(((x+i*width+2,y,x+(i+1)*width-2,y+53),value,('key',value)))
    numeric=form.field in ('date','time') or form.language=='number'
    if numeric:
        row('1234567890',454)
        row('-/:.,+()?!',512)
        row('@#%&=*',570)
    elif form.language=='symbol':
        row('，。！？：；、（）—',454)
        row('1234567890',512)
        row('+-/@#%=',570)
    else:
        row('QWERTYUIOP' if form.upper and form.language=='en' else 'qwertyuiop',454)
        row('ASDFGHJKL' if form.upper and form.language=='en' else 'asdfghjkl',512,x=52)
        row('ZXCVBNM' if form.upper and form.language=='en' else 'zxcvbnm',570)
    buttons.append(((414,570,573,623),'退格',('key','backspace')))
    x=25
    for label,key,width in [('中/英','language',85),('123','numbers',60),('符号','symbols',65),('空格','space',110),('←','left',60),('→','right',60),('清空','clear',110)]:
        buttons.append(((x+2,628,x+width-2,681),label,('key',key))); x+=width
    return buttons

def candidate_buttons(form):
    if form.field in ('date','time'):
        quick=[('前一天',-1),('今天','today'),('后一天',1)] if form.field=='date' else [('全天',''),('09:00','09:00'),('18:00','18:00')]
        return [((30+i*184,397,202+i*184,444),label,('quick',value)) for i,(label,value) in enumerate(quick)]
    values=form.candidates(); start=form.candidate_page*4
    result=[((30+i*112,397,138+i*112,444),word,('candidate',(word,used))) for i,(word,used) in enumerate(values[start:start+4])]
    if start>0: result.append(((478,397,521,444),'‹',('candidate_page',-1)))
    if start+4<len(values): result.append(((527,397,570,444),'›',('candidate_page',1)))
    return result

def tap(form,x,y):
    for field,box in field_boxes(form):
        if ui.inside(x,y,box): return ('field',field)
    for box,label,action in candidate_buttons(form)+keyboard_buttons(form):
        if ui.inside(x,y,box): return action
    if ui.inside(x,y,(450,22,570,67)) and form.identity: return ('delete',None)
    if ui.inside(x,y,(398,348,570,389)) and form.language=='en': return ('key','upper')
    if 728<=y<778:
        if 30<=x<202: return ('cancel',None)
        if 214<=x<386: return ('next',None)
        if 398<=x<570: return ('save',None)
    return None

def render(form):
    image=ui.Image.new('L',(600,800),255); d=ui.ImageDraw.Draw(image)
    heading=('修改' if form.identity else '新增')+('日程' if form.kind=='event' else '待办')
    d.text((30,27),heading,font=ui.font(28,True),fill=0,anchor='lt')
    if form.identity:
        ui.icon_button(d,(450,22,570,67),'check' if form.delete_confirm else 'trash',25)
    names=dict(date='日期',time='时间 · 空为全天',title='内容',note='备注 · 可选')
    for name,box in field_boxes(form):
        active=name==form.field; x,y,x2,y2=box
        d.rectangle(box,outline=0 if active else 160,width=3 if active else 1)
        d.text((x+12,y+8),names[name],font=ui.font(14,True),fill=60,anchor='lt')
        value=form.values[name]
        if active:
            if form.selected: shown=value or '全天'
            else: shown=value[:form.cursor]+'│'+value[form.cursor:]
            width=x2-x-24
            # Keep the cursor visible while editing long text.
            start=max(0,form.cursor-22)
            shown=('…' if start else '')+shown[start:]
            fill=0
        else: shown=value or ('全天' if name=='time' else '点击输入'); width=x2-x-24; fill=0 if value else 130
        lines=ui.wrap_lines(shown,21,width)
        for i,line in enumerate(lines[:max(1,(y2-y-30)//25)]):
            if active and form.selected: d.rectangle((x+9,y+28+i*25,x2-9,y+53+i*25),fill=225)
            d.text((x+12,y+29+i*25),line,font=ui.font(21),fill=fill,anchor='lt')
    if form.field in ('date','time'):
        hint='格式 2026-10-22 · 输入数字将替换选中日期' if form.field=='date' else '格式 09:30 · 点「全天」可清空时间'
    elif form.composition: hint='拼音：'+form.composition
    else: hint={'zh':'中文拼音 · 输入后点候选字','en':'英文 · 直接输入','number':'数字','symbol':'符号'}[form.language]
    d.text((30,359),ui.fit_text(d,hint,17,540 if form.language!='en' else 355),font=ui.font(17),fill=0,anchor='lt')
    if form.language=='en':
        d.rectangle((398,348,570,389),outline=0,width=1)
        d.text((484,360),'ABC' if form.upper else 'abc',font=ui.font(17),fill=0,anchor='mt')
    for box,label,action in candidate_buttons(form)+keyboard_buttons(form):
        d.rectangle(box,outline=0,width=1)
        glyph=None
        if action[0]=='key': glyph={'backspace':'backspace','space':'space','clear':'trash','left':'prev','right':'next'}.get(action[1])
        elif action[0]=='candidate_page': glyph='prev' if action[1]<0 else 'next'
        elif action[0]=='quick' and form.field=='date': glyph='today' if action[1]=='today' else 'prev' if action[1]<0 else 'next'
        if glyph:
            ui.action_icon(d,glyph,(box[0]+box[2])/2,(box[1]+box[3])/2,24)
        else:
            size=21 if len(label)<=3 else 18
            d.text(((box[0]+box[2])/2,box[1]+14),ui.fit_text(d,label,size,box[2]-box[0]-8),font=ui.font(size),fill=0,anchor='mt')
    hint=form.message or ('勾号保存 · 中文拼音用 v 输入 ü' if form.language=='zh' else '勾号保存')
    d.text((30,695),ui.fit_text(d,hint,16,540),font=ui.font(16),fill=0,anchor='lt')
    ui._draw_buttons(d,('放弃' if form.cancel_confirm else '取消','下一项','保存'))
    return image
