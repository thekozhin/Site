from unittest.mock import Mock
import pytest
from incontrol import find_open_main_tasks, RejectionError


def item(tid=10,board='164466',work='Заявка на ремонт',phase='Принято.',org=1059):
    return {'id':tid,'stage':'in_work','phase':{'phase_name':phase},'type_work':{'work_name':work},'organization_performer':{'id':org},
            'serviced_object':{'additional_fields':[{'field_name':'Бортовой номер','value':board}]}}


def test_lookup_checks_all_pages_exact_board_and_main_work_type():
    client=Mock()
    client._get.side_effect=[{'results':[item(1,'1644660'),item(2,work='Тормоза')],'next':'page=2'}, {'results':[item(3),item(3)],'next':None}]
    result=find_open_main_tasks(client,'164466','1 парк')
    assert [r['id'] for r in result]==[3]
    assert client._get.call_count==2
    assert client._get.call_args.args[2]==1059
    assert ('page',2) in client._get.call_args.args[1]


def test_no_open_requests_requires_complete_successful_search():
    client=Mock()
    client._get.return_value={'results':[item(phase='Выполнено.')],'next':None}
    assert find_open_main_tasks(client,'164466','1 парк')==[]
    client._get.side_effect=TimeoutError()
    with pytest.raises(TimeoutError):find_open_main_tasks(client,'164466','1 парк')


@pytest.mark.parametrize('bad',[{}, {'results':None}, {'results':[None]}, {'results':[item(org=1069)]}])
def test_bad_responses_do_not_mean_no_request(bad):
    client=Mock();client._get.return_value=bad
    with pytest.raises(RejectionError):find_open_main_tasks(client,'164466','1 парк')


def test_lookup_includes_service_and_repair_regardless_of_old_service_hint():
    client=Mock();client._get.return_value={'results':[item(),item(11,work='Заявка на сервисное обслуживание')],'next':None}
    for hint in (True, False, None):
        assert [x['id'] for x in find_open_main_tasks(client,'164466','1 парк',hint)]==[10,11]
