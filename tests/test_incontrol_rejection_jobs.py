from datetime import datetime, timedelta
from unittest.mock import Mock
import re
import pytest
from flask import g
from incontrol_rejection_jobs import form_values


@pytest.fixture
def rejection_page(app_ctx,monkeypatch):
    m=app_ctx
    monkeypatch.setitem(m.application.config,'WTF_CSRF_ENABLED',True)
    user=m.User(username='reject-personal',password='unused',full_name='Кожин О.Н.',company='Сетранс',allowed_parks=['14 парк'])
    task=m.Task(board_number='162758',description='Проверка',park='14 парк',status='in_repair',tag='in_work',created_at=datetime(2026,11,1),incontrol_task_id=10)
    m.db.session.add_all([user,task]);m.db.session.commit()
    monkeypatch.setattr(m.incontrol_rejections,'enabled',lambda:True)
    monkeypatch.setitem(m.application.extensions['incontrol_accounts'],'has_credentials',lambda uid:True)
    monkeypatch.setattr(m.incontrol_rejections,'personal_client',lambda uid:Mock())
    def submit(row,action,code=None):
        row.state='running';m.db.session.commit()
    monkeypatch.setattr(m.incontrol_rejections,'submit',submit)
    client=m.application.test_client()
    with client.session_transaction() as sess:
        sess['_user_id']=str(user.id);sess['_fresh']=True
        sess['available_parks']=['14 парк'];sess['selected_park']='14 парк'
    html=client.get(f'/complete_task/{task.id}').text
    token=re.search(r'name="csrf_token" value="([^"]+)"',html)[1]
    return m,client,task,user,token


def start(page):
    m,c,t,u,token=page
    response=c.post(f'/complete_task/{t.id}',data={'action':'reject','completed_work':'Борт на линии'},headers={'X-CSRFToken':token})
    assert response.status_code==302
    row=m.incontrol_rejections.model.query.filter_by(task_id=t.id).one()
    return row


def test_reject_waits_for_remote_confirmation(rejection_page):
    m,c,t,u,token=rejection_page
    row=start(rejection_page)
    assert t.status=='in_repair'
    assert t.completed_work is None
    assert row.state=='running'
    assert 'Борт на линии' not in row.payload_encrypted
    assert c.get(f'/incontrol/rejections/{row.id}').status_code==302
    assert c.get(f'/incontrol/rejections/{row.id}/status').json['state']=='running'
    assert start(rejection_page).id==row.id
    assert m.incontrol_rejections.model.query.count()==1


def test_other_park_keeps_local_form_and_blocks_remote_plan(rejection_page, monkeypatch):
    mod, client, task, _, csrf = rejection_page
    plan = Mock()
    monkeypatch.setattr(mod.incontrol_rejections, 'personal_client', plan)
    monkeypatch.setenv('INCONTROL_CLOSURE_PARKS', '14 парк')
    task.park = '1 парк'
    mod.db.session.commit()
    with client.session_transaction() as sess:
        sess['available_parks'] = ['1 парк']
        sess['selected_park'] = '1 парк'
    path = f'/complete_task/{task.id}'
    assert 'new (CompletionCheckController)' in client.get(path).text
    assert client.post(path + '/incontrol-plan', headers={'X-CSRFToken': csrf}).status_code == 409
    plan.assert_not_called()


def test_only_done_matching_job_allows_local_rejection(rejection_page):
    m,c,t,u,token=rejection_page
    row=start(rejection_page)
    data={'action':'reject','completed_work':'Борт на линии','incontrol_rejection_run':row.id}
    c.post(f'/complete_task/{t.id}',data=data,headers={'X-CSRFToken':token})
    assert t.status=='in_repair'
    row.state='done';m.db.session.commit()
    bad={**data,'completed_work':'Другая причина'}
    c.post(f'/complete_task/{t.id}',data=bad,headers={'X-CSRFToken':token})
    assert t.status=='in_repair'
    c.post(f'/complete_task/{t.id}',data=data,headers={'X-CSRFToken':token})
    assert t.status=='completed' and t.reject is True
    assert t.completed_work=='Борт на линии'
    assert row.state=='consumed'


def test_signature_action_binds_current_document_and_csrf(rejection_page):
    m,c,t,u,token=rejection_page
    row=start(rejection_page)
    p=m.incontrol_rejections.unpack(row);p.update(step='bid',document={'id':123})
    row.payload_encrypted=m.incontrol_rejections.pack(p);row.state='confirm';m.db.session.commit()
    url=f'/incontrol/rejections/{row.id}/action'
    assert c.post(url,data={'action':'sign'}).status_code==400
    assert c.post(url,data={'action':'sign','step':'act','document_id':'123'},headers={'X-CSRFToken':token}).status_code==409
    assert c.post(url,data={'action':'sign','step':'bid','document_id':'999'},headers={'X-CSRFToken':token}).status_code==409


def test_other_user_cannot_access_run(rejection_page):
    m,c,t,u,token=rejection_page
    row=start(rejection_page)
    other=m.User(username='other-reject',password='unused',allowed_parks=['14 парк'])
    m.db.session.add(other);m.db.session.commit()
    with c.session_transaction() as sess:sess['_user_id']=str(other.id)
    g.pop('_login_user',None)
    assert c.get(f'/incontrol/rejections/{row.id}').status_code==404
    assert c.get(f'/incontrol/rejections/{row.id}/status').status_code==404


def test_stale_worker_can_be_resumed(rejection_page):
    m,c,t,u,token=rejection_page
    row=start(rejection_page);row.updated_at=datetime.utcnow()-timedelta(minutes=5);m.db.session.commit()
    assert c.get(f'/incontrol/rejections/{row.id}/status').json['state']=='error'
    assert c.get(f'/incontrol/rejections/{row.id}').status_code==302
    assert row.state=='error'


def test_disabled_integration_blocks_local_completion(rejection_page,monkeypatch):
    m,c,t,u,token=rejection_page
    monkeypatch.setattr(m.incontrol_rejections,'enabled',lambda:False)
    monkeypatch.setattr(m.completion_checks,'consume',lambda *args:'Нужна обычная проверка')
    response=c.post(f'/complete_task/{t.id}',data={'completed_work':'Ремонт'},headers={'X-CSRFToken':token})
    assert 'Проверку пропустить нельзя' in response.text
    assert m.incontrol_rejections.model.query.count()==0
    assert t.status=='in_repair'


def test_cannot_switch_started_rejection_to_completed(rejection_page,monkeypatch):
    m,c,t,u,token=rejection_page
    row=start(rejection_page)
    monkeypatch.setattr(m.completion_checks,'consume',lambda *args:None)
    response=c.post(f'/complete_task/{t.id}',data={'completed_work':'Ремонт'},headers={'X-CSRFToken':token})
    assert t.status=='in_repair'
    assert m.incontrol_rejections.unpack(row).get('mode','reject')=='reject'


def test_remote_id_is_reserved_uniquely_when_discovered(rejection_page):
    from sqlalchemy.exc import IntegrityError
    m,c,t,u,token=rejection_page
    row=start(rejection_page)
    row.remote_id=10;m.db.session.commit()
    another=m.Task(board_number='164466',description='Другой экземпляр',park='14 парк',status='in_repair',tag='in_work',created_at=datetime(2026,11,1))
    m.db.session.add(another);m.db.session.commit()
    second=m.incontrol_rejections.model(id='second-job',task_id=another.id,user_id=u.id,remote_id=10,state='running',payload_encrypted='test',updated_at=datetime.utcnow())
    m.db.session.add(second)
    with pytest.raises(IntegrityError):m.db.session.commit()
    m.db.session.rollback()
    assert m.incontrol_rejections.model.query.count()==1


def test_reject_without_bound_id_still_starts_lookup(rejection_page):
    m,c,t,u,token=rejection_page
    t.incontrol_task_id=None;m.db.session.commit()
    row=start(rejection_page)
    assert row.remote_id is None
    assert m.incontrol_rejections.unpack(row)['hint_id'] is None
    assert t.status=='in_repair'


def test_absence_result_must_be_fresh_and_bound_to_form(rejection_page):
    from incontrol_rejection_jobs import now
    m,c,t,u,token=rejection_page
    row=start(rejection_page)
    payload=m.incontrol_rejections.unpack(row)
    payload.update(no_remote=True,checked_at=now().timestamp()-100)
    row.payload_encrypted=m.incontrol_rejections.pack(payload);row.state='no_remote';m.db.session.commit()
    data={'action':'reject','completed_work':'Борт на линии','incontrol_rejection_run':row.id}
    c.post(f'/complete_task/{t.id}',data=data,headers={'X-CSRFToken':token})
    assert t.status=='in_repair'
    payload['checked_at']=now().timestamp()
    row.payload_encrypted=m.incontrol_rejections.pack(payload);m.db.session.commit()
    c.post(f'/complete_task/{t.id}',data=data,headers={'X-CSRFToken':token})
    assert t.status=='completed' and t.reject


@pytest.mark.parametrize('failure',[False,True])
def test_worker_distinguishes_absence_from_search_error(rejection_page,monkeypatch,failure):
    import incontrol_rejection_jobs as jobs
    m,c,t,u,token=rejection_page
    t.incontrol_task_id=None;m.db.session.commit()
    row=start(rejection_page)
    client=Mock();client._refresh_token=None
    client.session.headers={'Authorization':'JWT test-access'}
    monkeypatch.setattr(m.incontrol_rejections,'personal_client',lambda uid:client)
    search=Mock(side_effect=TimeoutError() if failure else None,return_value=[])
    monkeypatch.setattr(jobs,'find_open_main_tasks',search)
    flow=Mock();monkeypatch.setattr(jobs,'RejectionFlow',flow)
    m.incontrol_rejections.slots.acquire()
    m.incontrol_rejections.worker(row.id,'prepare',None)
    m.db.session.expire_all()
    current=m.db.session.get(m.incontrol_rejections.model,row.id)
    assert current.state==('unavailable' if failure else 'no_remote')
    flow.assert_not_called()
    assert t.status=='in_repair'


def test_park_switch_refreshes_stale_permissions_from_user(rejection_page):
    m,c,t,u,token=rejection_page
    u.allowed_parks=['14 парк','1 парк'];m.db.session.commit()
    response=c.post('/set_park',json={'park':'1 парк'},headers={'X-CSRFToken':token})
    assert response.json['success']
    with c.session_transaction() as sess:
        assert sess['selected_park']=='1 парк'
        assert sess['available_parks']==['14 парк','1 парк']
    assert c.post('/set_park',json={'park':'18 парк'},headers={'X-CSRFToken':token}).status_code==403


def test_completion_mode_requires_done_and_closes_without_reject(rejection_page):
    m,c,t,u,token=rejection_page
    data={'completed_work':'Долив ОЖ','action':''}
    response=c.post(f'/complete_task/{t.id}',data=data,headers={'X-CSRFToken':token})
    assert response.status_code==302
    row=m.incontrol_rejections.model.query.filter_by(task_id=t.id).one()
    assert m.incontrol_rejections.unpack(row)['mode']=='complete'
    assert t.status=='in_repair'
    row.state='done';m.db.session.commit()
    data['incontrol_rejection_run']=row.id
    c.post(f'/complete_task/{t.id}',data=data,headers={'X-CSRFToken':token})
    assert t.status=='completed' and t.reject is False
    assert t.completed_work=='Долив ОЖ'


def test_inline_start_returns_links_and_waits_for_proof(rejection_page):
    m,c,t,u,token=rejection_page
    response=c.post(f'/complete_task/{t.id}',data={'action':'reject','completed_work':'Борт на линии'},headers={'X-CSRFToken':token,'Accept':'application/json'})
    assert response.status_code==202
    row=m.incontrol_rejections.model.query.one()
    assert response.json['run_id']==row.id
    assert response.json['status_url'].endswith('/status')
    assert t.status=='in_repair'
    data=m.incontrol_rejections.unpack(row)
    data.update(step='bid',document={'id':123,'file':'private.pdf'},auth={'access':'secret'})
    row.payload_encrypted=m.incontrol_rejections.pack(data);row.state='code';m.db.session.commit()
    status=c.get(response.json['status_url'])
    assert {key:status.json[key] for key in ('state','message','step','document_id','signature_requested_at')}=={'state':'code','message':None,'step':'bid','document_id':123,'signature_requested_at':None}
    assert 'no-store' in status.headers['Cache-Control']
    invalid=c.post(response.json['action_url'],data={'action':'sign','step':'bid','document_id':'123','code':'12'},headers={'X-CSRFToken':token,'Accept':'application/json'})
    assert invalid.status_code==400
    assert row.state=='code'
    resume=c.get(f'/complete_task/{t.id}?rejection_run={row.id}').text
    assert 'InlineIncontrolController' in resume
    assert 'Борт на линии' in resume
    assert 'id="hidden_action" value="reject"' in resume


def test_auto_flow_signs_both_documents_with_cached_confirmation():
    from incontrol_rejection_jobs import drive_flow
    from test_incontrol_fulfillment import CompletedRemote
    r=CompletedRemote()
    assert drive_flow(r.flow())=='done'
    assert r.tasks[10]['phase']['id']==4
    assert len([c for c in r.calls if c[:2]==('POST','/reports/sign/')])==2


def test_auto_flow_waits_for_email_code_without_resending_it():
    from incontrol_rejection_jobs import drive_flow
    from test_incontrol_rejection import Remote
    r=Remote(otp=True)
    assert drive_flow(r.flow())=='code'
    assert not [c for c in r.calls if c[:2]==('POST','/reports/sign/')]
    assert drive_flow(r.flow(),'sign','4321')=='done'
    assert len([c for c in r.calls if c[:2]==('POST','/auth/signature/')])==1


def test_auto_flow_recovers_lost_signature_response_without_resigning():
    from incontrol_rejection_jobs import drive_flow
    from test_incontrol_rejection import Remote
    r=Remote();r.timeout_after_signature=True
    with pytest.raises(TimeoutError):drive_flow(r.flow())
    assert drive_flow(r.flow())=='done'
    assert len([c for c in r.calls if c[:2]==('POST','/reports/sign/')])==1


def test_resend_requires_current_document_csrf_and_cooldown(rejection_page):
    import time
    m,c,t,u,token=rejection_page
    row=start(rejection_page)
    data=m.incontrol_rejections.unpack(row)
    data.update(step='bid',document={'id':123},signature_request={'at':time.time()})
    row.payload_encrypted=m.incontrol_rejections.pack(data);row.state='code';m.db.session.commit()
    url=f'/incontrol/rejections/{row.id}/action'
    form={'action':'resend','step':'bid','document_id':'123'}
    headers={'Accept':'application/json','X-CSRFToken':token}
    assert c.post(url,data=form).status_code==400
    assert c.post(url,data={**form,'document_id':'124'},headers=headers).status_code==409
    assert c.post(url,data=form,headers=headers).status_code==429
    data['signature_request']['at']=time.time()-11
    row.payload_encrypted=m.incontrol_rejections.pack(data);m.db.session.commit()
    assert c.post(url,data=form,headers=headers).status_code==202
    assert row.state=='running'


@pytest.mark.parametrize('tag',['success','waiting','otm','in_work'])
def test_all_main_search_scope_is_independent_of_tag_and_schedule(rejection_page,tag):
    m,c,t,u,token=rejection_page
    t.id_to='5084';t.tag=tag;m.db.session.commit()
    row=start(rejection_page)
    assert m.incontrol_rejections.unpack(row)['lookup_scope']=='all-main-v1'
    assert t.status=='in_repair'
    p=m.incontrol_rejections.unpack(row);p.pop('lookup_scope',None);p['checked_at']=__import__('time').time()
    row.payload_encrypted=m.incontrol_rejections.pack(p);row.state='no_remote';m.db.session.commit()
    response=c.post(f'/complete_task/{t.id}',data={'action':'reject','completed_work':'Борт на линии','incontrol_rejection_run':row.id},headers={'X-CSRFToken':token})
    assert 'Прежняя проверка учитывала не все типы' in response.text
    assert t.status=='in_repair'
    replacement=start(rejection_page)
    assert replacement.id==row.id
    assert m.incontrol_rejections.unpack(replacement)['lookup_scope']=='all-main-v1'
    assert replacement.state=='running'


@pytest.mark.parametrize('multiple',[False,True])
def test_worker_checks_service_for_repair_label_and_blocks_ambiguous_board(rejection_page,monkeypatch,multiple):
    import incontrol_rejection_jobs as jobs
    m,c,t,u,token=rejection_page
    t.incontrol_task_id=None;t.id_to=None;t.tag='success';m.db.session.commit()
    row=start(rejection_page)
    client=Mock();client._refresh_token=None;client.session.headers={'Authorization':'JWT test-access'}
    monkeypatch.setattr(m.incontrol_rejections,'personal_client',lambda uid:client)
    search=Mock(return_value=[{'id':11}]+([{'id':12}] if multiple else []))
    monkeypatch.setattr(jobs,'find_open_main_tasks',search)
    flow=Mock();monkeypatch.setattr(jobs,'RejectionFlow',flow)
    monkeypatch.setattr(jobs,'read_children',lambda probe: ({'id':11},[{'id':21,'title':'Сервисная задача','phase':'Назначено.'}]))
    m.incontrol_rejections.slots.acquire();m.incontrol_rejections.worker(row.id,'prepare',None)
    m.db.session.expire_all();current=m.db.session.get(m.incontrol_rejections.model,row.id)
    search.assert_called_once_with(client,t.board_number,t.park)
    assert t.status=='in_repair'
    assert current.state == 'choose' and current.remote_id is None
    assert [x['id'] for x in m.incontrol_rejections.unpack(current)['candidates']] == ([11, 12] if multiple else [11])
    flow.assert_not_called()


@pytest.mark.parametrize('has_current',[False,True])
def test_worker_keeps_latest_personal_session_and_uses_job_only_as_fallback(rejection_page,monkeypatch,has_current):
    import time
    import incontrol_rejection_jobs as jobs
    from incontrol import IncontrolClient
    from test_incontrol_session import token
    m,c,t,u,csrf=rejection_page
    t.incontrol_task_id=None;m.db.session.commit()
    row=start(rejection_page)
    old={'access':token(time.time()+600),'refresh':'old-refresh','cookies':[]}
    current={'access':token(time.time()+900),'refresh':'current-refresh','cookies':[]}
    p=m.incontrol_rejections.unpack(row);p['auth']=old
    row.payload_encrypted=m.incontrol_rejections.pack(p);m.db.session.commit()
    client=IncontrolClient('isolated-user','isolated-password',interactive=True)
    if has_current:client._apply_tokens(current)
    monkeypatch.setattr(m.incontrol_rejections,'personal_client',lambda uid:client)
    monkeypatch.setattr(jobs,'find_open_main_tasks',Mock(return_value=[]))
    m.incontrol_rejections.slots.acquire();m.incontrol_rejections.worker(row.id,'prepare',None)
    m.db.session.expire_all();saved=m.db.session.get(m.incontrol_rejections.model,row.id)
    assert saved.state=='no_remote'
    auth=m.incontrol_rejections.unpack(saved)['auth']
    expected=current if has_current else old
    assert auth['access']==expected['access'] and auth['refresh']==expected['refresh']

@pytest.mark.parametrize('phase,stage,found,expected',[
    ('Принято.','in_work',[{'id':11}],'plan'),
    ('Принято.','denied',[{'id':11}],'plan'),
    ('Выполнено.','completed',[{'id':11}],'plan'),
    ('Отклонено.','denied',[],'plan'),
])
def test_missing_bound_request_is_checked_directly_before_replacement(rejection_page,monkeypatch,phase,stage,found,expected):
    import incontrol_rejection_jobs as jobs
    m,c,t,u,token=rejection_page
    row=start(rejection_page)
    client=Mock();client._refresh_token=None;client.session.headers={'Authorization':'JWT test-access'}
    monkeypatch.setattr(m.incontrol_rejections,'personal_client',lambda uid:client)
    monkeypatch.setattr(jobs,'find_open_main_tasks',lambda *args:list(found))
    flow=Mock();flow.return_value.task.return_value={'id':10,'phase':{'phase_name':phase},'stage':stage,'type_work':{'work_name':'Заявка на ремонт'}}
    monkeypatch.setattr(jobs,'RejectionFlow',flow)
    monkeypatch.setattr(jobs,'read_children',lambda probe: ({'id':10},[{'id':21,'title':'Проверка','phase':'Назначено.'}]))
    m.incontrol_rejections.slots.acquire();m.incontrol_rejections.worker(row.id,'inspect',None)
    m.db.session.expire_all();current=m.db.session.get(m.incontrol_rejections.model,row.id)
    assert current.state==expected
    flow.return_value.task.assert_called_once_with(10)
    payload=m.incontrol_rejections.unpack(current)
    if stage in ('denied','completed'):
        assert payload['no_remote'] and payload['remote_closed']==10
        assert payload['child_plan']==[]
        assert payload.get('remote_id') is None
    else:
        assert not payload.get('no_remote')
        assert payload['remote_id']==10
    assert m.db.session.get(m.Task,t.id).status=='in_repair'


def test_already_closed_bound_request_can_be_closed_locally_without_touching_new_request(rejection_page,monkeypatch):
    import incontrol_rejection_jobs as jobs
    m,c,t,u,token=rejection_page
    row=start(rejection_page)
    client=Mock();client._refresh_token=None;client.session.headers={'Authorization':'JWT test-access'}
    monkeypatch.setattr(m.incontrol_rejections,'personal_client',lambda uid:client)
    monkeypatch.setattr(jobs,'find_open_main_tasks',lambda *args:[{'id':11}])
    flow=Mock();flow.return_value.task.return_value={'id':10,'phase':{'phase_name':'Принято.'},'stage':'denied','type_work':{'work_name':'Заявка на ремонт'}}
    monkeypatch.setattr(jobs,'RejectionFlow',flow)
    m.incontrol_rejections.slots.acquire();m.incontrol_rejections.worker(row.id,'prepare',None)
    m.db.session.expire_all()
    row=m.db.session.get(m.incontrol_rejections.model,row.id)
    assert row.state=='no_remote'
    flow.return_value.advance.assert_not_called()
    response=c.post(f'/complete_task/{t.id}',data={'action':'reject','completed_work':'Борт на линии','incontrol_rejection_run':row.id},headers={'X-CSRFToken':token})
    assert response.status_code==302
    m.db.session.expire_all();task=m.db.session.get(m.Task,t.id)
    assert task.status=='completed' and task.incontrol_task_id==10
    assert task.completed_work=='Борт на линии'


@pytest.mark.parametrize('outcome',['ready','blocked','unavailable'])
@pytest.mark.parametrize('action',['','reject'])
def test_no_personal_credentials_uses_shared_readonly_check(rejection_page,monkeypatch,outcome,action):
    from datetime import timedelta
    m,c,t,u,csrf=rejection_page
    monkeypatch.setitem(m.application.extensions['incontrol_accounts'],'has_credentials',lambda uid:False)
    monkeypatch.setattr(m.completion_checks,'enabled',lambda:True)
    monkeypatch.setattr(m.completion_checks,'_submit',lambda check_id:True)
    shared=Mock(return_value='Заявка на ремонт' if outcome=='blocked' else None)
    if outcome=='unavailable':shared.side_effect=TimeoutError()
    monkeypatch.setattr(m.completion_checks,'check_board',shared)
    personal=Mock(side_effect=AssertionError('Personal API must not be used'))
    monkeypatch.setattr(m.incontrol_rejections,'personal_client',personal)
    html=c.get(f'/complete_task/{t.id}').text
    assert 'new (CompletionCheckController)' in html
    data={'completed_work':'Диагностика выполнена','action':action,'mileage':'100',
          'work_in':(m.incontrol_now()-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')}
    c.post(f'/complete_task/{t.id}',data=data,headers={'X-CSRFToken':csrf})
    assert t.status=='in_repair'
    response=c.post(f'/complete_task/{t.id}/incontrol-check',headers={'X-CSRFToken':csrf})
    assert response.status_code==202
    check_id=response.json['check_id']
    m.completion_checks._run(check_id)
    m.db.session.expire_all()
    check=m.db.session.get(m.completion_checks.model,check_id)
    assert check.state==outcome
    if outcome=='blocked':assert 'в InControl ещё открыта' in check.message
    c.post(f'/complete_task/{t.id}',data={**data,'completion_check_id':check_id},headers={'X-CSRFToken':csrf})
    m.db.session.expire_all()
    assert m.db.session.get(m.Task,t.id).status==('completed' if outcome=='ready' else 'in_repair')
    personal.assert_not_called()
    assert m.incontrol_rejections.model.query.count()==0
