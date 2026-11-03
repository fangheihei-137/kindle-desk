"""Synthetic screenshots only; never reads private runtime data."""
import sys
from datetime import timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(ROOT/'kindle-standalone'))
import renderer as ui
import native_input
from tests.test_standalone_layout import fixture,weather

out=ROOT/'docs/images'; out.mkdir(parents=True,exist_ok=True)
moment=ui.now().replace(hour=10,minute=28,second=0,microsecond=0)
titles=['人工智能应用加速落地，产业关注持续升温','全球市场观察：科技与金融动态',
        '新一代模型能力提升，开发者生态持续扩展','算力需求增长，基础设施建设稳步推进',
        '金融机构探索智能助手与风险管理应用','更多行业开始部署人工智能工具']
items=[dict(name=name,content=name,time=int((moment-timedelta(hours=i)).timestamp()),
            source='示例资讯',url='https://example.invalid/'+str(i)) for i,name in enumerate(titles)]
state=weather()
state.update(battery=76,battery_at=moment.isoformat(),charging=True,charging_at=moment.isoformat(),
             news=items,news_at=moment.isoformat(),pomodoro=dict(state='idle',remaining=2700,duration=2700))
ui.render(fixture(),state,moment).save(out/'dashboard.png')
ui.render_pomodoro(dict(state='idle',remaining=2700,duration=2700)).save(out/'timer-setup.png')
ui.render_pomodoro(dict(state='running',remaining=2025,duration=2700)).save(out/'timer-running.png')
ui.render_calendar(fixture(),moment.year,moment.month,moment).save(out/'calendar.png')
ui.render_weather(state,moment).save(out/'weather.png')
form=native_input.NativeForm('todo',0); form.values['title']='整理本周阅读笔记'
native_input.render(form).save(out/'native-input.png')
ui.render_module_picker(fixture()).save(out/'modules.png')
ui.render_screensaver(fixture(),dict(system_sleep=True),moment).save(out/'standby-calendar.png')
print('Eight public demo screens rendered from synthetic data.')
