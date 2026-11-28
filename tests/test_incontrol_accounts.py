import re
from unittest.mock import Mock
import pytest
from flask import g


@pytest.fixture
def account_page(app_ctx, monkeypatch):
    mod = app_ctx
    monkeypatch.setitem(mod.application.config, 'WTF_CSRF_ENABLED', True)
    user = mod.User(username='personal-test', password='unused', full_name='Кожин О.Н.', company='Сетранс', allowed_parks=['14 парк'])
    mod.db.session.add(user)
    mod.db.session.commit()
    client = mod.application.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
        sess['available_parks'] = ['14 парк']
        sess['selected_park'] = '14 парк'
    html = client.get('/settings/incontrol').text
    token = re.search(r'name="csrf_token" value="([^"]+)"', html)[1]
    return mod, client, user.id, token


def save(page, login='personal@example.test', password='private-password'):
    _, client, _, token = page
    return client.post('/settings/incontrol', data={'action':'save', 'incontrol_login':login, 'incontrol_password':password}, headers={'X-CSRFToken':token})


def test_save_encrypts_credentials_and_never_renders_password(account_page):
    mod, client, uid, _ = account_page
    assert save(account_page).status_code == 302
    row = mod.db.session.get(mod.incontrol_integration.account_model, uid)
    assert 'private-password' not in row.credentials_encrypted
    assert 'personal@example.test' not in row.credentials_encrypted
    html = client.get('/settings/incontrol')
    assert 'personal@example.test' in html.text
    assert 'private-password' not in html.text
    assert html.headers['Cache-Control'] == 'no-store'
    personal = mod.incontrol_integration.personal_client(uid)
    assert personal.login == 'personal@example.test'
    assert personal.password == 'private-password'
    personal.session.close()


def test_blank_password_preserves_existing_login_only(account_page):
    mod, _, uid, _ = account_page
    save(account_page)
    assert save(account_page, password='').status_code == 302
    response = save(account_page, login='different@example.test', password='')
    assert 'Введите пароль' in response.text
    assert mod.incontrol_integration.personal_client(uid).login == 'personal@example.test'


def test_other_user_cannot_read_or_overwrite_saved_account(account_page):
    mod, client, uid, token = account_page
    save(account_page)
    second = mod.User(username='other-personal', password='unused')
    mod.db.session.add(second)
    mod.db.session.commit()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(second.id)
    g.pop('_login_user', None)
    assert 'personal@example.test' not in client.get('/settings/incontrol').text
    save(account_page, login='other@example.test', password='other-private')
    assert mod.incontrol_integration.personal_client(uid).login == 'personal@example.test'
    assert mod.incontrol_integration.personal_client(second.id).login == 'other@example.test'


def test_check_authenticates_only_personal_account(account_page, monkeypatch):
    import incontrol
    _, client, _, token = account_page
    save(account_page)
    fake = Mock()
    factory = Mock(return_value=fake)
    monkeypatch.setattr(incontrol, 'IncontrolClient', factory)
    client.post('/settings/incontrol', data={'action':'check'}, headers={'X-CSRFToken':token})
    factory.assert_called_once_with('personal@example.test', 'private-password', timeout=25, interactive=True)
    fake.authenticate.assert_called_once_with()
    fake.session.close.assert_called_once_with()


def test_missing_account_does_not_use_global_credentials(account_page):
    from incontrol import PersonalAccountError
    mod, _, uid, _ = account_page
    with pytest.raises(PersonalAccountError):
        mod.incontrol_integration.personal_client(uid)


def test_csrf_login_and_deletion(account_page):
    mod, client, uid, token = account_page
    assert client.post('/settings/incontrol', data={'action':'save'}).status_code == 400
    g.pop('_login_user', None)
    assert mod.application.test_client().get('/settings/incontrol').status_code == 302
    g.pop('_login_user', None)
    save(account_page)
    client.post('/settings/incontrol', data={'action':'delete'}, headers={'X-CSRFToken':token})
    assert mod.db.session.get(mod.incontrol_integration.account_model, uid) is None


def test_auth_error_does_not_disclose_credentials(account_page, monkeypatch):
    import incontrol
    _, client, _, token = account_page
    save(account_page)
    fake=Mock()
    fake.authenticate.side_effect=RuntimeError('private-password')
    monkeypatch.setattr(incontrol,'IncontrolClient',Mock(return_value=fake))
    response=client.post('/settings/incontrol',data={'action':'check'},headers={'X-CSRFToken':token})
    assert 'private-password' not in response.text
    assert 'Войти в InControl не удалось' in response.text


def test_personal_session_is_encrypted_and_reused_until_credentials_change(account_page):
    mod, client, uid, token=account_page
    save(account_page)
    personal=mod.incontrol_integration.personal_client(uid)
    personal._apply_tokens({'access':'session-access-test','refresh':'session-refresh-test'})
    mod.application.extensions['incontrol_accounts']['remember_auth'](uid,personal)
    row=mod.db.session.get(mod.incontrol_integration.account_model,uid)
    assert 'session-access-test' not in row.credentials_encrypted
    assert 'session-refresh-test' not in client.get('/settings/incontrol').text
    reused=mod.incontrol_integration.personal_client(uid)
    assert reused.session.headers['Authorization']=='JWT session-access-test'
    assert reused._refresh_token=='session-refresh-test'
    save(account_page,password='new-private-password')
    changed=mod.incontrol_integration.personal_client(uid)
    assert changed._refresh_token is None
    assert 'Authorization' not in changed.session.headers
    for item in [personal,reused,changed]:item.session.close()


def test_account_presence_controls_closing_mode(account_page):
    mod,client,uid,_=account_page
    configured=mod.application.extensions['incontrol_accounts']['has_credentials']
    assert not configured(uid)
    save(account_page)
    assert configured(uid)
