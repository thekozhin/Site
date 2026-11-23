"""Сетевые ошибки и неполные ответы не считаются разрешением на закрытие."""
import time
from unittest.mock import Mock

import pytest
import requests

import incontrol


@pytest.fixture
def client():
    instance = incontrol.IncontrolClient('test', 'test', timeout=7, interactive=True)
    yield instance
    instance.session.close()


def task(board='164076', work='Заявка на ремонт'):
    return {
        'serviced_object': {'additional_fields': [{'field_name': 'Бортовой номер', 'value': board}]},
        'phase': {'phase_name': 'Назначено.'},
        'type_work': {'work_name': work},
    }


def test_interactive_auth_does_not_retry_network_error(client, monkeypatch):
    post = Mock(side_effect=requests.Timeout('slow'))
    monkeypatch.setattr(client.session, 'post', post)
    with pytest.raises(incontrol.IncontrolError):
        client.authenticate()
    assert post.call_count == 1


def test_interactive_get_does_not_retry_network_error(client, monkeypatch):
    get = Mock(side_effect=requests.Timeout('slow'))
    monkeypatch.setattr(client.session, 'get', get)
    with pytest.raises(incontrol.IncontrolError):
        client._get('/tasks/', [], 1069)
    assert get.call_count == 1


def test_deadline_prevents_another_network_call(client, monkeypatch):
    post = Mock()
    monkeypatch.setattr(client.session, 'post', post)
    client._deadline = time.monotonic() - 1
    with pytest.raises(incontrol.IncontrolError):
        client.authenticate()
    post.assert_not_called()


@pytest.mark.parametrize('payload', [{}, {'results': None}, {'results': {}}, [], {'results': [None]}])
def test_malformed_api_response_is_not_empty_result(client, monkeypatch, payload):
    monkeypatch.setattr(client, '_get', lambda *args: payload)
    with pytest.raises(incontrol.IncontrolError):
        client.check_board_details('164076', '14 парк', {'Заявка на ремонт'})


def test_checks_next_page_before_allowing_completion(client, monkeypatch):
    calls = []
    def get(path, params, org):
        assert [value for key, value in params if key == 'phase'] == [1, 2, 3]
        page = dict(params)['page']
        calls.append(page)
        return {'results': [] if page == 1 else [task()], 'next': 'page=2' if page == 1 else None}
    monkeypatch.setattr(client, '_get', get)
    assert client.check_board_details('164076', '14 парк', {'Заявка на ремонт'}) == (True, 'Назначено.', 'Заявка на ремонт')
    assert calls == [1, 2]


def test_service_and_repair_work_are_checked_separately(client, monkeypatch):
    monkeypatch.setattr(client, '_get', lambda *args: {'results': [task(work='Заявка на ремонт')], 'next': None})
    assert client.check_board_details('164076', '14 парк', {'Заявка на сервисное обслуживание'}) == (False, None, None)


@pytest.mark.parametrize('missing', ['phase', 'type_work'])
def test_incomplete_matching_task_cannot_allow_completion(client, monkeypatch, missing):
    row = task()
    row.pop(missing)
    monkeypatch.setattr(client, '_get', lambda *args: {'results': [row]})
    with pytest.raises(incontrol.IncontrolError):
        client.check_board_details('164076', '14 парк', {'Заявка на ремонт'})


def test_background_sweep_keeps_existing_retry_configuration():
    instance = incontrol.IncontrolClient('test', 'test')
    try:
        assert instance.timeout == 30
        assert instance._deadline is None
        assert instance.session.get_adapter('https://').max_retries.total == 4
    finally:
        instance.session.close()


def test_slow_valid_response_is_not_cut_off_after_seven_seconds(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(incontrol.time, 'monotonic', lambda: clock[0])
    instance = incontrol.IncontrolClient('test', 'test', timeout=25, interactive=True)
    def slow_get(url, **kwargs):
        assert kwargs['timeout'] == (3, 25)
        clock[0] += 20
        return Mock(status_code=200, ok=True, json=lambda: {'results': [], 'next': None})
    monkeypatch.setattr(instance.session, 'get', slow_get)
    try:
        assert instance.check_board_details('162746', '14 парк', {'Заявка на ремонт'}) == (False, None, None)
    finally:
        instance.session.close()


def test_timeout_stays_within_remaining_operation_budget(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(incontrol.time, 'monotonic', lambda: clock[0])
    instance = incontrol.IncontrolClient('test', 'test', timeout=25, interactive=True)
    try:
        assert instance._deadline == 135
        assert instance._request_timeout() == (3, 25)
        clock[0] = 130
        assert instance._request_timeout() == (3, 5)
        clock[0] = 135
        with pytest.raises(incontrol.IncontrolError):
            instance._request_timeout()
    finally:
        instance.session.close()


def test_completion_uses_slow_response_timeout(app_ctx, monkeypatch):
    monkeypatch.setattr(app_ctx, 'INCONTROL_ENABLED', True)
    instance = Mock()
    instance.check_board_details.return_value = (False, None, None)
    factory = Mock(return_value=instance)
    monkeypatch.setattr(app_ctx, '_make_incontrol_client', factory)
    assert app_ctx.refresh_incontrol_status_for_board('162746', '14 парк', {'Заявка на ремонт'}) is None
    factory.assert_called_once_with(timeout=25, interactive=True)
    instance.session.close.assert_called_once()


@pytest.mark.parametrize('has_open', [False, True])
def test_board_with_large_closed_history_only_fetches_open_tasks(client, monkeypatch, has_open):
    # Без фильтра этот борт потребовал бы вторую страницу истории,
    # на которой в рабочем журнале истёк общий бюджет проверки.
    history = []
    for _ in range(150):
        row = task(board='162702')
        row['phase'] = {'id': 4, 'phase_name': 'Выполнено.'}
        row['stage'] = 'completed'
        history.append(row)
    if has_open:
        row = task(board='162702')
        row['phase']['id'] = 2
        history.append(row)
    calls = []
    def get(path, params, org):
        phases = [value for key, value in params if key == 'phase']
        filtered = [row for row in history if not phases or row['phase']['id'] in phases]
        page = dict(params)['page']
        page_size = dict(params)['per_page']
        calls.append(page)
        if page == 2:
            raise requests.Timeout('historical page is too slow')
        return {'results': filtered[:page_size], 'next': 'page=2' if len(filtered) > page_size else None}
    monkeypatch.setattr(client, '_get', get)
    result = client.check_board_details('162702', '14 парк', {'Заявка на ремонт'})
    assert result == ((True, 'Назначено.', 'Заявка на ремонт') if has_open else (False, None, None))
    assert calls == [1]


def test_server_ignoring_phase_filter_is_checked_locally(client, monkeypatch):
    completed = task(board='162702')
    completed['phase'] = {'id': 4, 'phase_name': 'Выполнено.'}
    monkeypatch.setattr(client, '_get', lambda *args: {'results': [completed], 'next': None})
    assert client.check_board_details('162702', '14 парк', {'Заявка на ремонт'}) == (False, None, None)


def test_filtered_second_page_failure_still_blocks_permission(client, monkeypatch):
    def get(path, params, org):
        if dict(params)['page'] == 2:
            raise requests.Timeout('second page unavailable')
        return {'results': [], 'next': 'page=2'}
    monkeypatch.setattr(client, '_get', get)
    with pytest.raises(requests.Timeout):
        client.check_board_details('162702', '14 парк', {'Заявка на ремонт'})
