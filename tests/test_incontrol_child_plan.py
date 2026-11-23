import json
from copy import deepcopy
from unittest.mock import Mock
import pytest
from flask import g
from incontrol_child_plan import decisions_from_json,decisions_summary
from incontrol_rejection import RejectionError
from test_incontrol_rejection_jobs import rejection_page
from test_incontrol_fulfillment import ServiceRemote


def configure(page,monkeypatch,multiple=False):
    import incontrol_rejection_jobs as jobs
    m,c,t,u,csrf=page
    t.incontrol_task_id=None;m.db.session.commit()
    remote=ServiceRemote(mixed=True)
    monkeypatch.setattr(m.incontrol_rejections,'personal_client',lambda uid:remote.client)
    monkeypatch.setattr(jobs,'find_open_main_tasks',Mock(return_value=[{'id':10,'type_work':{'work_name':'Заявка на сервисное обслуживание'}}]+([{'id':20,'type_work':{'work_name':'Заявка на ремонт'}}] if multiple else [])))
    # Dispatch workers explicitly to make transactions deterministic in the in-memory DB.
    def submit(row,action,code=None):row.state='running';m.db.session.commit()
    monkeypatch.setattr(m.incontrol_rejections,'submit',submit)
    return remote


def run(page,action='inspect'):
    m,c,t,u,csrf=page;row=m.incontrol_rejections.model.query.filter_by(task_id=t.id).one()
    m.incontrol_rejections.slots.acquire();m.incontrol_rejections.worker(row.id,action,None)
    m.db.session.expire_all()
    return m.db.session.get(m.incontrol_rejections.model,row.id)


def plan(page,remote):
    m,c,t,u,csrf=page
    result=c.post(f'/complete_task/{t.id}/incontrol-plan',headers={'Accept':'application/json','X-CSRFToken':csrf})
    assert result.status_code==202
    row=run(page)
    assert row.state=='choose'
    choice=c.post(result.json['action_url'],data={'action':'select','remote_id':'10'},
                  headers={'Accept':'application/json','X-CSRFToken':csrf})
    assert choice.status_code==202
    row=run(page)
    assert row.state=='plan'
    assert all(call[0]=='GET' for call in remote.calls)
    assert t.incontrol_task_id is None
    return row,m.incontrol_rejections.unpack(row)


@pytest.mark.parametrize('actions',[['complete','reject'],['complete','complete'],['reject','reject']])
def test_plan_and_each_child_decision_drive_matching_remote_task(rejection_page,monkeypatch,actions):
    from datetime import timedelta
    m,c,t,u,csrf=rejection_page
    remote=configure(rejection_page,monkeypatch)
    row,p=plan(rejection_page,remote)
    decisions=[{'id':item['id'],'action':action,'work':f'Работы по задаче {item["id"]}'} for item,action in zip(p['child_plan'],actions)]
    text,mode=decisions_summary(p['child_plan'],decisions)
    form={'completed_work':text.replace('\n','\r\n'),'child_decisions':json.dumps(decisions),'action':'reject' if mode=='reject' else '',
          'mileage':'100','work_in':(m.incontrol_now()-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')}
    response=c.post(f'/complete_task/{t.id}',data=form,headers={'Accept':'application/json','X-CSRFToken':csrf})
    assert response.status_code==202
    assert t.status=='in_repair'
    row=run(rejection_page,'prepare')
    assert row.state=='done',row.message
    assert t.incontrol_task_id==10
    assert remote.tasks[11]['phase']['id']==(4 if actions[0]=='complete' else 8)
    assert remote.tasks[12]['phase']['id']==(4 if actions[1]=='complete' else 8)
    assert remote.tasks[11]['additional_fields'][0]['value']==decisions[0]['work']
    assert remote.tasks[12]['additional_fields'][0]['value'] is None
    assert remote.tasks[10]['phase']['id']==(4 if mode=='complete' else 8)
    c.post(f'/complete_task/{t.id}',data={**form,'incontrol_rejection_run':row.id},headers={'X-CSRFToken':csrf})
    assert t.status=='completed' and t.reject==(mode=='reject')
    assert t.completed_work==text


def test_multiple_parents_require_explicit_owned_selection_without_remote_writes(rejection_page,monkeypatch):
    m,c,t,u,csrf=rejection_page
    remote=configure(rejection_page,monkeypatch,multiple=True)
    response=c.post(f'/complete_task/{t.id}/incontrol-plan',headers={'Accept':'application/json','X-CSRFToken':csrf})
    row=run(rejection_page)
    assert row.state=='choose' and not remote.calls
    assert t.incontrol_task_id is None
    url=response.json['action_url']
    assert c.post(url,data={'action':'select','remote_id':'10'}).status_code==400
    assert c.post(url,data={'action':'select','remote_id':'99'},headers={'X-CSRFToken':csrf}).status_code==409
    assert c.post(url,data={'action':'select','remote_id':'10'},headers={'X-CSRFToken':csrf,'Accept':'application/json'}).status_code==202
    row=run(rejection_page)
    assert row.state=='plan'
    assert m.incontrol_rejections.unpack(row)['remote_id']==10
    assert all(call[0]=='GET' for call in remote.calls)


def test_missing_or_foreign_child_decisions_cannot_start_mutation(rejection_page,monkeypatch):
    m,c,t,u,csrf=rejection_page;remote=configure(rejection_page,monkeypatch)
    row,p=plan(rejection_page,remote)
    wrong=[{'id':999,'action':'reject','work':'Нет борта'}]
    response=c.post(f'/complete_task/{t.id}',data={'completed_work':'Нет борта','action':'reject','child_decisions':json.dumps(wrong)},headers={'X-CSRFToken':csrf})
    assert t.status=='in_repair'
    assert all(call[0]=='GET' for call in remote.calls)
    assert 'Состав связанных задач изменился' in response.text


@pytest.mark.parametrize('raw',['{}','[]','[{"id":1,"action":"complete","work":""}]','[{"id":1,"action":"x","work":"text"}]'])
def test_invalid_decision_json_is_rejected(raw):
    with pytest.raises(RejectionError):decisions_from_json(raw)


def test_each_repair_child_receives_its_own_text():
    from incontrol_rejection_jobs import drive_flow
    remote=ServiceRemote(mixed=True)
    remote.tasks[12]['additional_fields']=[{'id':501,'field_name':'Выполненные услуги','value':None}]
    remote.state['child_decisions']=[{'id':11,'action':'complete','work':'Замена фитинга'}, {'id':12,'action':'complete','work':'Замена патрубка'}]
    remote.state['reason']='Сводка двух работ'
    assert drive_flow(remote.flow())=='done'
    assert remote.tasks[11]['additional_fields'][0]['value']=='Замена фитинга'
    assert remote.tasks[12]['additional_fields'][0]['value']=='Замена патрубка'


def test_changed_links_after_plan_do_not_modify_remote_tasks(rejection_page,monkeypatch):
    from datetime import timedelta
    m,c,t,u,csrf=rejection_page;remote=configure(rejection_page,monkeypatch)
    row,p=plan(rejection_page,remote)
    decisions=[{'id':x['id'],'action':'complete','work':'Выполнена работа'} for x in p['child_plan']]
    text,mode=decisions_summary(p['child_plan'],decisions)
    remote.tasks[13]=deepcopy(remote.tasks[12]);remote.tasks[13]['id']=13
    remote.tasks[13]['custom_field_dependent_tasks']=[{'linked_task':13,'dependent_task':10}]
    remote.tasks[10]['additional_fields'][0]['value'].append(13)
    data={'completed_work':text,'child_decisions':json.dumps(decisions),'work_in':(m.incontrol_now()-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),'mileage':'100'}
    assert c.post(f'/complete_task/{t.id}',data=data,headers={'X-CSRFToken':csrf,'Accept':'application/json'}).status_code==202
    row=run(rejection_page,'prepare')
    assert row.state=='error' and t.status=='in_repair'
    assert all(call[0]=='GET' for call in remote.calls)
    assert t.incontrol_task_id is None


def test_imported_parent_binding_is_used_when_board_has_other_parent(rejection_page,monkeypatch):
    m,c,t,u,csrf=rejection_page;remote=configure(rejection_page,monkeypatch,multiple=True)
    t.incontrol_task_id=10;m.db.session.commit()
    assert c.post(f'/complete_task/{t.id}/incontrol-plan',headers={'Accept':'application/json','X-CSRFToken':csrf}).status_code==202
    row=run(rejection_page)
    assert row.state=='plan'
    assert m.incontrol_rejections.unpack(row)['remote_id']==10
    assert all(call[0]=='GET' for call in remote.calls)


@pytest.mark.parametrize('reject',[False,True])
def test_absent_remote_keeps_original_completion_and_rejection(rejection_page,monkeypatch,reject):
    from datetime import timedelta
    import incontrol_rejection_jobs as jobs
    m,c,t,u,csrf=rejection_page;remote=configure(rejection_page,monkeypatch)
    monkeypatch.setattr(jobs,'find_open_main_tasks',Mock(return_value=[]))
    assert c.post(f'/complete_task/{t.id}/incontrol-plan',headers={'X-CSRFToken':csrf,'Accept':'application/json'}).status_code==202
    row=run(rejection_page)
    assert row.state=='plan' and m.incontrol_rejections.unpack(row)['no_remote']
    assert not m.incontrol_rejections.unpack(row).get('child_plan')
    data={'completed_work':'Борт на линии' if reject else 'Выполнена диагностика','action':'reject' if reject else '',
          'mileage':'100','work_in':(m.incontrol_now()-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')}
    assert c.post(f'/complete_task/{t.id}',data=data,headers={'X-CSRFToken':csrf,'Accept':'application/json'}).status_code==202
    row=run(rejection_page,'prepare')
    assert row.state=='no_remote'
    c.post(f'/complete_task/{t.id}',data={**data,'incontrol_rejection_run':row.id},headers={'X-CSRFToken':csrf})
    assert t.status=='completed' and t.reject==reject
    assert not remote.calls


def test_blanket_rejection_uses_one_reason_and_no_child_services(rejection_page,monkeypatch):
    m,c,t,u,csrf=rejection_page;remote=configure(rejection_page,monkeypatch)
    row,p=plan(rejection_page,remote)
    data={'main_outcome':'reject','action':'reject','completed_work':'Борт на линии, в ремонт не поступал','mileage':'100'}
    assert c.post(f'/complete_task/{t.id}',data=data,headers={'X-CSRFToken':csrf,'Accept':'application/json'}).status_code==202
    row=run(rejection_page,'prepare')
    assert row.state=='done',row.message
    assert remote.tasks[10]['phase']['id']==8
    assert remote.tasks[11]['phase']['id']==remote.tasks[12]['phase']['id']==8
    for method,path,body in remote.calls:
        if method=='PATCH' and path in ('/tasks/11/','/tasks/12/'):
            assert body=={'phase_id':8}
    assert next(f['value'] for f in remote.tasks[10]['additional_fields'] if f.get('field_name')=='Причина отклонения')==data['completed_work']
    assert len([call for call in remote.calls if call[:2]==('POST','/reports/sign/')])==1
    assert 'child_decisions' not in m.incontrol_rejections.unpack(row)
    c.post(f'/complete_task/{t.id}',data={**data,'incontrol_rejection_run':row.id},headers={'X-CSRFToken':csrf})
    assert t.status=='completed' and t.reject
    assert t.completed_work==data['completed_work']


def test_parent_result_is_explicit_even_if_all_children_rejected(rejection_page,monkeypatch):
    from datetime import timedelta
    m,c,t,u,csrf=rejection_page;remote=configure(rejection_page,monkeypatch)
    row,p=plan(rejection_page,remote)
    decisions=[{'id':x['id'],'action':'reject','work':'Неисправность не подтвердилась'} for x in p['child_plan']]
    text,_=decisions_summary(p['child_plan'],decisions)
    remote.files.append({'id':101,'task':10,'file':'https://example.test/act.pdf','upload_type':'akt_to_sign','report_type':{'id':31}})
    data={'main_outcome':'complete','action':'','completed_work':text,'child_decisions':json.dumps(decisions),'mileage':'100',
          'work_in':(m.incontrol_now()-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')}
    assert c.post(f'/complete_task/{t.id}',data=data,headers={'X-CSRFToken':csrf,'Accept':'application/json'}).status_code==202
    row=run(rejection_page,'prepare')
    assert row.state=='done',row.message
    assert remote.tasks[10]['phase']['id']==4
    assert remote.tasks[11]['phase']['id']==remote.tasks[12]['phase']['id']==8
    assert m.incontrol_rejections.unpack(row)['mode']=='complete'


@pytest.mark.parametrize('invalid',['empty_reason','child_decisions','wrong_action','completed_child'])
def test_invalid_blanket_rejection_cannot_change_remote(rejection_page,monkeypatch,invalid):
    m,c,t,u,csrf=rejection_page;remote=configure(rejection_page,monkeypatch)
    row,p=plan(rejection_page,remote)
    data={'main_outcome':'reject','action':'reject','completed_work':'Борт на линии','mileage':'100'}
    if invalid=='empty_reason':data['completed_work']=''
    elif invalid=='wrong_action':data['action']=''
    elif invalid=='child_decisions':data['child_decisions']=json.dumps([{'id':11,'action':'reject','work':'Линия'}])
    else:remote.tasks[11]['phase']={'id':4,'phase_name':'Выполнено.'}
    response=c.post(f'/complete_task/{t.id}',data=data,headers={'X-CSRFToken':csrf,'Accept':'application/json'})
    if invalid=='completed_child':
        assert response.status_code==202
        row=run(rejection_page,'prepare');assert row.state=='error'
    assert t.status=='in_repair'
    assert all(call[0]=='GET' for call in remote.calls)


def test_old_signed_process_accepts_new_selector_without_changing_its_outcome(rejection_page,monkeypatch):
    from datetime import timedelta
    m,c,t,u,csrf=rejection_page
    data={'action':'','completed_work':'Диагностика','mileage':'100','work_in':(m.incontrol_now()-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')}
    assert c.post(f'/complete_task/{t.id}',data=data,headers={'X-CSRFToken':csrf,'Accept':'application/json'}).status_code==202
    row=m.incontrol_rejections.model.query.filter_by(task_id=t.id).one();row.state='done';m.db.session.commit()
    c.post(f'/complete_task/{t.id}',data={**data,'main_outcome':'reject','incontrol_rejection_run':row.id},headers={'X-CSRFToken':csrf})
    assert t.status=='in_repair'
    c.post(f'/complete_task/{t.id}',data={**data,'main_outcome':'complete','incontrol_rejection_run':row.id},headers={'X-CSRFToken':csrf})
    assert t.status=='completed' and not t.reject


def test_summary_text_does_not_block_individual_decisions_or_parent_outcome(rejection_page,monkeypatch):
    from datetime import timedelta
    m,c,t,u,csrf=rejection_page;remote=configure(rejection_page,monkeypatch)
    row,p=plan(rejection_page,remote)
    remote.files.append({'id':101,'task':10,'file':'https://example.test/act.pdf','upload_type':'akt_to_sign','report_type':{'id':31}})
    decisions=[{'id':x['id'],'action':'reject','work':'Неисправность не подтвердилась'} for x in p['child_plan']]
    form={'completed_work':'Проверка выполнена','child_decisions':json.dumps(decisions),'action':'',
          'work_in':(m.incontrol_now()-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),'mileage':'100'}
    response=c.post(f'/complete_task/{t.id}',data=form,headers={'X-CSRFToken':csrf,'Accept':'application/json'})
    assert response.status_code==202
    row=run(rejection_page,'prepare')
    assert row.state=='done',row.message
    assert remote.tasks[10]['phase']['id']==4
    assert remote.tasks[11]['phase']['id']==remote.tasks[12]['phase']['id']==8
    c.post(f'/complete_task/{t.id}',data={**form,'incontrol_rejection_run':row.id},headers={'X-CSRFToken':csrf})
    assert t.status=='completed' and t.completed_work=='Проверка выполнена'


def test_work_summary_contains_only_individual_comments():
    plan=[{'id':1,'title':'Электрика дверей','phase':'Назначено.'},{'id':2,'title':'Течь ОЖ','phase':'Назначено.'}]
    decisions=[{'id':1,'action':'complete','work':'Ремонт пинов фишки дверей'},{'id':2,'action':'reject','work':'Течь не обнаружена'}]
    text,mode=decisions_summary(plan,decisions)
    assert text=='Ремонт пинов фишки дверей\nТечь не обнаружена'
    assert mode=='complete'


def test_legacy_work_display_removes_generated_descriptions_only():
    from incontrol_child_plan import plain_completed_work
    assert plain_completed_work('Электросистема. Электрика дверей — Выполнено: Ремонт пинов фишки дверей')=='Ремонт пинов фишки дверей'
    assert plain_completed_work('Течь ОЖ — Отклонено: Течь не найдена')=='Течь не найдена'
    assert plain_completed_work('Диагностика: проверены двери\nЗаменён разъём')=='Диагностика: проверены двери\nЗаменён разъём'
