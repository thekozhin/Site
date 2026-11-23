"""Частичное обновление не меняет сессию и не забирает flash/формы."""
import pytest


@pytest.fixture
def index_client(app_ctx):
    mod = app_ctx
    user = mod.User(username='live-index', password='unused', full_name='Тест',
                    company='Сетранс', allowed_parks=['14 парк', '1 парк'])
    mod.db.session.add(user)
    for board, park in [('164076', '14 парк'), ('164077', '14 парк'), ('100001', '1 парк')]:
        mod.db.session.add(mod.Task(board_number=board, park=park, status='in_repair',
                                   description=f'Ремонт {board}', tag='waiting', type_ts='Автобус'))
    mod.db.session.commit()
    client = mod.application.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
        sess['available_parks'] = ['14 парк', '1 парк']
        sess['selected_park'] = '14 парк'
    assert client.get('/').status_code == 200
    return mod, client, user.id


def refresh(client, query=''):
    return client.get('/' + query, headers={'X-Index-Refresh': '1'})


def test_full_page_has_persistent_forms_and_refresh_containers(index_client):
    _, client, _ = index_client
    html = client.get('/').get_data(as_text=True)
    for element in ['indexLiveView', 'indexCounters', 'indexContractCounters', 'taskList', 'commentModal', 'ozchModal']:
        assert f'id="{element}"' in html
    assert 'js/index-live.js' in html
    assert '}, 300000)' not in html


def test_partial_returns_only_current_park_and_no_forms(index_client):
    _, client, _ = index_client
    response = refresh(client)
    assert response.status_code == 200
    data = response.get_json()
    assert data['park'] == '14 парк'
    assert '164076' in data['tasks_html']
    assert '100001' not in data['tasks_html']
    assert 'Всего заявок: 2' in data['counters_html']
    assert '<form' not in data['tasks_html']
    assert '<script' not in data['tasks_html']
    assert 'commentModal' not in data['tasks_html']
    assert 'no-store' in response.headers['Cache-Control']
    assert 'X-Index-Refresh' in response.headers['Vary']
    assert 'Set-Cookie' not in response.headers


def test_refresh_keeps_flash_and_saved_sort_settings(index_client):
    _, client, _ = index_client
    with client.session_transaction() as sess:
        sess['_flashes'] = [('success', 'Не терять уведомление')]
        sess['sort_by'] = 'created_at'
        sess['order'] = 'desc'
    response = refresh(client, '?sort_by=board_number&order=asc')
    assert response.status_code == 200
    with client.session_transaction() as sess:
        assert sess['_flashes'] == [('success', 'Не терять уведомление')]
        assert (sess['sort_by'], sess['order']) == ('created_at', 'desc')
    data = response.get_json()
    assert (data['sort_by'], data['order']) == ('board_number', 'asc')
    assert 'sort_by=board_number' in data['tasks_html']
    assert 'sort_by=created_at' not in data['tasks_html']


def test_new_and_completed_tasks_update_list_and_counters(index_client):
    mod, client, _ = index_client
    old = mod.Task.query.filter_by(board_number='164076').one()
    old.status = 'completed'
    old.completed_at = mod.incontrol_now()
    mod.db.session.add(mod.Task(board_number='164088', park='14 парк', status='in_repair',
                               description='Новая заявка', tag='in_work'))
    mod.db.session.commit()
    data = refresh(client).get_json()
    assert '164076' not in data['tasks_html']
    assert '164088' in data['tasks_html']
    assert 'Всего заявок: 2' in data['counters_html']
    assert 'Закрытых 1' in data['counters_html']


def test_refresh_respects_explicit_sort(index_client):
    _, client, _ = index_client
    html = refresh(client, '?sort_by=board_number&order=desc').get_json()['tasks_html']
    assert html.index('164077') < html.index('164076')


def test_empty_list_is_a_valid_refresh(index_client):
    mod, client, _ = index_client
    mod.Task.query.filter_by(park='14 парк').update({'status': 'completed'})
    mod.db.session.commit()
    data = refresh(client).get_json()
    assert 'data-task-id=' not in data['tasks_html']
    assert 'Всего заявок: 0' in data['counters_html']


def test_changed_session_park_is_identified_in_response(index_client):
    _, client, _ = index_client
    with client.session_transaction() as sess:
        sess['selected_park'] = '1 парк'
    data = refresh(client).get_json()
    assert data['park'] == '1 парк'
    assert '100001' in data['tasks_html']
    assert '164076' not in data['tasks_html']


def test_other_company_cannot_read_task_fragments(index_client):
    mod, client, user_id = index_client
    mod.db.session.get(mod.User, user_id).company = 'Другая компания'
    mod.db.session.commit()
    response = refresh(client)
    assert response.status_code == 403
    assert 'tasks_html' not in response.get_json()


def test_logged_out_refresh_requires_login(app_ctx):
    response = refresh(app_ctx.application.test_client())
    assert response.status_code == 302
    assert '/login' in response.headers['Location']


def test_task_text_is_escaped_in_partial(index_client):
    mod, client, _ = index_client
    mod.Task.query.filter_by(board_number='164076').one().description = '<script>alert("x")</script>'
    mod.db.session.commit()
    html = refresh(client).get_json()['tasks_html']
    assert '<script>' not in html
    assert '&lt;script&gt;' in html


def test_fresh_csrf_allows_saving_after_old_token_expired(index_client, monkeypatch):
    import time
    from itsdangerous import URLSafeTimedSerializer, TimestampSigner
    mod, client, _ = index_client
    monkeypatch.setitem(mod.application.config, 'WTF_CSRF_ENABLED', True)
    with client.session_transaction() as sess:
        raw_token = sess['csrf_token']
        sess['_flashes'] = [('success', 'Не терять flash')]
    class ExpiredSigner(TimestampSigner):
        def get_timestamp(self):
            return int(time.time()) - 7200
    old_token = URLSafeTimedSerializer(mod.application.secret_key, salt='wtf-csrf-token', signer=ExpiredSigner).dumps(raw_token)
    task_id = mod.Task.query.filter_by(board_number='164076').one().id
    path = f'/add_comment/{task_id}'
    assert client.post(path, data={'comment': 'Просрочен'}, headers={'X-CSRFToken': old_token}).status_code == 400
    response = client.get('/index/csrf-token')
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'Set-Cookie' not in response.headers
    fresh_token = response.get_json()['csrf_token']
    saved = client.post(path, data={'comment': 'Сохранён'}, headers={
        'X-CSRFToken': fresh_token, 'X-Requested-With': 'XMLHttpRequest'})
    assert saved.status_code == 200
    assert saved.get_json()['comment'] == 'Сохранён'
    with client.session_transaction() as sess:
        assert sess['_flashes'] == [('success', 'Не терять flash')]
