from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import Mock

import pytest
from openpyxl import load_workbook

from incontrol import utcnow
from incontrol import IncontrolError
from test_completion_checks import complete, completion, run_check, saved_task
from test_incontrol_rejection_jobs import rejection_page
from test_yandex_mail import ADDRESS, FOLDER, ORIGINAL_ID, PARK, WORK, fake_mail, mail_message
from yandex_mail import MOSCOW


def test_outage_close_records_snapshot_and_exports_once(completion, monkeypatch):
    mod, client, task_id = completion
    task = saved_task(completion)
    task.incontrol_task_id = 21500123
    mod.db.session.commit()
    job, status = run_check(completion, monkeypatch, error=IncontrolError('Сервис недоступен', status_code=503))
    assert status['state'] == 'unavailable'
    fields = {'offline_check_id': job['check_id'], 'completed_work': '=1+1'}
    assert complete(completion, **fields).status_code == 200
    task = saved_task(completion)
    entry = mod.incontrol_outbox.model.query.one()
    assert task.status == 'completed' and task.completed_at == entry.completed_at
    assert (entry.task_id, entry.remote_id, entry.description, entry.completed_work) == (task_id, 21500123, 'Ремонт', '=1+1')
    assert complete(completion, **fields).status_code == 302
    assert mod.incontrol_outbox.model.query.count() == 1
    assert client.get('/incontrol/pending/count').json == {'count': 1}
    assert 'Скачать Excel' in client.get('/incontrol/pending').text
    response = client.get('/incontrol/pending/export')
    sheet = load_workbook(BytesIO(response.data)).active
    assert sheet['E2'].value == entry.completed_at.replace(microsecond=0)
    assert sheet['G2'].value == '=1+1' and sheet['G2'].data_type == 's'
    assert client.post(f'/incontrol/pending/{entry.id}/reported').status_code == 302
    assert client.get('/incontrol/pending/count').json == {'count': 0}
    assert entry.reported_by == 'Тест'


@pytest.mark.parametrize('case', ['forbidden', 'open', 'expired', 'unknown_user'])
def test_only_recent_outage_for_this_user_allows_offline_close(completion, monkeypatch, case):
    mod, client, _ = completion
    if case == 'open':
        job, status = run_check(completion, monkeypatch, result='Заявка на ремонт')
        assert status['state'] == 'blocked'
    else:
        error = IncontrolError('Нет доступа', status_code=403) if case == 'forbidden' else TimeoutError()
        job, status = run_check(completion, monkeypatch, error=error)
        record = mod.db.session.get(mod.completion_checks.model, job['check_id'])
        if case == 'expired':
            record.finished_at = utcnow() - timedelta(minutes=16)
        if case == 'unknown_user':
            record.user_id += 1
        mod.db.session.commit()
    response = complete(completion, offline_check_id=job['check_id'])
    assert 'Недоступность InControl не подтверждена' in response.text
    assert saved_task(completion).status == 'in_repair'
    assert mod.incontrol_outbox.model.query.count() == 0


def test_missing_outbox_rolls_back_task_and_proof(completion, monkeypatch):
    mod, _, _ = completion
    job, _ = run_check(completion, monkeypatch, error=TimeoutError())
    mod.incontrol_outbox.model.__table__.drop(mod.db.engine)
    assert complete(completion, offline_check_id=job['check_id']).status_code == 503
    assert saved_task(completion).status == 'in_repair'
    assert mod.db.session.get(mod.completion_checks.model, job['check_id']).state == 'unavailable'


def test_session_pause_skips_incontrol_without_outage_proof(completion, monkeypatch):
    mod, client, task_id = completion
    submit_check = Mock()
    monkeypatch.setattr(mod.completion_checks, '_submit', submit_check)

    # Параметры формы закрытия сами по себе не выключают проверки.
    complete(completion, incontrol='0')
    assert saved_task(completion).status == 'in_repair'
    assert mod.incontrol_outbox.model.query.count() == 0

    settings = '/settings/incontrol'
    assert client.post(settings, data={'action': 'closure_checks', 'yandex_mail': '1'}).status_code == 302
    page = client.get(f'/complete_task/{task_id}')
    assert 'будет заявка для InControl' in page.text
    assert 'required: false' in page.text
    assert client.post(f'/complete_task/{task_id}/incontrol-check').json == {'state': 'ready', 'check_id': ''}
    submit_check.assert_not_called()

    assert complete(completion).status_code == 200
    entry = mod.incontrol_outbox.model.query.one()
    assert entry.task_id == task_id and entry.service == 'incontrol'
    assert saved_task(completion).status == 'completed'

    assert client.get('/logout').status_code == 302
    with client.session_transaction() as settings_session:
        assert 'closure_pauses' not in settings_session


def test_pausing_incontrol_stops_requests_from_an_already_open_form(rejection_page, monkeypatch):
    mod, client, task, _, csrf = rejection_page
    headers = {'X-CSRFToken': csrf, 'Accept': 'application/json'}
    personal_client = Mock()
    monkeypatch.setattr(mod.incontrol_integration.rejections, 'personal_client', personal_client)
    path = f'/complete_task/{task.id}'
    assert client.post('/settings/incontrol', headers=headers,
                       data={'action': 'closure_checks', 'yandex_mail': '1'}).status_code == 302

    plan = client.post(f'{path}/incontrol-plan', headers=headers, data={'main_outcome': 'complete'})
    assert plan.status_code == 409
    personal_client.assert_not_called()
    assert 'new (CompletionCheckController)' in client.get(path).text


@pytest.mark.parametrize('pause_incontrol', [False, True])
def test_paused_mail_uses_same_outbox_and_is_marked_independently(completion, monkeypatch, pause_incontrol):
    mod, client, _ = completion
    requests = mod.yandex_requests
    saved_task(completion).status = 'completed'
    mod.db.session.commit()
    submit_reply = Mock()
    monkeypatch.setattr(requests, 'submit_reply', submit_reply)
    original = fake_mail(datetime.now(MOSCOW) - timedelta(hours=3), {
        ORIGINAL_ID: mail_message(ORIGINAL_ID, 'УА160222071'),
    })
    assert requests.import_requests(original, ADDRESS, {PARK: FOLDER}, None, apply=True)['created'] == 1
    task_id = requests.model.query.one().task_id
    task_case = mod, client, task_id

    settings = {'action': 'closure_checks'}
    if not pause_incontrol:
        settings['incontrol'] = '1'
    assert client.post('/settings/incontrol', data=settings).status_code == 302
    check_id = None
    if not pause_incontrol:
        complete(task_case, completed_work=WORK)
        assert saved_task(task_case).status == 'in_repair'
        job, _ = run_check(task_case, monkeypatch)
        check_id = job['check_id']
    response = complete(task_case, check_id, completed_work=WORK)
    assert response.status_code == 200
    assert 'Письмо сохранено в «К отписке» для ручного ответа' in response.text
    assert 'в очереди на ответ' not in response.text
    submit_reply.assert_not_called()
    assert saved_task(task_case).status == 'completed'

    queue = mod.incontrol_outbox.model
    mail_entry = queue.query.filter_by(service='yandex_mail').one()
    assert (mail_entry.task_id, mail_entry.mail_ticket_number, mail_entry.completed_work) == (task_id, 'УА160222071', WORK)
    assert queue.query.filter_by(service='incontrol').count() == int(pause_incontrol)
    assert client.get('/incontrol/pending/count').json == {'count': 1 + int(pause_incontrol)}
    page = client.get('/incontrol/pending').text
    assert 'УА160222071' in page
    assert page.count('<strong>164076</strong>') == 1
    assert f'/incontrol/pending/{mail_entry.id}/reported' in page
    sheet = load_workbook(BytesIO(client.get('/incontrol/pending/export').data)).active
    assert sheet.max_row == 2
    assert sheet['A2'].value == '164076'
    assert sheet['L2'].value == 'УА160222071'
    services = {'Яндекс Почта', 'InControl'} if pause_incontrol else {'Яндекс Почта'}
    assert set(sheet['K2'].value.splitlines()) == services
    if pause_incontrol:
        incontrol_entry = queue.query.filter_by(service='incontrol').one()
        assert f'/incontrol/pending/{incontrol_entry.id}/reported' in page

    assert requests.model.query.filter_by(task_id=task_id).one_or_none().reply_state == 'deferred'
    assert client.post('/settings/incontrol', data={'action': 'closure_checks', 'incontrol': '1', 'yandex_mail': '1'}).status_code == 302
    assert requests.deliver_replies(original, ADDRESS) == 0
    original.send_reply.assert_not_called()
    assert client.post(f'/incontrol/pending/{mail_entry.id}/reported').status_code == 302
    assert client.get('/incontrol/pending/count').json == {'count': int(pause_incontrol)}
    assert requests.model.query.filter_by(task_id=task_id).one_or_none().reply_state == 'answered'


def test_paused_mail_clears_previous_lookup_for_unlinked_task(completion, monkeypatch):
    mod, client, task_id = completion
    requests = mod.yandex_requests
    monkeypatch.setenv('YANDEX_MAIL_ENABLED', '1')
    submit_reply = Mock()
    monkeypatch.setattr(requests, 'submit_reply', submit_reply)
    task = saved_task(completion)
    task.author_open = 'Приёмщик'
    task.yandex_reply_lookup = True
    mod.db.session.commit()
    assert client.post('/settings/incontrol', data={'action': 'closure_checks'}).status_code == 302

    assert complete(completion, completed_work=WORK).status_code == 200
    assert saved_task(completion).status == 'completed'
    assert not saved_task(completion).yandex_reply_lookup
    assert requests.model.query.filter_by(task_id=task_id).count() == 0
    submit_reply.assert_not_called()


def test_personal_planning_outage_can_close_with_locally_entered_work(rejection_page, monkeypatch):
    import incontrol as jobs
    mod, client, task, _, csrf = rejection_page
    headers = {'X-CSRFToken': csrf, 'Accept': 'application/json'}
    monkeypatch.setattr(jobs, 'ensure_session', Mock(side_effect=TimeoutError('InControl не ответил')))
    response = client.post(f'/complete_task/{task.id}/incontrol-plan', data={'main_outcome': 'reject', 'action': 'reject'}, headers=headers)
    run_id = response.json['run_id']
    mod.incontrol_integration.rejections.slots.acquire()
    mod.incontrol_integration.rejections.worker(run_id, 'inspect', None)
    status = client.get(response.json['status_url']).json
    assert status['state'] == 'unavailable' and status['planning']
    response = client.post(f'/complete_task/{task.id}', data={'action': 'reject', 'main_outcome': 'reject',
                           'completed_work': 'Отклонено после осмотра', 'offline_run_id': run_id}, headers=headers)
    assert response.status_code == 302
    mod.db.session.expire_all()
    assert task.status == 'completed'
    entry = mod.incontrol_outbox.model.query.one()
    assert entry.is_reject and entry.remote_id == 10 and entry.completed_work == 'Отклонено после осмотра'
    assert mod.db.session.get(mod.incontrol_integration.rejections.model, run_id).state == 'consumed'


def test_each_selected_park_has_its_own_list_export_and_acknowledgements(completion, monkeypatch):
    mod, client, _ = completion
    job, _ = run_check(completion, monkeypatch, error=TimeoutError())
    complete(completion, offline_check_id=job['check_id'])
    entry = mod.incontrol_outbox.model.query.one()
    other = mod.incontrol_outbox.model(task_id=99, park='1 парк', board_number='164099',
               description='Другая заявка', completed_work='Осмотр', is_reject=False,
               completed_at=entry.completed_at, closed_by='Тест')
    mod.db.session.add(other)
    mod.db.session.commit()
    with client.session_transaction() as sess:
        sess['available_parks'] = ['14 парк', '1 парк']
        sess['selected_park'] = '1 парк'
    assert client.get('/incontrol/pending/count').json == {'count': 1}
    page = client.get('/incontrol/pending').text
    assert '164099' in page and entry.board_number not in page
    sheet = load_workbook(BytesIO(client.get('/incontrol/pending/export').data)).active
    assert sheet.max_row == 2 and sheet['A2'].value == '164099'
    assert client.post(f'/incontrol/pending/{entry.id}/reported').status_code == 404
    assert entry.reported_at is None
    assert client.post(f'/incontrol/pending/{other.id}/reported').status_code == 302
    assert client.get('/incontrol/pending/count').json == {'count': 0}
    with client.session_transaction() as sess:
        sess['selected_park'] = '14 парк'
    assert client.get('/incontrol/pending/count').json == {'count': 1}
    assert entry.board_number in client.get('/incontrol/pending').text
    with client.session_transaction() as sess:
        sess['selected_park'] = None
    assert client.get('/incontrol/pending/count').json == {'count': 0}


def test_after_personal_outage_only_the_submitted_result_can_be_recorded(rejection_page, monkeypatch):
    import incontrol as jobs
    mod, client, task, _, csrf = rejection_page
    headers = {'X-CSRFToken': csrf, 'Accept': 'application/json'}
    monkeypatch.setattr(jobs, 'ensure_session', Mock(side_effect=TimeoutError('InControl не ответил')))
    fields = {'action': 'reject', 'main_outcome': 'reject', 'completed_work': 'Борт на линии'}
    path = f'/complete_task/{task.id}'
    response = client.post(path, data=fields, headers=headers)
    assert response.status_code == 202
    run_id = response.json['run_id']
    mod.incontrol_integration.rejections.slots.acquire()
    mod.incontrol_integration.rejections.worker(run_id, 'prepare', None)
    assert client.get(response.json['status_url']).json['state'] == 'unavailable'
    wrong = {**fields, 'offline_run_id': run_id, 'completed_work': 'Другие данные'}
    assert 'Данные отличаются' in client.post(path, data=wrong, headers=headers).text
    assert task.status == 'in_repair'
    assert client.post(path, data={**fields, 'offline_run_id': run_id}, headers=headers).status_code == 302
    mod.db.session.expire_all()
    assert task.status == 'completed'
    assert mod.incontrol_outbox.model.query.one().completed_work == 'Борт на линии'
