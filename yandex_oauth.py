"""Получение и обновление OAuth-токенов для Яндекс Почты."""

import json
import os
from contextlib import contextmanager, suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic, sleep, time

import requests

from yandex_mail import MailError, YandexMailbox


OAUTH_BASE = 'https://oauth.yandex.ru'
DEVICE_PAGE = f'{OAUTH_BASE}/device'
ACCOUNT_INFO_URL = 'https://login.yandex.ru/info'
MAIL_SCOPES = {'mail:imap_full', 'mail:smtp'}
OAUTH_ERRORS = {
    'access_denied', 'authorization_pending', 'bad_verification_code', 'expired_token',
    'invalid_client', 'invalid_grant', 'invalid_request', 'invalid_scope', 'slow_down',
    'temporarily_unavailable', 'unauthorized_client', 'unsupported_grant_type',
}


class OAuthError(MailError):
    def __init__(self, code):
        super().__init__(f'Яндекс OAuth: {code}.')
        self.code = code


class OAuthConnectionError(MailError):
    pass


def _post(endpoint, params, *, auth=None):
    try:
        response = requests.post(f'{OAUTH_BASE}/{endpoint}', data=params, auth=auth,
                                 timeout=(5, 10), allow_redirects=False)
    except requests.RequestException as exc:
        raise OAuthConnectionError(f'Не удалось связаться с Яндекс OAuth ({type(exc).__name__}).') from None
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or not 200 <= response.status_code < 300 or 'error' in data:
        code = data.get('error') if isinstance(data, dict) else None
        if not isinstance(code, str) or code not in OAUTH_ERRORS:
            code = f'HTTP {response.status_code}'
        raise OAuthError(code)
    return data


def _valid_secret(value):
    return isinstance(value, str) and bool(value) and all(32 < ord(char) < 127 for char in value)


def _token_values(data):
    access = data.get('access_token')
    refresh = data.get('refresh_token')
    lifetime = data.get('expires_in')
    scope = data.get('scope')
    if not (_valid_secret(access) and _valid_secret(refresh) and isinstance(lifetime, int) and lifetime > 0):
        raise MailError('Яндекс OAuth вернул неполные данные токена.')
    if scope is not None and (not isinstance(scope, str) or not MAIL_SCOPES <= set(scope.split())):
        raise MailError('Яндекс не выдал все права на чтение и отправку почты.')
    return dict(access_token=access, refresh_token=refresh,
                refresh_at=int(time()) + min(max(0, lifetime - 60), 90 * 86400))


class YandexMailOAuth:
    def __init__(self, address):
        self.address = address
        default_path = Path.home() / '.config' / 'setrans' / 'yandex-mail-oauth.json'
        self.path = Path(os.getenv('YANDEX_MAIL_OAUTH_FILE') or default_path).expanduser()

    @contextmanager
    def _locked(self):
        import fcntl  # macOS и Linux, на которых работает почтовая интеграция.

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + '.lock')
        with os.fdopen(os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600), 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _write(self, data):
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, delete=False) as temp:
            temp_path = Path(temp.name)
            try:
                json.dump(data, temp)
                temp.flush()
                os.fsync(temp.fileno())
                os.replace(temp_path, self.path)
            finally:
                with suppress(FileNotFoundError):
                    temp_path.unlink()

    def _verify_account(self, access_token):
        try:
            response = requests.get(ACCOUNT_INFO_URL, headers={'Authorization': f'OAuth {access_token}'},
                                    timeout=(5, 10), allow_redirects=False)
            data = response.json() if response.status_code == 200 else None
        except (requests.RequestException, ValueError):
            data = None
        login = data.get('login') if isinstance(data, dict) else None
        if not isinstance(login, str) or not login:
            raise MailError('Не удалось проверить владельца OAuth-токена в Яндекс ID.')
        address = self.address.casefold()
        username, _, domain = address.partition('@')
        expected_login = username if domain in ('yandex.ru', 'yandex.com', 'ya.ru') else address
        if login.casefold() not in (address, expected_login):
            raise MailError('OAuth-токен выдан для другого аккаунта Яндекса. Запустите yandex-mail-authorize и выберите нужный ящик.')

    def access_token(self):
        try:
            with self._locked():
                with self.path.open(encoding='utf-8') as file:
                    if os.fstat(file.fileno()).st_mode & 0o077:
                        raise MailError('Установите права 600 на файл OAuth для Яндекс Почты.')
                    values = json.load(file)
                required = ('client_id', 'client_secret', 'access_token', 'refresh_token')
                if (not isinstance(values, dict) or values.get('address') != self.address
                        or not all(_valid_secret(values.get(key)) for key in required)
                        or not isinstance(values.get('refresh_at'), int)):
                    raise MailError('Файл OAuth не подходит для этого ящика. Запустите yandex-mail-authorize.')
                if values['refresh_at'] <= time():
                    payload = _post('token', {'grant_type': 'refresh_token', 'refresh_token': values['refresh_token']},
                                    auth=(values['client_id'], values['client_secret']))
                    values.update(_token_values(payload))
                    self._write(values)
                if values.get('account_verified') is not True:
                    self._verify_account(values['access_token'])
                    values['account_verified'] = True
                    self._write(values)
                return values['access_token']
        except (FileNotFoundError, ValueError):
            raise MailError('Файл OAuth отсутствует или повреждён. Запустите yandex-mail-authorize.') from None
        except OSError as exc:
            raise MailError(f'Файл OAuth недоступен ({type(exc).__name__}).') from None

    def authorize(self, client_id, client_secret, show_code, logger):
        if not (_valid_secret(client_id) and _valid_secret(client_secret)):
            raise MailError('Укажите Client ID и Client Secret из карточки приложения.')
        device = _post('device/code', {'client_id': client_id, 'scope': ' '.join(sorted(MAIL_SCOPES))})
        code = device.get('device_code')
        user_code = device.get('user_code')
        interval = device.get('interval', 5)
        lifetime = device.get('expires_in')
        if not (_valid_secret(code) and _valid_secret(user_code) and isinstance(interval, int) and interval > 0
                and isinstance(lifetime, int) and lifetime > 0):
            raise MailError('Яндекс OAuth вернул неполные данные для подтверждения.')
        deadline = monotonic() + lifetime
        show_code(DEVICE_PAGE, user_code)
        while monotonic() + interval < deadline:
            sleep(interval)
            try:
                payload = _post('token', {'grant_type': 'device_code', 'code': code}, auth=(client_id, client_secret))
            except OAuthConnectionError:
                interval *= 2
                continue
            except OAuthError as exc:
                if exc.code in ('slow_down', 'temporarily_unavailable'):
                    interval += 5
                    continue
                if exc.code == 'authorization_pending':
                    continue
                raise
            values = _token_values(payload)
            self._verify_account(values['access_token'])
            with YandexMailbox(self.address, None, oauth_token=values['access_token'], logger=logger):
                pass  # Проверяем доступ к IMAP после проверки аккаунта в Яндекс ID.
            values.update(address=self.address, account_verified=True, client_id=client_id, client_secret=client_secret)
            try:
                with self._locked():
                    self._write(values)
            except OSError as exc:
                raise MailError(f'Не удалось сохранить файл OAuth ({type(exc).__name__}).') from None
            return
        raise MailError('Время подтверждения Яндекс OAuth истекло. Запустите yandex-mail-authorize снова.')
