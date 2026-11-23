"""Выполнение: услуги в связанных задачах, подпись заявки, выполнение и подпись акта."""
import hashlib

from incontrol_rejection import RejectionFlow, RejectionError, ActSignatureRequired, service_fields
from incontrol_completion import normalized_name, is_service_request


class FulfillmentFlow(RejectionFlow):
    def validate(self):
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
        names={'bid':{'bid_signed_by_controller','bid_signed_by_performer','bid_signed_by_customer'},
               'act':{'akt_to_sign','akt_signed_by_performer','akt_signed_by_controller','akt_signed_by_author'}}[kind]
        baseline=self.state.get('act_baseline',[]) if kind=='act' else []
        found=[f for f in files if f.get('upload_type') in names and (f.get('report_type') or {}).get('id') in self.state['report_types'][kind]
               and not any(x['id']==f['id'] and x['file']==f['file'] for x in baseline)]
        if len({f['report_type']['id'] for f in found})>1:raise RejectionError('Найдено несколько типов документов для одного шага.')
        return max(found,key=lambda f:(f.get('timestamp') or 0,f.get('id') or 0)) if found else None

    def signed(self,files,kind):
        d=self.current_document(files,kind)
        return bool(d and d.get('upload_type') in {'bid_signed_by_performer','bid_signed_by_customer','akt_signed_by_performer','akt_signed_by_controller','akt_signed_by_author'})

    def pending_document(self,files,kind):
        d=self.current_document(files,kind)
        if not d or d.get('upload_type') not in {'bid_signed_by_controller','akt_to_sign'} or not d.get('file'):
            raise RejectionError('Актуальный документ для подписи пока не готов. Повторите проверку.')
        return d

    def advance(self,action='prepare',code=None):
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
