from copy import deepcopy
import json
from unittest.mock import Mock
import pytest
from incontrol_rejection import RejectionFlow, RejectionError


class Remote:
    def __init__(self, otp=False):
        self.actor='personal-user'
        obj={'additional_fields':[{'field_name':'Бортовой номер','value':'162758'}]}
        common={'organization_performer':{'id':1069},'serviced_object':obj,'read_only_fields':[], 'user_performer':None,'type_work':{'id':30}}
        self.tasks={10:{**deepcopy(common),'id':10,'phase':{'id':2,'phase_name':'Назначено.'},'additional_fields':[
            {'id':416,'field_name':'Связанные нарушения','link_type':'task','value':[11]},
            {'id':914,'field_name':'Причина отклонения','value':None}]},
            11:{**deepcopy(common),'id':11,'phase':{'id':2,'phase_name':'Назначено.'},'additional_fields':[],
                'custom_field_dependent_tasks':[{'linked_task':11,'dependent_task':10}]}}
        self.files=[{'id':100,'task':10,'file':'https://example.test/bid.pdf','upload_type':'bid_signed_by_controller','report_type':{'id':30,'report_name':'Заявка на ремонт'}}]
        self.calls=[]
        self.otp=otp
        self.timeout_after_signature=False
        self.state={'remote_id':10,'board':'162758','park':'14 парк','full_name':'Кожин О.Н.','reason':'Борт на линии'}
        self.client=Mock(interactive=True)
        self.client.session.request.side_effect=self.request

    def flow(self):
        return RejectionFlow(self.client,self.state,lambda state: None)

    def request(self, method, url, json=None, **kwargs):
        path=url.split('/api/v1')[1]
        self.calls.append((method,path,deepcopy(json)))
        result={}
        if method=='GET':
            if path=='/me/':result={'id':self.actor,'last_name':'Кожин'}
            elif path=='/signature-settings/':result={'blanks':{'bid_report_types_ids':[30],'akt_report_types_ids':[31]}}
            elif path=='/tasks/available-phases/':result={'results':[{'id':3,'phase_name':'Принято.'},{'id':8,'phase_name':'Отклонено.'}],'next':None}
            elif path.endswith('/available-report-files/'):result={'results':deepcopy(self.files)}
            else:result=deepcopy(self.tasks[int(path.split('/')[2])])
        elif method=='PATCH':
            task=self.tasks[int(path.split('/')[2])]
            if 'phase_id' in json:
                task['phase']={'id':json['phase_id'],'phase_name':{3:'Принято.',8:'Отклонено.'}[json['phase_id']]}
            if 'user_performer_id' in json:task['user_performer']={'id':json['user_performer_id']}
            if json.get('additional_fields'):
                for f in json['additional_fields']:
                    next(x for x in task['additional_fields'] if x['id']==f['id'])['value']=f['value']
            result=deepcopy(task)
        elif path=='/auth/signature/':result={} if self.otp else {'sign_code':'1234'}
        elif path=='/reports/sign/':
            target=next(f for f in self.files if f['file']==json['file_path'])
            target['upload_type']='bid_signed_by_performer' if target['report_type']['id']==30 else 'akt_signed_by_performer'
            if self.timeout_after_signature:
                self.timeout_after_signature=False
                raise TimeoutError('response lost')
        else:raise AssertionError((method,path))
        return Mock(ok=True,status_code=200,content=b'json',json=lambda:result)


def writes(remote,method,path):return [x for x in remote.calls if x[0]==method and x[1]==path]


def test_one_request_signature_and_reason_are_required_for_done():
    r=Remote()
    assert r.flow().advance()=='confirm'
    assert r.tasks[11]['phase']['id']==8
    assert r.tasks[10]['phase']['id']==3
    assert r.tasks[10]['user_performer']['id']==r.actor
    assert not writes(r,'POST','/reports/sign/')
    assert r.flow().advance('sign')=='advance'
    assert r.tasks[10]['phase']['id']==3
    assert r.flow().advance()=='done'
    assert r.tasks[10]['phase']['id']==8
    assert r.tasks[10]['additional_fields'][1]['value']=='Борт на линии'
    assert len(writes(r,'POST','/reports/sign/'))==1
    before=len(r.calls)
    assert r.flow().advance()=='done'
    assert not [x for x in r.calls[before:] if x[0]!='GET']


def test_main_rejection_forbidden_preserves_confirmed_child_and_signature(caplog):
    remote = Remote()
    assert remote.flow().advance() == 'confirm'
    assert remote.flow().advance('sign') == 'advance'

    def deny_main_rejection(method, url, json=None, **kwargs):
        if method == 'PATCH' and url.endswith('/tasks/10/') and json.get('phase_id') == 8:
            return Mock(ok=False, status_code=403, json=lambda: {'detail': 'private remote detail'})
        return remote.request(method, url, json=json, **kwargs)

    remote.client.session.request.side_effect = deny_main_rejection
    with pytest.raises(RejectionError, match='№10.*«Отклонено»') as error:
        remote.flow().advance()
    assert error.value.status_code == 403
    assert remote.tasks[10]['phase']['id'] == 3
    assert remote.tasks[11]['phase']['id'] == 8
    assert len(writes(remote, 'POST', '/reports/sign/')) == 1
    assert 'method=PATCH path=/tasks/10/' in caplog.text
    assert 'private remote detail' not in caplog.text


def test_code_is_requested_only_if_server_does_not_return_sign_code():
    r=Remote(otp=True)
    assert r.flow().advance()=='confirm'
    assert r.flow().advance('sign')=='code'
    assert not writes(r,'POST','/reports/sign/')
    # Цифры кода могут случайно встретиться в числовом timestamp.
    assert '"1234"' not in json.dumps(r.state)
    assert r.flow().advance('sign',code='5678')=='advance'
    assert len(writes(r,'POST','/auth/signature/'))==1
    assert writes(r,'POST','/reports/sign/')[0][2]['code']=='5678'
    assert '5678' not in str(r.state)


def test_lost_signature_response_does_not_repeat_signature():
    r=Remote();r.flow().advance();r.timeout_after_signature=True
    with pytest.raises(TimeoutError):r.flow().advance('sign')
    assert r.flow().advance()=='done'  # сверка подписи, затем отклонение с причиной
    assert len(writes(r,'POST','/reports/sign/'))==1


def test_changed_document_requires_new_confirmation():
    r=Remote();r.flow().advance();r.files[0]['id']=999
    assert r.flow().advance('sign')=='confirm'
    assert not writes(r,'POST','/reports/sign/')


@pytest.mark.parametrize('change', ['board','org','completed_child','reverse','reason','links'])
def test_invalid_preflight_has_no_external_writes(change):
    r=Remote()
    if change=='board':r.state['board']='000000'
    elif change=='org':r.tasks[10]['organization_performer']['id']=1060
    elif change=='completed_child':r.tasks[11]['phase']={'id':4,'phase_name':'Выполнено.'}
    elif change=='reverse':r.tasks[11]['custom_field_dependent_tasks']=[]
    elif change=='reason':r.state['reason']=''
    elif change=='links':r.state['children']=[12]
    with pytest.raises(RejectionError):r.flow().advance()
    assert not [x for x in r.calls if x[0]!='GET']


def test_preexisting_declined_main_is_not_claimed():
    r=Remote();r.tasks[10]['phase']={'id':8,'phase_name':'Отклонено.'}
    with pytest.raises(RejectionError):r.flow().advance()
    assert not [x for x in r.calls if x[0]!='GET']


def test_old_signed_bid_does_not_skip_current_unsigned_bid():
    r=Remote()
    r.files.insert(0,{'id':99,'task':10,'file':'https://example.test/old-bid.pdf','upload_type':'bid_signed_by_performer','report_type':{'id':30}})
    assert r.flow().advance()=='confirm'
    assert r.state['step']=='bid'
    assert r.tasks[10]['phase']['id']==3


def test_act_is_not_signed_in_rejection_flow():
    r=Remote()
    r.files.append({'id':50,'task':10,'file':'https://example.test/old-act.pdf','upload_type':'akt_signed_by_author','report_type':{'id':31}})
    r.flow().advance()
    r.flow().advance('sign')
    assert r.flow().advance()=='done'
    assert len(writes(r,'POST','/reports/sign/'))==1
    assert writes(r,'POST','/reports/sign/')[0][2]['file_path'].endswith('/bid.pdf')


def test_preparing_pending_email_signature_does_not_send_more_mail():
    from incontrol_rejection_jobs import drive_flow
    r=Remote(otp=True)
    assert drive_flow(r.flow())=='code'
    request=r.state['signature_request']
    assert request['outcome']=='email_requested'
    for _ in range(3):assert drive_flow(r.flow())=='code'
    assert len(writes(r,'POST','/auth/signature/'))==1
    assert not writes(r,'POST','/reports/sign/')


def test_explicit_resend_is_throttled_and_never_signs_without_code():
    import time
    from incontrol_rejection_jobs import drive_flow
    r=Remote(otp=True)
    assert drive_flow(r.flow())=='code'
    with pytest.raises(RejectionError,match='10 секунд'):drive_flow(r.flow(),'resend')
    r.state['signature_request']['at']=time.time()-11
    assert drive_flow(r.flow(),'resend')=='code'
    assert len(writes(r,'POST','/auth/signature/'))==2
    assert not writes(r,'POST','/reports/sign/')

@pytest.mark.parametrize('method,path,data',[
    ('GET','/me/',None),
    ('PATCH','/tasks/11/',{'phase_id':8}),
    ('POST','/reports/sign/',{'file_path':'https://example.test/bid.pdf'}),
])
def test_explicit_401_renews_session_once_and_applies_operation_once(method,path,data):
    r=Remote()
    denied=Mock(ok=False,status_code=401,content=b'json')
    calls=0
    def request(*args,**kwargs):
        nonlocal calls
        calls+=1
        return denied if calls==1 else r.request(*args,**kwargs)
    r.client.session.request.side_effect=request
    r.flow().request(method,path,data)
    r.client._renew.assert_called_once()
    assert calls==2
    assert len(r.calls)==1


def test_repeated_401_stops_without_retry_loop():
    r=Remote();r.client.session.request.return_value=Mock(ok=False,status_code=401)
    r.client.session.request.side_effect=None
    with pytest.raises(RejectionError,match='Не удалось восстановить'):
        r.flow().request('GET','/me/')
    assert r.client.session.request.call_count==2
    r.client._renew.assert_called_once()


def test_uncertain_signature_timeout_is_never_retried():
    r=Remote();r.client.session.request.side_effect=TimeoutError('response lost')
    with pytest.raises(TimeoutError):r.flow().request('POST','/reports/sign/',{'file_path':'test'})
    assert r.client.session.request.call_count==1
    r.client._renew.assert_not_called()
