"""Возобновляемая цепочка отклонения. Состояние сохраняет вызывающая сторона.

POST/PATCH не повторяются автоматически. Перед каждым возобновлением
проверяется удалённое состояние и уже подписанные документы.
"""
import logging
import time
from incontrol import API_BASE, PARK_TO_ORG
from incontrol_completion import normalized_name


def service_fields(task):
    return [field for field in task.get('additional_fields', [])
            if normalized_name(field.get('field_name')) in {'выполненные услуги', 'выполнение услуги'}]


class RejectionError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class ActSignatureRequired(RejectionError):
    pass


class RejectionFlow:
    def __init__(self, client, state, save):
        self.client, self.state, self.save = client, state, save
        self.org = PARK_TO_ORG.get(state['park'])
        if not self.org:
            raise RejectionError('Организация парка не настроена.')
        self.deadline = time.monotonic() + 90
        # Личный клиент должен иметь retries=0: повтор подписи недопустим.
        if not client.interactive:
            raise RejectionError('Для отклонения необходим личный интерактивный клиент.')

    def request(self, method, path, data=None, params=None):
        from incontrol_session import capture_session
        # Повтор разрешён только после явного HTTP 401: сервер отклонил
        # операцию до выполнения. Таймауты и неоднозначные ответы не повторяем.
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
        self.state.update(values)
        self.save(self.state)

    def task(self, task_id):
        task = self.request('GET', f'/tasks/{task_id}/')
        if task.get('id') != int(task_id) or (task.get('organization_performer') or {}).get('id') != self.org:
            raise RejectionError('Номер или организация заявки не совпадает с ожидаемыми.')
        board = next((str(f.get('value') or '') for f in (task.get('serviced_object') or {}).get('additional_fields', []) if f.get('field_name') == 'Бортовой номер'), '')
        if board != self.state['board']:
            raise RejectionError('Борт заявки InControl не совпадает с локальной заявкой.')
        return task

    def identify(self):
        me = self.request('GET', '/me/')
        if not me.get('id') or normalized_name(me.get('last_name')) != normalized_name(self.state['full_name']).split(' ')[0]:
            raise RejectionError('Фамилия личного аккаунта InControl не совпадает с пользователем сайта.')
        self.user_id = me['id']
        if self.state.get('actor') and self.state['actor'] != self.user_id:
            raise RejectionError('Личный аккаунт изменился во время подписания.')
        self.remember(actor=self.user_id)

    def phase_ids(self, task, required=None):
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
        data = self.request('GET', f"/tasks/{self.state['remote_id']}/available-report-files/", params={'is_paginated': 'false'})
        if not isinstance(data, dict) or not isinstance(data.get('results'), list):
            raise RejectionError('Неизвестный формат документов InControl.')
        files = data['results']
        if any(f.get('task') != self.state['remote_id'] for f in files):
            raise RejectionError('Документ относится к другой заявке.')
        return files

    def current_document(self, files, kind):
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
        types = {'bid_signed_by_performer', 'bid_signed_by_customer'}
        current = self.current_document(files, kind)
        return bool(current and current.get('upload_type') in types)

    def pending_document(self, files, kind):
        types = {'bid_signed_by_controller'}
        document = self.current_document(files, kind)
        if not document or document.get('upload_type') not in types or not document.get('file'):
            raise RejectionError('Актуальный документ для подписи пока не готов. Повторите проверку.')
        return document

    def validate(self):
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
    from incontrol import board_of, normalize_board, OPEN_PHASES, PAGE_SIZE, MAX_PAGES, ALLOWED_WORK_NAMES
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
