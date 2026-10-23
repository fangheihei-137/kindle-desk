import re,sys,tempfile,unittest
from datetime import date,datetime,timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'kindle-standalone'))
import app,renderer as ui
from store import atomic_json

class CalendarHeaderTests(unittest.TestCase):
    def test_dates_and_terms_against_hko_two_full_years(self):
        traditional=str.maketrans('閏驚蟄穀滿種處', '闰惊蛰谷满种处')
        month_names={'正':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,
                     '八':8,'九':9,'十':10,'十一':11,'十二':12}
        for year in (2026,2033):
            rows=0; terms=0
            source=(ROOT/f'tests/calendar-reference/{year}.txt').read_text(encoding='utf-8')
            for line in source.splitlines():
                match=re.match(r'(\d+)年(\d+)月(\d+)日\s+(\S+)\s+星期\S(?:\s+(\S+))?',line)
                if not match: continue
                y,m,d,lunar,term=match.groups()
                day=date(int(y),int(m),int(d)); rows+=1
                label,festivals,actual_term=ui.calendar_labels(day)
                self.assertEqual(actual_term,(term or '').translate(traditional),(day,'term'))
                terms+=bool(term)
                lunar=lunar.translate(traditional)
                if '月' in lunar:
                    self.assertEqual(ui.lunar_date(day)[2],1,day)
                    month=month_names[lunar.removeprefix('闰').removesuffix('月')]
                    self.assertEqual(ui.lunar_date(day)[1],-month if lunar.startswith('闰') else month,day)
                else: self.assertTrue(label.endswith(lunar),(day,label,lunar))
            self.assertEqual(rows,365)
            self.assertEqual(terms,24)

    def test_new_year_eve_leap_month_and_daily_rollover(self):
        self.assertEqual(ui.calendar_labels(date(2026,10,23)),('农历 九月十四',(),'霜降'))
        self.assertEqual(ui.calendar_labels(date(2026,2,16))[:2],('农历 腊月廿九',('除夕',)))
        self.assertEqual(ui.calendar_labels(date(2026,2,17))[:2],('农历 正月初一',('春节',)))
        self.assertEqual(ui.calendar_labels(date(2025,7,25))[0],'农历 闰六月初一')
        self.assertEqual(ui.calendar_labels(date(2033,12,22))[0],'农历 闰冬月初一')
        self.assertEqual(ui.calendar_labels(date(2026,10,24)),('农历 九月十五',(),''))
        # A leap seventh month does not repeat Qixi.
        row=next(row for row in ui.LUNAR_YEARS if row[1]==7)
        offset=sum(29+((row[2]>>i)&1) for i in range(7))+6
        leap_day=date.fromordinal(row[0]+offset)
        self.assertEqual(ui.lunar_date(leap_day)[1:3],(-7,7))
        self.assertNotIn('七夕',ui.calendar_labels(leap_day)[1])

    def test_festival_and_term_coexist_without_filling_ordinary_days(self):
        self.assertEqual(ui.calendar_labels(date(2026,3,20))[1:],(('龙抬头',),'春分'))
        self.assertEqual(ui.calendar_labels(date(2026,6,21))[1:],(('父亲节',),'夏至'))
        self.assertIn('母亲节',ui.calendar_labels(date(2026,5,10))[1])
        self.assertEqual(ui.calendar_labels(date(2026,10,1))[1],('国庆节',))
        self.assertEqual(ui.calendar_labels(date(2026,10,22))[1:],((),''))

    def test_supported_range_boundaries_and_month_continuity(self):
        for day in (date(1901,1,1),date(2100,12,31)):
            self.assertTrue(ui.calendar_labels(day)[0])
        for day in (date(1900,12,31),date(2101,1,1)):
            self.assertIsNone(ui.lunar_date(day))
            self.assertEqual(ui.calendar_labels(day)[0],'')
        for index,(start,leap,bits) in enumerate(ui.LUNAR_YEARS[:-1]):
            length=sum(29+((bits>>i)&1) for i in range(13 if leap else 12))
            self.assertEqual(start+length,ui.LUNAR_YEARS[index+1][0])

    def test_date_shortcut_and_clock_hit_regions_remain_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'data').mkdir()
            atomic_json(root/'data/agenda.json',dict(token='calendar',revision=0,events=[],todos=[]))
            a=app.Application(root)
            for point in ((100,65),(100,115),(303,40),(303,90)):
                self.assertEqual(a.tap_action(*point),'calendar_today')
            self.assertEqual(a.tap_action(480,85),'pomodoro')
            a.handle_action('calendar_today')
            self.assertEqual(a.mode,'calendar')

if __name__=='__main__': unittest.main(verbosity=2)
