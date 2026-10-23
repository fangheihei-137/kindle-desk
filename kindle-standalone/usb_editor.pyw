"""Edit the Kindle's authoritative agenda while it is connected by USB."""
import json, string, sys, tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog
from datetime import date
from store import Store, Conflict

class Editor:
    def __init__(self):
        self.window=tk.Tk()
        self.window.title('Kindle 独立看板 · 日程管理')
        self.window.geometry('900x690')
        self.window.minsize(780,620)
        self.window.option_add('*Font','{Microsoft YaHei UI} 11')
        self.store=None; self.data=None; self.identity=''
        outer=ttk.Frame(self.window,padding=22); outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='管理 Kindle 中的日程',font=('Microsoft YaHei UI',20,'bold')).pack(anchor='w')
        ttk.Label(outer,text='先在 Kindle 点「回到书库」，再接 USB。保存后安全弹出，打开 IndependentDashboard。').pack(anchor='w',pady=(8,14))
        bar=ttk.Frame(outer); bar.pack(fill='x')
        self.status=tk.StringVar(value='正在寻找 Kindle…')
        ttk.Label(bar,textvariable=self.status).pack(side='left')
        ttk.Button(bar,text='重新读取',command=self.connect).pack(side='right')
        ttk.Button(bar,text='选择 Kindle 磁盘',command=self.browse).pack(side='right',padx=8)
        ttk.Button(bar,text='待办事项',command=self.todos).pack(side='right',padx=8)
        content=ttk.Frame(outer); content.pack(fill='both',expand=True,pady=15)
        left=ttk.Frame(content); left.pack(side='left',fill='both',expand=True,padx=(0,24))
        self.list=ttk.Treeview(left,columns=('date','time','title'),show='headings',selectmode='browse')
        for name,label,width in [('date','日期',105),('time','时间',65),('title','内容',230)]:
            self.list.heading(name,text=label); self.list.column(name,width=width,minwidth=55)
        scroll=ttk.Scrollbar(left,orient='vertical',command=self.list.yview)
        self.list.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y'); self.list.pack(fill='both',expand=True)
        self.list.bind('<<TreeviewSelect>>',self.select)
        right=ttk.Frame(content,width=280); right.pack(side='right',fill='y')
        self.fields={}
        for name,label in [('date','日期（年-月-日）'),('time','时间（时:分，留空为全天）'),('title','日程内容'),('note','备注（可选）')]:
            ttk.Label(right,text=label).pack(anchor='w',pady=(10,3))
            variable=tk.StringVar(); self.fields[name]=variable
            ttk.Entry(right,textvariable=variable,width=28).pack(fill='x')
        ttk.Button(right,text='保存日程',command=self.save).pack(fill='x',pady=(20,8))
        ttk.Button(right,text='新建一条',command=self.clear).pack(fill='x',pady=4)
        ttk.Button(right,text='删除选中日程',command=self.delete).pack(fill='x',pady=4)
        cityrow=ttk.Frame(outer); cityrow.pack(fill='x')
        ttk.Label(cityrow,text='天气城市：').pack(side='left')
        self.city=tk.StringVar(); ttk.Entry(cityrow,textvariable=self.city,width=22).pack(side='left')
        ttk.Label(cityrow,text='留空自动按网络位置估算').pack(side='left',padx=8)
        ttk.Button(cityrow,text='保存城市',command=lambda:self.change('city',{'city':self.city.get()})).pack(side='right')
        ttk.Label(outer,text='手机、Kindle 和这里共用一份日程。天气和画面更新由 Kindle 自己完成，无须运行电脑程序。',wraplength=830).pack(anchor='w',pady=(17,0))
        self.connect()

    def candidates(self):
        if sys.platform=='win32':
            return [Path(c+':/') for c in string.ascii_uppercase if c not in 'C']
        return []

    def connect(self,root=None):
        candidates=[Path(root)] if root else self.candidates()
        matches=[p/'kindle-standalone/data/agenda.json' for p in candidates if (p/'kindle-standalone/data/agenda.json').is_file()]
        if len(matches)!=1:
            self.store=None
            self.status.set('请连接 Kindle USB' if not matches else '发现多个 Kindle，请选择磁盘')
            return
        self.store=Store(matches[0])
        try:
            self.refresh()
            self.status.set('已连接 '+str(matches[0].anchor)+' · 日程直接保存到 Kindle')
        except (OSError,ValueError):
            self.status.set('日程暂时无法读取，请重新连接 Kindle')

    def browse(self):
        folder=filedialog.askdirectory(title='选择 Kindle 磁盘的最外层文件夹')
        if folder: self.connect(folder)

    def todos(self):
        if not self.store:
            messagebox.showinfo('请连接 Kindle','请先连接 Kindle，再打开待办事项。'); return
        TodoEditor(self)

    def refresh(self):
        self.data=self.store.read()
        self.list.delete(*self.list.get_children())
        for e in sorted(self.data['events'],key=lambda e:(e['date'],e.get('time',''),e['id'])):
            self.list.insert('', 'end',iid=e['id'],values=(e['date'],e.get('time') or '全天',e['title']))
        self.city.set(self.data.get('city','')); self.clear()

    def clear(self):
        self.identity=''
        for key,var in self.fields.items(): var.set(date.today().isoformat() if key=='date' else '')
        self.list.selection_remove(self.list.selection())

    def select(self,event=None):
        selected=self.list.selection()
        if not selected or not self.data: return
        entry=next(e for e in self.data['events'] if e['id']==selected[0])
        self.identity=entry['id']
        for key,var in self.fields.items(): var.set(entry.get(key,''))

    def change(self,action,values):
        if not self.store or not self.data:
            messagebox.showinfo('请连接 Kindle','请先把 Kindle 接到 USB，再点击「重新读取」。'); return
        try:
            self.store.change(self.data['revision'],action,values)
            self.refresh(); self.status.set('已保存到 Kindle · 安全弹出后打开 IndependentDashboard')
        except Conflict as error: messagebox.showwarning('日程已有更新',str(error)+'\n请点击「重新读取」。')
        except ValueError as error: messagebox.showwarning('请检查内容','请检查日期、时间和内容。\n'+str(error))
        except OSError: messagebox.showwarning('保存未完成','Kindle 可能已断开，请重新连接后再保存。')

    def save(self):
        self.change('save',dict(id=self.identity,**{k:v.get() for k,v in self.fields.items()}))

    def delete(self):
        if not self.identity: return
        if messagebox.askyesno('删除日程','确定删除选中的日程吗？'):
            self.change('delete',{'id':self.identity})

class TodoEditor:
    def __init__(self,parent):
        self.parent=parent
        self.store=parent.store
        self.window=tk.Toplevel(parent.window)
        self.window.title('Kindle · 待办事项')
        self.window.geometry('850x610')
        self.identity=''
        frame=ttk.Frame(self.window,padding=20); frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='待办事项 · 已完成项目保留记录',font=('Microsoft YaHei UI',17,'bold')).pack(anchor='w')
        self.list=ttk.Treeview(frame,columns=('state','title'),show='headings',selectmode='browse',height=10)
        self.list.heading('state',text='状态'); self.list.column('state',width=90,stretch=False)
        self.list.heading('title',text='内容'); self.list.column('title',width=620)
        scroll=ttk.Scrollbar(frame,orient='vertical',command=self.list.yview)
        self.list.configure(yscrollcommand=scroll.set)
        self.list.pack(fill='both',expand=True,pady=12)
        scroll.place(in_=self.list,relx=1,rely=0,relheight=1,anchor='ne')
        self.list.bind('<<TreeviewSelect>>',self.select)
        self.title=tk.StringVar(); self.note=tk.StringVar()
        for label,variable in (('内容',self.title),('备注',self.note)):
            ttk.Label(frame,text=label).pack(anchor='w')
            ttk.Entry(frame,textvariable=variable).pack(fill='x',pady=(3,8))
        buttons=ttk.Frame(frame); buttons.pack(fill='x',pady=10)
        for label,command in (('保存',self.save),('新建',self.clear),('完成 / 恢复',self.toggle),('删除',self.delete),('重新读取',self.refresh)):
            ttk.Button(buttons,text=label,command=command).pack(side='left',padx=(0,8))
        self.refresh()

    def refresh(self):
        try: self.data=self.store.read()
        except (OSError,ValueError):
            messagebox.showwarning('无法读取','请检查 Kindle USB 连接。',parent=self.window); return
        from renderer import todo_items
        self.list.delete(*self.list.get_children())
        for item in todo_items(self.data):
            self.list.insert('','end',iid=item['id'],values=('已完成' if item.get('done') else '□ 未完成',item['title']))
        self.clear()

    def clear(self):
        self.identity=''; self.title.set(''); self.note.set('')
        self.list.selection_remove(self.list.selection())

    def select(self,event=None):
        selected=self.list.selection()
        if not selected: return
        item=next(e for e in self.data['todos'] if e['id']==selected[0])
        self.identity=item['id']; self.title.set(item['title']); self.note.set(item.get('note',''))

    def change(self,action,values):
        try:
            self.store.change(self.data['revision'],action,values)
            self.refresh()
            # Keep an already open agenda form's revision stale, so it cannot
            # silently overwrite another edit. Its refresh button reloads it.
        except (Conflict,ValueError) as error:
            messagebox.showwarning('暂未保存',str(error),parent=self.window)
        except OSError:
            messagebox.showwarning('暂未保存','请检查 Kindle USB 连接。',parent=self.window)

    def save(self):
        self.change('todo_save',dict(id=self.identity,title=self.title.get(),note=self.note.get()))

    def toggle(self):
        if not self.identity: return
        item=next(e for e in self.data['todos'] if e['id']==self.identity)
        self.change('todo_state',dict(id=self.identity,done='0' if item.get('done') else '1'))

    def delete(self):
        if self.identity and messagebox.askyesno('删除待办','确定删除选中的待办吗？',parent=self.window):
            self.change('todo_delete',{'id':self.identity})

if __name__=='__main__': Editor().window.mainloop()
