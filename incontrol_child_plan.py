"""План связанных задач: только чтение API и проверка решений пользователя."""
import json
from incontrol_completion import normalized_name
from incontrol_rejection import RejectionError,service_fields


def decisions_from_json(raw):
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
    if {x['id'] for x in plan} != {x['id'] for x in decisions}:
        raise RejectionError('Состав связанных задач изменился. Обновите их список.')
    by_id={x['id']:x for x in decisions}
    for item in plan:
        phase=normalized_name(item['phase']);decision=by_id[item['id']]
        if phase in ('выполнено','отклонено') and decision['action'] != ('complete' if phase=='выполнено' else 'reject'):
            raise RejectionError(f"Задача {item['id']} уже завершена с другим результатом.")


def decisions_summary(plan, decisions):
    validate_decisions(plan,decisions)
    text='\n'.join(d['work'] for d in decisions)
    mode='complete' if any(d['action']=='complete' for d in decisions) else 'reject'
    return text,mode


def validate_blanket_rejection(plan, fresh):
    if not plan or {x['id'] for x in plan} != {x['id'] for x in fresh}:
        raise RejectionError('Состав связанных задач изменился. Обновите форму перед отклонением.')
    if any(normalized_name(x['phase']) == 'выполнено' for x in fresh):
        raise RejectionError('Среди связанных задач есть выполненные. Отклонение основной требует ручной сверки в InControl.')


def read_children(flow):
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
