"""Клиент API incontrol24 (app.incontrol24.ru).

Документации у API нет: поведение восстановлено из JS-бандла SPA и проверено
запросами. Подробности и тупики — в docs/superpowers/specs/.

Модуль намеренно не зависит ни от Flask, ни от базы: модели проекта объявлены
в app.py, и обращение к ним отсюда создало бы круговой импорт.
"""

import requests
import logging
import json
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import time

API_BASE = 'https://skk.mttech.moscow/api/v1'

# Без этих двух заголовков бэкенд отвечает 404, а не 401 — легко принять за
# опечатку в пути.
APP_ID = 'x6VWRinSpNGt39J'
APP_VERSION = '1.47.5'

ROLE_ID = 143

# Открытые этапы: «Новое.», «Назначено.», «Принято.» (точка — часть названия).
OPEN_PHASES = (1, 2, 3)

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
        if self._deadline is None:
            return self.timeout
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise IncontrolError('Истекло время проверки InControl')
        return (min(3, remaining), min(self.timeout, remaining))

    def _auth_audit(self, event, status=None):
        logging.getLogger('app').info('INCONTROL_AUTH context=%s event=%s http=%s',
            getattr(self, 'auth_context', 'personal' if self.interactive else 'background'), event, status)

    def authenticate(self):
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

                # Метка "Не пройдено СТК" — полностью игнорируем
                label_name = (
                        (task.get('label') or {}).get('label_name') or ''
                ).strip()

                if label_name in ('Не пройдено СТК', 'Неисправности не устранены'):
                    continue

                phase_name = (
                    (task.get('phase') or {}).get('phase_name')
                )

                work_name = (
                        (task.get('type_work') or {}).get('work_name') or ''
                ).strip()

                # Нам нужны только эти типы
                if work_name not in ALLOWED_WORK_NAMES:
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
                if label_name in ('Не пройдено СТК', 'Неисправности не устранены'):
                    continue

                work_name = (
                    (task.get('type_work') or {}).get('work_name') or ''
                ).strip()
                if work_name not in ALLOWED_WORK_NAMES:
                    continue

                phase_name = (
                    (task.get('phase') or {}).get('phase_name')
                )

                applications.append({
                    'board': board,
                    'work_name': work_name,
                    'phase_name': phase_name,
                    'task_id': task.get('id'),
                    'linked_ids': linked_task_ids(task),
                    'org_id': org_id,
                    'creation_date': task.get('creation_date'),
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

                    # 1. Стадии "Отклонено" и "Выполнено" — полностью игнорируем
                    if task.get('stage') in ('denied', 'completed'):
                        continue

                    # 2. "Не пройдено СТК" не учитываем
                    label_name = (
                            (task.get('label') or {}).get('label_name') or ''
                    ).strip()

                    if label_name in ('Не пройдено СТК', 'Неисправности не устранены'):
                        continue

                    # 3. Дополнительно проверяем фазу из ответа API.
                    phase_name = (task.get('phase') or {}).get('phase_name')
                    if not isinstance(phase_name, str) or not phase_name:
                        raise IncontrolError('InControl не сообщил фазу заявки')
                    if phase_name not in OPEN_PHASE_NAMES:
                        continue

                    # 4. Проверка типа работ
                    work_name = (
                            (task.get('type_work') or {}).get('work_name') or ''
                    ).strip()
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

        import json

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
