"""Подготовка закрытия InControl. На этом этапе выполняются только GET-запросы."""
import re
import time

from incontrol import API_BASE, PARK_TO_ORG


class CompletionPreparationError(Exception):
    pass


def normalized_name(value):
    return re.sub(r'[.\s]+', ' ', str(value or '').strip().casefold().replace('ё', 'е')).strip()


def is_service_request(task):
    return normalized_name((task.get('type_work') or {}).get('work_name')) == 'заявка на сервисное обслуживание'


class CompletionPreparation:
    def __init__(self, client):
        self.client = client
        self.deadline = time.monotonic() + 60

    def get(self, path, org, params=None):
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
