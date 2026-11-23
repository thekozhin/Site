import imaplib
import json
import smtplib
from email.message import EmailMessage
from time import time
from unittest.mock import Mock, call

import pytest

import yandex_oauth as oauth
from yandex_mail import MailError, YandexMailbox


ADDRESS = 'oauth-test@yandex.ru'
TOKEN = {
    'access_token': 'access-test', 'refresh_token': 'refresh-test',
    'expires_in': 3600, 'scope': 'mail:imap_full mail:smtp',
}


@pytest.fixture
def account_info(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {'login': ADDRESS.split('@')[0]}
    get = Mock(return_value=response)
    monkeypatch.setattr(oauth.requests, 'get', get)
    return get


def test_authorize_checks_mailbox_and_persists_credentials(tmp_path, monkeypatch, account_info):
    path = tmp_path / 'oauth.json'
    monkeypatch.setenv('YANDEX_MAIL_OAUTH_FILE', str(path))
    device = {'device_code': 'test-device', 'user_code': 'test-display', 'interval': 1, 'expires_in': 60}
    server = Mock(side_effect=[device, oauth.OAuthConnectionError('ReadTimeout'),
                               oauth.OAuthError('authorization_pending'), TOKEN])
    mailbox = Mock()
    mailbox.return_value.__enter__ = Mock()
    mailbox.return_value.__exit__ = Mock()
    monkeypatch.setattr(oauth, '_post', server)
    sleep = Mock()
    monkeypatch.setattr(oauth, 'sleep', sleep)
    monkeypatch.setattr(oauth, 'YandexMailbox', mailbox)
    display = Mock()
    logger = Mock()

    store = oauth.YandexMailOAuth(ADDRESS)
    store.authorize('client-test', 'secret-test', display, logger)

    display.assert_called_once_with(oauth.DEVICE_PAGE, 'test-display')
    account_info.assert_called_once_with(oauth.ACCOUNT_INFO_URL, headers={'Authorization': 'OAuth access-test'},
                                         timeout=(5, 10), allow_redirects=False)
    mailbox.assert_called_once_with(ADDRESS, None, oauth_token='access-test', logger=logger)
    assert server.call_args_list == [
        call('device/code', {'client_id': 'client-test', 'scope': 'mail:imap_full mail:smtp'}),
        call('token', {'grant_type': 'device_code', 'code': 'test-device'}, auth=('client-test', 'secret-test')),
        call('token', {'grant_type': 'device_code', 'code': 'test-device'}, auth=('client-test', 'secret-test')),
        call('token', {'grant_type': 'device_code', 'code': 'test-device'}, auth=('client-test', 'secret-test')),
    ]
    assert sleep.call_args_list == [call(1), call(2), call(2)]
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.access_token() == 'access-test'
    assert server.call_count == 4
    with pytest.raises(MailError, match='не подходит для этого ящика'):
        oauth.YandexMailOAuth('other@yandex.ru').access_token()


def test_refresh_replaces_both_tokens(tmp_path, monkeypatch, account_info):
    path = tmp_path / 'oauth.json'
    monkeypatch.setenv('YANDEX_MAIL_OAUTH_FILE', str(path))
    path.write_text(json.dumps({
        'address': ADDRESS, 'client_id': 'client-test', 'client_secret': 'secret-test',
        'access_token': 'expired', 'refresh_token': 'previous-refresh', 'refresh_at': 1,
    }))
    path.chmod(0o600)
    server = Mock(return_value=TOKEN)
    monkeypatch.setattr(oauth, '_post', server)

    store = oauth.YandexMailOAuth(ADDRESS)
    assert store.access_token() == 'access-test'
    assert store.access_token() == 'access-test'
    server.assert_called_once_with('token', {'grant_type': 'refresh_token', 'refresh_token': 'previous-refresh'},
                                   auth=('client-test', 'secret-test'))
    saved = json.loads(path.read_text())
    assert saved['refresh_token'] == 'refresh-test' and saved['address'] == ADDRESS
    assert saved['account_verified'] is True
    account_info.assert_called_once()
    assert path.stat().st_mode & 0o777 == 0o600


def test_imap_and_smtp_use_xoauth2_with_full_address(monkeypatch):
    imap = Mock()
    smtp = Mock()
    connect_imap = Mock(return_value=imap)
    connect_smtp = Mock(return_value=smtp)
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', connect_imap)
    monkeypatch.setattr(smtplib, 'SMTP_SSL', connect_smtp)
    expected = f'user={ADDRESS}\x01auth=Bearer access-test\x01\x01'

    with YandexMailbox(ADDRESS, None, oauth_token='access-test') as mail:
        reply = EmailMessage()
        mail.send_reply(reply)

    assert connect_imap.call_args.args == ('imap.yandex.com', 993)
    mechanism, callback = imap.authenticate.call_args.args
    assert mechanism == 'XOAUTH2' and callback(b'') == expected.encode('ascii')
    assert callback(b'error') == b''
    imap.login.assert_not_called()
    assert connect_smtp.call_args.args == ('smtp.yandex.com', 465)
    mechanism, callback = smtp.auth.call_args.args
    assert mechanism == 'XOAUTH2' and callback() == expected
    smtp.login.assert_not_called()
    smtp.send_message.assert_called_once_with(reply)


def test_preview_uses_oauth_without_an_app_password(app_ctx, tmp_path, monkeypatch, account_info):
    path = tmp_path / 'oauth.json'
    path.write_text(json.dumps({
        'address': ADDRESS, 'client_id': 'client-test', 'client_secret': 'secret-test',
        'access_token': 'access-test', 'refresh_token': 'refresh-test', 'refresh_at': int(time()) + 3600,
    }))
    path.chmod(0o600)
    monkeypatch.setenv('YANDEX_MAIL_ADDRESS', ADDRESS)
    monkeypatch.setenv('YANDEX_MAIL_AUTH', 'oauth')
    monkeypatch.setenv('YANDEX_MAIL_OAUTH_FILE', str(path))
    monkeypatch.setenv('YANDEX_MAIL_FOLDERS', '{"14 парк":"14АП NEW"}')
    monkeypatch.delenv('YANDEX_MAIL_APP_PASSWORD', raising=False)
    imap = Mock()
    imap.select.return_value = 'OK', [b'0']
    imap.uid.return_value = 'OK', [b'']
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', Mock(return_value=imap))

    response = app_ctx.application.test_cli_runner().invoke(args=['yandex-mail-sync'])

    assert response.exit_code == 0
    assert json.loads(response.output.splitlines()[-1])['preview'] == 0
    imap.authenticate.assert_called_once()
    imap.select.assert_called_once_with('"14&BBAEHw- NEW"', readonly=True)
    imap.login.assert_not_called()


def test_sync_rejects_saved_token_for_other_account(app_ctx, tmp_path, monkeypatch, account_info):
    path = tmp_path / 'oauth.json'
    stored = {
        'address': ADDRESS, 'client_id': 'client-test', 'client_secret': 'secret-test',
        'access_token': 'wrong-account-token', 'refresh_token': 'refresh-test', 'refresh_at': int(time()) + 3600,
    }
    path.write_text(json.dumps(stored))
    path.chmod(0o600)
    monkeypatch.setenv('YANDEX_MAIL_ADDRESS', ADDRESS)
    monkeypatch.setenv('YANDEX_MAIL_AUTH', 'oauth')
    monkeypatch.setenv('YANDEX_MAIL_OAUTH_FILE', str(path))
    account_info.return_value.json.return_value = {'login': 'different-account'}
    connect_imap = Mock()
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', connect_imap)

    response = app_ctx.application.test_cli_runner().invoke(args=['yandex-mail-sync'])

    assert response.exit_code == 1
    assert 'OAuth-токен выдан для другого аккаунта' in response.output
    assert 'different-account' not in response.output
    connect_imap.assert_not_called()
    assert json.loads(path.read_text()) == stored
