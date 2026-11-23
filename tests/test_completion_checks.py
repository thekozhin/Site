"""Закрытие разрешено только после успешного одноразового ответа InControl."""

from datetime import timedelta
import threading

import pytest

from completion_checks import utcnow


@pytest.fixture
def completion(app_ctx, monkeypatch):
    mod = app_ctx
    monkeypatch.setattr(mod, 'INCONTROL_ENABLED', True)
    monkeypatch.setattr(mod.completion_checks, '_submit', lambda check_id: True)
    mod.application.config['WTF_CSRF_ENABLED'] = False
    user = mod.User(username='completion-tester', password='unused', full_name='Тест',
                    allowed_parks=['14 парк'], company='Сетранс')
    task = mod.Task(board_number='164076', park='14 парк', status='in_repair',
                    description='Ремонт', tag='in_work', type_ts='Автобус',
                    created_at=mod.incontrol_now() - timedelta(hours=2))
    car = mod.Car(bort='164076', location='14 парк', contract='216644', mileage=100)
    mod.db.session.add_all([user, task, car])
    mod.db.session.commit()
    client = mod.application.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
        sess['available_parks'] = ['14 парк']
        sess['selected_park'] = '14 парк'
    return mod, client, task.id


def start(completion):
    mod, client, task_id = completion
    result = client.post(f'/complete_task/{task_id}/incontrol-check')
    assert result.status_code == 202
    return result.get_json()


def complete(completion, check_id=None, **extra):
    mod, client, task_id = completion
    data = dict(completed_work='Заменён узел', mileage='555',
                work_in=(mod.incontrol_now() - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'))
    if check_id:
        data['completion_check_id'] = check_id
    data.update(extra)
    return client.post(f'/complete_task/{task_id}', data=data)


def saved_task(completion):
    mod, _, task_id = completion
    mod.db.session.expire_all()
    return mod.db.session.get(mod.Task, task_id)


def run_check(completion, monkeypatch, result=None, error=None):
    mod, client, _ = completion
    def fake_check(board, park, required_names):
        if error:
            raise error
        return result
    monkeypatch.setattr(mod.completion_checks, 'check_board', fake_check)
    job = start(completion)
    mod.completion_checks._run(job['check_id'])
    return job, client.get(job['status_url']).get_json()


def test_pending_check_keeps_task_open_and_does_not_start_twice(completion):
    job = start(completion)
    assert start(completion)['check_id'] == job['check_id']
    assert completion[1].get(job['status_url']).get_json()['state'] == 'pending'
    response = complete(completion, job['check_id'])
    assert response.status_code == 200
    assert 'Заменён узел' in response.get_data(as_text=True)
    assert saved_task(completion).status == 'in_repair'
    assert completion[0].Car.query.first().mileage == 100


def test_success_is_consumed_and_preserves_print_prompt(completion, monkeypatch):
    job, status = run_check(completion, monkeypatch)
    assert status['state'] == 'ready'
    assert saved_task(completion).status == 'in_repair'
    response = complete(completion, job['check_id'])
    assert response.status_code == 200
    assert 'Да, печатать' in response.get_data(as_text=True)
    task = saved_task(completion)
    assert task.status == 'completed'
    assert task.completed_work == 'Заменён узел'
    assert completion[0].Car.query.first().mileage == 555
    assert completion[0].db.session.get(completion[0].completion_checks.model, job['check_id']).state == 'consumed'
    response = complete(completion, job['check_id'], completed_work='Повторная запись')
    assert response.status_code == 302
    assert saved_task(completion).completed_work == 'Заменён узел'


@pytest.mark.parametrize('error', [RuntimeError('network'), TimeoutError('timeout')])
def test_service_failure_never_closes_task(completion, monkeypatch, error):
    job, status = run_check(completion, monkeypatch, error=error)
    assert status['state'] == ('unavailable' if isinstance(error, TimeoutError) else 'error')
    complete(completion, job['check_id'])
    assert saved_task(completion).status == 'in_repair'
    assert completion[0].Car.query.first().mileage == 100


def test_open_incontrol_task_blocks_completion(completion, monkeypatch):
    job, status = run_check(completion, monkeypatch, result='Заявка на ремонт')
    assert status['state'] == 'blocked'
    complete(completion, job['check_id'])
    assert saved_task(completion).status == 'in_repair'


def test_post_without_verification_cannot_bypass_check(completion):
    complete(completion)
    assert saved_task(completion).status == 'in_repair'


def test_expired_success_cannot_close(completion, monkeypatch):
    mod, client, _ = completion
    job, _ = run_check(completion, monkeypatch)
    record = mod.db.session.get(mod.completion_checks.model, job['check_id'])
    record.finished_at = utcnow() - timedelta(seconds=61)
    mod.db.session.commit()
    assert client.get(job['status_url']).get_json()['state'] == 'error'
    complete(completion, job['check_id'])
    assert saved_task(completion).status == 'in_repair'


def test_stalled_worker_expires_and_late_success_is_ignored(completion):
    mod, client, _ = completion
    job = start(completion)
    record = mod.db.session.get(mod.completion_checks.model, job['check_id'])
    record.created_at = utcnow() - timedelta(seconds=46)
    mod.db.session.commit()
    mod.completion_checks._save_result(job['check_id'], 'ready', None)
    assert client.get(job['status_url']).get_json()['state'] == 'error'
    complete(completion, job['check_id'])
    assert saved_task(completion).status == 'in_repair'
    assert start(completion)['check_id'] != job['check_id']


def test_changed_task_invalidates_success(completion, monkeypatch):
    job, _ = run_check(completion, monkeypatch)
    task = saved_task(completion)
    task.tag = 'to'
    completion[0].db.session.commit()
    complete(completion, job['check_id'])
    assert saved_task(completion).status == 'in_repair'


def test_another_user_cannot_use_result(completion, monkeypatch):
    mod, client, _ = completion
    job, _ = run_check(completion, monkeypatch)
    other = mod.User(username='other', password='unused', allowed_parks=['14 парк'])
    mod.db.session.add(other)
    mod.db.session.commit()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(other.id)
    # app_ctx держит общий g между тестовыми запросами; в настоящем сервере
    # каждый запрос получает новый контекст и заново загружает пользователя.
    from flask import g
    g.pop('_login_user', None)
    assert client.get(job['status_url']).status_code == 404
    complete(completion, job['check_id'])
    assert saved_task(completion).status == 'in_repair'


def test_queue_full_returns_retryable_error(completion, monkeypatch):
    mod, client, task_id = completion
    monkeypatch.setattr(mod.completion_checks, '_submit', lambda _: False)
    response = client.post(f'/complete_task/{task_id}/incontrol-check')
    assert response.status_code == 503
    assert response.get_json()['state'] == 'error'
    assert saved_task(completion).status == 'in_repair'


def test_disabled_integration_does_not_allow_unchecked_completion(completion, monkeypatch):
    monkeypatch.setattr(completion[0], 'INCONTROL_ENABLED', False)
    monkeypatch.setattr(completion[0].incontrol_rejections, 'enabled', lambda: False)
    response = complete(completion, check_id='forged')
    assert response.status_code == 200
    assert 'Проверку пропустить нельзя' in response.text
    assert saved_task(completion).status == 'in_repair'


def test_reject_also_requires_success(completion, monkeypatch):
    complete(completion, action='reject')
    assert saved_task(completion).status == 'in_repair'
    job, _ = run_check(completion, monkeypatch)
    assert complete(completion, job['check_id'], action='reject').status_code == 302
    assert saved_task(completion).reject is True


def test_to_checks_service_work_and_saves_seasonal_service(completion, monkeypatch):
    mod, _, _ = completion
    task = saved_task(completion)
    task.tag = 'to'
    mod.db.session.commit()
    observed = []
    monkeypatch.setattr(mod.completion_checks, 'check_board', lambda b, p, names: observed.append(names))
    job = start(completion)
    mod.completion_checks._run(job['check_id'])
    assert observed == [{'Заявка на сервисное обслуживание','Заявка на ремонт','Заявка на ремонт с линии'}]
    complete(completion, job['check_id'], seasonal_service='1')
    assert saved_task(completion).status == 'completed'
    assert mod.Car.query.first().seasonal_service is True


def test_invalid_form_does_not_consume_result(completion, monkeypatch):
    job, _ = run_check(completion, monkeypatch)
    response = complete(completion, job['check_id'], work_in='invalid', completed_work='Не терять текст')
    assert 'Не терять текст' in response.get_data(as_text=True)
    assert saved_task(completion).status == 'in_repair'
    mod = completion[0]
    assert mod.db.session.get(mod.completion_checks.model, job['check_id']).state == 'ready'


def test_worker_does_not_block_http_and_cannot_close_by_itself(completion, monkeypatch):
    mod, client, task_id = completion
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_run = mod.completion_checks._run
    def observed_run(check_id):
        try:
            original_run(check_id)
        finally:
            finished.set()
    def delayed_check(*args):
        entered.set()
        assert release.wait(5)
        return None
    monkeypatch.setattr(mod.completion_checks, 'check_board', delayed_check)
    monkeypatch.setattr(mod.completion_checks, '_run', observed_run)
    monkeypatch.setattr(mod.completion_checks, '_submit', mod.completion_checks.__class__._submit.__get__(mod.completion_checks))
    try:
        job = start(completion)
        assert entered.wait(3)
        assert client.get('/health').status_code == 200
        assert client.get(job['status_url']).get_json()['state'] == 'pending'
        assert saved_task(completion).status == 'in_repair'
    finally:
        release.set()
        # Дождаться реального фонового задания до очистки тестовой БД.
        assert finished.wait(5)
    # completion state проверяется отдельными тестами после _run.


def test_timeout_is_forwarded_and_interactive_retries_are_disabled(app_ctx):
    client = app_ctx._make_incontrol_client(timeout=7, interactive=True)
    try:
        assert client.timeout == 7
        assert client.session.get_adapter('https://').max_retries.total == 0
        assert client.interactive is True
    finally:
        client.session.close()


def test_schema_is_created_by_explicit_idempotent_command(app_ctx):
    model = app_ctx.completion_checks.model
    model.__table__.drop(app_ctx.db.engine)
    runner = app_ctx.application.test_cli_runner()
    assert runner.invoke(args=['init-completion-checks']).exit_code == 0
    assert runner.invoke(args=['init-completion-checks']).exit_code == 0
    assert model.query.count() == 0


def test_missing_table_returns_error_without_closing_task(completion):
    mod, client, task_id = completion
    mod.completion_checks.model.__table__.drop(mod.db.engine)
    response = client.post(f'/complete_task/{task_id}/incontrol-check')
    assert response.status_code == 503
    assert response.get_json()['state'] == 'error'
    assert saved_task(completion).status == 'in_repair'


def test_check_cannot_be_used_for_another_task(completion, monkeypatch):
    mod, client, _ = completion
    job, _ = run_check(completion, monkeypatch)
    other = mod.Task(board_number='164077', park='14 парк', status='in_repair',
                     description='Другая заявка', tag='in_work', created_at=mod.incontrol_now() - timedelta(hours=2))
    mod.db.session.add(other)
    mod.db.session.commit()
    complete((mod, client, other.id), job['check_id'])
    mod.db.session.expire_all()
    assert mod.db.session.get(mod.Task, other.id).status == 'in_repair'


def test_start_requires_csrf_token(completion, monkeypatch):
    mod, client, task_id = completion
    monkeypatch.setitem(mod.application.config, 'WTF_CSRF_ENABLED', True)
    response = client.post(f'/complete_task/{task_id}/incontrol-check')
    assert response.status_code == 400
    assert mod.completion_checks.model.query.count() == 0


def test_legacy_check_uses_schedule_link_after_ready_tag(completion,monkeypatch):
    mod,client,task_id=completion
    task=mod.db.session.get(mod.Task,task_id)
    task.tag='success';task.id_to='5084';mod.db.session.commit()
    names=[]
    def check(board,park,required_names):
        names.append(required_names)
        return 'Заявка на сервисное обслуживание'
    monkeypatch.setattr(mod.completion_checks,'check_board',check)
    job=start(completion);mod.completion_checks._run(job['check_id'])
    assert names==[{'Заявка на сервисное обслуживание','Заявка на ремонт','Заявка на ремонт с линии'}]
    assert client.get(job['status_url']).json['state']=='blocked'
    complete(completion,job['check_id'])
    assert saved_task(completion).status=='in_repair'


def test_legacy_result_from_old_classifier_is_rejected(completion):
    mod,client,task_id=completion
    job=start(completion)
    task=mod.db.session.get(mod.Task,task_id)
    record=mod.db.session.get(mod.completion_checks.model,job['check_id'])
    record.state='ready';record.finished_at=utcnow()
    record.fingerprint=mod.completion_checks.fingerprint(task)
    mod.db.session.commit()
    complete(completion,job['check_id'])
    assert saved_task(completion).status=='in_repair'
