from datetime import date, datetime
import pytest
from dashboard_metrics import build_dashboard_metrics


@pytest.fixture
def dashboard_page(app_ctx):
    mod = app_ctx
    user = mod.User(username='dashboard-test', password='unused', company='Сетранс', allowed_parks=['14 парк'])
    mod.db.session.add(user)
    for park, timestamp, ktg, count in [
        ('14 парк', datetime(2026, 11, 12, 9), 90, 10),
        ('14 парк', datetime(2026, 11, 12, 10), 94, 6),
        ('14 парк', datetime(2026, 11, 11, 10), None, 12),
        ('14 парк', datetime(2026, 10, 31, 23), 80, 20),
        ('14 парк', datetime(2026, 12, 1), 98, 2),
        ('1 парк', datetime(2026, 11, 12, 9), 0, 999),
    ]:
        mod.db.session.add(mod.KtgSnapshot(park=park, recorded_at=timestamp, ktg_park=ktg, in_repair=count))
    mod.db.session.commit()
    client = mod.application.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
        sess['available_parks'] = ['14 парк']
        sess['selected_park'] = '14 парк'
    return mod, client


def test_day_uses_moscow_hours_and_selected_park(dashboard_page):
    mod, _ = dashboard_page
    data = build_dashboard_metrics(mod.KtgSnapshot, mod.db.session, '14 парк', 'day', date(2026, 11, 12))
    assert len(data['labels']) == 24
    assert data['ktg'][9:11] == [90, 94]
    assert data['repair'][9:11] == [10, 6]
    assert data['ktg'][8] is None
    assert data['latest'] == {'ktg': 94, 'repair': 6, 'time': '12.11.2026 10:00'}


@pytest.mark.parametrize('period,index,ktg,repair,length', [
    ('week', 3, 92, 8, 7), ('month', 11, 92, 8, 30),
    ('year', 10, 92, 9.33, 12),
])
def test_period_averages_and_boundaries(dashboard_page, period, index, ktg, repair, length):
    mod, _ = dashboard_page
    data = build_dashboard_metrics(mod.KtgSnapshot, mod.db.session, '14 парк', period, date(2026, 11, 12))
    assert len(data['labels']) == length
    assert data['ktg'][index] == ktg
    assert data['repair'][index] == repair


def test_empty_and_missing_park(dashboard_page):
    mod, _ = dashboard_page
    for park in ['Красная Пахра', None]:
        data = build_dashboard_metrics(mod.KtgSnapshot, mod.db.session, park, 'month', date(2024, 2, 1))
        assert len(data['labels']) == 29
        assert not data['has_data']
        assert data['latest'] is None


def test_dashboard_renders_real_data_and_validates_inputs(dashboard_page):
    _, client = dashboard_page
    response = client.get('/dashboard?date=2026-11-12&period=day')
    assert response.status_code == 200
    assert 'dashboardChartData' in response.text
    assert '14 парк' in response.text
    import json, re
    chart = json.loads(re.search(r'<script id="dashboardChartData" type="application/json">(.*?)</script>', response.text)[1])
    assert 999 not in chart['repair']
    for date_arg in ['invalid', '9999-12-31']:
        assert client.get(f'/dashboard?period=bad&date={date_arg}').status_code == 200
    with client.session_transaction() as sess:
        sess['selected_park'] = '1 парк'
    assert client.get('/dashboard').status_code == 403


def test_dashboard_requires_login(dashboard_page):
    mod, _ = dashboard_page
    assert mod.application.test_client().get('/dashboard').status_code == 302
