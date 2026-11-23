import time
from itsdangerous import TimestampSigner, URLSafeTimedSerializer
from test_index_live import index_client


def test_refresh_available_before_login_and_keeps_each_session_separate(app_ctx, monkeypatch):
    mod = app_ctx
    monkeypatch.setitem(mod.application.config, 'WTF_CSRF_ENABLED', True)
    first, second = mod.application.test_client(), mod.application.test_client()
    response = first.get('/csrf-token')
    assert response.status_code == 200
    assert 'no-store' in response.headers['Cache-Control']
    assert 'Cookie' in response.headers['Vary']
    assert 'Access-Control-Allow-Origin' not in response.headers
    token = response.json['csrf_token']
    second.get('/csrf-token')
    assert second.post('/login', data={'csrf_token': token}).status_code == 400
    with first.session_transaction() as sess:
        assert '_user_id' not in sess


def test_old_form_can_be_saved_with_fresh_token_and_retains_session(index_client, monkeypatch):
    mod, client, user_id = index_client
    monkeypatch.setitem(mod.application.config, 'WTF_CSRF_ENABLED', True)
    with client.session_transaction() as sess:
        raw = sess['csrf_token']
        sess['_flashes'] = [('success', 'Сохранить уведомление')]
    class Expired(TimestampSigner):
        def get_timestamp(self): return int(time.time()) - 7200
    old = URLSafeTimedSerializer(mod.application.secret_key, salt='wtf-csrf-token', signer=Expired).dumps(raw)
    task_id = mod.Task.query.filter_by(board_number='164076').one().id
    path = f'/add_comment/{task_id}'
    assert client.post(path, data={'comment': 'Работы', 'csrf_token': old}).status_code == 400
    response = client.get('/csrf-token')
    token = response.json['csrf_token']
    assert 'Set-Cookie' not in response.headers
    assert client.post(path, data={'comment': 'Работы', 'csrf_token': old}, headers={'X-CSRFToken': token}).status_code == 400
    response = client.post(path, data={'comment': 'Работы', 'csrf_token': token}, headers={'X-Requested-With': 'XMLHttpRequest'})
    assert response.status_code == 200 and response.json['comment'] == 'Работы'
    with client.session_transaction() as sess:
        assert sess['_user_id'] == str(user_id)
        assert sess['csrf_token'] == raw
        assert sess['selected_park'] == '14 парк'
        assert sess['_flashes'] == [('success', 'Сохранить уведомление')]
