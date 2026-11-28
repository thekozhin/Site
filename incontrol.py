"""Интеграция Сетранса с InControl: синхронизация, проверка и закрытие заявок.

Клиент API выполняет сетевые операции. Сценарии закрытия управляют этапами
и подписью. В конце файла IncontrolIntegration связывает их с Flask, моделями
Сетранса и планировщиком. Модели передаются при запуске приложения, поэтому
импортировать app.py из этого файла не нужно.
"""

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

import pytz
import requests
from apscheduler.triggers.cron import CronTrigger
from cryptography.fernet import Fernet, InvalidToken
from flask import (
    abort, current_app, flash, has_request_context, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)
from flask_login import current_user, login_required
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from requests.adapters import HTTPAdapter
from requests.cookies import create_cookie
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from urllib3.util.retry import Retry

from task_import_matching import active_repairs_for_board, manual_repair_candidates, match_imported_repair
from yandex_mail import MAIL_TASK_AUTHOR, utcnow


MOSCOW_TZ = pytz.timezone('Europe/Moscow')


# --- HTTP-клиент, организации, форматы заявок и открытые этапы -----------------

API_BASE = 'https://skk.mttech.moscow/api/v1'

# Без этих двух заголовков бэкенд отвечает 404, а не 401 — легко принять за
# опечатку в пути.
APP_ID = 'x6VWRinSpNGt39J'
APP_VERSION = '1.47.5'

ROLE_ID = 143

# Открытые этапы: «Новое.», «Назначено.», «Принято.» (точка — часть названия).
OPEN_PHASES = (1, 2, 3)

STK_FAILED_LABELS = {'Не пройдено СТК', 'Не принято СТК'}
UNRESOLVED_REPAIR_LABEL = 'Неисправности не устранены'
IGNORED_LABELS = STK_FAILED_LABELS | {UNRESOLVED_REPAIR_LABEL}

BOARD_FIELD = 'Бортовой номер'

# Task.park хранит внутренние коды парков ('1 парк', '14 парк'), а не названия.
# Соответствие кодов названиям — в PARK_NAME (app.py). Принимаем оба написания:
# в базе встречаются и коды, и 'Красная Пахра'.
PARK_TO_ORG = {
    '14 парк': 1069,
    'Теплый Стан': 1069,
    'Красная Пахра': 1060,
    '1 парк': 1059,
    'Коньково': 1059,
    '18 парк': 1375,
    'Ясенево': 1375,
}

# Парки, которых в incontrol24 нет: у учётной записи всего три организации,
# Ясенево среди них не значится. Проверять такие борта по всем организациям
# означало бы тратить по три запроса впустую на каждый.
PARKS_OUTSIDE_INCONTROL = {}

# Организации без повторов: в PARK_TO_ORG каждая встречается дважды из-за
# синонимов, и values() дал бы лишние запросы в запасной ветке.
ALL_ORGS = (1069, 1060, 1059, 1375)

# Размер страницы. Работает только per_page: page_size и limit API молча
# игнорирует, отвечая 200 и отдавая 10 записей.
PAGE_SIZE = 100

# Предохранитель от бесконечной пагинации, если API перестанет отдавать next.
# При 100 записях на странице это 20 000 задач - втрое больше нынешнего объёма.
MAX_PAGES = 200


def repair_return_reason(work_name, phase_name, label_name):
    """Определить причину возврата ремонта по типу, этапу и метке заявки."""
    phase = str(phase_name or '').strip().rstrip('.').casefold()
    if work_name == 'Заявка на ремонт' and phase in {'новое', 'назначено', 'принято'}:
        if label_name in STK_FAILED_LABELS:
            return 'stk'
        if label_name == UNRESOLVED_REPAIR_LABEL:
            return 'unresolved'
    return None


def is_outside_incontrol(park):
    """Парк заведомо отсутствует в incontrol24 — проверять его не нужно."""
    if not park:
        return False
    return str(park).strip() in PARKS_OUTSIDE_INCONTROL


def normalize_board(value):
    """Привести бортовой номер к строке: API отдаёт его то строкой, то числом."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def board_of(task):
    """Достать бортовой номер задачи incontrol24.

    Номер лежит не в самой задаче, а в дополнительных полях объекта обслуживания.
    """
    serviced_object = (task or {}).get('serviced_object') or {}
    for field in serviced_object.get('additional_fields') or []:
        if field.get('field_name') == BOARD_FIELD:
            return normalize_board(field.get('value'))
    return None


def park_to_org(park):
    """Парк setrans → id организации incontrol24. Неизвестный парк → None."""
    if not park:
        return None
    return PARK_TO_ORG.get(str(park).strip())


# Обратное соответствие: org_id incontrol24 → код парка в Task.park.
# Синонимы ('Теплый Стан', 'Коньково') в PARK_TO_ORG намеренно пропускаем —
# в Task хранятся только коды вида '14 парк'.
ORG_TO_PARK = {
    1069: '14 парк',
    1060: 'Красная Пахра',
    1059: '1 парк',
    1375: '18 парк',
}


ALLOWED_WORK_NAMES = {
    'Заявка на сервисное обслуживание',
    'Заявка на ремонт',
    'Заявка на ремонт с линии',
}


LINKED_VIOLATIONS_FIELD = 'Связанные нарушения'


def linked_task_ids(task):
    """ID связанных нарушений из поля «Связанные нарушения»."""
    ids = []
    for field in (task or {}).get('additional_fields') or []:
        if field.get('field_name') != LINKED_VIOLATIONS_FIELD:
            continue
        value = field.get('value')
        if isinstance(value, list):
            for item in value:
                if item is not None and str(item).strip():
                    ids.append(str(item).strip())
        elif value is not None and str(value).strip():
            ids.append(str(value).strip())
    return ids


def format_child_description(task):
    """Описание дочерней заявки: «тип работ, текст»."""
    work_name = (
        ((task or {}).get('type_work') or {}).get('work_name') or ''
    ).strip()
    text = ((task or {}).get('task_description') or '').strip()
    if work_name and text:
        return f'{work_name}, {text}'
    return work_name or text


def org_to_park(org_id):
    """id организации incontrol24 → код парка setrans."""
    if org_id is None:
        return None
    try:
        return ORG_TO_PARK.get(int(org_id))
    except (TypeError, ValueError):
        return None


class IncontrolError(Exception):
    """Ошибка обращения к incontrol24."""

    def __init__(self, message, status_code=None):
        """Сохранить описание сбоя API и HTTP-статус, если он известен."""
        super().__init__(message)
        self.status_code = status_code


def is_incontrol_unavailable(error):
    """Отличает сетевой сбой и отказ сервиса от запрета или ошибки данных."""
    while error is not None:
        if isinstance(error, (TimeoutError, requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
        if getattr(error, 'status_code', None) in (500, 502, 503, 504):
            return True
        error = error.__cause__
    return False


class IncontrolClient:
    """Клиент API incontrol24 с обновлением токена.

    Access-токен живёт около пяти минут, поэтому обновление — штатный ход
    работы, а не обработка сбоя.
    """

    def __init__(self, login, password, session=None, timeout=30, interactive=False):
        """Подготовить HTTP-сеанс, заголовки, повторы и бюджет времени."""
        self.login = login
        self.password = password
        self.timeout = timeout
        self.interactive = interactive
        self._deadline = time.monotonic() + 35 if interactive else None
        self.session = session if session is not None else requests.Session()
        self.session.headers.update({
            'accept': 'application/json',
            'X-Incontrol-App': APP_ID,
            'X-Incontrol-App-Version': APP_VERSION,
            'X-Incontrol-Role': str(ROLE_ID),
        })
        self._refresh_token = None
        retry = Retry(
            total=0 if interactive else 4,
            connect=0 if interactive else 4,
            read=0 if interactive else 3,
            backoff_factor=0.7,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
        self.session.mount("https://", adapter)

    def _request_timeout(self):
        """Рассчитать тайм-аут запроса с учётом общего срока проверки."""
        if self._deadline is None:
            return self.timeout
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise IncontrolError('Истекло время проверки InControl')
        return (min(3, remaining), min(self.timeout, remaining))

    def _auth_audit(self, event, status=None):
        """Записать результат входа или обновления сеанса в журнал."""
        logging.getLogger('app').info('INCONTROL_AUTH context=%s event=%s http=%s',
            getattr(self, 'auth_context', 'personal' if self.interactive else 'background'), event, status)

    def authenticate(self):
        """Войти в InControl и установить токены для запросов."""
        last_error = None
        attempts = 1 if self.interactive else 4
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.post(
                    f'{API_BASE}/auth/get-code/',
                    json={'email': self.login, 'password': self.password},
                    timeout=self._request_timeout(),
                )
                if not response.ok:
                    raise IncontrolError(f'вход не выполнен: HTTP {response.status_code}', status_code=response.status_code)

                data = response.json()
                if 'access' not in data:
                    raise IncontrolError(
                        'incontrol24 требует код подтверждения — автоматический вход невозможен'
                    )
                self._apply_tokens(data)
                self._auth_audit('password_login', response.status_code)
                return

            except (requests.exceptions.SSLError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last_error = e
                if attempt < attempts:
                    time.sleep(0.6 * (2 ** (attempt - 1)))  # 0.6, 1.2, 2.4
                    continue
                raise IncontrolError(f'вход не выполнен после {attempt} попыток: {e}') from e

        raise IncontrolError(f'вход не выполнен: {last_error}')

    def _apply_tokens(self, data):
        """Обновить заголовок авторизации и токен для продления сеанса."""
        self._refresh_token = data.get('refresh', self._refresh_token)
        self.session.headers['Authorization'] = f'JWT {data["access"]}'

    def _renew(self):
        """Обновить access-токен, при неудаче — войти заново."""
        if self._refresh_token:
            response = self.session.post(
                f'{API_BASE}/auth/refresh/',
                json={'refresh': self._refresh_token},
                timeout=self._request_timeout(),
            )
            if response.ok:
                self._apply_tokens(response.json())
                self._auth_audit('refresh', response.status_code)
                return
            self._auth_audit('refresh_rejected', response.status_code)
        self.authenticate()

    def _get(self, path, params, org_id):
        """Прочитать данные API для указанной организации, при необходимости обновив токен."""
        headers = {'X-Incontrol-Organization': str(org_id)}
        last_error = None

        attempts = 1 if self.interactive else 3
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.get(
                    API_BASE + path, params=params, headers=headers, timeout=self._request_timeout()
                )
                if response.status_code == 401:
                    self._renew()
                    response = self.session.get(
                        API_BASE + path, params=params, headers=headers, timeout=self._request_timeout()
                    )
                if not response.ok:
                    raise IncontrolError(f'GET {path}: HTTP {response.status_code}', status_code=response.status_code)
                return response.json()

            except (requests.exceptions.SSLError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last_error = e
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
                    continue
                raise IncontrolError(f'GET {path} не удался: {e}') from e

        raise IncontrolError(f'GET {path}: {last_error}')

    def fetch_open_boards(self, org_id):
        """
        Получить открытые заявки по организации.
        Один борт может иметь несколько заявок разных типов.
        Возвращает:

        {
            '164055': [
                {
                    'phase_name': 'Принято.',
                    'work_name': 'Заявка на сервисное обслуживание'
                },
                {
                    'phase_name': 'Назначено.',
                    'work_name': 'Заявка на ремонт'
                }
            ]
        }
        """

        boards = {}
        page = 1

        while page <= MAX_PAGES:

            params = (
                    [('phase', phase) for phase in OPEN_PHASES]
                    + [
                        ('per_page', PAGE_SIZE),
                        ('page', page)
                    ]
            )

            data = self._get('/tasks/', params, org_id)

            for task in data.get('results') or []:

                board = board_of(task)

                if not board:
                    continue

                # Стадии "Отклонено" и "Выполнено" — полностью игнорируем
                if task.get('stage') in ('denied', 'completed'):
                    continue

                label_name = (
                        (task.get('label') or {}).get('label_name') or ''
                ).strip()


                phase_name = (
                    (task.get('phase') or {}).get('phase_name')
                )

                work_name = (
                        (task.get('type_work') or {}).get('work_name') or ''
                ).strip()

                # Нам нужны только эти типы
                if work_name not in ALLOWED_WORK_NAMES:
                    continue
                if label_name in IGNORED_LABELS and not repair_return_reason(work_name, phase_name, label_name):
                    continue

                boards.setdefault(board, [])

                # Не добавляем один и тот же тип заявки дважды
                already_exists = any(
                    item['work_name'] == work_name
                    for item in boards[board]
                )

                if not already_exists:
                    boards[board].append({
                        'phase_name': phase_name,
                        'work_name': work_name
                    })

            if not data.get('next'):
                break

            page += 1

        return boards

    def fetch_open_applications(self, org_id):
        """Открытые заявки ALLOWED_WORK_NAMES для импорта в Task.

        Возвращает список словарей:
        {
            'board': '164003',
            'work_name': 'Заявка на ремонт',
            'phase_name': 'Принято.',
            'task_id': 19311671,
            'linked_ids': ['19311705'],
            'org_id': 1069,
        }

        Фазы — OPEN_PHASES (Новое / Назначено / Принято).
        """
        applications = []
        page = 1

        while page <= MAX_PAGES:
            params = (
                [('phase', phase) for phase in OPEN_PHASES]
                + [('per_page', PAGE_SIZE), ('page', page)]
            )
            data = self._get('/tasks/', params, org_id)

            for task in data.get('results') or []:
                board = board_of(task)
                if not board:
                    continue

                if task.get('stage') in ('denied', 'completed'):
                    continue

                label_name = (
                    (task.get('label') or {}).get('label_name') or ''
                ).strip()

                work_name = (
                    (task.get('type_work') or {}).get('work_name') or ''
                ).strip()
                if work_name not in ALLOWED_WORK_NAMES:
                    continue

                phase_name = (
                    (task.get('phase') or {}).get('phase_name')
                )

                return_reason = repair_return_reason(work_name, phase_name, label_name)
                if label_name in IGNORED_LABELS and not return_reason:
                    continue

                applications.append({
                    'board': board,
                    'work_name': work_name,
                    'phase_name': phase_name,
                    'task_id': task.get('id'),
                    'linked_ids': linked_task_ids(task),
                    'org_id': org_id,
                    'creation_date': task.get('creation_date'),
                    'return_reason': return_reason,
                })

            if not data.get('next'):
                break
            page += 1

        return applications

    def get_task_by_id(self, task_id, org_id):
        """Найти заявку по ID внутри организации (через search)."""
        if task_id is None:
            return None

        params = [
            ('search', str(task_id)),
            ('per_page', PAGE_SIZE),
            ('page', 1),
        ]
        data = self._get('/tasks/', params, org_id)
        for task in data.get('results') or []:
            if str(task.get('id')) == str(task_id):
                return task
        return None

    def build_description_from_linked(self, linked_ids, org_id):
        """Собрать description из дочерних заявок через « / ».

        Формат одной части: «Кузов. Кузовные работы, передний бампер ...».
        Если дочерних нет — пустая строка.
        """
        parts = []
        for linked_id in linked_ids or []:
            try:
                child = self.get_task_by_id(linked_id, org_id)
            except IncontrolError:
                continue
            if not child:
                continue
            part = format_child_description(child)
            if part:
                parts.append(part)
        return ' / '.join(parts)

    def check_board(self, board, park):
        """Есть ли по борту открытая заявка. Возвращает (есть, название этапа).

        Один запрос на борт. Нужен только факт наличия, поэтому пагинацию
        не разворачиваем — хватает первой страницы.
        """
        board = normalize_board(board)
        if not board or is_outside_incontrol(park):
            return (False, None)

        org_id = park_to_org(park)
        org_ids = [org_id] if org_id else list(ALL_ORGS)

        params = [('search', board)] + [('phase', phase) for phase in OPEN_PHASES]
        for current_org in org_ids:
            data = self._get('/tasks/', params, current_org)
            for task in data.get('results') or []:
                # Сверяем номер сами: search сейчас даёт точное совпадение, но
                # это недокументированное поведение чужого API.
                if board_of(task) == board:
                    phase = (task.get('phase') or {}).get('phase_name')
                    return (True, phase)
        return (False, None)

    def check_board_details(
            self,
            board,
            park,
            required_work_names=None
    ):
        """
        Точечно проверить заявки по одному борту.

        required_work_names:
            None — искать любую разрешённую заявку.

            Для ТО:
            {'Заявка на сервисное обслуживание'}

            Для ремонта:
            {
                'Заявка на ремонт',
                'Заявка на ремонт с линии'
            }

        Возвращает:
            (has_open, phase_name, work_name)
        """

        board = normalize_board(board)

        if not board or is_outside_incontrol(park):
            return False, None, None

        org_id = park_to_org(park)

        org_ids = (
            [org_id]
            if org_id
            else list(ALL_ORGS)
        )
        
        OPEN_PHASE_NAMES = {'Новое.', 'Назначено.', 'Принято.'}

        for current_org in org_ids:
            for page in range(1, MAX_PAGES + 1):
                # Для закрытия нужны открытые заявки. Без phase поиск
                # возвращает также многолетнюю историю борта; один поиск
                # может занять несколько страниц и исчерпать весь бюджет.
                # Фильтры такие же, как в fetch_open_boards; проверку
                # ответа в Python и полную пагинацию оставляем.
                params = (
                    [('search', board)]
                    + [('phase', phase) for phase in OPEN_PHASES]
                    + [('per_page', PAGE_SIZE), ('page', page)]
                )
                data = self._get('/tasks/', params, current_org)
                if not isinstance(data, dict) or not isinstance(data.get('results'), list):
                    raise IncontrolError('InControl вернул некорректную выдачу заявок')
                if any(not isinstance(task, dict) for task in data['results']):
                    raise IncontrolError('InControl вернул некорректную заявку')

                for task in data['results']:

                    if board_of(task) != board:
                        continue

                    if task.get('stage') in ('denied', 'completed'):
                        continue

                    label_name = ((task.get('label') or {}).get('label_name') or '').strip()
                    phase_name = (task.get('phase') or {}).get('phase_name')
                    work_name = ((task.get('type_work') or {}).get('work_name') or '').strip()
                    if label_name in IGNORED_LABELS and not repair_return_reason(work_name, phase_name, label_name):
                        continue

                    if not isinstance(phase_name, str) or not phase_name:
                        raise IncontrolError('InControl не сообщил фазу заявки')
                    if phase_name not in OPEN_PHASE_NAMES:
                        continue
                    if not work_name:
                        raise IncontrolError('InControl не сообщил тип открытой заявки')
                    if work_name not in ALLOWED_WORK_NAMES:
                        continue

                    if (
                            required_work_names is not None
                            and work_name not in required_work_names
                    ):
                        continue

                    return True, phase_name, work_name


                if not data.get('next'):
                    break
            else:
                raise IncontrolError('Не удалось проверить все страницы заявок InControl')

        return False, None, None

    def debug_task_by_id(self, task_id):
        """
        Временная диагностика одной заявки InControl по ID.
        """

        print("\n")
        print("=" * 100)
        print(f"ПОИСК ЗАЯВКИ #{task_id}")
        print("=" * 100)

        found = False

        for org_id in ALL_ORGS:

            try:
                params = [
                    ('search', str(task_id)),
                    ('per_page', 100),
                    ('page', 1)
                ]

                data = self._get(
                    '/tasks/',
                    params,
                    org_id
                )

            except Exception as error:
                print(
                    f"Организация {org_id}: ошибка: {error}"
                )
                continue

            for task in data.get('results') or []:

                if str(task.get('id')) != str(task_id):
                    continue

                found = True

                print("\n")
                print("#" * 100)
                print(f"НАЙДЕНА ЗАЯВКА #{task_id}")
                print(f"ORGANIZATION: {org_id}")
                print("#" * 100)

                print(
                    json.dumps(
                        task,
                        ensure_ascii=False,
                        indent=4
                    )
                )

                print("#" * 100)

        if not found:
            print(
                f"\nЗаявка #{task_id} не найдена."
            )

        return found


# --- Сохранение и обновление сеанса --------------------------------------------

COOKIE_DOMAINS = {'skk.mttech.moscow', 'mttech.moscow'}


def capture_session(client):
    """Снять снимок JWT и cookies, нужных для продолжения личного сеанса."""
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
    """Восстановить токены и действующие cookies клиента из сохранённого снимка."""
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
    """Использовать действующий сеанс, при необходимости обновить токен или войти."""
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


# --- Подготовительная проверка заявки и исполнителя ----------------------------

class CompletionPreparationError(Exception):
    """Ошибка предварительного чтения заявки или проверки её связей."""
    pass


def normalized_name(value):
    """Привести названия этапов и фамилии к виду для сравнения."""
    return re.sub(r'[.\s]+', ' ', str(value or '').strip().casefold().replace('ё', 'е')).strip()


def is_service_request(task):
    """Определить, относится ли основная заявка к сервисному обслуживанию."""
    return normalized_name((task.get('type_work') or {}).get('work_name')) == 'заявка на сервисное обслуживание'


class CompletionPreparation:
    """Диагностика заявки и возможности закрытия через запросы на чтение."""
    def __init__(self, client):
        """Задать клиент и общий срок предварительной проверки."""
        self.client = client
        self.deadline = time.monotonic() + 60

    def get(self, path, org, params=None):
        """Прочитать данные для диагностики с учётом оставшегося времени."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CompletionPreparationError('Проверка InControl заняла слишком много времени. Повторите её.')
        response = self.client.session.get(
            API_BASE + path, params=params or {},
            headers={'X-Incontrol-Organization': str(org)},
            timeout=(min(3, remaining), min(25, remaining)),
        )
        if not response.ok:
            raise CompletionPreparationError(f'InControl не разрешил чтение данных (HTTP {response.status_code}).')
        return response.json()

    def inspect(self, task_id, park, full_name, board=None):
        """Сверить парк, борт, исполнителя, дочерние задачи и доступные документы."""
        org = PARK_TO_ORG.get(park)
        if not org:
            raise CompletionPreparationError('Для выбранного парка не настроена организация InControl.')
        main = self.get(f'/tasks/{int(task_id)}/', org)
        if (main.get('organization_performer') or {}).get('id') != org:
            raise CompletionPreparationError('Организация исполнителя заявки не совпадает с выбранным парком.')
        actual_board = next((str(f.get('value') or '') for f in (main.get('serviced_object') or {}).get('additional_fields', [])
                             if f.get('field_name') == 'Бортовой номер'), '')
        if board and actual_board != str(board):
            raise CompletionPreparationError('Борт заявки InControl не совпадает с локальной заявкой.')
        issues = []
        identity = self.get('/me/', org)
        surname = normalized_name(full_name).split(' ')[0]
        if not surname or surname != normalized_name(identity.get('last_name')):
            issues.append('Фамилия личного аккаунта InControl не совпадает с фамилией пользователя сайта.')
        identity_name = ' '.join(str(identity.get(k) or '') for k in ['last_name', 'first_name', 'middle_name']).strip()
        linked_ids = []
        for field in main.get('additional_fields', []):
            if field.get('link_type') == 'task' and field.get('field_name') in {'Связанные нарушения', 'Связанные задачи'}:
                value = field.get('value') or []
                if not isinstance(value, list) or any(not isinstance(x, int) or isinstance(x, bool) for x in value):
                    raise CompletionPreparationError('Неизвестный формат связей InControl. Автоматическое закрытие недоступно.')
                linked_ids.extend(value)
        linked_ids = list(dict.fromkeys(linked_ids))
        if not linked_ids:
            issues.append('У заявки нет связанных задач в ожидаемом поле. Требуется проверка схемы.')
        if len(linked_ids) > 25 or int(task_id) in linked_ids:
            raise CompletionPreparationError('Список связанных задач требует ручной проверки.')
        children = []
        service_main = is_service_request(main)
        for child_id in linked_ids:
            task = self.get(f'/tasks/{child_id}/', org)
            if (task.get('organization_performer') or {}).get('id') != org:
                issues.append(f'Связанная задача {child_id}: другой исполнитель-организация.')
            # Проверяем обратную связь, а не только совпадение борта.
            reverse = task.get('custom_field_dependent_tasks') or []
            if not any(x.get('linked_task') == child_id and x.get('dependent_task') == int(task_id) for x in reverse):
                issues.append(f'Связанная задача {child_id}: не подтверждена обратная связь с основной заявкой.')
            phase = normalized_name((task.get('phase') or {}).get('phase_name'))
            service_fields = [f for f in task.get('additional_fields', []) if f.get('field_name') == 'Выполненные услуги']
            if phase == 'отклонено':
                issues.append(f'Связанная задача {child_id} уже отклонена. Схема «Выполнено» требует отдельного решения.')
            elif phase not in {'новое', 'назначено', 'принято', 'выполнено'}:
                issues.append(f'Связанная задача {child_id}: неизвестный этап.')
            phase_only = service_main and not service_fields
            if not phase_only and (len(service_fields) != 1 or (phase != 'выполнено' and service_fields[0].get('is_disabled'))):
                issues.append(f'Связанная задача {child_id}: поле «Выполненные услуги» недоступно.')
            if phase != 'выполнено' and 'phase' in task.get('read_only_fields', []):
                issues.append(f'Связанная задача {child_id}: изменение этапа запрещено.')
            if service_fields and phase != 'выполнено' and 'additional_fields' in task.get('read_only_fields', []):
                issues.append(f'Связанная задача {child_id}: изменение услуг запрещено.')
            children.append({'id': child_id, 'work': (task.get('type_work') or {}).get('work_name'),
                             'phase': (task.get('phase') or {}).get('phase_name'),
                             'services_field_id': service_fields[0]['id'] if len(service_fields) == 1 else None})
        phase = normalized_name((main.get('phase') or {}).get('phase_name'))
        if phase not in {'новое', 'назначено', 'принято'}:
            issues.append('Основная заявка не находится на открытом этапе схемы «Выполнено».')
        reports = self.get(f'/tasks/{int(task_id)}/available-report-files/', org, {'is_paginated': 'false'})
        if not isinstance(reports, dict) or not isinstance(reports.get('results'), list):
            raise CompletionPreparationError('InControl вернул неизвестный формат документов.')
        return {'id': int(task_id), 'board': actual_board, 'work': (main.get('type_work') or {}).get('work_name'),
                'phase': (main.get('phase') or {}).get('phase_name'), 'identity': identity_name,
                'children': children, 'issues': issues, 'can_prepare': not issues,
                'reports_count': len(reports['results'])}


# --- Личные учётные записи и настройки закрытия --------------------------------

class PersonalAccountError(Exception):
    """Ошибка чтения или отсутствия личных учётных данных."""
    pass


def paused_closure_services():
    """Получить список интеграций, отключённых текущим пользователем для закрытия."""
    if not has_request_context():
        return ()
    settings = session.get('closure_pauses', {})
    if settings.get('user_id') == current_user.get_id():
        return settings.get('services', ())
    return ()


def credential_cipher():
    """Получить Fernet для шифрования личных данных и сохранённых процессов."""
    explicit = current_app.config.get('INCONTROL_CREDENTIALS_KEY')
    key = explicit or base64.urlsafe_b64encode(hashlib.sha256(
        b'incontrol-personal-credentials:v1:' + current_app.secret_key.encode('utf-8')
    ).digest())
    return Fernet(key)


def register_incontrol_accounts(application, db):
    """Подключить модель личных учётных данных и страницу настроек."""
    class IncontrolAccount(db.Model):
        """Зашифрованные личные данные InControl сотрудника."""
        __tablename__ = 'incontrol_account'
        __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
        user_id = db.Column(db.Integer, db.ForeignKey('users.id'), primary_key=True)
        credentials_encrypted = db.Column(db.Text, nullable=False)
        updated_at = db.Column(db.DateTime, nullable=False)

    def read_credentials(user_id):
        """Расшифровать сохранённые личные данные сотрудника."""
        row = db.session.get(IncontrolAccount, user_id)
        if row is None:
            raise PersonalAccountError('Сначала сохраните личный логин и пароль InControl.')
        try:
            return json.loads(credential_cipher().decrypt(row.credentials_encrypted.encode()))
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise PersonalAccountError('Не удалось прочитать настройки InControl. Сохраните логин и пароль заново.') from None

    def personal_client(user_id):
        """Создать клиент от имени сотрудника и восстановить его сохранённый сеанс."""
        data = read_credentials(user_id)
        # Никакого fallback на общую учётную запись.
        client = IncontrolClient(data['login'], data['password'], timeout=25, interactive=True)
        if isinstance(data.get('auth'), dict) and data['auth'].get('access') and data['auth'].get('refresh'):
            restore_session(client, data['auth'])
        return client

    def remember_auth(user_id, client):
        """Обновить сохранённый сеанс, если данные личного аккаунта ещё совпадают."""
        row = db.session.get(IncontrolAccount, user_id)
        if row is None:
            return
        data = read_credentials(user_id)
        # Не возвращаем старые данные при одновременной смене личного логина/пароля.
        if data['login'] != client.login or data['password'] != client.password:
            return
        auth = capture_session(client)
        if not auth or data.get('auth') == auth:
            return
        data['auth'] = auth
        row.credentials_encrypted = credential_cipher().encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()
        db.session.commit()

    application.extensions['incontrol_accounts'] = {
        'remember_auth': remember_auth,
        'has_credentials': lambda user_id: db.session.get(IncontrolAccount, user_id) is not None,
    }

    @application.route('/settings/incontrol', methods=['GET', 'POST'])
    @login_required
    def incontrol_settings():
        """Обработать сохранение, проверку или удаление личных настроек и паузы закрытия."""
        if request.method == 'POST' and request.form.get('action') == 'closure_checks':
            session['closure_pauses'] = {
                'user_id': current_user.get_id(),
                'services': [service for service in ('incontrol', 'yandex_mail')
                             if request.form.get(service) != '1'],
            }
            flash('Настройки закрытия заявок сохранены.', 'success')
            return redirect(url_for('incontrol_settings'))
        login = ''
        configured = False
        error = None
        preparation = None
        try:
            row = db.session.get(IncontrolAccount, current_user.id)
            configured = row is not None
            if configured:
                try:
                    login = read_credentials(current_user.id)['login']
                except PersonalAccountError as exc:
                    error = str(exc)
            if request.method == 'POST':
                action = request.form.get('action', 'save')
                if action == 'save':
                    new_login = request.form.get('incontrol_login', '').strip()
                    password = request.form.get('incontrol_password', '')
                    if not new_login or len(new_login) > 254 or len(password) > 4096:
                        error = 'Укажите корректный логин и пароль InControl.'
                    else:
                        if not password and configured:
                            previous = read_credentials(current_user.id)
                            if new_login == previous['login']:
                                password = previous['password']
                        if not password:
                            error = 'Введите пароль для этого логина.'
                        else:
                            encrypted = credential_cipher().encrypt(json.dumps(
                                {'login': new_login, 'password': password}, ensure_ascii=False
                            ).encode()).decode()
                            if row is None:
                                row = IncontrolAccount(user_id=current_user.id)
                                db.session.add(row)
                            row.credentials_encrypted = encrypted
                            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                            db.session.commit()
                            flash('Личные настройки InControl сохранены.', 'success')
                            return redirect(url_for('incontrol_settings'))
                elif action == 'check':
                    client = personal_client(current_user.id)
                    db.session.rollback()  # Не держать транзакцию во время HTTP.
                    try:
                        ensure_session(client)
                        remember_auth(current_user.id, client)
                        flash('Вход в InControl под вашей учётной записью выполнен.', 'success')
                    except Exception:
                        error = 'Войти в InControl не удалось. Проверьте личные данные и доступность сервиса. Возможно, требуется подтверждение входа.'
                    finally:
                        client.session.close()
                elif action == 'inspect':
                    park = session.get('selected_park')
                    if not park or park not in session.get('available_parks', []):
                        error = 'Выберите доступный парк.'
                    else:
                        value = request.form.get('incontrol_task_id', '').strip()
                        if not value.isdecimal() or not 0 < int(value) <= 2147483647:
                            error = 'Укажите числовой номер заявки InControl.'
                        else:
                            client = personal_client(current_user.id)
                            name = current_user.full_name
                            db.session.rollback()
                            try:
                                ensure_session(client)
                                remember_auth(current_user.id, client)
                                db.session.rollback()
                                preparation = CompletionPreparation(client).inspect(int(value), park, name)
                            except CompletionPreparationError as exc:
                                error = str(exc)
                            except Exception:
                                error = 'Не удалось проверить заявку InControl. Повторите проверку позже.'
                            finally:
                                client.session.close()
                elif action == 'delete':
                    if row is not None:
                        db.session.delete(row)
                        db.session.commit()
                    flash('Личные настройки InControl удалены.', 'success')
                    return redirect(url_for('incontrol_settings'))
                else:
                    error = 'Неизвестное действие.'
        except PersonalAccountError as exc:
            db.session.rollback()
            error = str(exc)
        except SQLAlchemyError:
            db.session.rollback()
            error = 'Настройки пока недоступны. Необходимо создать таблицу incontrol_account на сервере.'
        response = application.make_response(render_template(
            'incontrol_settings.html', incontrol_login=login, configured=configured, settings_error=error,
            preparation=preparation, paused_services=paused_closure_services()))
        response.headers['Cache-Control'] = 'no-store'
        return response

    @application.cli.command('init-incontrol-accounts')
    def init_accounts():
        """Создать таблицу личных учётных данных по отдельной команде Flask."""
        IncontrolAccount.__table__.create(bind=db.engine, checkfirst=True)
        print('Таблица incontrol_account готова.')

    return IncontrolAccount, personal_client


# --- Отклонение заявки, исполнитель и подписание документов --------------------

def service_fields(task):
    """Найти в связанной задаче поля для записи выполненных услуг."""
    return [field for field in task.get('additional_fields', [])
            if normalized_name(field.get('field_name')) in {'выполненные услуги', 'выполнение услуги'}]


class RejectionError(Exception):
    """Ошибка закрытия, которую можно показать в форме."""
    def __init__(self, message, status_code=None):
        """Сохранить сообщение о сбое закрытия и HTTP-статус, если он известен."""
        super().__init__(message)
        self.status_code = status_code


class ActSignatureRequired(RejectionError):
    """Уведомление о том, что дальнейший переход требует подписи акта."""
    pass


class RejectionFlow:
    """Общие операции закрытия и последовательность шагов для отклонения."""
    def __init__(self, client, state, save):
        """Привязать личный клиент, состояние заявки и сохранение шагов."""
        self.client, self.state, self.save = client, state, save
        self.org = PARK_TO_ORG.get(state['park'])
        if not self.org:
            raise RejectionError('Организация парка не настроена.')
        self.deadline = time.monotonic() + 90
        # Личный клиент должен иметь retries=0: повтор подписи недопустим.
        if not client.interactive:
            raise RejectionError('Для отклонения необходим личный интерактивный клиент.')

    def request(self, method, path, data=None, params=None):
        # Повтор разрешён только после явного HTTP 401: сервер отклонил
        # операцию до выполнения. Таймауты и неоднозначные ответы не повторяем.
        """Отправить запрос в организацию выбранного парка и обработать сбой или обновление сеанса."""
        for attempt in range(2):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise RejectionError('Истекло время обращения к InControl. Повторите проверку состояния.')
            response = self.client.session.request(method, API_BASE + path,
                json=data if method != 'GET' else None, params=params,
                headers={'X-Incontrol-Organization': str(self.org)}, timeout=(min(3, remaining), min(25, remaining)))
            if response.status_code == 401 and attempt == 0:
                self.client._renew()
                auth = capture_session(self.client)
                if auth:
                    self.state['auth'] = auth
                    self.save(self.state)
                continue
            auth = capture_session(self.client)
            if auth and auth != self.state.get('auth'):
                self.state['auth'] = auth
                self.save(self.state)
            if response.status_code == 401:
                raise RejectionError('Не удалось восстановить сеанс InControl. Проверьте личный логин и пароль в настройках.', status_code=401)
            break
        if not response.ok:
            if response.status_code == 403:
                logging.getLogger('app').warning('incontrol: HTTP 403 method=%s path=%s task=%s',
                                                 method, path, self.state.get('remote_id'))
            if response.status_code == 403 and method == 'PATCH' and path.startswith('/tasks/'):
                try:
                    error = response.json()
                except ValueError:
                    error = {}
                if isinstance(error, dict) and error.get('code') == 'permission_denied' and 'акт не подписан' in str(error.get('detail', '')).casefold():
                    raise ActSignatureRequired('InControl требует подписать сформированный акт перед выполнением.')
            # Не помещаем ответ API (в нём могут быть персональные данные) в логи/HTML.
            if path == '/reports/sign/' and response.status_code in (400, 403):
                raise RejectionError('Подпись не принята. Проверьте код из письма или запросите новый.')
            raise RejectionError(f'InControl отказал в операции (HTTP {response.status_code}).', status_code=response.status_code)
        return response.json() if response.content else {}

    def remember(self, **values):
        """Записать изменения шага, чтобы повтор мог продолжиться с сохранённого места."""
        self.state.update(values)
        self.save(self.state)

    def task(self, task_id):
        """Получить карточку по ID и проверить её принадлежность организации."""
        task = self.request('GET', f'/tasks/{task_id}/')
        if task.get('id') != int(task_id) or (task.get('organization_performer') or {}).get('id') != self.org:
            raise RejectionError('Номер или организация заявки не совпадает с ожидаемыми.')
        board = next((str(f.get('value') or '') for f in (task.get('serviced_object') or {}).get('additional_fields', []) if f.get('field_name') == 'Бортовой номер'), '')
        if board != self.state['board']:
            raise RejectionError('Борт заявки InControl не совпадает с локальной заявкой.')
        return task

    def identify(self):
        """Убедиться, что фамилия личного аккаунта совпадает с фамилией сотрудника."""
        me = self.request('GET', '/me/')
        if not me.get('id') or normalized_name(me.get('last_name')) != normalized_name(self.state['full_name']).split(' ')[0]:
            raise RejectionError('Фамилия личного аккаунта InControl не совпадает с пользователем сайта.')
        self.user_id = me['id']
        if self.state.get('actor') and self.state['actor'] != self.user_id:
            raise RejectionError('Личный аккаунт изменился во время подписания.')
        self.remember(actor=self.user_id)

    def phase_ids(self, task, required=None):
        """Уточнить идентификаторы допустимых этапов для типа заявки."""
        results = []
        for page in range(1, 11):
            data = self.request('GET', '/tasks/available-phases/', params={
                'id': task['id'], 'type_work_id': task['type_work']['id'], 'per_page': 100, 'page': page})
            if not isinstance(data, dict) or not isinstance(data.get('results'), list):
                raise RejectionError('Неизвестный формат этапов InControl.')
            results.extend(data['results'])
            if not data.get('next'):
                break
        else:
            raise RejectionError('Не удалось получить все этапы InControl.')
        ids = {}
        for name in (required or (['принято', 'выполнено'] if self.state.get('mode') == 'complete' else ['принято', 'отклонено'])):
            matches = [p['id'] for p in results if normalized_name(p.get('phase_name')) == name]
            if len(matches) != 1:
                raise RejectionError('Не удалось однозначно определить этапы InControl.')
            ids[name] = matches[0]
        return ids

    def documents(self):
        """Загрузить список документов основной заявки."""
        data = self.request('GET', f"/tasks/{self.state['remote_id']}/available-report-files/", params={'is_paginated': 'false'})
        if not isinstance(data, dict) or not isinstance(data.get('results'), list):
            raise RejectionError('Неизвестный формат документов InControl.')
        files = data['results']
        if any(f.get('task') != self.state['remote_id'] for f in files):
            raise RejectionError('Документ относится к другой заявке.')
        return files

    def current_document(self, files, kind):
        """Найти единственный актуальный документ нужного вида."""
        names = {'bid_signed_by_controller', 'bid_signed_by_performer', 'bid_signed_by_customer'}
        candidates = [f for f in files if f.get('upload_type') in names
                      and (f.get('report_type') or {}).get('id') in self.state['report_types'][kind]]
        if len({f['report_type']['id'] for f in candidates}) > 1:
            raise RejectionError('Найдено несколько типов документов для одного шага. Требуется ручная проверка.')
        if not candidates:
            return None
        # Старая подписанная версия не подтверждает подпись нового документа.
        return max(candidates, key=lambda f: (f.get('timestamp') or 0, f.get('id') or 0))

    def signed(self, files, kind):
        """Проверить, подписан ли актуальный документ нужного вида."""
        types = {'bid_signed_by_performer', 'bid_signed_by_customer'}
        current = self.current_document(files, kind)
        return bool(current and current.get('upload_type') in types)

    def pending_document(self, files, kind):
        """Вернуть актуальный документ, если его ещё нужно подписать."""
        types = {'bid_signed_by_controller'}
        document = self.current_document(files, kind)
        if not document or document.get('upload_type') not in types or not document.get('file'):
            raise RejectionError('Актуальный документ для подписи пока не готов. Повторите проверку.')
        return document

    def validate(self):
        """Проверить этап основной заявки и допустимость отклонения связанных задач."""
        if not self.state.get('reason', '').strip():
            raise RejectionError('Заполните причину отклонения в поле «Выполненные работы».')
        self.identify()
        main = self.task(self.state['remote_id'])
        links = []
        for field in main.get('additional_fields', []):
            if field.get('link_type') == 'task' and field.get('field_name') in {'Связанные нарушения', 'Связанные задачи'}:
                value = field.get('value') or []
                if not isinstance(value, list) or any(type(x) is not int for x in value):
                    raise RejectionError('Неизвестный формат связанных задач.')
                links.extend(value)
        links = sorted(set(links))
        if not links or len(links) > 25 or main['id'] in links:
            raise RejectionError('Связанные задачи требуют ручной проверки.')
        if self.state.get('children') and self.state['children'] != links:
            raise RejectionError('Список связанных задач изменился. Требуется новая проверка.')
        children = []
        decisions = {x['id']: x for x in self.state.get('child_decisions', [])}
        for tid in links:
            child = self.task(tid)
            if not any(x.get('linked_task') == tid and x.get('dependent_task') == main['id'] for x in child.get('custom_field_dependent_tasks', [])):
                raise RejectionError('Не подтверждена обратная связь задач.')
            if normalized_name(child['phase']['phase_name']) not in {'новое', 'назначено', 'принято', 'отклонено'}:
                raise RejectionError(f'Связанная задача {tid} уже выполнена или находится на неизвестном этапе.')
            if 'phase' in child.get('read_only_fields', []):
                raise RejectionError('Изменение этапа связанной задачи запрещено.')
            if tid in decisions:
                fields = service_fields(child)
                if len(fields) > 1:
                    raise RejectionError('Поле услуг связанной задачи определено неоднозначно.')
                if normalized_name(child['phase']['phase_name']) != 'отклонено' and fields:
                    if fields[0].get('is_disabled') or 'additional_fields' in child.get('read_only_fields', []):
                        raise RejectionError('Изменение услуг связанной задачи запрещено.')
            children.append(child)
        reason_fields = [f for f in main.get('additional_fields', []) if f.get('field_name') == 'Причина отклонения']
        if len(reason_fields) != 1 or reason_fields[0].get('is_disabled') or 'phase' in main.get('read_only_fields', []):
            raise RejectionError('Этап или причина отклонения недоступны для изменения.')
        settings = self.request('GET', '/signature-settings/').get('blanks', {})
        report_types = {'bid': settings.get('bid_report_types_ids', [])}
        if not all(report_types.values()):
            raise RejectionError('Настройки подписания документов отсутствуют.')
        self.remember(children=links, report_types=report_types, reason_field=reason_fields[0]['id'])
        return main, children, reason_fields[0]

    def assign_closing_performer(self, main):
        """Назначить закрывающего исполнителем; возможность смены проверяет InControl."""
        previous = (main.get('user_performer') or {}).get('id')
        if previous == self.user_id:
            return main
        if normalized_name((main.get('phase') or {}).get('phase_name')) not in {'новое', 'назначено', 'принято'} or main.get('stage') in ('denied', 'completed'):
            if previous is None:
                return main
            raise RejectionError('Смена исполнителя доступна только у открытой заявки.')
        if any(field in main.get('read_only_fields', []) for field in ('user_performer', 'user_performer_id')):
            raise RejectionError('InControl запрещает смену исполнителя этой заявки.')
        patch = {'user_performer_id': self.user_id}
        accepting = normalized_name((main.get('phase') or {}).get('phase_name')) != 'принято'
        if accepting:
            # На этапе «Назначено» API требует принятия вместе с исполнителем.
            patch['phase_id'] = self.phase_ids(main, ['принято'])['принято']
        self.request('PATCH', f"/tasks/{main['id']}/", patch)
        checked = self.task(main['id'])
        if ((checked.get('user_performer') or {}).get('id') != self.user_id
                or (accepting and normalized_name((checked.get('phase') or {}).get('phase_name')) != 'принято')):
            raise RejectionError('InControl не подтвердил смену исполнителя. Заявка не закрыта.')
        self.remember(performer_reassigned=True)
        return checked

    def advance(self, action='prepare', code=None):
        """Отклонить дочерние задачи, подписать заявку и сохранить причину отклонения."""
        main, children, reason_field = self.validate()
        phase = normalized_name(main['phase']['phase_name'])
        if phase not in {'новое', 'назначено', 'принято', 'отклонено'}:
            raise RejectionError('Основная заявка уже выполнена или находится на неизвестном этапе.')
        main = self.assign_closing_performer(main)
        phase = normalized_name(main['phase']['phase_name'])
        files = self.documents()
        bid_signed = self.signed(files, 'bid')
        if phase == 'отклонено' and not self.state.get('started'):
            raise RejectionError('Заявка уже отклонена другим процессом. Необходима ручная проверка.')
        if phase == 'отклонено' and (reason_field.get('value') or '') != self.state['reason']:
            raise RejectionError('Причина отклонения изменилась в InControl. Проверьте её вручную.')
        if phase == 'отклонено' and bid_signed:
            self.remember(step='done', document=None)
            return 'done'
        if not self.state.get('started'):
            if phase not in {'новое', 'назначено', 'принято'}:
                raise RejectionError('Нельзя начать отклонение на этом этапе.')
            # Отмечаем начало ДО HTTP: при неопределённом исходе сверяем состояние, а не повторяем вслепую.
            self.remember(started=True)
        decisions = {x['id']: x for x in self.state.get('child_decisions', [])}
        for child in children:
            if normalized_name(child['phase']['phase_name']) != 'отклонено':
                ids = self.phase_ids(child)
                patch = {'phase_id': ids['отклонено']}
                fields = service_fields(child) if child['id'] in decisions else []
                if fields:
                    patch['additional_fields'] = [{'id': fields[0]['id'], 'value': decisions[child['id']]['work']}]
                self.request('PATCH', f"/tasks/{child['id']}/", patch)
                checked = self.task(child['id'])
                if (normalized_name(checked['phase']['phase_name']) != 'отклонено'
                        or (fields and not any(f['id'] == fields[0]['id'] and f.get('value') == decisions[child['id']]['work'] for f in checked.get('additional_fields', [])))):
                    raise RejectionError('Отклонение или комментарий связанной задачи не подтверждены.')
        # Основную задачу нельзя закрывать до подтверждённого отклонения всех связанных.
        if not bid_signed:
            if phase == 'отклонено':
                raise RejectionError('Основная заявка отклонена без подтверждённой подписи заявки.')
            if phase != 'принято' or (main.get('user_performer') or {}).get('id') != self.user_id:
                ids = self.phase_ids(main)
                self.request('PATCH', f"/tasks/{main['id']}/", {'phase_id': ids['принято'], 'user_performer_id': self.user_id})
                main = self.task(main['id'])
                if normalized_name(main['phase']['phase_name']) != 'принято' or (main.get('user_performer') or {}).get('id') != self.user_id:
                    raise RejectionError('Принятие заявки личным исполнителем не подтверждено.')
            return self.sign_or_wait('bid', action, code)
        if phase != 'отклонено':
            ids = self.phase_ids(main)
            try:
                self.request('PATCH', f"/tasks/{main['id']}/", {'phase_id': ids['отклонено'],
                    'additional_fields': [{'id': self.state['reason_field'], 'value': self.state['reason']}]})
            except RejectionError as exc:
                if exc.status_code != 403:
                    raise
                raise RejectionError(f'InControl отказал в переводе заявки №{main["id"]} в «Отклонено» '
                                     'с сохранением причины (HTTP 403).', status_code=403) from exc
            main = self.task(main['id'])
            values = [f.get('value') for f in main['additional_fields'] if f['id'] == self.state['reason_field']]
            if normalized_name(main['phase']['phase_name']) != 'отклонено' or values != [self.state['reason']]:
                raise RejectionError('Отклонение или причина в основной заявке не подтверждены.')
        self.remember(step='done', document=None, needs_code=False)
        return 'done'

    def sign_or_wait(self, kind, action, code):
        """Подписать актуальный документ либо сообщить, какой код нужно ввести."""
        files = self.documents()
        if self.signed(files, kind):
            self.remember(step='decline', document=None)
            return 'advance'
        document = self.pending_document(files, kind)
        expected = self.state.get('document')
        if action in ('sign', 'resend') and (not expected or expected['file'] != document['file'] or expected['id'] != document['id']):
            self.remember(step=kind, document=document)
            return 'confirm'
        self.remember(step=kind, document=document)
        if action not in ('sign', 'resend'):
            if self.state.get('needs_code') and expected and expected['id'] == document['id'] and expected['file'] == document['file']:
                return 'code'
            return 'confirm'
        if not code:
            previous = self.state.get('signature_request') or {}
            if action == 'resend' and time.time() - previous.get('at', 0) < 10:
                raise RejectionError('Повторно запросить код можно через 10 секунд.')
            result = self.request('POST', '/auth/signature/', {'task_id': self.state['remote_id']})
            if not isinstance(result, dict):
                raise RejectionError('Не удалось подтвердить запрос кода: неизвестный ответ InControl.')
            code = result.get('sign_code')
            outcome = 'automatic' if code else 'email_requested'
            self.remember(signature_request={'at': time.time(), 'document_id': document['id'], 'outcome': outcome})
            logging.getLogger('app').info('incontrol-signature: task=%s document=%s outcome=%s accepted=True', self.state['remote_id'], document['id'], outcome)
            if not code:
                self.remember(needs_code=True)
                return 'code'
        self.request('POST', '/reports/sign/', {'code': str(code), 'file_path': document['file']})
        if not self.signed(self.documents(), kind):
            raise RejectionError('Подпись отправлена, но ещё не подтверждена InControl. Проверьте состояние перед повтором.')
        self.remember(step='decline', needs_code=False, document=None)
        return 'advance'


def find_open_main_tasks(client, board, park, service=None):
    """Полный поиск перед отклонением: пустой результат допустим только после всех страниц."""
    org = PARK_TO_ORG.get(park)
    if not org or not normalize_board(board):
        raise RejectionError('Нельзя проверить InControl без известного парка и борта.')
    names = ALLOWED_WORK_NAMES
    found = {}
    for page in range(1, MAX_PAGES + 1):
        params = [('search', normalize_board(board)), ('per_page', PAGE_SIZE), ('page', page)] + [('phase', x) for x in OPEN_PHASES]
        data = client._get('/tasks/', params, org)
        if not isinstance(data, dict) or not isinstance(data.get('results'), list) or 'next' not in data:
            raise RejectionError('InControl вернул некорректный результат поиска.')
        for item in data['results']:
            if not isinstance(item, dict):
                raise RejectionError('InControl вернул некорректную заявку.')
            if board_of(item) != normalize_board(board):
                continue
            phase = normalized_name((item.get('phase') or {}).get('phase_name'))
            work = (item.get('type_work') or {}).get('work_name')
            if not phase or not work:
                raise RejectionError('InControl не сообщил этап или тип заявки.')
            if phase not in {'новое', 'назначено', 'принято'} or item.get('stage') in ('denied', 'completed'):
                continue
            if work not in names:
                continue
            if type(item.get('id')) is not int:
                raise RejectionError('InControl не сообщил номер заявки.')
            if (item.get('organization_performer') or {}).get('id') != org:
                raise RejectionError('Найденная заявка относится к другой организации.')
            found[item['id']] = item
        if not data.get('next'):
            return list(found.values())
    raise RejectionError('Не удалось проверить все страницы заявок InControl.')


# --- Чтение связанных задач и проверка решений ---------------------------------

def decisions_from_json(raw):
    """Разобрать и проверить переданные формой решения по связанным задачам."""
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except (ValueError, TypeError):
        raise RejectionError('Не удалось прочитать решения по связанным задачам.') from None
    if not isinstance(values, list) or not 1 <= len(values) <= 25:
        raise RejectionError('Необходимо решение по каждой связанной задаче.')
    result=[]; seen=set()
    for value in values:
        if not isinstance(value,dict) or type(value.get('id')) is not int or value['id'] in seen or value.get('action') not in ('complete','reject'):
            raise RejectionError('Некорректное или повторное решение связанной задачи.')
        work=value.get('work')
        if not isinstance(work,str) or not work.strip() or len(work)>10000:
            raise RejectionError('Заполните выполненные работы или причину отклонения каждой задачи.')
        seen.add(value['id']);result.append({'id':value['id'],'action':value['action'],'work':work.replace('\r\n','\n').replace('\r','\n').strip()})
    return sorted(result,key=lambda x:x['id'])


def validate_decisions(plan, decisions):
    """Сверить решения с текущим составом задач и уже сохранёнными этапами."""
    if {x['id'] for x in plan} != {x['id'] for x in decisions}:
        raise RejectionError('Состав связанных задач изменился. Обновите их список.')
    by_id={x['id']:x for x in decisions}
    for item in plan:
        phase=normalized_name(item['phase']);decision=by_id[item['id']]
        if phase in ('выполнено','отклонено') and decision['action'] != ('complete' if phase=='выполнено' else 'reject'):
            raise RejectionError(f"Задача {item['id']} уже завершена с другим результатом.")


def decisions_summary(plan, decisions):
    """Собрать тексты работ и определить общий исход по связанным задачам."""
    validate_decisions(plan,decisions)
    text='\n'.join(d['work'] for d in decisions)
    mode='complete' if any(d['action']=='complete' for d in decisions) else 'reject'
    return text,mode


def validate_blanket_rejection(plan, fresh):
    """Перед общим отклонением сверить связи и отсутствие выполненных задач."""
    if not plan or {x['id'] for x in plan} != {x['id'] for x in fresh}:
        raise RejectionError('Состав связанных задач изменился. Обновите форму перед отклонением.')
    if any(normalized_name(x['phase']) == 'выполнено' for x in fresh):
        raise RejectionError('Среди связанных задач есть выполненные. Отклонение основной требует ручной сверки в InControl.')


def read_children(flow):
    """Прочитать связанные задачи, проверяя обратные ссылки, этапы и поля услуг."""
    main=flow.task(flow.state['remote_id'])
    ids=[]
    for field in main.get('additional_fields',[]):
        if field.get('link_type')=='task' and field.get('field_name') in {'Связанные нарушения','Связанные задачи'}:
            value=field.get('value') or []
            if not isinstance(value,list) or any(type(x) is not int for x in value):
                raise RejectionError('Неизвестный формат связей InControl.')
            ids.extend(value)
    ids=sorted(set(ids))
    if not ids or len(ids)>25 or main['id'] in ids:
        raise RejectionError('Связанные задачи требуют ручной проверки.')
    result=[]
    for tid in ids:
        child=flow.task(tid)
        if not any(x.get('linked_task')==tid and x.get('dependent_task')==main['id'] for x in child.get('custom_field_dependent_tasks',[])):
            raise RejectionError('Не подтверждена обратная связь связанных задач.')
        phase=(child.get('phase') or {}).get('phase_name')
        if normalized_name(phase) not in {'новое','назначено','принято','выполнено','отклонено'}:
            raise RejectionError(f'Задача {tid}: неизвестный этап.')
        name=(child.get('type_work') or {}).get('work_name') or 'Связанная задача'
        description=(child.get('task_description') or '').replace('\r\n','\n').replace('\r','\n').strip()
        fields=service_fields(child)
        result.append({'id':tid,'title':f'{name}: {description}' if description else name,'phase':phase,
                       'services':len(fields)==1,'existing_work':fields[0].get('value') or '' if len(fields)==1 else ''})
    return main,result


def plain_completed_work(value):
    """Отображение старой сводки без повторения описания связанной задачи."""
    lines=[]
    for line in (value or '').splitlines():
        for marker in (' — Выполнено: ', ' — Отклонено: '):
            if marker in line:
                line=line.split(marker,1)[1]
                break
        lines.append(line)
    return '\n'.join(lines)


# --- Выполнение связанных задач и основной заявки ------------------------------

class FulfillmentFlow(RejectionFlow):
    """Выполнение основной и связанных задач с подписью заявки и акта."""
    def validate(self):
        """Сверить этапы, связи и возможность записать услуги перед выполнением."""
        if not self.state.get('reason','').strip():
            raise RejectionError('Заполните выполненные работы.')
        self.identify()
        main=self.task(self.state['remote_id'])
        links=[]
        for field in main.get('additional_fields',[]):
            if field.get('link_type')=='task' and field.get('field_name') in {'Связанные нарушения','Связанные задачи'}:
                value=field.get('value') or []
                if not isinstance(value,list) or any(type(x) is not int for x in value):raise RejectionError('Неизвестный формат связей.')
                links.extend(value)
        links=sorted(set(links))
        if not links or len(links)>25 or main['id'] in links:raise RejectionError('Связанные задачи требуют проверки.')
        if self.state.get('children') and self.state['children']!=links:raise RejectionError('Связи изменились.')
        decisions={x['id']:x for x in self.state.get('child_decisions',[])}
        if decisions and set(decisions)!=set(links):raise RejectionError('Состав решений по связанным задачам не совпадает с InControl.')
        children=[]
        projected_services=[]
        service_main=is_service_request(main)
        for tid in links:
            child=self.task(tid)
            if not any(x.get('linked_task')==tid and x.get('dependent_task')==main['id'] for x in child.get('custom_field_dependent_tasks',[])):
                raise RejectionError('Не подтверждена обратная связь задач.')
            phase=normalized_name(child['phase']['phase_name'])
            decision=decisions.get(tid,{'action':'complete','work':self.state['reason']})
            target='выполнено' if decision['action']=='complete' else 'отклонено'
            if phase not in {'новое','назначено','принято',target}:raise RejectionError('Связанная задача отклонена или имеет неизвестный этап.')
            fields=service_fields(child)
            phase_only=not fields and (decision['action']=='reject' or service_main)
            if not phase_only and len(fields)!=1:
                raise RejectionError(f'Задача {tid}: поле выполненных услуг не определено.')
            field=fields[0] if fields else None
            if phase=='выполнено' and field is not None and field.get('value')!=decision['work']:
                raise RejectionError('Связанная задача уже выполнена с другим текстом услуг. Требуется ручная проверка.')
            if phase!=target:
                readonly=child.get('read_only_fields',[])
                if 'phase' in readonly or (field is not None and (field.get('is_disabled') or 'additional_fields' in readonly)):
                    raise RejectionError('Изменение услуг или этапа недоступно.')
            if field is not None:
                projected_services.append(field.get('value') if phase == target else decision['work'])
            children.append((child,field['id'] if field is not None else None))
        if normalized_name(main['phase']['phase_name']) != 'выполнено':
            self.comment_plan(main, projected_services)
        child_fields={str(child['id']):field_id for child,field_id in children}
        if self.state.get('child_services_fields') is not None and self.state['child_services_fields']!=child_fields:
            raise RejectionError('Поля связанных задач изменились. Требуется ручная проверка.')
        settings=self.request('GET','/signature-settings/').get('blanks',{})
        types={'bid':settings.get('bid_report_types_ids',[]),'act':settings.get('akt_report_types_ids',[])}
        if not all(types.values()):raise RejectionError('Настройки подписания отсутствуют.')
        self.remember(children=links,child_services_fields=child_fields,report_types=types)
        return main,children

    @staticmethod
    def comment_plan(main, values):
        """Определить, куда записать сводку услуг основной заявки."""
        texts=[]
        for value in values:
            if value is None:
                continue
            if not isinstance(value, str):
                raise RejectionError('Неизвестный формат текста выполненных услуг.')
            if value.strip():
                texts.append(value.strip())
        if not texts:
            return None
        text=texts[0] if len(texts)==1 else '\n'.join('- '+value for value in texts)
        fields=[field for field in main.get('additional_fields', [])
                if normalized_name(field.get('field_name')) == 'комментарий']
        if not fields and normalized_name((main.get('type_work') or {}).get('work_name')) == 'заявка на ремонт с линии':
            if 'comments' in main.get('read_only_fields', []):
                raise RejectionError('InControl запрещает добавление комментария основной заявки.')
            return {'destination':'thread', 'field_id':None, 'text':text}
        if len(fields)!=1 or type(fields[0].get('id')) is not int:
            raise RejectionError('Поле «Комментарий» основной заявки не определено однозначно.')
        field=fields[0]
        current=field.get('value')
        if current is None:
            current=''
        if not isinstance(current, str):
            raise RejectionError('Неизвестный формат комментария основной заявки.')
        # Сохраняем ручной комментарий и не добавляем услуги повторно после таймаута.
        present=('\n'+text+'\n') in ('\n'+current+'\n')
        if present:
            target=current
        elif current.strip():
            target=current.rstrip()+'\n'+text
        else:
            target=text
        if target!=current and (field.get('is_disabled') or 'additional_fields' in main.get('read_only_fields', [])):
            raise RejectionError('InControl запрещает запись услуг в комментарий основной заявки.')
        return {'field_id':field['id'], 'text':text, 'value':target, 'current':current}

    def find_services_thread_comment(self, task_id, text):
        """Найти уже добавленную сводку во вкладке комментариев."""
        for page in range(1, 21):
            data=self.request('GET',f'/tasks/{task_id}/comments/',params={'per_page':100,'page':page})
            if not isinstance(data,dict) or not isinstance(data.get('results'),list) or 'next' not in data:
                raise RejectionError('InControl вернул некорректный список комментариев.')
            for item in data['results']:
                if not isinstance(item,dict) or type(item.get('id')) is not int or not isinstance(item.get('comment'),str):
                    raise RejectionError('InControl вернул некорректный комментарий.')
                related=item.get('task',task_id)
                related=related.get('id') if isinstance(related,dict) else related
                if related!=task_id:
                    raise RejectionError('Комментарий относится к другой заявке InControl.')
                if item['comment']==text:
                    return item
            if not data['next']:
                return None
        raise RejectionError('Не удалось проверить все комментарии основной заявки.')

    def sync_services_thread_comment(self, main, text):
        """Обновить или добавить сводку во вкладке комментариев."""
        found=self.find_services_thread_comment(main['id'],text)
        if found:
            if self.state.get('services_thread_comment_id')!=found['id']:
                self.remember(services_thread_comment_id=found['id'])
            return main,False
        if self.state.get('services_thread_post_attempted'):
            raise RejectionError('Комментарий отправлялся, но его сохранение пока не подтверждено. Повторите проверку состояния.')
        # POST не идемпотентен: отметка сохраняется до сети, после таймаута
        # повторяем только чтение, пока не подтверждена судьба первой отправки.
        self.remember(services_thread_post_attempted=True)
        try:
            self.request('POST','/tasks-comments/',{'task':main['id'],'comment':text})
        except RejectionError as exc:
            if exc.status_code in (400,401,403,404,405,413,415,422):
                self.remember(services_thread_post_attempted=False)
            raise
        found=self.find_services_thread_comment(main['id'],text)
        if not found:
            raise RejectionError('InControl пока не подтвердил добавление комментария. Повторите проверку состояния.')
        self.remember(services_thread_comment_id=found['id'])
        return main,True

    def sync_services_comment(self, main, children):
        """Собрать услуги из дочерних задач и записать их в основную."""
        values=[]
        for child in children:
            fields=service_fields(child)
            if len(fields)>1:
                raise RejectionError('Поле услуг связанной задачи определено неоднозначно.')
            if fields:
                values.append(fields[0].get('value'))
        if values:
            # За время обработки дочерних задач комментарий могли дополнить вручную.
            main=self.task(main['id'])
            if (normalized_name(main['phase']['phase_name']) not in {'новое','назначено','принято'}
                    or main.get('stage') in ('denied','completed')):
                raise RejectionError('Основная заявка уже закрыта. Комментарий не изменён.')
        plan=self.comment_plan(main, values)
        if plan is None:
            return main,False
        proof={'field_id':plan['field_id'], 'text_hash':hashlib.sha256(plan['text'].encode('utf-8')).hexdigest()}
        if self.state.get('services_comment') not in (None,proof):
            raise RejectionError('Текст услуг или поле комментария изменились. Требуется ручная проверка.')
        if self.state.get('services_comment') is None:
            self.remember(services_comment=proof)
        if plan.get('destination')=='thread':
            return self.sync_services_thread_comment(main,plan['text'])
        if plan['value']==plan['current']:
            return main,False
        self.request('PATCH',f"/tasks/{main['id']}/",{'additional_fields':[
            {'id':plan['field_id'], 'value':plan['value']}]})
        checked=self.task(main['id'])
        fields=[field for field in checked.get('additional_fields', []) if field.get('id')==plan['field_id']]
        if len(fields)!=1 or fields[0].get('value')!=plan['value']:
            raise RejectionError('InControl не подтвердил сохранение услуг в комментарии основной заявки.')
        return checked,True

    def current_document(self,files,kind):
        """Выбрать актуальную заявку на ремонт или акт нужного этапа."""
        names={'bid':{'bid_signed_by_controller','bid_signed_by_performer','bid_signed_by_customer'},
               'act':{'akt_to_sign','akt_signed_by_performer','akt_signed_by_controller','akt_signed_by_author'}}[kind]
        baseline=self.state.get('act_baseline',[]) if kind=='act' else []
        found=[f for f in files if f.get('upload_type') in names and (f.get('report_type') or {}).get('id') in self.state['report_types'][kind]
               and not any(x['id']==f['id'] and x['file']==f['file'] for x in baseline)]
        if len({f['report_type']['id'] for f in found})>1:raise RejectionError('Найдено несколько типов документов для одного шага.')
        return max(found,key=lambda f:(f.get('timestamp') or 0,f.get('id') or 0)) if found else None

    def signed(self,files,kind):
        """Проверить подпись актуальной заявки или акта после смены этапа."""
        d=self.current_document(files,kind)
        return bool(d and d.get('upload_type') in {'bid_signed_by_performer','bid_signed_by_customer','akt_signed_by_performer','akt_signed_by_controller','akt_signed_by_author'})

    def pending_document(self,files,kind):
        """Выбрать документ, который ещё предстоит подписать."""
        d=self.current_document(files,kind)
        if not d or d.get('upload_type') not in {'bid_signed_by_controller','akt_to_sign'} or not d.get('file'):
            raise RejectionError('Актуальный документ для подписи пока не готов. Повторите проверку.')
        return d

    def advance(self,action='prepare',code=None):
        """Записать выполненные услуги, перевести этапы и подписать заявку и акт."""
        main,children=self.validate()
        phase=normalized_name(main['phase']['phase_name'])
        if phase not in {'новое','назначено','принято','выполнено'}:raise RejectionError('Основная заявка отклонена или имеет неизвестный этап.')
        main=self.assign_closing_performer(main)
        phase=normalized_name(main['phase']['phase_name'])
        files=self.documents()
        if self.state.get('fulfillment_protocol') != 2:
            # Ранняя версия ошибочно включала новый неподписанный акт в baseline.
            self.remember(act_baseline=[],fulfillment_protocol=2)
        if not self.state.get('started'):
            self.remember(act_baseline=[{'id':f['id'],'file':f['file']} for f in files
                if f.get('upload_type') in {'akt_signed_by_performer','akt_signed_by_controller','akt_signed_by_author'}])
        bid_signed=self.signed(files,'bid')
        if phase=='выполнено' and not self.state.get('started'):raise RejectionError('Заявка уже выполнена другим процессом.')
        if phase=='выполнено' and not bid_signed:raise RejectionError('Не подтверждена подпись заявки на ремонт.')
        if phase=='выполнено' and bid_signed and self.signed(files,'act'):
            self.remember(step='done',document=None,needs_code=False);return 'done'
        self.remember(started=True)
        decisions={x['id']:x for x in self.state.get('child_decisions',[])}
        confirmed_children=[]
        for child,field_id in children:
            decision=decisions.get(child['id'],{'action':'complete','work':self.state['reason']})
            target='выполнено' if decision['action']=='complete' else 'отклонено'
            if normalized_name(child['phase']['phase_name'])!=target:
                ids=self.phase_ids(child,[target])
                patch={'phase_id':ids[target]}
                if field_id is not None:
                    patch['additional_fields']=[{'id':field_id,'value':decision['work']}]
                self.request('PATCH',f"/tasks/{child['id']}/",patch)
                checked=self.task(child['id'])
                if normalized_name(checked['phase']['phase_name'])!=target or (field_id is not None and not any(f['id']==field_id and f.get('value')==decision['work'] for f in checked.get('additional_fields',[]))):
                    raise RejectionError('Выполнение или услуги связанной задачи не подтверждены.')
                child=checked
            confirmed_children.append(child)
        if phase!='выполнено':
            main,comment_changed=self.sync_services_comment(main,confirmed_children)
            if comment_changed and bid_signed:
                bid_signed=self.signed(self.documents(),'bid')
        if not bid_signed:
            if phase!='принято' or (main.get('user_performer') or {}).get('id')!=self.user_id:
                ids=self.phase_ids(main)
                self.request('PATCH',f"/tasks/{main['id']}/",{'phase_id':ids['принято'],'user_performer_id':self.user_id})
                checked=self.task(main['id'])
                if normalized_name(checked['phase']['phase_name'])!='принято' or (checked.get('user_performer') or {}).get('id')!=self.user_id:
                    raise RejectionError('Принятие личным исполнителем не подтверждено.')
            return self.sign_or_wait('bid',action,code)
        # Сценарий InControl запрещает выполнение основной задачи до подписи акта.
        act_files=self.documents()
        if not self.signed(act_files,'act'):
            if self.current_document(act_files,'act') is None and phase!='выполнено' and not self.state.get('act_creation_attempted'):
                # Некоторые сценарии создают акт только при попытке выполнения,
                # но возвращают 403 до его подписи. Остальные ошибки не игнорируем.
                self.remember(act_creation_attempted=True)
                ids=self.phase_ids(main)
                try:
                    self.request('PATCH',f"/tasks/{main['id']}/",{'phase_id':ids['выполнено']})
                except ActSignatureRequired:
                    pass
            return self.sign_or_wait('act',action,code)
        if phase!='выполнено':
            ids=self.phase_ids(main)
            self.request('PATCH',f"/tasks/{main['id']}/",{'phase_id':ids['выполнено']})
            if normalized_name(self.task(main['id'])['phase']['phase_name'])!='выполнено':raise RejectionError('Выполнение основной заявки не подтверждено.')
        self.remember(step='done',document=None,needs_code=False)
        return 'done'


# --- Фоновая проверка перед локальным закрытием --------------------------------

class CompletionChecks:
    """Короткие HTTP-запросы формы и однократные результаты фоновой проверки."""
    WAIT_SECONDS = 45
    RESULT_SECONDS = 60
    OUTAGE_SECONDS = 900
    TIMEOUT_MESSAGE = 'Проверка InControl не завершилась вовремя. Заявка не закрыта. Повторите проверку.'

    def __init__(self, application, db, Task, check_board, enabled):
        """Подключить таблицу результатов, маршруты и ограниченный пул проверок."""
        self.application = application
        self.db = db
        self.Task = Task
        self.check_board = check_board
        self.enabled = enabled
        # Потоки создаются только при submit, после запуска Passenger-процесса.
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='completion-check')
        self.slots = threading.BoundedSemaphore(4)

        class CompletionCheck(db.Model):
            """Сохранённая проверка, доступная всем процессам веб-сервера."""
            __tablename__ = 'completion_check'
            __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
            id = db.Column(db.String(32), primary_key=True)
            task_id = db.Column(db.Integer, nullable=False, index=True)
            user_id = db.Column(db.Integer, nullable=False, index=True)
            fingerprint = db.Column(db.String(64), nullable=False)
            state = db.Column(db.String(16), nullable=False, default='pending')
            message = db.Column(db.String(500), nullable=True)
            created_at = db.Column(db.DateTime, nullable=False, index=True)
            finished_at = db.Column(db.DateTime, nullable=True)

        self.model = CompletionCheck
        application.add_url_rule(
            '/complete_task/<int:task_id>/incontrol-check',
            'start_completion_check', login_required(self.start), methods=['POST'],
        )
        application.add_url_rule(
            '/complete_task/<int:task_id>/incontrol-check/<check_id>',
            'completion_check_status', login_required(self.status), methods=['GET'],
        )

        @application.cli.command('init-completion-checks')
        def init_table():
            """Создать таблицу результатов перед обновлением сайта."""
            CompletionCheck.__table__.create(bind=db.engine, checkfirst=True)
            print('Таблица completion_check готова.')

    def required(self, task):
        """Определить, нужна ли проверка InControl для заявки этого парка."""
        return self.enabled() and task.park in ('1 парк', '14 парк', 'Красная Пахра', '18 парк') and not self.paused(task)

    @staticmethod
    def paused(task):
        """Проверить, отключил ли пользователь InControl для данной заявки."""
        return 'incontrol' in paused_closure_services() and (task.park in PARK_TO_ORG or bool(task.incontrol_task_id))

    @staticmethod
    def fingerprint(task):
        """Получить отпечаток полей заявки, влияющих на проверку."""
        data = [task.id, task.board_number, task.park, task.tag, task.status, task.id_to]
        return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()

    def proof_fingerprint(self, task):
        # Не принимать старые результаты поиска, который ориентировался только на tag.
        """Связать результат проверки с версией правил поиска."""
        data = [self.fingerprint(task), 'board-main-v3']
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()

    def _task_for_user(self, task_id, lock=False):
        """Получить заявку доступного парка и при необходимости заблокировать строку."""
        query = self.Task.query.filter_by(id=task_id)
        if lock:
            query = query.populate_existing().with_for_update()
        task = query.first_or_404()
        if task.park not in session.get('available_parks', []) and current_user.username != 'vasilev':
            abort(403)
        return task

    def _expired(self, job, now):
        """Проверить срок ожидания или действия сохранённого результата."""
        if job.state == 'pending':
            return now >= job.created_at + timedelta(seconds=self.WAIT_SECONDS)
        if job.state == 'unavailable':
            return not job.finished_at or now >= job.finished_at + timedelta(seconds=self.OUTAGE_SECONDS)
        if job.state == 'ready':
            return not job.finished_at or now >= job.finished_at + timedelta(seconds=self.RESULT_SECONDS)
        return False

    def start(self, task_id):
        """Создать либо переиспользовать фоновую проверку перед закрытием."""
        try:
            task = self._task_for_user(task_id, lock=True)
            if task.status != 'in_repair':
                return jsonify(state='error', message='Заявка уже закрыта. Обновите страницу.'), 409
            if not self.required(task):
                return jsonify(state='ready', check_id='')
            now = utcnow()
            fingerprint = self.proof_fingerprint(task)
            # Повтор HTTP-запроса не создаёт ещё одну активную проверку.
            job = self.model.query.filter_by(
                task_id=task.id, user_id=current_user.id,
                fingerprint=fingerprint, state='pending',
            ).filter(self.model.created_at > now - timedelta(seconds=self.WAIT_SECONDS)).with_for_update().first()
            if job is None:
                # Результаты одноразовые и не нужны дольше суток.
                self.model.query.filter(self.model.created_at < now - timedelta(days=1)).delete(synchronize_session=False)
                job = self.model(
                    id=uuid.uuid4().hex, task_id=task.id, user_id=current_user.id,
                    fingerprint=fingerprint, state='pending', created_at=now,
                )
                self.db.session.add(job)
                self.db.session.commit()
                check_id = job.id
                if not self._submit(check_id):
                    job.state = 'error'
                    job.message = 'Все проверки заняты. Заявка не закрыта. Повторите через несколько секунд.'
                    self.db.session.commit()
                    return jsonify(state='error', message=job.message), 503
            else:
                check_id = job.id
                self.db.session.commit()
            return jsonify(
                state='pending', check_id=check_id,
                status_url=url_for('completion_check_status', task_id=task_id, check_id=check_id),
            ), 202
        except SQLAlchemyError:
            self.db.session.rollback()
            self.application.logger.exception('completion-check: не удалось начать проверку task_id=%s', task_id)
            return jsonify(state='error', message='Не удалось начать проверку. Заявка не закрыта. Повторите позже.'), 503

    def status(self, task_id, check_id):
        """Отдать форме результат принадлежащей сотруднику проверки."""
        try:
            task = self._task_for_user(task_id)
            job = self.model.query.filter_by(id=check_id, task_id=task.id, user_id=current_user.id).first_or_404()
            if self._expired(job, utcnow()):
                return jsonify(state='error', message=self.TIMEOUT_MESSAGE)
            if job.fingerprint != self.proof_fingerprint(task):
                return jsonify(state='error', message='Заявка изменилась. Обновите страницу и повторите проверку.')
            response = jsonify(state=job.state, message=job.message, check_id=job.id)
            response.headers['Cache-Control'] = 'no-store'
            return response
        except SQLAlchemyError:
            self.db.session.rollback()
            return jsonify(state='error', message='Не удалось получить результат. Заявка не закрыта. Повторите проверку.'), 503

    def _submit(self, check_id):
        """Поставить проверку в пул, если есть свободный слот."""
        if not self.slots.acquire(blocking=False):
            return False
        try:
            self.executor.submit(self._run_with_slot, check_id)
        except RuntimeError:
            self.slots.release()
            return False
        return True

    def _run_with_slot(self, check_id):
        """Выполнить проверку и освободить слот пула."""
        try:
            self._run(check_id)
        finally:
            self.slots.release()

    def _run(self, check_id):
        """Проверить InControl в фоне и сохранить готовность, блокировку или сбой."""
        with self.application.app_context():
            try:
                job = self.db.session.get(self.model, check_id)
                if job is None or job.state != 'pending' or self._expired(job, utcnow()):
                    return
                task = self.db.session.get(self.Task, job.task_id)
                if task is None or job.fingerprint != self.proof_fingerprint(task):
                    state, message = 'error', 'Заявка изменилась. Обновите страницу.'
                else:
                    board, park = task.board_number, task.park
                    # Не держим соединение/транзакцию MySQL во время внешнего запроса.
                    self.db.session.remove()
                    required_names = set(ALLOWED_WORK_NAMES)
                    work_name = self.check_board(board, park, required_names)
                    state = 'blocked' if work_name else 'ready'
                    message = (f'Нельзя закрыть заявку: в InControl ещё открыта «{work_name}».' if work_name else None)
                self._save_result(check_id, state, message)
            except Exception as exc:
                self.db.session.rollback()
                self.application.logger.exception('completion-check: ошибка проверки id=%s', check_id)
                try:
                    state = 'unavailable' if is_incontrol_unavailable(exc) else 'error'
                    message = ('InControl недоступен. Можно закрыть заявку в Сетранс и отписать её позже.'
                               if state == 'unavailable' else 'InControl вернул ошибку. Заявка не закрыта. Повторите проверку.')
                    self._save_result(check_id, state, message)
                except SQLAlchemyError:
                    self.db.session.rollback()
                    self.application.logger.exception('completion-check: не удалось записать ошибку id=%s', check_id)
            finally:
                self.db.session.remove()

    def _save_result(self, check_id, state, message):
        # Поздний ответ не оживляет истёкшую проверку.
        """Сохранить итог проверки, пока она ещё ожидает ответа."""
        now = utcnow()
        self.model.query.filter_by(id=check_id, state='pending').filter(
            self.model.created_at > now - timedelta(seconds=self.WAIT_SECONDS)
        ).update(dict(state=state, message=message, finished_at=now), synchronize_session=False)
        self.db.session.commit()

    def consume_outage(self, task, check_id):
        """Принять свежий результат о недоступности для отложенной отписки."""
        job = self.model.query.filter_by(id=check_id, task_id=task.id, user_id=current_user.id).populate_existing().with_for_update().first()
        if (job is None or job.state != 'unavailable' or self._expired(job, utcnow())
                or job.fingerprint != self.proof_fingerprint(task)):
            raise RejectionError('Недоступность InControl не подтверждена или проверка устарела. Повторите проверку.')
        job.state = 'consumed'

    def consume(self, task, check_id):
        """Вызывается под блокировкой Task и в транзакции её закрытия."""
        if not self.required(task):
            if task.park in ('1 парк', '14 парк', 'Красная Пахра', '18 парк'):
                return 'Проверка InControl выключена. Проверку пропустить нельзя. Включите интеграцию и повторите закрытие.'
            return None
        job = self.model.query.filter_by(
            id=check_id, task_id=task.id, user_id=current_user.id,
        ).populate_existing().with_for_update().first() if check_id else None
        if job is None or job.state != 'ready':
            return 'Перед закрытием дождитесь успешной проверки InControl. Повторите проверку.'
        if self._expired(job, utcnow()) or job.fingerprint != self.proof_fingerprint(task):
            return 'Результат проверки устарел или заявка изменилась. Повторите проверку.'
        job.state = 'consumed'
        return None


# --- Фоновое закрытие под личным аккаунтом -------------------------------------

FORM_FIELDS = ('completed_work', 'mileage', 'work_in', 'seasonal_service', 'evacuation_photos_confirmed')


def form_values(form):
    """Нормализовать значения формы для сохранения и повторной сверки."""
    values = {key: form.get(key, '').replace('\r\n','\n').replace('\r','\n').strip() for key in FORM_FIELDS}
    if form.get('main_outcome'):
        if form['main_outcome'] not in ('complete', 'reject'):
            raise RejectionError('Выберите результат основной заявки.')
        values['main_outcome'] = form['main_outcome']
    if form.get('child_decisions'):
        values['child_decisions'] = json.dumps(decisions_from_json(form['child_decisions']), ensure_ascii=False, sort_keys=True)
    return values


def digest(values):
    """Посчитать стабильный отпечаток значений формы."""
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def drive_flow(flow, action='prepare', code=None):
    """Продолжить сценарий автоматически до завершения или запроса кода."""
    result = flow.advance(action, code)
    for _ in range(8):
        if result in ('done', 'code'):
            return result
        if result == 'confirm':
            result = flow.advance('sign')
        elif result == 'advance':
            result = flow.advance('prepare')
        else:
            break
    raise RejectionError('Состояние документов изменилось. Повторите проверку.')


class IncontrolRejections:
    """Фоновые процессы отклонения и выполнения под личным аккаунтом."""
    def __init__(self, app, db, Task, checks, personal_client, enabled):
        """Зарегистрировать модель, маршруты и пул сохранённых процессов закрытия."""
        self.app, self.db, self.Task, self.checks = app, db, Task, checks
        self.personal_client, self.enabled = personal_client, enabled
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='incontrol-reject')
        self.slots = threading.BoundedSemaphore(2)

        class Run(db.Model):
            """Зашифрованный процесс закрытия одной заявки и его последнее состояние."""
            __tablename__ = 'incontrol_rejection_run'
            __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
            id = db.Column(db.String(32), primary_key=True)
            task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False, unique=True)
            remote_id = db.Column(db.BigInteger, nullable=True, unique=True)
            user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
            state = db.Column(db.String(20), nullable=False)
            payload_encrypted = db.Column(db.Text, nullable=False)
            message = db.Column(db.String(500))
            updated_at = db.Column(db.DateTime, nullable=False)

        self.model = Run
        app.add_url_rule('/incontrol/rejections/<run_id>', 'incontrol_rejection_page', login_required(self.page), methods=['GET'])
        app.add_url_rule('/incontrol/rejections/<run_id>/status', 'incontrol_rejection_status', login_required(self.status), methods=['GET'])
        app.add_url_rule('/incontrol/rejections/<run_id>/action', 'incontrol_rejection_action', login_required(self.action), methods=['POST'])

        app.add_url_rule('/complete_task/<int:task_id>/incontrol-plan', 'start_incontrol_plan', login_required(self.start_plan), methods=['POST'])

        @app.cli.command('init-incontrol-rejections')
        def init_table():
            """Создать таблицу процессов закрытия отдельной командой Flask."""
            Run.__table__.create(bind=db.engine, checkfirst=True)
            print('Таблица incontrol_rejection_run готова.')

        @app.context_processor
        def rejection_options():
            """Передать шаблону доступность личного закрытия для заявки."""
            return {'incontrol_rejection_enabled': self.enabled(),
                    'uses_inline_incontrol': lambda task: self.park_enabled(task.park) and self.has_personal_account()}

    def pack(self, value):
        """Проверить объём и зашифровать сохранённое состояние процесса."""
        raw = json.dumps(value, ensure_ascii=False).encode()
        if len(raw) > 45000:
            raise RejectionError('Слишком большой объём текста связанных задач. Сократите описания работ.')
        return credential_cipher().encrypt(raw).decode()

    def unpack(self, row):
        """Расшифровать сохранённое состояние процесса."""
        return json.loads(credential_cipher().decrypt(row.payload_encrypted.encode()))

    def owned(self, run_id, lock=False):
        """Найти личный процесс и проверить доступ сотрудника к заявке."""
        query = self.model.query.filter_by(id=run_id, user_id=current_user.id)
        if lock:
            query = query.with_for_update()
        row = query.first_or_404()
        task = self.checks._task_for_user(row.task_id)
        return row, task

    def has_personal_account(self):
        """Проверить наличие личных учётных данных текущего сотрудника."""
        return current_user.is_authenticated and self.app.extensions['incontrol_accounts']['has_credentials'](current_user.id)

    @staticmethod
    def park_enabled(park):
        """Проверить, включено ли личное закрытие для указанного парка."""
        parks = os.getenv('INCONTROL_CLOSURE_PARKS', '14 парк').split(',')
        return park in PARK_TO_ORG and park in {name.strip() for name in parks}

    def applies(self, task, form):
        """Выбрать личное закрытие при наличии аккаунта или начатого процесса."""
        return self.park_enabled(task.park) and bool(form.get('incontrol_rejection_run') or self.has_personal_account())

    def start_plan(self, task_id):
        """Начать чтение основных и связанных задач для формы закрытия."""
        task = self.checks._task_for_user(task_id, lock=True)
        if task.status != 'in_repair':
            abort(409)
        try:
            return self.start(task, request.form, planning=True)
        except (RejectionError, PersonalAccountError) as exc:
            self.db.session.rollback()
            return jsonify(message=str(exc)), 409

    def start(self, task, form, planning=False):
        """Восстановить или создать процесс и поставить его следующий шаг в очередь."""
        if not self.park_enabled(task.park):
            raise RejectionError('Автоматическая отписка в InControl выключена для этого парка.')
        if self.checks.paused(task):
            raise RejectionError('Проверка InControl при закрытии приостановлена. Обновите форму закрытия.')
        if not self.enabled():
            raise RejectionError('Закрытие не выполнено: интеграция InControl выключена. Включите INCONTROL_REJECTION_ENABLED=1 и перезапустите приложение. Проверку пропустить нельзя.')
        self.personal_client(current_user.id).session.close()
        hint = int(task.incontrol_task_id) if task.incontrol_task_id else None
        values = form_values(form)
        mode = 'reject' if form.get('action') == 'reject' else 'complete'
        explicit_mode = values.get('main_outcome')
        if explicit_mode and explicit_mode != mode:
            raise RejectionError('Результат основной заявки не совпадает с выбранным действием.')
        row = self.model.query.filter_by(task_id=task.id).with_for_update().first()
        old = self.unpack(row) if row else {}
        if row and row.user_id != current_user.id:
            raise RejectionError('Закрытие этой заявки уже начато другим пользователем.')
        if row and planning and (old.get('started') or (row.state == 'running' and not old.get('planning'))):
            response = jsonify(resume_url=url_for('complete_task', task_id=task.id, rejection_run=row.id))
            response.headers['Cache-Control'] = 'no-store'
            return response, 202
        if row and row.state == 'running':
            return self.respond(row.id)
        decisions = decisions_from_json(values.get('child_decisions'))
        blanket = explicit_mode == 'reject' and not planning
        if blanket and decisions:
            raise RejectionError('Для отклонения основной заявки укажите одну общую причину.')
        if blanket and not values['completed_work']:
            raise RejectionError('Укажите причину отклонения основной заявки.')
        if decisions:
            # Проверяем состав и решения, без сравнения сводного текста.
            # Результат основной заявки выбирает приёмщик.
            decisions_summary(old.get('child_plan', []), decisions)
        elif not planning and not blanket and old.get('planning') and old.get('child_plan'):
            raise RejectionError('Заполните решение по каждой связанной задаче.')
        if row and old.get('started'):
            if old['form_hash'] != digest(values) or old.get('mode', 'reject') != mode:
                raise RejectionError('Закрытие уже начато с другими данными. Продолжите прежний процесс из формы закрытия.')
            return self.respond(row.id)
        payload = {'mode':mode, 'remote_id':None, 'hint_id':hint, 'lookup_scope':'all-main-v1',
                   'board':task.board_number, 'park':task.park, 'full_name':current_user.full_name,
                   'reason':values['completed_work'], 'form':values, 'form_hash':digest(values),
                   'fingerprint':self.checks.fingerprint(task), 'planning':planning, 'blanket_reject':blanket}
        if old.get('fingerprint') == payload['fingerprint']:
            for key in ('confirmed_remote_id', 'child_plan', 'candidates', 'unrelated_remote_ids'):
                if key in old: payload[key] = old[key]
        if decisions:
            payload['child_decisions'] = decisions
        if row:
            row.remote_id=None; row.payload_encrypted=self.pack(payload); row.state='new'; row.message=None
        else:
            row=self.model(id=uuid.uuid4().hex,task_id=task.id,user_id=current_user.id,remote_id=None,
                           state='new',payload_encrypted=self.pack(payload),updated_at=utcnow())
            self.db.session.add(row)
        try:
            self.db.session.commit()
        except IntegrityError:
            self.db.session.rollback()
            raise RejectionError('Процесс уже запущен. Обновите страницу и продолжите его.') from None
        self.submit(row, 'inspect' if planning else 'prepare')
        return self.respond(row.id)

    def respond(self, run_id):
        """Вернуть форме адреса страницы, состояния и действий процесса."""
        if request.accept_mimetypes.best == 'application/json':
            response = jsonify(run_id=run_id, status_url=url_for('incontrol_rejection_status', run_id=run_id),
                               action_url=url_for('incontrol_rejection_action', run_id=run_id))
            response.headers['Cache-Control'] = 'no-store'
            return response, 202
        row, task = self.owned(run_id)
        return redirect(url_for('complete_task', task_id=task.id, rejection_run=run_id))

    def submit(self, row, action, code=None):
        """Запустить шаг в ограниченном пуле и записать его состояние."""
        if not self.slots.acquire(blocking=False):
            raise RejectionError('Все проверки заняты. Повторите через несколько секунд.')
        row.state = 'running'
        row.message = None
        row.updated_at = utcnow()
        run_id = row.id
        self.db.session.commit()
        try:
            self.pool.submit(self.worker, run_id, action, code)
        except Exception:
            self.slots.release()
            row.state = 'error'
            row.message = 'Не удалось запустить обработку. Повторите проверку состояния.'
            self.db.session.commit()
            raise

    def worker(self, run_id, action, code):
        """Выполнить очередной шаг под личным сеансом и сохранить результат."""
        with self.app.app_context():
            client = None
            try:
                row = self.db.session.get(self.model, run_id)
                payload = self.unpack(row)
                task = self.db.session.get(self.Task, row.task_id)
                if not self.park_enabled(task.park):
                    raise RejectionError('Автоматическая отписка в InControl выключена для этого парка.')
                if task.status != 'in_repair' or self.checks.fingerprint(task) != payload['fingerprint']:
                    raise RejectionError('Локальная заявка изменилась или уже закрыта. Требуется ручная проверка.')
                scheduled_service = bool(task.id_to or task.tag == 'to')
                if not task.incontrol_task_id and not payload.get('confirmed_remote_id'):
                    # Старый план мог выбрать единственную заявку сам, до подтверждения приёмщиком.
                    payload.update(hint_id=None, remote_id=None, selected_id=None)
                user_id = row.user_id
                client = self.personal_client(user_id)
                client.auth_context = f'run:{run_id}'
                self.db.session.rollback()
                # Личные настройки содержат последний сеанс, в том числе после других подписей.
                # Снимок старой заявки используется только при отсутствии актуального JWT.
                authorization = client.session.headers.get('Authorization')
                if payload.get('auth') and not (isinstance(authorization, str) and authorization.startswith('JWT ')):
                    restore_session(client, payload['auth'])
                ensure_session(client)
                self.app.extensions['incontrol_accounts']['remember_auth'](user_id, client)
                self.db.session.rollback()
                payload['auth'] = capture_session(client)

                def save(state):
                    """Сохранить промежуточный шаг и снимок сеанса во время фоновой работы."""
                    record = self.db.session.get(self.model, run_id)
                    record.payload_encrypted = self.pack(state)
                    record.updated_at = utcnow()
                    self.db.session.commit()
                    self.app.extensions['incontrol_accounts']['remember_auth'](user_id, client)
                    self.db.session.remove()  # Никакой транзакции во время внешних HTTP.

                save(payload)
                if payload.get('remote_id') is None:
                    payload['lookup_scope'] = 'all-main-v1'
                    found = find_open_main_tasks(client, payload['board'], payload['park'])
                    hint = payload.get('hint_id')
                    selected = hint or payload.get('confirmed_remote_id')
                    ids = [item['id'] for item in found]
                    if selected and selected not in ids:
                        # Отсутствие в поиске само по себе не доказывает закрытие.
                        direct = RejectionFlow(client, payload, save).task(selected)
                        phase = normalized_name((direct.get('phase') or {}).get('phase_name'))
                        if (direct.get('type_work') or {}).get('work_name') not in ALLOWED_WORK_NAMES:
                            raise RejectionError('Привязанная заявка имеет неподдерживаемый тип.')
                        if phase in {'новое', 'назначено', 'принято'} and direct.get('stage') not in ('denied', 'completed'):
                            found.append(direct); ids.append(selected)
                        elif (phase in {'выполнено', 'отклонено'} or direct.get('stage') in ('denied', 'completed')) and hint == selected:
                            payload.update(no_remote=True, remote_closed=hint, checked_at=utcnow().timestamp(), child_plan=[], candidates=[])
                            save(payload)
                            row = self.db.session.get(self.model, run_id)
                            row.state = 'plan' if action == 'inspect' else 'no_remote'
                            row.updated_at = utcnow()
                            self.db.session.commit()
                            return
                        else:
                            raise RejectionError('Не удалось подтвердить открытый этап выбранной заявки InControl.')
                    # Выбор «нет связанной заявки» действует только на проверенный список ID.
                    unrelated = not selected and sorted(ids) == payload.get('unrelated_remote_ids')
                    if not found or unrelated:
                        if hint:
                            raise RejectionError(f'Привязанная заявка {hint} закрыта. Открытые заявки по борту не найдены.')
                        payload.update(no_remote=True, checked_at=utcnow().timestamp(), child_plan=[], candidates=[])
                        save(payload)
                        row = self.db.session.get(self.model, run_id)
                        row.state = 'plan' if action == 'inspect' else 'no_remote'
                        row.updated_at = utcnow()
                        self.db.session.commit()
                        return
                    if not selected and scheduled_service:
                        service_requests = [item['id'] for item in found if is_service_request(item)]
                        if len(service_requests) == 1:
                            selected = service_requests[0]
                    if not selected:
                        payload['candidates'] = [{'id':x['id'], 'title':(x.get('type_work') or {}).get('work_name','Заявка'),
                                                  'description':x.get('task_description') or ''} for x in found]
                        payload['planning'] = True
                        save(payload)
                        row = self.db.session.get(self.model, run_id); row.state='choose'; self.db.session.commit()
                        return
                    payload.update(remote_id=selected, no_remote=False, selected_id=selected)
                if action == 'inspect' or (not payload.get('started') and not payload.get('child_decisions') and not payload.get('blanket_reject')):
                    probe = RejectionFlow(client, payload, save)
                    main, children = read_children(probe)
                    payload.update(child_plan=children, planning=True)
                    save(payload)
                    row = self.db.session.get(self.model, run_id); row.state='plan'; row.updated_at=utcnow(); self.db.session.commit()
                    return
                if not payload.get('started') and (payload.get('child_decisions') or payload.get('blanket_reject')):
                    probe = RejectionFlow(client, payload, save)
                    _, fresh_plan = read_children(probe)
                    if payload.get('blanket_reject'):
                        validate_blanket_rejection(payload.get('child_plan',[]), fresh_plan)
                    else:
                        decisions_summary(fresh_plan, payload['child_decisions'])
                    row = self.db.session.get(self.model, run_id)
                    row.remote_id=payload['remote_id']
                    local = self.db.session.get(self.Task, row.task_id, populate_existing=True, with_for_update=True)
                    if local.incontrol_task_id and int(local.incontrol_task_id) != payload['remote_id']:
                        raise RejectionError('Привязка локальной заявки изменилась.')
                    local.incontrol_task_id=payload['remote_id']
                    payload['hint_id']=payload['remote_id']
                    row.payload_encrypted=self.pack(payload)
                    self.db.session.commit()
                    self.db.session.remove()
                flow = (FulfillmentFlow if payload.get('mode') == 'complete' else RejectionFlow)(client, payload, save)
                result = drive_flow(flow, action, code)
                row = self.db.session.get(self.model, run_id)
                row.state = {'confirm': 'confirm', 'code': 'code', 'done': 'done'}[result]
                row.updated_at = utcnow()
                self.db.session.commit()
            except Exception as exc:
                self.db.session.rollback()
                row = self.db.session.get(self.model, run_id)
                if row:
                    if is_incontrol_unavailable(exc):
                        row.state = 'unavailable'
                        row.message = 'InControl недоступен. Можно закрыть заявку в Сетранс и отписать её позже.'
                    else:
                        row.state = 'error'
                        row.message = str(exc)[:500] if isinstance(exc, (RejectionError, PersonalAccountError)) else 'Ответ InControl не получен. Проверьте состояние перед повтором; уже выполненные шаги будут учтены.'
                    row.updated_at = utcnow()
                    self.db.session.commit()
                self.app.logger.warning('incontrol-rejection: run=%s error_type=%s', run_id, type(exc).__name__)
            finally:
                if client:
                    client.session.close()
                self.db.session.remove()
                self.slots.release()

    def page(self, run_id):
        """Перенаправить старую ссылку процесса в стандартную форму закрытия."""
        row, task = self.owned(run_id)
        if row.state == 'running' and row.updated_at < utcnow() - timedelta(minutes=3):
            row.state = 'error'
            row.message = 'Обработка прервалась. Проверьте состояние перед продолжением.'
            self.db.session.commit()
        response = redirect(url_for('complete_task', task_id=task.id, rejection_run=run_id))
        response.headers['Cache-Control'] = 'no-store'
        return response

    def status(self, run_id):
        """Вернуть форме результат, план или ожидаемое действие процесса."""
        row, _ = self.owned(run_id)
        stale = row.state == 'running' and row.updated_at < utcnow() - timedelta(minutes=3)
        payload = self.unpack(row)
        if row.state == 'unavailable':
            stale = row.updated_at < utcnow() - timedelta(seconds=self.checks.OUTAGE_SECONDS)
        response = jsonify(state='error' if stale else row.state,
                           message='Обработка прервалась. Проверьте состояние.' if stale else row.message,
                           step=payload.get('step'), document_id=(payload.get('document') or {}).get('id'),
                           signature_requested_at=(payload.get('signature_request') or {}).get('at'),
                           child_plan=payload.get('child_plan', []), decisions=payload.get('child_decisions', []),
                           candidates=payload.get('candidates', []), selected_id=payload.get('remote_id') or payload.get('selected_id'),
                           allow_unrelated=not payload.get('hint_id'),
                           unrelated_remote=bool(payload.get('no_remote') and payload.get('unrelated_remote_ids')),
                           planning=bool(payload.get('planning')),
                           no_remote=bool(payload.get('no_remote')), remote_closed=payload.get('remote_closed'))
        response.headers['Cache-Control'] = 'no-store'
        return response

    def action(self, run_id):
        """Проверить запрос формы и поставить действие с документом в очередь."""
        row, task = self.owned(run_id, lock=True)
        if not self.park_enabled(task.park):
            return jsonify(message='Автоматическая отписка в InControl выключена для этого парка.'), 409
        if self.checks.paused(task):
            return jsonify(message='Проверка InControl при закрытии приостановлена. Обновите форму закрытия.'), 409
        if row.state == 'running' and row.updated_at >= utcnow() - timedelta(minutes=3):
            return self.respond(run_id)
        action = request.form.get('action')
        if row.state in ('done', 'consumed'):
            return self.respond(run_id)
        payload = self.unpack(row)
        if action == 'select':
            if payload.get('started') or row.state != 'choose': abort(409)
            candidate_ids = {x['id'] for x in payload.get('candidates', [])}
            choice = request.form.get('remote_id', '')
            if choice == 'unrelated' and not task.incontrol_task_id:
                selected = None
                payload['unrelated_remote_ids'] = sorted(candidate_ids)
            else:
                try: selected = int(choice)
                except ValueError: abort(400)
                if selected not in candidate_ids: abort(409)
                payload.pop('unrelated_remote_ids', None)
            payload.update(confirmed_remote_id=selected, selected_id=selected, remote_id=None)
            row.payload_encrypted=self.pack(payload); self.db.session.commit()
            action, code = 'inspect', None
        elif action in ('sign', 'resend'):
            if row.state not in ('confirm', 'code') or request.form.get('step') != payload.get('step') or request.form.get('document_id') != str((payload.get('document') or {}).get('id')):
                abort(409)
            code = request.form.get('code', '').strip() or None
            if action == 'resend':
                if row.state != 'code':
                    abort(409)
                if datetime.now(timezone.utc).timestamp() - (payload.get('signature_request') or {}).get('at', 0) < 10:
                    return jsonify(message='Повторно запросить код можно через 10 секунд.'), 429
                code = None
            if action == 'sign' and row.state == 'code' and (not code or not code.isdecimal() or len(code) != 4):
                if request.accept_mimetypes.best == 'application/json':
                    return jsonify(message='Введите четыре цифры кода из письма.'), 400
                flash('Введите четыре цифры кода из письма.', 'error')
                return self.respond(run_id)
        elif action == 'retry':
            action, code = ('inspect' if payload.get('planning') and not payload.get('started') else 'prepare'), None
        else:
            abort(400)
        try:
            self.submit(row, action, code)
        except RejectionError as exc:
            self.db.session.rollback()
            if request.accept_mimetypes.best == 'application/json':
                return jsonify(message=str(exc)), 409
            flash(str(exc), 'error')
        return self.respond(run_id)

    def validate_outage(self, run, task, run_id, payload):
        """Убедиться, что сбой процесса подтверждает право на отложенную отписку."""
        if (run is None or run.id != run_id or run.user_id != current_user.id or run.state != 'unavailable'
                or run.updated_at < utcnow() - timedelta(seconds=self.checks.OUTAGE_SECONDS)
                or payload['fingerprint'] != self.checks.fingerprint(task)
                or (payload.get('hint_id') or None) != (task.incontrol_task_id or None)):
            raise RejectionError('Недоступность InControl не подтверждена или проверка устарела. Повторите проверку.')

    def consume(self, task, form):
        """Подтвердить результат личного процесса перед сохранением заявки в Сетрансе."""
        row = self.model.query.filter_by(id=form.get('incontrol_rejection_run', ''), task_id=task.id, user_id=current_user.id).with_for_update().first()
        if row is None or row.state not in ('done', 'no_remote'):
            return 'Отклонение и подпись заявки в InControl ещё не подтверждены.'
        payload = self.unpack(row)
        if row.state == 'no_remote' and payload.get('lookup_scope') != 'all-main-v1':
            return 'Прежняя проверка учитывала не все типы заявок InControl. Начните проверку заново.'
        if row.state == 'no_remote' and utcnow().timestamp() - payload.get('checked_at', 0) > 90:
            return 'Проверка отсутствия заявки устарела. В форме закрытия нажмите «Проверить состояние и продолжить».'
        if payload.get('mode', 'reject') != ('reject' if form.get('action') == 'reject' else 'complete'):
            return 'Способ закрытия изменён после подписания.'
        try:
            submitted = form_values(form)
        except RejectionError as exc:
            return str(exc)
        if 'main_outcome' not in payload['form'] and 'main_outcome' in submitted:
            if submitted['main_outcome'] != payload.get('mode', 'reject'):
                return 'Результат основной заявки изменён после начала закрытия.'
            submitted.pop('main_outcome')  # Совместимость с уже начатыми процессами прежней формы.
        if payload['fingerprint'] != self.checks.fingerprint(task) or payload['form_hash'] != digest(submitted):
            return 'Данные заявки изменились после подписания. Требуется проверка.'
        row.state = 'consumed'  # Коммит — в одной транзакции с локальным закрытием.
        return None


# --- Отложенная отписка в InControl и Яндекс Почте -----------------------------

class IncontrolOutbox:
    """Список отложенных отписок в InControl и Яндекс Почте."""
    def __init__(self, app, db, checks, rejections, mail_requests):
        """Подключить модель и маршруты списка отложенных отписок."""
        self.db = db
        self.checks = checks
        self.rejections = rejections
        self.mail_requests = mail_requests

        class PendingClosure(db.Model):
            """Отписка по закрытой заявке для одного сервиса."""
            __tablename__ = 'incontrol_pending_closure'
            __table_args__ = (
                db.Index('ix_incontrol_pending_park', 'reported_at', 'park'),
                {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'},
            )
            id = db.Column(db.Integer, primary_key=True)
            task_id = db.Column(db.Integer, nullable=False)
            remote_id = db.Column(db.BigInteger, nullable=True)
            service = db.Column(db.String(16), nullable=False, server_default='incontrol')
            mail_ticket_number = db.Column(db.String(40), nullable=True)
            park = db.Column(db.String(50), nullable=False)
            board_number = db.Column(db.String(20), nullable=False)
            description = db.Column(db.Text, nullable=False)
            completed_work = db.Column(db.Text, nullable=False)
            child_details = db.Column(db.Text, nullable=True)
            is_reject = db.Column(db.Boolean, nullable=False)
            completed_at = db.Column(db.DateTime, nullable=False)
            closed_by = db.Column(db.String(100), nullable=False)
            reported_at = db.Column(db.DateTime, nullable=True)
            reported_by = db.Column(db.String(100), nullable=True)

            @property
            def service_name(self):
                """Вернуть отображаемое имя сервиса для отложенной отписки."""
                return 'Яндекс Почта' if self.service == 'yandex_mail' else 'InControl'

        self.model = PendingClosure
        mail_requests.outbox = self
        app.add_url_rule('/incontrol/pending', 'incontrol_pending', login_required(self.page))
        app.add_url_rule('/incontrol/pending/count', 'incontrol_pending_count', login_required(self.count))
        app.add_url_rule('/incontrol/pending/export', 'incontrol_pending_export', login_required(self.export))
        app.add_url_rule('/incontrol/pending/<int:entry_id>/reported', 'incontrol_pending_reported',
                         login_required(self.mark_reported), methods=['POST'])

        @app.cli.command('init-incontrol-pending-closures')
        def init_table():
            """Создать таблицу отложенных отписок отдельной командой Flask."""
            PendingClosure.__table__.create(bind=db.engine, checkfirst=True)
            print('Таблица incontrol_pending_closure готова.')

    @staticmethod
    def selected_park():
        """Вернуть выбранный парк, если сотруднику разрешён доступ."""
        park = session.get('selected_park')
        if park and (current_user.username == 'vasilev' or park in session.get('available_parks', [])):
            return park
        return None

    def pending(self):
        """Выбрать незавершённые отписки выбранного парка."""
        return self.model.query.filter_by(park=self.selected_park(), reported_at=None)

    def pending_closures(self):
        """Сгруппировать отписки разных сервисов по одной закрытой заявке."""
        entries = self.pending().order_by(self.model.completed_at.desc(), self.model.id.desc()).all()
        closures = {}
        for entry in entries:
            key = (entry.board_number, entry.task_id, entry.completed_at, entry.description,
                   entry.completed_work, entry.is_reject, entry.closed_by)
            closures.setdefault(key, []).append(entry)
        return list(closures.values())

    def page(self):
        """Показать список незавершённых отписок выбранного парка."""
        closures = self.pending_closures()
        services = {entry.service for entries in closures for entry in entries}
        return render_template('incontrol_pending.html', closures=closures, services=services,
                               park=self.selected_park())

    def count(self):
        """Посчитать открытые записи для индикатора списка отписок."""
        try:
            count = self.pending().count()
        except SQLAlchemyError:
            self.db.session.rollback()
            return jsonify(message='Список отписок временно недоступен.'), 503
        response = jsonify(count=count)
        response.headers['Cache-Control'] = 'no-store'
        return response

    def mark_reported(self, entry_id):
        """Отметить запись отписанной и сохранить исполнителя и время."""
        entry = self.pending().filter_by(id=entry_id).with_for_update().first()
        if entry is None:
            abort(404)
        entry.reported_at = utcnow()
        entry.reported_by = current_user.full_name or current_user.username
        if entry.service == 'yandex_mail':
            self.mail_requests.confirm_manual_reply(entry.task_id, entry.mail_ticket_number, entry.reported_at)
        self.db.session.commit()
        flash(f'Отписка по борту {entry.board_number} отмечена.', 'success')
        return redirect(url_for('incontrol_pending'))

    def export(self):
        """Выгрузить незавершённые отписки выбранного парка в Excel."""
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'К отписке'
        sheet.append(['Борт', 'Парк', 'Заявка Сетранс', 'Заявка InControl', 'Окончание (МСК)',
                      'Описание заявки', 'Работы или причина', 'Результат', 'Закрыл', 'Связанные задачи',
                      'Сервис', 'Заявка Яндекс Почты'])
        for column, width in zip('ABCDEFGHIJKL', (14, 21, 17, 18, 22, 48, 48, 16, 25, 52, 20, 24)):
            sheet.column_dimensions[column].width = width
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for entries in self.pending_closures():
            entry = entries[0]
            remote_ids = list(dict.fromkeys(record.remote_id for record in entries if record.remote_id))
            remote = remote_ids[0] if len(remote_ids) == 1 else '\n'.join(map(str, remote_ids)) or None
            child_details = '\n'.join(dict.fromkeys(record.child_details for record in entries if record.child_details))
            services = '\n'.join(dict.fromkeys(record.service_name for record in entries))
            mail_tickets = '\n'.join(dict.fromkeys(record.mail_ticket_number for record in entries if record.mail_ticket_number))
            sheet.append([entry.board_number, entry.park, entry.task_id, remote, entry.completed_at.replace(microsecond=0),
                          entry.description, entry.completed_work, 'Отклонено' if entry.is_reject else 'Выполнено',
                          entry.closed_by, child_details or None, services, mail_tickets or None])
            for cell in sheet[sheet.max_row]:
                cell.alignment = Alignment(wrap_text=True, vertical='top')
                if isinstance(cell.value, str):
                    cell.data_type = 's'  # Тексты заявок в Excel остаются текстом.
            sheet.cell(sheet.max_row, 5).number_format = 'dd.mm.yyyy hh:mm'
        sheet.freeze_panes = 'C2'
        sheet.auto_filter.ref = sheet.dimensions
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        stamp = datetime.now(ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%d')
        return send_file(output, as_attachment=True, download_name=f'К_отписке_{self.selected_park() or stamp}_{stamp}.xlsx',
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', max_age=0)

    def stage_closure(self, task, completed_at, completed_work, is_reject, *, paused=False):
        """Сохраняет отписку в транзакции закрытия после проверки сбоя или при паузе."""
        if paused:
            self._stage(task, completed_at, completed_work, is_reject, remote_id=task.incontrol_task_id)
            return
        run_id = request.form.get('offline_run_id')
        check_id = request.form.get('offline_check_id')
        if bool(run_id) == bool(check_id):
            raise RejectionError('Для закрытия без InControl повторите проверку доступности.')
        run = self.rejections.model.query.filter_by(task_id=task.id).populate_existing().with_for_update().first()
        payload = self.rejections.unpack(run) if run else {}
        if run_id:
            self.rejections.validate_outage(run, task, run_id, payload)
            if not payload.get('planning'):
                mode = 'reject' if is_reject else 'complete'
                if mode != payload['mode'] or digest(form_values(request.form)) != payload['form_hash']:
                    raise RejectionError('Данные отличаются от начатого закрытия. Вернитесь к сохранённой форме.')
        else:
            if run and run.state != 'consumed' and (run.state in ('running', 'done', 'no_remote') or payload.get('started')):
                raise RejectionError('Для заявки уже начато закрытие в InControl. Продолжите его из сохранённой формы.')
            self.checks.consume_outage(task, check_id)
        details = None
        if run_id:
            titles = {item['id']: item['title'] for item in payload.get('child_plan', [])}
            lines = []
            for item in payload.get('child_decisions', []):
                result = 'Выполнено' if item['action'] == 'complete' else 'Отклонено'
                title = titles.get(item['id'], '')
                lines.append(f"№{item['id']} {title} — {result}: {item['work']}")
            details = '\n'.join(lines) or None
            run.state = 'consumed'
        remote_id = task.incontrol_task_id
        if run_id:
            remote_id = payload.get('remote_id') or remote_id or payload.get('selected_id')
        self._stage(task, completed_at, completed_work, is_reject, remote_id=remote_id, child_details=details)

    def stage_mail_closure(self, task, completed_at, completed_work, is_reject, ticket_number):
        """Записать ручную отписку по письму при отключённой автоотправке."""
        self._stage(task, completed_at, completed_work, is_reject, service='yandex_mail', mail_ticket_number=ticket_number)

    def stage_mail_review(self, task, link):
        """Добавить почтовый ответ, который требует ручной сверки."""
        if self.model.query.filter_by(task_id=task.id, service='yandex_mail',
                                      mail_ticket_number=link.ticket_number, reported_at=None).first() is None:
            self._stage(task, task.completed_at, link.reply_text, bool(task.reject), service='yandex_mail',
                        mail_ticket_number=link.ticket_number, closed_by=task.author_closed or 'Система')

    def _stage(self, task, completed_at, completed_work, is_reject, *, service='incontrol',
               remote_id=None, child_details=None, mail_ticket_number=None, closed_by=None):
        """Добавить запись отписки в текущую транзакцию закрытия."""
        self.db.session.add(self.model(
            task_id=task.id, remote_id=remote_id, service=service, mail_ticket_number=mail_ticket_number,
            park=task.park, board_number=task.board_number, description=task.description,
            completed_work=completed_work, child_details=child_details, is_reject=is_reject,
            completed_at=completed_at.replace(tzinfo=None),
            closed_by=closed_by or current_user.full_name or current_user.username,
        ))


# --- Интеграция с Flask и планировщиком ----------------------------------------

def incontrol_now():
    """Текущее московское время без таймзоны — как хранится в базе проекта."""
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)


def _incontrol_chunks(values, size=500):
    """Разбить набор значений на пакеты для SQL-фильтров IN."""
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _incontrol_board_key(value):
    """Те же варианты номера, которые учитывает get_active_task_for_board."""
    board = normalize_board(value)
    if not board:
        return None
    board = board.replace('\xa0', '').replace(' ', '')
    return board[:-2] if board.endswith('.0') else board


class IncontrolIntegration:
    """Точки подключения InControl к локальным заявкам и планировщику."""
    def __init__(self, application, db, task_model, car_model, electric_bus_model, mail_requests, run_locked_job):
        """Связать модели, маршруты, фоновую проверку и отписку с приложением."""
        self.application = application
        self.db = db
        self.task_model = task_model
        self.car_model = car_model
        self.electric_bus_model = electric_bus_model
        self.run_locked_job = run_locked_job
        self.enabled = os.getenv('INCONTROL_ENABLED', '0') == '1'

        class IncontrolStatus(db.Model):
            """Кеш заявок incontrol24 по бортам.

            На один борт может приходиться несколько заявок,
            поэтому уникальность определяется парой:
            board_number + work_name.
            """

            __tablename__ = 'incontrol_status'

            __table_args__ = {
                'mysql_charset': 'utf8mb4',
                'mysql_collate': 'utf8mb4_unicode_ci',
            }

            board_number = db.Column(db.String(20), primary_key=True)
            work_name = db.Column(db.String(255), primary_key=True)

            has_open = db.Column(db.Boolean, nullable=False, default=False)
            phase_name = db.Column(db.String(50), nullable=True)
            checked_at = db.Column(db.DateTime, nullable=False, index=True)

            def __repr__(self):
                """Показать борт, тип заявки и состояние строки кеша при диагностике."""
                return (
                    f"<IncontrolStatus "
                    f"{self.board_number} "
                    f"{self.work_name} "
                    f"open={self.has_open}>"
                )

        self.status_model = IncontrolStatus
        self.checks = CompletionChecks(
            application, db, task_model, self.refresh_incontrol_status_for_board, lambda: self.enabled,
        )
        self.account_model, self.personal_client = register_incontrol_accounts(application, db)
        self.rejections = IncontrolRejections(
            application, db, task_model, self.checks, self.personal_client,
            lambda: os.getenv('INCONTROL_REJECTION_ENABLED', '0') == '1',
        )
        self.outbox = IncontrolOutbox(application, db, self.checks, self.rejections, mail_requests)

        application.context_processor(self.completion_options)
        application.add_template_filter(plain_completed_work, 'completed_work_text')
        application.add_url_rule('/debug_incontrol_tasks', 'debug_incontrol_tasks', self.debug_incontrol_tasks)

    def completion_options(self):
        """Передать шаблонам настройки проверки и приостановки."""
        return {
            'requires_completion_check': self.checks.required,
            'incontrol_closure_paused': self.checks.paused,
            'paused_closure_services': paused_closure_services,
        }

    def run_job_once(self, job_name, job_func):
        """Запустить обход под общей блокировкой импорта и кеша InControl."""
        return self.run_locked_job(job_name, job_func, 'incontrol_sync')

    def register_scheduler_jobs(self, add_job):
        """Запланировать раздельные обходы кеша и новых заявок."""
        if not self.enabled:
            return
        add_job(
            'refresh_incontrol_statuses',
            lambda: self.run_job_once('refresh_incontrol_statuses', self.refresh_incontrol_statuses),
            CronTrigger(minute='7,37', timezone=MOSCOW_TZ),
        )
        add_job(
            'create_tasks_from_incontrol',
            lambda: self.run_job_once('create_tasks_from_incontrol', self.create_tasks_from_incontrol),
            CronTrigger(minute='*/15', timezone=MOSCOW_TZ),
        )

    def get_incontrol_boards(self, boards):
        """Выбрать борта с открытыми заявками для значка в списке ремонтов."""
        normalized = {normalize_board(board) for board in boards}
        normalized.discard(None)
        if not normalized:
            return set()

        try:
            rows = self.status_model.query.filter(
                self.status_model.board_number.in_(normalized),
                self.status_model.has_open.is_(True),
                self.status_model.work_name != 'Заявка на мойку'
            ).all()
        except SQLAlchemyError as error:
            # Таблицы может ещё не быть (не создана на сервере) или база недоступна.
            # Бейдж - вспомогательная метка: она не имеет права ронять список задач.
            self.application.logger.warning(f'incontrol24: кеш недоступен: {error}')
            self.db.session.rollback()
            return set()
        return {row.board_number for row in rows}

    @staticmethod
    def _make_incontrol_client(timeout=30, interactive=False):
        """Отдельной функцией — чтобы подменяться в тестах."""
        return IncontrolClient(
            os.getenv('INCONTROL_LOGIN'),
            os.getenv('INCONTROL_PASSWORD'),
            timeout=timeout,
            interactive=interactive,
        )

    def refresh_incontrol_status_for_board(
        self,
        board_number,
        park,
        required_work_names
    ):
        """Точечно проверить нужный тип заявки для одного борта."""

        if not self.enabled:
            return None

        board = normalize_board(board_number)

        if not board:
            raise IncontrolError('Не указан борт для проверки InControl')
        if is_outside_incontrol(park):
            return None

        started = time.monotonic()

        client = None
        try:
            t = time.monotonic()
            # Ответ списка заявок InControl на практике занимает до 20+ секунд.
            # Проверка уже фоновая: 7 секунд обрывали корректные медленные ответы.
            client = self._make_incontrol_client(timeout=25, interactive=True)
            self.application.logger.info(
                f'incontrol24: создание клиента: {time.monotonic() - t:.2f} сек'
            )

            t = time.monotonic()
            client.authenticate()
            self.application.logger.info(
                f'incontrol24: authenticate: {time.monotonic() - t:.2f} сек'
            )

            t = time.monotonic()
            has_open, phase_name, work_name = client.check_board_details(
                board,
                park,
                required_work_names
            )
            self.application.logger.info(
                f'incontrol24: check_board_details: {time.monotonic() - t:.2f} сек'
            )

            self.application.logger.info(
                f'incontrol24: точечная проверка борта {board}: '
                f'Есть в БД = {has_open}, '
                f'Этап = {phase_name}, '
                f'Тип заявки = {work_name}, '
                f'общее время = {time.monotonic() - started:.2f} сек'
            )

            return work_name if has_open else None

        except Exception as error:
            self.application.logger.error(
                f'incontrol24: ошибка точечной проверки борта '
                f'{board}: {error}, '
                f'время = {time.monotonic() - started:.2f} сек'
            )

            raise IncontrolError('Не удалось проверить заявку в InControl') from error
        finally:
            if client is not None:
                client.session.close()

    def refresh_incontrol_statuses(self):
        """Получить снимок InControl без транзакции БД, затем обновить кеш."""
        if not self.enabled:
            return {'status': 'disabled'}

        started = time.monotonic()
        fetch_seconds = 0.0
        write_seconds = 0.0
        checked = skipped = 0
        failed_orgs = set()
        client = None
        try:
            has_tasks = self.db.session.query(self.task_model.id).filter_by(status='in_repair').first() is not None
            self.db.session.remove()
            if not has_tasks:
                self.status_model.query.delete(synchronize_session=False)
                self.db.session.commit()
                return {'status': 'ok', 'проверено': 0, 'пропущено': 0, 'ошибок': 0}

            fetch_started = time.monotonic()
            try:
                client = self._make_incontrol_client()
                client.authenticate()
                open_by_org = {}
                for org_id in ALL_ORGS:
                    try:
                        open_by_org[org_id] = client.fetch_open_boards(org_id)
                    except Exception:
                        failed_orgs.add(org_id)
                        self.application.logger.exception('incontrol24: кеш, организация %s недоступна', org_id)
            finally:
                fetch_seconds += time.monotonic() - fetch_started

            # Перечитываем после сетевого обхода: за это время заявку могли закрыть
            # или создать вручную. ORM-соединение не удерживалось во время обхода.
            write_started = time.monotonic()
            pairs = self.db.session.query(self.task_model.board_number, self.task_model.park).filter(
                self.task_model.status == 'in_repair'
            ).distinct().all()
            boards = {}
            for board_number, park in pairs:
                board = normalize_board(board_number)
                if board:
                    boards.setdefault(board, park)

            replacements = []
            rows = []
            now = incontrol_now()
            for board, park in boards.items():
                org_id = park_to_org(park)
                orgs = ([] if is_outside_incontrol(park) else
                        [org_id] if org_id else list(ALL_ORGS))
                # При неизвестном парке нужен полный ответ всех организаций.
                # Частичный ответ не доказывает отсутствие заявки.
                if any(org in failed_orgs for org in orgs):
                    skipped += 1
                    continue
                board_tasks = {}
                for org in orgs:
                    for info in open_by_org.get(org, {}).get(board, []):
                        work_name = (info.get('work_name') or '').strip()
                        if work_name:
                            board_tasks[work_name] = info.get('phase_name')
                replacements.append(board)
                rows.extend(self.status_model(
                    board_number=board, work_name=work_name, has_open=True,
                    phase_name=phase, checked_at=now,
                ) for work_name, phase in board_tasks.items())
                checked += 1

            # Вся замена атомарна: до commit читатели видят прежний снимок.
            for chunk in _incontrol_chunks(replacements):
                self.status_model.query.filter(self.status_model.board_number.in_(chunk)).delete(synchronize_session=False)
            if boards:
                self.status_model.query.filter(~self.status_model.board_number.in_(list(boards))).delete(synchronize_session=False)
            else:
                self.status_model.query.delete(synchronize_session=False)
            self.db.session.add_all(rows)
            self.db.session.commit()
            write_seconds = time.monotonic() - write_started
            return {'status': 'ok', 'проверено': checked, 'пропущено': skipped, 'ошибок': len(failed_orgs)}
        except Exception as error:
            self.db.session.rollback()
            self.application.logger.exception('incontrol24: обновление кеша не выполнено, прежние данные сохранены')
            return {'status': 'error', 'message': str(error)}
        finally:
            self.db.session.remove()
            client_session = getattr(client, 'session', None)
            if client_session is not None:
                client_session.close()
            self.application.logger.info(
                'INCONTROL_SYNC job=cache total_ms=%.1f fetch_ms=%.1f write_ms=%.1f checked=%s skipped=%s failed_orgs=%s',
                (time.monotonic() - started) * 1000, fetch_seconds * 1000,
                write_seconds * 1000, checked, skipped, len(failed_orgs),
            )

    def _incontrol_return_candidates(self, applications):
        """Найти закрытые карточки по подтверждённой привязке к InControl."""
        run_model = self.rejections.model
        closed = self.db.session.query(self.task_model.id, self.task_model.board_number, self.task_model.park, self.task_model.completed_at)
        closed = closed.filter(self.task_model.status == 'completed')
        matches = {}
        for remote_ids in _incontrol_chunks(applications):
            direct = closed.add_columns(self.task_model.incontrol_task_id).filter(self.task_model.incontrol_task_id.in_(remote_ids))
            historical = closed.add_columns(run_model.remote_id).join(run_model, run_model.task_id == self.task_model.id).filter(
                self.task_model.incontrol_task_id.is_(None), run_model.remote_id.in_(remote_ids), run_model.state == 'consumed',
            )
            for task_id, board, park, completed_at, remote_id in (*direct.all(), *historical.all()):
                info = applications[remote_id]
                if (_incontrol_board_key(board) == _incontrol_board_key(info['board'])
                        and park == org_to_park(info['org_id'])):
                    matches.setdefault(remote_id, set()).add((task_id, completed_at))
        return {remote_id: next(iter(rows)) for remote_id, rows in matches.items() if len(rows) == 1}

    def _reopen_incontrol_repair(self, task_id, completed_at, remote_id, board, park, reason):
        """Вернуть совпавшую карточку в ремонт и сбросить прежний процесс закрытия."""
        task = self.task_model.query.filter_by(id=task_id).populate_existing().with_for_update().first()
        if (task is None or task.status != 'completed' or task.completed_at != completed_at
                or task.incontrol_task_id not in (None, remote_id) or task.park != park
                or _incontrol_board_key(task.board_number) != _incontrol_board_key(board)):
            return False
        run_model = self.rejections.model
        run = run_model.query.filter_by(task_id=task_id).with_for_update().first()
        if run and (run.state != 'consumed' or run.remote_id not in (None, remote_id)):
            return False
        if task.incontrol_task_id is None and (run is None or run.remote_id != remote_id):
            return False
        if run:
            self.db.session.delete(run)
        self.checks.model.query.filter_by(task_id=task_id).delete(synchronize_session=False)
        task.incontrol_task_id = remote_id
        task.status = 'in_repair'
        task.tag = 'in_work'
        task.stk_returned = reason == 'stk'
        task.unresolved_returned = reason == 'unresolved'
        task.completed_at = None
        task.author_closed = None
        task.reject = False
        task.yandex_reply_lookup = False
        self.db.session.commit()
        return True

    def create_tasks_from_incontrol(self):
        """Импорт: сетевой обход, короткий отбор, затем сеть и короткая запись."""
        if not self.enabled:
            return {'status': 'disabled'}

        started = time.monotonic()
        fetch_seconds = write_seconds = 0.0
        created = linked = reopened = skipped = errors = 0
        client = None
        import_parks = {'14 парк', '1 парк', 'Красная Пахра', '18 парк'}
        import_orgs = [org for org, park in ORG_TO_PARK.items() if park in import_parks]
        return_parks = set()
        if os.getenv('INCONTROL_RETURNS_ENABLED', '1') == '1':
            configured_parks = os.getenv('INCONTROL_RETURN_PARKS', ','.join(sorted(import_parks)))
            return_parks = {name.strip() for name in configured_parks.split(',')} & import_parks
        self.application.logger.info('incontrol24: запуск проверки новых заявок из InControl...')
        # Функция вызывается в отдельном app_context планировщика.
        self.db.session.remove()
        try:
            fetch_started = time.monotonic()
            try:
                client = self._make_incontrol_client()
                client.authenticate()
                applications = []
                for org_id in import_orgs:
                    try:
                        applications.extend(client.fetch_open_applications(org_id))
                    except Exception:
                        errors += 1
                        self.application.logger.exception('incontrol24: импорт, организация %s недоступна', org_id)
            finally:
                fetch_seconds += time.monotonic() - fetch_started

            by_id = {}
            returned_repairs = {}
            for info in applications:
                if info.get('work_name') == 'Заявка на сервисное обслуживание':
                    continue
                if info.get('task_id'):
                    if info.get('return_reason'):
                        if org_to_park(info.get('org_id')) in return_parks:
                            returned_repairs.setdefault(info['task_id'], info)
                    else:
                        by_id.setdefault(info['task_id'], info)

            # Уже известные id исключаем пакетно, вместо SQL-запроса на каждую
            # старую заявку в полной выдаче InControl.
            select_started = time.monotonic()
            return_candidates = self._incontrol_return_candidates(returned_repairs)
            existing_ids = set()
            for chunk in _incontrol_chunks(by_id):
                existing_ids.update(row[0] for row in self.db.session.query(self.task_model.incontrol_task_id).filter(
                    self.task_model.incontrol_task_id.in_(chunk)
                ).all())
            new_info = [info for task_id, info in by_id.items() if task_id not in existing_ids]
            vehicle_boards = {normalize_board(info.get('board')) for info in new_info}
            vehicle_boards.discard(None)
            locations = {}
            for model in (self.electric_bus_model, self.car_model):  # При совпадении номера приоритет автобуса, как раньше.
                for chunk in _incontrol_chunks(vehicle_boards):
                    locations.update(self.db.session.query(model.bort, model.location).filter(model.bort.in_(chunk)).all())
            candidates = []
            for info in by_id.values():
                board = normalize_board(info.get('board'))
                if info['task_id'] in existing_ids or not board or board not in locations:
                    skipped += 1
                    continue
                park = org_to_park(info.get('org_id')) or locations[board]
                if park not in import_parks:
                    skipped += 1
                    continue
                candidates.append((board, dict(info)))
            self.db.session.remove()
            write_seconds += time.monotonic() - select_started

            for remote_id, (task_id, completed_at) in return_candidates.items():
                info = returned_repairs[remote_id]
                park = org_to_park(info['org_id'])
                fetch_started = time.monotonic()
                try:
                    fresh = client.get_task_by_id(remote_id, info['org_id'])
                    if not fresh or fresh.get('stage') in ('denied', 'completed'):
                        continue
                    if _incontrol_board_key(board_of(fresh)) != _incontrol_board_key(info['board']):
                        continue
                    reason = repair_return_reason(
                        ((fresh.get('type_work') or {}).get('work_name') or '').strip(),
                        (fresh.get('phase') or {}).get('phase_name'),
                        ((fresh.get('label') or {}).get('label_name') or '').strip(),
                    )
                    if not reason:
                        continue
                except Exception:
                    errors += 1
                    self.application.logger.exception('incontrol24: не удалось подтвердить возврат по заявке %s', remote_id)
                    continue
                finally:
                    fetch_seconds += time.monotonic() - fetch_started

                write_started = time.monotonic()
                try:
                    if self._reopen_incontrol_repair(task_id, completed_at, remote_id, info['board'], park, reason):
                        reopened += 1
                        return_label = 'СТК' if reason == 'stk' else '«Неисправности не устранены»'
                        self.application.logger.info('incontrol24: возврат %s, заявка %s, task=%s, борт=%s, парк=%s',
                                                return_label, remote_id, task_id, info['board'], park)
                finally:
                    self.db.session.remove()
                    write_seconds += time.monotonic() - write_started

            for board, info in candidates:
                # Предыдущая запись полностью завершена, соединение возвращено
                # в пул до любого обращения к внешнему сервису.
                fetch_started = time.monotonic()
                try:
                    fresh = client.get_task_by_id(info['task_id'], info.get('org_id'))
                    if not fresh or fresh.get('stage') in ('denied', 'completed'):
                        skipped += 1
                        continue
                    phase_id = (fresh.get('phase') or {}).get('id')
                    label = ((fresh.get('label') or {}).get('label_name') or '').strip()
                    if (phase_id is not None and phase_id not in OPEN_PHASES) or label in IGNORED_LABELS:
                        skipped += 1
                        continue
                    work_name = ((fresh.get('type_work') or {}).get('work_name') or info.get('work_name') or '').strip()
                    if work_name not in {'Заявка на ремонт', 'Заявка на ремонт с линии'}:
                        skipped += 1
                        continue
                    description = client.build_description_from_linked(info.get('linked_ids'), info.get('org_id'))
                    description = description or info.get('work_name') or 'Заявка InControl'
                except Exception:
                    skipped += 1
                    errors += 1
                    self.application.logger.exception('incontrol24: перепроверка заявки %s не выполнена', info['task_id'])
                    continue
                finally:
                    fetch_seconds += time.monotonic() - fetch_started

                write_started = time.monotonic()
                try:
                    # Импорты и ручное создание используют одну блокировку строки борта.
                    vehicle = self.car_model.query.filter_by(bort=board).with_for_update().first()
                    is_elbus = vehicle is None
                    if vehicle is None:
                        vehicle = self.electric_bus_model.query.filter_by(bort=board).with_for_update().first()
                    if vehicle is None:
                        skipped += 1
                        continue
                    park = org_to_park(info.get('org_id')) or vehicle.location
                    if park not in import_parks:
                        skipped += 1
                        continue
                    if self.task_model.query.filter_by(incontrol_task_id=info['task_id']).first():
                        skipped += 1
                        continue
                    active_repairs = active_repairs_for_board(self.task_model, board).with_for_update().all()
                    if active_repairs:
                        candidates = manual_repair_candidates(
                            active_repairs, park, excluded_authors=(MAIL_TASK_AUTHOR, 'InControl', 'Система'),
                        )
                        task, extras = match_imported_repair(
                            (candidate for candidate in candidates if candidate.incontrol_task_id is None), description,
                        )
                        if task is None:
                            skipped += 1
                            continue
                        task_id = task.id
                        task.incontrol_task_id = info['task_id']
                        if extras:
                            task.description = '\n'.join((task.description.rstrip(), *extras))
                        self.db.session.commit()
                        linked += 1
                        self.application.logger.info('incontrol24: привязана заявка %s, task=%s, борт=%s, парк=%s',
                                                info['task_id'], task_id, board, park)
                        continue
                    created_at = incontrol_now()
                    if info.get('creation_date') is not None:
                        try:
                            created_at = datetime.fromtimestamp(int(info['creation_date']), MOSCOW_TZ).replace(tzinfo=None)
                        except (TypeError, ValueError, OSError):
                            self.application.logger.warning('incontrol24: некорректная дата заявки %s', info['task_id'])
                    task = self.task_model(
                        board_number=board, description=description, park=park,
                        status='in_repair', created_at=created_at, tag='waiting',
                        type_ts=vehicle.type_ts or ('Электробус' if is_elbus else 'Автобус'),
                        author_open='InControl', contract=vehicle.contract, vin=vehicle.vin,
                        incontrol_task_id=info['task_id'],
                    )
                    self.db.session.add(task)
                    self.db.session.commit()
                    created += 1
                    self.application.logger.info('incontrol24: импортирована заявка %s, борт=%s, парк=%s', info['task_id'], board, park)
                finally:
                    self.db.session.remove()
                    write_seconds += time.monotonic() - write_started

            return {'status': 'ok', 'created': created, 'linked': linked, 'reopened': reopened, 'skipped': skipped, 'errors': errors}
        except Exception as error:
            self.db.session.rollback()
            self.application.logger.exception('incontrol24: импорт прерван')
            return {'status': 'error', 'message': str(error), 'created': created, 'linked': linked,
                    'reopened': reopened, 'skipped': skipped, 'errors': errors + 1}
        finally:
            self.db.session.remove()
            client_session = getattr(client, 'session', None)
            if client_session is not None:
                client_session.close()
            self.application.logger.info(
                'INCONTROL_SYNC job=import total_ms=%.1f fetch_ms=%.1f db_ms=%.1f created=%s linked=%s reopened=%s skipped=%s errors=%s',
                (time.monotonic() - started) * 1000, fetch_seconds * 1000,
                write_seconds * 1000, created, linked, reopened, skipped, errors,
            )

    def prepare_closure(self, task, form, completed_at, completed_work, is_reject):
        """Проверить основание для закрытия, отложить отписку или запустить личный процесс."""
        incontrol_paused = self.checks.paused(task)
        offline = incontrol_paused or bool(form.get('offline_run_id') or form.get('offline_check_id'))
        if offline:
            try:
                self.outbox.stage_closure(task, completed_at, completed_work, is_reject, paused=incontrol_paused)
                check_error = None
            except RejectionError as exc:
                check_error = str(exc)
        elif form.get('completion_check_id') and self.checks.required(task):
            # Одноразовое подтверждение старой проверки допустимо только при реально
            # включённой проверке: оно удостоверяет отсутствие открытой заявки.
            check_error = self.checks.consume(task, form['completion_check_id'])
        elif self.rejections.applies(task, form):
            if form.get('incontrol_rejection_run'):
                check_error = self.rejections.consume(task, form)
            else:
                try:
                    return False, None, self.rejections.start(task, form)
                except (RejectionError, PersonalAccountError) as exc:
                    check_error = str(exc)
                except SQLAlchemyError:
                    check_error = 'Не удалось начать отклонение InControl. Проверьте наличие таблиц настроек и процесса.'
        else:
            check_error = self.checks.consume(task, form.get('completion_check_id'))
        return offline, check_error, None

    def resume_closure(self, task, current_mileage, is_evacuation, run_id):
        """Открыть форму закрытия с восстановленным сохранённым черновиком."""
        run, run_task = self.rejections.owned(run_id)
        if run_task.id != task.id:
            abort(404)
        saved = self.rejections.unpack(run)
        return render_template('complete_task.html', task=task, current_mileage=current_mileage,
                               is_evacuation=is_evacuation, completed_work_value=saved['form']['completed_work'],
                               mileage_value=saved['form']['mileage'], work_in_value=saved['form']['work_in'],
                               incontrol_main_id_value=saved['remote_id'], incontrol_mode_value=saved.get('mode', 'reject'),
                               seasonal_service_value=saved['form'].get('seasonal_service', '0'),
                               evacuation_confirmed_value=saved['form'].get('evacuation_photos_confirmed', ''),
                               child_decisions_value=saved['form'].get('child_decisions', ''))

    def debug_incontrol_tasks(self):
        """Вывести в журнал диагностику выбранных заявок InControl."""
        client = self._make_incontrol_client()
        client.authenticate()

        client.debug_task_by_id(19311671)
        client.debug_task_by_id(19311705)

        return 'Проверка выполнена. Смотри консоль Flask.'
