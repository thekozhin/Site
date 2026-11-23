"""Сохранение личного сеанса без введённого пользователем кода подписи."""
import base64
import json
import time
from requests.cookies import create_cookie


COOKIE_DOMAINS = {'skk.mttech.moscow', 'mttech.moscow'}


def capture_session(client):
    authorization = client.session.headers.get('Authorization')
    refresh = client._refresh_token
    if not isinstance(authorization, str) or not authorization.startswith('JWT ') or not isinstance(refresh, str):
        return {}
    cookies = []
    for cookie in client.session.cookies:
        if cookie.domain.lstrip('.') not in COOKIE_DOMAINS or cookie.is_expired():
            continue
        cookies.append({'name': cookie.name, 'value': cookie.value, 'domain': cookie.domain,
                        'path': cookie.path, 'secure': cookie.secure, 'expires': cookie.expires,
                        'rest': dict(cookie._rest)})
    return {'access': authorization.removeprefix('JWT '), 'refresh': refresh, 'cookies': cookies}


def restore_session(client, data):
    if not isinstance(data, dict) or not data.get('access') or not data.get('refresh'):
        return
    client._apply_tokens(data)
    for values in data.get('cookies', []):
        if values.get('domain', '').lstrip('.') not in COOKIE_DOMAINS:
            continue
        if values.get('expires') is not None and values['expires'] <= time.time():
            continue
        cookie = create_cookie(**{key: values[key] for key in ('name','value','domain','path','secure','expires','rest') if key in values})
        client.session.cookies.set_cookie(cookie)


def ensure_session(client):
    authorization = client.session.headers.get('Authorization')
    if isinstance(authorization, str) and authorization.startswith('JWT '):
        token = authorization.removeprefix('JWT ')
        try:
            part = token.split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
            # Это только выбор момента обновления. Подлинность JWT проверяет API.
            if float(claims['exp']) > time.time() + 30:
                return
        except (ValueError, KeyError, IndexError, TypeError):
            pass
    if isinstance(getattr(client, '_refresh_token', None), str):
        client._renew()
    else:
        client.authenticate()
