from copy import deepcopy
from unittest.mock import Mock
import pytest
from incontrol_completion import CompletionPreparation, CompletionPreparationError


def tasks():
    main={'id':10,'organization_performer':{'id':1069},'phase':{'phase_name':'Назначено.'},'type_work':{'work_name':'Заявка на ремонт'},
          'serviced_object':{'additional_fields':[{'field_name':'Бортовой номер','value':'162811'}]},
          'additional_fields':[{'field_name':'Связанные нарушения','link_type':'task','value':[11]}]}
    child={'id':11,'organization_performer':{'id':1069},'phase':{'phase_name':'Назначено.'},'type_work':{'work_name':'Тормоза'},
           'additional_fields':[{'id':501,'field_name':'Выполненные услуги','is_disabled':False}],
           'custom_field_dependent_tasks':[{'linked_task':11,'dependent_task':10}]}
    return main, child


def prepare(main, child, surname='Кожин'):
    client=Mock()
    data={'/tasks/10/':main,'/tasks/11/':child,'/me/':{'last_name':surname,'first_name':'Олег','middle_name':'Николаевич'},
          '/tasks/10/available-report-files/':{'results':[]}}
    def get(url,**kwargs):
        key=url.split('/api/v1')[1]
        return Mock(ok=True,json=lambda:deepcopy(data[key]))
    client.session.get.side_effect=get
    return client,CompletionPreparation(client)


def test_preflight_does_not_mutate_tasks_or_request_signature():
    client,plan=prepare(*tasks())
    result=plan.inspect(10,'14 парк','Кожин О.Н.','162811')
    assert result['can_prepare']
    assert result['children'][0]['services_field_id']==501
    assert len(client.session.get.call_args_list)==4
    client.session.post.assert_not_called()
    client.session.patch.assert_not_called()


@pytest.mark.parametrize('phase',['Отклонено.','Неизвестный этап'])
def test_declined_and_unknown_child_are_blocked(phase):
    main,child=tasks();child['phase']['phase_name']=phase
    _,plan=prepare(main,child)
    result=plan.inspect(10,'14 парк','Кожин О.Н.')
    assert not result['can_prepare']
    assert result['issues']


def test_surname_must_match_personal_account():
    _,plan=prepare(*tasks(),surname='Иванов')
    assert not plan.inspect(10,'14 парк','Кожин О.Н.')['can_prepare']


def test_reverse_link_must_be_confirmed():
    main,child=tasks();child['custom_field_dependent_tasks']=[]
    _,plan=prepare(main,child)
    assert not plan.inspect(10,'14 парк','Кожин О.Н.')['can_prepare']


@pytest.mark.parametrize('org,board',[(1060,'162811'),(1069,'999999')])
def test_different_park_or_board_is_refused(org,board):
    main,child=tasks();main['organization_performer']['id']=org
    _,plan=prepare(main,child)
    with pytest.raises(CompletionPreparationError):plan.inspect(10,'14 парк','Кожин О.Н.',board)


def test_unknown_link_format_is_refused():
    main,child=tasks();main['additional_fields'][0]['value']='11'
    _,plan=prepare(main,child)
    with pytest.raises(CompletionPreparationError):plan.inspect(10,'14 парк','Кожин О.Н.')


def test_service_preflight_allows_phase_only_child_but_repair_does_not():
    main,child=tasks();child['additional_fields']=[{'id':444,'field_name':'Чек-лист ТО-1','value':None}]
    _,plan=prepare(main,child)
    assert not plan.inspect(10,'14 парк','Кожин О.Н.')['can_prepare']
    main['type_work']['work_name']='Заявка на сервисное обслуживание'
    client,plan=prepare(main,child)
    result=plan.inspect(10,'14 парк','Кожин О.Н.')
    assert result['can_prepare']
    assert result['children'][0]['services_field_id'] is None
    client.session.post.assert_not_called();client.session.patch.assert_not_called()
