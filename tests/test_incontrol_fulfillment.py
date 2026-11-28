from unittest.mock import Mock
import pytest
from test_incontrol_rejection import Remote
from incontrol import FulfillmentFlow
from incontrol import RejectionError


class CompletedRemote(Remote):
    def __init__(self):
        super().__init__()
        self.state['mode']='complete'
        self.tasks[10]['additional_fields'].append({'id':1108,'field_name':'Комментарий','value':None})
        self.tasks[11]['additional_fields']=[{'id':501,'field_name':'Выполненные услуги','value':None}]
    def request(self,method,url,json=None,**kwargs):
        path=url.split('/api/v1')[1]
        if method=='PATCH' and json.get('phase_id')==4:
            self.calls.append((method,path,json))
            task=self.tasks[int(path.split('/')[2])]
            task['phase']={'id':4,'phase_name':'Выполнено.'}
            if task['id']==11:
                task['additional_fields'][0]['value']=json['additional_fields'][0]['value']
                self.files.append({'id':101,'task':10,'file':'https://example.test/act.pdf','upload_type':'akt_to_sign','report_type':{'id':31}})
            else:
                assert any(f['upload_type']=='akt_signed_by_performer' for f in self.files), 'основную нельзя выполнить до подписи акта'
            return Mock(ok=True,status_code=200,content=b'json',json=lambda:task)
        result=super().request(method,url,json=json,**kwargs)
        if path=='/tasks/available-phases/':result.json()['results'].append({'id':4,'phase_name':'Выполнено.'})
        return result
    def flow(self):return FulfillmentFlow(self.client,self.state,lambda x:None)


def test_fulfillment_requires_services_and_both_signatures():
    r=CompletedRemote()
    assert r.flow().advance()=='confirm'
    assert r.tasks[11]['phase']['id']==4
    assert r.tasks[11]['additional_fields'][0]['value']=='Борт на линии'
    assert r.flow().advance('sign')=='advance'
    assert r.flow().advance()=='confirm'
    assert r.state['step']=='act' and r.tasks[10]['phase']['id']==3
    assert r.flow().advance('sign')=='advance'
    assert r.flow().advance()=='done'
    assert len([x for x in r.calls if x[:2]==('POST','/reports/sign/')])==2


def test_declined_child_blocks_completion():
    r=CompletedRemote();r.tasks[11]['phase']['phase_name']='Отклонено.'
    with pytest.raises(RejectionError):r.flow().advance()
    assert not [x for x in r.calls if x[0]!='GET']


def test_act_created_by_completion_attempt_with_scenario_403():
    r=CompletedRemote()
    original=r.client.session.request.side_effect
    def request(method,url,json=None,**kwargs):
        if method=='PATCH' and url.endswith('/tasks/10/') and json.get('phase_id')==4 and not any(f['report_type']['id']==31 for f in r.files):
            r.calls.append((method,'/tasks/10/',json))
            r.files.append({'id':101,'task':10,'file':'https://example.test/act.pdf','upload_type':'akt_to_sign','report_type':{'id':31}})
            return Mock(ok=False,status_code=403,content=b'json',json=lambda:{'code':'permission_denied','detail':'Выполнение запрещено, акт не подписан!'})
        return original(method,url,json=json,**kwargs)
    r.client.session.request.side_effect=request
    r.flow().advance()
    r.files=[f for f in r.files if f['report_type']['id']!=31]
    r.flow().advance('sign')
    assert r.flow().advance()=='confirm'
    assert r.state['step']=='act'
    assert r.tasks[10]['phase']['id']==3
    assert r.flow().advance('sign')=='advance'
    assert r.flow().advance()=='done'


class ServiceRemote(CompletedRemote):
    def __init__(self,mixed=False):
        from copy import deepcopy
        super().__init__()
        self.tasks[10]['type_work']={'id':12603,'work_name':'Заявка на сервисное обслуживание'}
        if mixed:
            self.tasks[12]=deepcopy(self.tasks[11])
            self.tasks[12]['id']=12
            self.tasks[12]['custom_field_dependent_tasks']=[{'linked_task':12,'dependent_task':10}]
            self.tasks[10]['additional_fields'][0]['value']=[11,12]
            target=self.tasks[12]
        else:target=self.tasks[11]
        target['type_work']={'id':13182,'work_name':'ТО-1 НЕФАЗ 5299'}
        target['additional_fields']=[{'id':444,'field_name':'Чек-лист ТО-1 НЕФАЗ 5299','value':None}]
    def request(self,method,url,json=None,**kwargs):
        path=url.split('/api/v1')[1]
        if method=='PATCH' and json.get('phase_id')==4 and path!='/tasks/10/':
            task=self.tasks[int(path.split('/')[2])]
            self.calls.append((method,path,json))
            task['phase']={'id':4,'phase_name':'Выполнено.'}
            for field in json.get('additional_fields',[]):
                next(f for f in task['additional_fields'] if f['id']==field['id'])['value']=field['value']
            if not any(f['report_type']['id']==31 for f in self.files):
                self.files.append({'id':101,'task':10,'file':'https://example.test/act.pdf','upload_type':'akt_to_sign','report_type':{'id':31}})
            return Mock(ok=True,status_code=200,content=b'json',json=lambda:task)
        return super().request(method,url,json=json,**kwargs)


@pytest.mark.parametrize('mixed',[False,True])
def test_service_children_use_phase_only_and_optional_repair_services(mixed):
    from incontrol import drive_flow
    r=ServiceRemote(mixed=mixed)
    assert drive_flow(r.flow())=='done'
    service_id=12 if mixed else 11
    patches=[body for method,path,body in r.calls if method=='PATCH' and path==f'/tasks/{service_id}/']
    assert patches==[{'phase_id':4}]
    assert r.tasks[service_id]['additional_fields'][0]['value'] is None
    if mixed:
        assert r.tasks[11]['additional_fields'][0]['value']==r.state['reason']
    assert r.tasks[10]['phase']['id']==4
    assert len([c for c in r.calls if c[:2]==('POST','/reports/sign/')])==2
    assert drive_flow(r.flow())=='done'
    assert len([c for c in r.calls if c[:2]==('POST','/reports/sign/')])==2


def test_already_completed_service_child_is_not_modified():
    r=ServiceRemote();r.tasks[11]['phase']={'id':4,'phase_name':'Выполнено.'}
    assert r.flow().advance()=='confirm'
    assert not [c for c in r.calls if c[:2]==('PATCH','/tasks/11/')]


def test_repair_parent_still_requires_services_field():
    r=ServiceRemote();r.tasks[10]['type_work']={'id':30,'work_name':'Заявка на ремонт'}
    with pytest.raises(RejectionError,match='поле выполненных услуг'):r.flow().advance()
    assert not [c for c in r.calls if c[0]!='GET']


def test_service_phase_permission_checked_before_any_child_changes():
    r=ServiceRemote(mixed=True);r.tasks[12]['read_only_fields']=['phase']
    with pytest.raises(RejectionError):r.flow().advance()
    assert not [c for c in r.calls if c[0]!='GET']


def test_service_does_not_skip_disabled_or_duplicate_services_fields():
    for fields in ([{'id':501,'field_name':'Выполненные услуги','is_disabled':True}],
                   [{'id':501,'field_name':'Выполненные услуги'},{'id':502,'field_name':'Выполненные услуги'}]):
        r=ServiceRemote();r.tasks[11]['additional_fields']=fields
        with pytest.raises(RejectionError):r.flow().advance()
        assert not [c for c in r.calls if c[0]!='GET']


@pytest.mark.parametrize('mode',['complete','reject'])
def test_closing_reassigns_other_performer_before_child_updates(mode):
    from test_incontrol_rejection import Remote
    from incontrol import drive_flow
    r=CompletedRemote() if mode=='complete' else Remote()
    r.tasks[10]['user_performer']={'id':'previous-user'}
    assert drive_flow(r.flow())=='done'
    writes=[c for c in r.calls if c[0]!='GET']
    assert writes[0]==('PATCH','/tasks/10/',{'user_performer_id':r.actor,'phase_id':3})
    assert r.tasks[10]['user_performer']['id']==r.actor
    assert r.state['performer_reassigned']


@pytest.mark.parametrize('failure',['readonly','denied','not_applied'])
def test_failed_performer_change_keeps_children_untouched(failure):
    r=CompletedRemote();r.tasks[10]['user_performer']={'id':'previous-user'}
    if failure=='readonly':r.tasks[10]['read_only_fields']=['user_performer']
    else:
        original=r.client.session.request.side_effect
        def request(method,url,json=None,**kwargs):
            if method=='PATCH' and url.endswith('/tasks/10/') and json.get('user_performer_id')==r.actor:
                r.calls.append((method,'/tasks/10/',json))
                if failure=='denied':return Mock(ok=False,status_code=403,content=b'json',json=lambda:{})
                return Mock(ok=True,status_code=200,content=b'json',json=lambda:r.tasks[10])
            return original(method,url,json=json,**kwargs)
        r.client.session.request.side_effect=request
    with pytest.raises(RejectionError):r.flow().advance()
    assert r.tasks[11]['phase']['id']==2
    assert not [c for c in r.calls if c[0]=='POST']


@pytest.mark.parametrize('mode',['complete','reject'])
@pytest.mark.parametrize('previous',[None,'previous-user'])
def test_current_closer_is_assigned_even_when_bid_is_already_signed(mode,previous):
    from test_incontrol_rejection import Remote
    from incontrol import drive_flow
    r=CompletedRemote() if mode=='complete' else Remote()
    r.tasks[10]['user_performer']={'id':previous} if previous else None
    r.files[0]['upload_type']='bid_signed_by_performer'
    assert drive_flow(r.flow())=='done'
    assert r.tasks[10]['user_performer']['id']==r.actor
    writes=[c for c in r.calls if c[0]!='GET']
    assert writes[0]==('PATCH','/tasks/10/',{'user_performer_id':r.actor,'phase_id':3})
    assert not [c for c in writes if c[0]=='DELETE']


def test_accepted_request_changes_only_performer_without_repeating_phase():
    r=CompletedRemote();r.tasks[10]['phase']={'id':3,'phase_name':'Принято.'}
    r.tasks[10]['user_performer']={'id':'previous-user'}
    flow=r.flow();flow.identify()
    flow.assign_closing_performer(r.tasks[10])
    writes=[c for c in r.calls if c[0]!='GET']
    assert writes==[('PATCH','/tasks/10/',{'user_performer_id':r.actor})]

@pytest.mark.parametrize('field_name',['Выполнение услуги','Выполненные услуги'])
@pytest.mark.parametrize('parent_mode',['complete','reject'])
def test_rejected_child_receives_individual_comment(field_name,parent_mode):
    from test_incontrol_rejection import Remote
    from incontrol import drive_flow
    r=CompletedRemote() if parent_mode=='complete' else Remote()
    r.tasks[11]['additional_fields']=[{'id':501,'field_name':field_name,'value':None}]
    r.state['child_decisions']=[{'id':11,'action':'reject','work':'Неисправность не подтвердилась'}]
    if parent_mode=='complete':
        r.files.append({'id':101,'task':10,'file':'https://example.test/act.pdf','upload_type':'akt_to_sign','report_type':{'id':31}})
    assert drive_flow(r.flow())=='done'
    assert r.tasks[11]['phase']['id']==8
    assert r.tasks[11]['additional_fields'][0]['value']=='Неисправность не подтвердилась'
    child_writes=[c for c in r.calls if c[:2]==('PATCH','/tasks/11/')]
    assert len(child_writes)==1
    assert child_writes[0][2]['additional_fields']==[{'id':501,'value':'Неисправность не подтвердилась'}]


def test_blanket_rejection_does_not_copy_common_reason_to_child_services():
    from test_incontrol_rejection import Remote
    from incontrol import drive_flow
    r=Remote();r.tasks[11]['additional_fields']=[{'id':501,'field_name':'Выполнение услуги','value':None}]
    assert drive_flow(r.flow())=='done'
    assert r.tasks[11]['additional_fields'][0]['value'] is None


@pytest.mark.parametrize('parent_mode',['complete','reject'])
def test_rejected_child_disabled_services_prevent_all_mutations(parent_mode):
    from test_incontrol_rejection import Remote
    r=CompletedRemote() if parent_mode=='complete' else Remote()
    r.tasks[11]['additional_fields']=[{'id':501,'field_name':'Выполнение услуги','is_disabled':True}]
    r.state['child_decisions']=[{'id':11,'action':'reject','work':'Нет неисправности'}]
    with pytest.raises(RejectionError):r.flow().advance()
    assert not [c for c in r.calls if c[0]!='GET']


def main_comment(remote):
    return next(f['value'] for f in remote.tasks[10]['additional_fields'] if f['id']==1108)


def comment_writes(remote):
    return [call for call in remote.calls if call[:2]==('PATCH','/tasks/10/')
            and any(f['id']==1108 for f in call[2].get('additional_fields',[]))]


def test_service_text_is_copied_exactly_before_main_completion_attempt():
    from incontrol import drive_flow
    r=CompletedRemote();r.state['reason']='Долив ОЖ    Готов 15.11.2026 в 20-56'
    assert drive_flow(r.flow())=='done'
    assert main_comment(r)==r.state['reason']
    writes=comment_writes(r)
    assert len(writes)==1
    assert r.calls.index(writes[0]) < next(i for i,c in enumerate(r.calls) if c[:2]==('PATCH','/tasks/10/') and c[2].get('phase_id')==4)
    assert r.calls.index(writes[0]) < next(i for i,c in enumerate(r.calls) if c[:2]==('POST','/reports/sign/'))


def test_multiple_children_include_rejected_services_on_separate_lines_without_deduplication():
    from copy import deepcopy
    from incontrol import drive_flow
    r=ServiceRemote(mixed=True)
    r.tasks[12]['additional_fields']=[{'id':502,'field_name':'Выполнение услуги','value':None}]
    r.tasks[13]=deepcopy(r.tasks[11]);r.tasks[13]['id']=13
    r.tasks[13]['custom_field_dependent_tasks']=[{'linked_task':13,'dependent_task':10}]
    r.tasks[10]['additional_fields'][0]['value']=[13,11,12]
    r.state['child_decisions']=[{'id':11,'action':'complete','work':'Долив ОЖ'},
                              {'id':12,'action':'reject','work':'Неисправности нет'},
                              {'id':13,'action':'complete','work':'Долив ОЖ'}]
    assert drive_flow(r.flow())=='done'
    assert main_comment(r)=='- Долив ОЖ\n- Неисправности нет\n- Долив ОЖ'


def test_comment_uses_actual_already_rejected_child_services():
    from incontrol import drive_flow
    r=CompletedRemote();r.tasks[11]['phase']={'id':8,'phase_name':'Отклонено.'}
    r.tasks[11]['additional_fields'][0]['value']='Ранее проверено, неисправности нет'
    r.state['child_decisions']=[{'id':11,'action':'reject','work':'Этот текст в дочернюю не записывался'}]
    r.files.append({'id':101,'task':10,'file':'https://example.test/act.pdf','upload_type':'akt_to_sign','report_type':{'id':31}})
    assert drive_flow(r.flow())=='done'
    assert main_comment(r)=='Ранее проверено, неисправности нет'
    assert not [c for c in r.calls if c[:2]==('PATCH','/tasks/11/')]


@pytest.mark.parametrize('failure',['missing','duplicate','disabled','readonly'])
def test_comment_permission_is_checked_before_any_mutation(failure):
    r=CompletedRemote();field=r.tasks[10]['additional_fields'][-1]
    if failure=='missing':r.tasks[10]['additional_fields'].pop()
    elif failure=='duplicate':r.tasks[10]['additional_fields'].append({**field,'id':1109})
    elif failure=='disabled':field['is_disabled']=True
    else:r.tasks[10]['read_only_fields']=['additional_fields']
    with pytest.raises(RejectionError,match='комментар|Комментарий'):r.flow().advance()
    assert not [c for c in r.calls if c[0]!='GET']


def test_comment_timeout_preserves_existing_text_and_does_not_duplicate_on_resume():
    from incontrol import drive_flow
    r=CompletedRemote();r.state['reason']='Замена разъёма'
    r.tasks[10]['additional_fields'][-1]['value']='Осмотр при приёмке'
    original=r.client.session.request.side_effect
    lost=True
    def request(method,url,json=None,**kwargs):
        nonlocal lost
        response=original(method,url,json=json,**kwargs)
        if lost and method=='PATCH' and url.endswith('/tasks/10/') and any(f['id']==1108 for f in (json or {}).get('additional_fields',[])):
            lost=False
            raise TimeoutError('comment response lost')
        return response
    r.client.session.request.side_effect=request
    with pytest.raises(TimeoutError):drive_flow(r.flow())
    assert main_comment(r)=='Осмотр при приёмке\nЗамена разъёма'
    assert r.tasks[10]['phase']['id']!=4
    assert drive_flow(r.flow())=='done'
    assert main_comment(r)=='Осмотр при приёмке\nЗамена разъёма'
    assert len(comment_writes(r))==1


@pytest.mark.parametrize('failure',['denied','not_applied'])
def test_comment_failure_prevents_main_completion(failure):
    from incontrol import drive_flow
    r=CompletedRemote();original=r.client.session.request.side_effect
    def request(method,url,json=None,**kwargs):
        if method=='PATCH' and url.endswith('/tasks/10/') and any(f['id']==1108 for f in (json or {}).get('additional_fields',[])):
            return Mock(ok=failure!='denied',status_code=403 if failure=='denied' else 200,content=b'json',json=lambda:{})
        return original(method,url,json=json,**kwargs)
    r.client.session.request.side_effect=request
    with pytest.raises(RejectionError):drive_flow(r.flow())
    assert r.tasks[10]['phase']['id']!=4
    assert not [c for c in r.calls if c[:2]==('POST','/reports/sign/')]


def test_comment_added_during_child_update_is_preserved():
    from incontrol import drive_flow
    r=CompletedRemote();original=r.client.session.request.side_effect
    def request(method,url,json=None,**kwargs):
        response=original(method,url,json=json,**kwargs)
        if method=='PATCH' and url.endswith('/tasks/11/'):
            r.tasks[10]['additional_fields'][-1]['value']='Добавлено вручную во время ремонта'
        return response
    r.client.session.request.side_effect=request
    assert drive_flow(r.flow())=='done'
    assert main_comment(r)=='Добавлено вручную во время ремонта\n'+r.state['reason']


def test_empty_or_missing_services_do_not_replace_main_comment():
    from incontrol import drive_flow
    r=ServiceRemote();r.tasks[10]['additional_fields'][-1]['value']='Ручное примечание'
    assert drive_flow(r.flow())=='done'
    assert main_comment(r)=='Ручное примечание'
    assert comment_writes(r)==[]


def test_completed_process_is_not_backfilled_on_resume():
    from incontrol import drive_flow
    r=CompletedRemote()
    assert drive_flow(r.flow())=='done'
    r.tasks[10]['additional_fields'][-1]['value']=None
    r.calls.clear()
    assert drive_flow(r.flow())=='done'
    assert main_comment(r) is None
    assert not [c for c in r.calls if c[0]!='GET']


class LineRepairRemote(ServiceRemote):
    def __init__(self,multiple=False):
        super().__init__(mixed=multiple)
        self.tasks[10]['type_work']={'id':14022,'work_name':'Заявка на ремонт с линии'}
        self.tasks[10]['additional_fields']=[f for f in self.tasks[10]['additional_fields'] if f['id']!=1108]
        self.tasks[11]['additional_fields']=[{'id':501,'field_name':'Выполненные услуги','value':None}]
        if multiple:self.tasks[12]['additional_fields']=[{'id':502,'field_name':'Выполнение услуги','value':None}]
        self.comments=[];self.comment_pages=[];self.hide_comments=False
        self.state['reason']='Долив ОЖ'

    def request(self,method,url,json=None,**kwargs):
        from copy import deepcopy
        path=url.split('/api/v1')[1]
        if method=='GET' and path=='/tasks/10/comments/':
            self.calls.append((method,path,deepcopy(json)))
            page=kwargs['params']['page'];size=kwargs['params']['per_page'];self.comment_pages.append(page)
            items=[] if self.hide_comments else self.comments
            data={'results':deepcopy(items[(page-1)*size:page*size]),'next':f'page={page+1}' if page*size<len(items) else None}
            return Mock(ok=True,status_code=200,content=b'json',json=lambda:data)
        if method=='POST' and path=='/tasks-comments/':
            self.calls.append((method,path,deepcopy(json)))
            assert set(json)=={'task','comment'} and json['task']==10
            data={'id':len(self.comments)+1,'task':json['task'],'comment':json['comment']}
            self.comments.append(data)
            return Mock(ok=True,status_code=201,content=b'json',json=lambda:deepcopy(data))
        return super().request(method,url,json=json,**kwargs)


def test_line_repair_posts_services_to_thread_before_main_completion():
    from incontrol import drive_flow
    r=LineRepairRemote()
    assert drive_flow(r.flow())=='done'
    assert [c['comment'] for c in r.comments]==['Долив ОЖ']
    posts=[c for c in r.calls if c[:2]==('POST','/tasks-comments/')]
    assert len(posts)==1
    assert r.calls.index(posts[0])<next(i for i,c in enumerate(r.calls) if c[:2]==('PATCH','/tasks/10/') and c[2].get('phase_id')==4)
    assert r.state['services_thread_comment_id']==1
    assert comment_writes(r)==[]


def test_line_repair_preserves_other_comments_and_collects_rejected_services():
    from incontrol import drive_flow
    r=LineRepairRemote(multiple=True)
    existing={'id':1,'task':10,'comment':'Комментарий при создании'};r.comments.append(existing.copy())
    r.state['child_decisions']=[{'id':11,'action':'complete','work':'Долив ОЖ'}, {'id':12,'action':'reject','work':'Неисправности нет'}]
    assert drive_flow(r.flow())=='done'
    assert r.comments[0]==existing
    assert r.comments[1]['comment']=='- Долив ОЖ\n- Неисправности нет'


def test_line_repair_checks_all_comment_pages_before_posting():
    from incontrol import drive_flow
    r=LineRepairRemote();r.comments=[{'id':i+1,'task':10,'comment':f'Запись {i}'} for i in range(100)]
    r.comments.append({'id':101,'task':10,'comment':'Долив ОЖ'})
    assert drive_flow(r.flow())=='done'
    assert 2 in r.comment_pages
    assert not [c for c in r.calls if c[:2]==('POST','/tasks-comments/')]
    assert r.state['services_thread_comment_id']==101


def test_line_repair_lost_post_response_is_recovered_without_duplicate():
    from incontrol import drive_flow
    r=LineRepairRemote();original=r.client.session.request.side_effect;lost=True
    def request(method,url,json=None,**kwargs):
        nonlocal lost
        result=original(method,url,json=json,**kwargs)
        if lost and method=='POST' and url.endswith('/tasks-comments/'):
            lost=False;raise TimeoutError('response lost after post')
        return result
    r.client.session.request.side_effect=request
    with pytest.raises(TimeoutError):drive_flow(r.flow())
    assert len(r.comments)==1 and r.tasks[10]['phase']['id']!=4
    assert drive_flow(r.flow())=='done'
    assert len(r.comments)==1
    assert len([c for c in r.calls if c[:2]==('POST','/tasks-comments/')])==1


def test_line_repair_uncertain_post_without_visible_comment_is_not_repeated():
    from incontrol import drive_flow
    r=LineRepairRemote();original=r.client.session.request.side_effect;posts=0
    def request(method,url,json=None,**kwargs):
        nonlocal posts
        if method=='POST' and url.endswith('/tasks-comments/'):
            posts+=1;raise TimeoutError('unknown post outcome')
        return original(method,url,json=json,**kwargs)
    r.client.session.request.side_effect=request
    with pytest.raises(TimeoutError):drive_flow(r.flow())
    with pytest.raises(RejectionError,match='сохранение пока не подтверждено'):drive_flow(r.flow())
    assert posts==1 and r.tasks[10]['phase']['id']!=4


def test_line_repair_waits_for_comment_visibility_and_does_not_repeat_post():
    from incontrol import drive_flow
    r=LineRepairRemote();r.hide_comments=True
    with pytest.raises(RejectionError,match='пока не подтвердил'):drive_flow(r.flow())
    assert len(r.comments)==1 and r.tasks[10]['phase']['id']!=4
    with pytest.raises(RejectionError,match='сохранение пока не подтверждено'):drive_flow(r.flow())
    r.hide_comments=False
    assert drive_flow(r.flow())=='done'
    assert len(r.comments)==1


def test_line_repair_definite_post_denial_can_be_retried_after_permission_fix():
    from incontrol import drive_flow
    r=LineRepairRemote();original=r.client.session.request.side_effect
    def request(method,url,json=None,**kwargs):
        if method=='POST' and url.endswith('/tasks-comments/'):
            return Mock(ok=False,status_code=403,content=b'json',json=lambda:{})
        return original(method,url,json=json,**kwargs)
    r.client.session.request.side_effect=request
    with pytest.raises(RejectionError):drive_flow(r.flow())
    assert not r.state['services_thread_post_attempted']
    assert not r.comments and r.tasks[10]['phase']['id']!=4
    r.client.session.request.side_effect=original
    assert drive_flow(r.flow())=='done'
    assert len(r.comments)==1


@pytest.mark.parametrize('data',[{}, {'results':[]}, {'results':[{'id':1,'task':99,'comment':'Чужая запись'}],'next':None}])
def test_bad_comment_list_cannot_be_treated_as_empty(data):
    from incontrol import drive_flow
    r=LineRepairRemote();original=r.client.session.request.side_effect
    def request(method,url,json=None,**kwargs):
        if method=='GET' and url.endswith('/comments/'):
            return Mock(ok=True,status_code=200,content=b'json',json=lambda:data)
        return original(method,url,json=json,**kwargs)
    r.client.session.request.side_effect=request
    with pytest.raises(RejectionError):drive_flow(r.flow())
    assert not r.comments and r.tasks[10]['phase']['id']!=4
    assert not [c for c in r.calls if c[:2]==('POST','/reports/sign/')]


def test_line_repair_with_actual_comment_field_keeps_using_field():
    from incontrol import drive_flow
    r=LineRepairRemote();r.tasks[10]['additional_fields'].append({'id':1108,'field_name':'Комментарий','value':None})
    assert drive_flow(r.flow())=='done'
    assert main_comment(r)=='Долив ОЖ' and not r.comments
