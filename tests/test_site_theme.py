from pathlib import Path
import jinja2
import pytest

from labs.services.display_format import planning_capital

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('maximum,budget', [
    (126487,130000),(12648.7,13000),(1264.87,1300),(126.487,130),
    (12.6487,13),(1.26487,2),(0.05,1),(130000,130000),
    (999999,1000000),('99999.000001',100000),
    (None,None),(0,None),(-1,None),('bad',None),('NaN',None),('Infinity',None),
])
def test_planning_capital_rounds_up_without_changing_recorded_maximum(maximum,budget):
    assert planning_capital(maximum) == budget


def test_tile_return_uses_planning_capital_for_selected_dates():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(ROOT/'templates'), autoescape=True)
    env.filters['planning_capital'] = planning_capital
    env.globals['url_for'] = lambda *a,**k: '/labs/live'
    card = dict(key='example',label='Example',size='1 lot',instrument='Options',status='Running',
                today_net=0,position='Flat',open_net=0,win_pct=66.666666,win_days=2,active_days=3,
                capital=126487,total_net=13000,return_pct=10.2777,trades=3,sessions=3,last_date='2026-10-22')
    html = env.get_template('_overview.html').render(overview_cards=[card],
        active_live_tab='overview',live_tabs={'overview':'Overview'},date_from='2026-10-01',date_to='2026-10-22')
    from html import unescape
    text = unescape(html)
    assert '₹130,000' in text and 'Recorded peak at one time ₹126,487' in text
    assert '(+10.0%)' in text and '66.7%' in text and '66.666666%' not in text
    assert card['capital'] == 126487 and card['return_pct'] == 10.2777


def test_all_pages_receive_shared_site_assets_and_saved_theme():
    chrome = (ROOT/'templates/_chrome.html').read_text(encoding='utf-8')
    assert '/static/site_theme.css' in chrome and '/static/site_theme.js' in chrome
    assert "classList.add('site-theme')" in chrome
    assert "setAttribute('data-theme', 'light')" in chrome
    assert "localStorage.getItem('labs-theme')" in chrome
    for path in (ROOT/'templates').glob('*.html'):
        if not path.name.startswith('_'):
            assert '{{ theme_head() }}' in path.read_text(encoding='utf-8'),path.name
