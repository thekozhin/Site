"""Сеть без ORM-транзакции, сохранность кеша и защита фоновых заданий."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import event


@pytest.fixture
def sync(app_ctx, monkeypatch):
    monkeypatch.setattr(app_ctx, 'INCONTROL_ENABLED', True)
    return app_ctx


def add_task(mod, board='164076', park='14 парк', description='Ремонт', **values):
    task = mod.Task(board_number=board, park=park, description=description,
                    status='in_repair', tag='waiting', **values)
    mod.db.session.add(task)
    mod.db.session.commit()
    return task.id


def add_badge(mod, board='164076', work='Заявка на ремонт'):
    mod.db.session.add(mod.IncontrolStatus(board_number=board, work_name=work,
        has_open=True, phase_name='Назначено.', checked_at=mod.incontrol_now() - timedelta(minutes=5)))
    mod.db.session.commit()


def cached(mod):
    return {(row.board_number, row.work_name) for row in mod.IncontrolStatus.query.all()}


class FakeClient:
    def __init__(self, mod, *, badges=None, applications=None, failed_orgs=(), auth_error=False):
        self.mod = mod
        self.badges = badges or {}
        self.applications = applications or {}
        self.failed_orgs = set(failed_orgs)
        self.auth_error = auth_error
        self.session = SimpleNamespace(close=Mock())
        self.details = []
        self.before_details = None
        self.before_fetch = None

    def no_orm_transaction(self):
        assert not self.mod.db.session.registry.has(), 'ORM-соединение удерживается во время сети'

    def authenticate(self):
        self.no_orm_transaction()
        if self.auth_error:
            raise RuntimeError('auth unavailable')

    def fetch_open_boards(self, org):
        self.no_orm_transaction()
        if self.before_fetch:
            self.before_fetch(org)
        if org in self.failed_orgs:
            raise RuntimeError('org unavailable')
        return self.badges.get(org, {})

    def fetch_open_applications(self, org):
        self.no_orm_transaction()
        if org in self.failed_orgs:
            raise RuntimeError('org unavailable')
        return self.applications.get(org, [])

    def get_task_by_id(self, task_id, org):
        self.no_orm_transaction()
        self.details.append(task_id)
        if self.before_details:
            self.before_details(task_id)
        return {'phase': {'id': 2}, 'type_work': {'work_name': 'Заявка на ремонт'}}

    def build_description_from_linked(self, linked, org):
        self.no_orm_transaction()
        return 'Подтверждённое описание'


def use_client(mod, monkeypatch, **kwargs):
    client = FakeClient(mod, **kwargs)
    monkeypatch.setattr(mod, '_make_incontrol_client', lambda: client)
    return client


def incoming(task_id=123, board='164076', **values):
    return dict(task_id=task_id, board=board, org_id=1069,
                work_name='Заявка на ремонт', linked_ids=[], **values)


def add_car(mod, board='164076'):
    mod.db.session.add(mod.Car(bort=board, location='14 парк', contract='216644', vin='test-vin'))
    mod.db.session.commit()


def test_cache_is_unchanged_until_network_finishes(sync, monkeypatch):
    mod = sync
    add_task(mod)
    add_badge(mod)
    client = use_client(mod, monkeypatch, badges={1069: {'164076': [
        {'work_name': 'Заявка на ремонт с линии', 'phase_name': 'Принято.'}]}})
    def inspect_cache(org):
        with mod.application.app_context():
            assert cached(mod) == {('164076', 'Заявка на ремонт')}
    client.before_fetch = inspect_cache
    result = mod.refresh_incontrol_statuses()
    assert result['ошибок'] == 0
    assert cached(mod) == {('164076', 'Заявка на ремонт с линии')}
    client.session.close.assert_called_once()


def test_partial_outage_preserves_failed_park_and_updates_other(sync, monkeypatch):
    mod = sync
    add_task(mod)
    add_badge(mod)
    add_task(mod, board='460653', park='Красная Пахра')
    add_badge(mod, board='460653')
    use_client(mod, monkeypatch, failed_orgs=[1069])
    result = mod.refresh_incontrol_statuses()
    assert result['проверено'] == 1
    assert result['пропущено'] == 1
    assert result['ошибок'] == 1
    assert cached(mod) == {('164076', 'Заявка на ремонт')}


def test_auth_failure_preserves_cache(sync, monkeypatch):
    add_task(sync)
    add_badge(sync)
    client = use_client(sync, monkeypatch, auth_error=True)
    assert sync.refresh_incontrol_statuses()['status'] == 'error'
    assert cached(sync) == {('164076', 'Заявка на ремонт')}
    client.session.close.assert_called_once()


def test_unknown_park_requires_complete_org_response(sync, monkeypatch):
    add_task(sync, park=None)
    add_badge(sync)
    use_client(sync, monkeypatch, failed_orgs=[1060])
    assert sync.refresh_incontrol_statuses()['пропущено'] == 1
    assert cached(sync) == {('164076', 'Заявка на ремонт')}


def test_empty_task_list_clears_old_cache_without_network(sync, monkeypatch):
    add_badge(sync)
    factory = Mock(side_effect=AssertionError('network not needed'))
    monkeypatch.setattr(sync, '_make_incontrol_client', factory)
    assert sync.refresh_incontrol_statuses()['проверено'] == 0
    assert cached(sync) == set()
    factory.assert_not_called()


def test_cache_uses_current_tasks_after_network(sync, monkeypatch):
    mod = sync
    task_id = add_task(mod)
    add_badge(mod)
    client = use_client(mod, monkeypatch, badges={1069: {'164076': [
        {'work_name': 'Заявка на ремонт', 'phase_name': 'Принято.'}]}})
    changed = []
    def close_during_network(org):
        if not changed:
            with mod.application.app_context():
                mod.db.session.get(mod.Task, task_id).status = 'completed'
                mod.db.session.commit()
            changed.append(True)
    client.before_fetch = close_during_network
    assert mod.refresh_incontrol_statuses()['проверено'] == 0
    assert cached(mod) == set()


def test_cache_write_failure_rolls_back_deletion(sync, monkeypatch):
    add_task(sync)
    add_badge(sync)
    use_client(sync, monkeypatch)
    original_commit = sync.db.session.commit
    def fail_commit():
        sync.db.session.flush()
        raise RuntimeError('write failed')
    monkeypatch.setattr(sync.db.session, 'commit', fail_commit)
    assert sync.refresh_incontrol_statuses()['status'] == 'error'
    monkeypatch.setattr(sync.db.session, 'commit', original_commit)
    assert cached(sync) == {('164076', 'Заявка на ремонт')}


def test_import_network_never_holds_orm_connection_and_is_idempotent(sync, monkeypatch):
    mod = sync
    add_car(mod)
    client = use_client(mod, monkeypatch, applications={1069: [incoming()]})
    first = mod.create_tasks_from_incontrol()
    assert first['created'] == 1
    task = mod.Task.query.one()
    assert task.description == 'Подтверждённое описание'
    assert task.author_open == 'InControl'
    assert task.incontrol_task_id == 123
    assert task.contract == '216644'
    second = mod.create_tasks_from_incontrol()
    assert second['created'] == 0
    assert client.details == [123]
    assert mod.Task.query.count() == 1
    assert client.session.close.call_count == 2


@pytest.mark.parametrize('link_source', ['task', 'previous_run', 'both', 'unlinked'])
@pytest.mark.parametrize('fresh_phase', ['Новое.', 'Назначено.', 'Принято.', 'Выполнено.'])
@pytest.mark.parametrize('locally_rejected', [False, True])
def test_stk_return_reopens_the_same_finished_repair(sync, monkeypatch, link_source, fresh_phase, locally_rejected):
    mod = sync
    previous_works = 'Заменили ремень генератора'
    now = mod.incontrol_now()
    task_id = add_task(mod, board='162850', incontrol_task_id=21774480 if link_source in ('task', 'both') else None)
    task = mod.db.session.get(mod.Task, task_id)
    task.status, task.tag = 'completed', 'success'
    task.reject = locally_rejected
    task.completed_at, task.completed_work, task.author_closed = now, previous_works, 'Мастер'
    user = mod.User(username='stk-test', password='unused', full_name='Тест')
    mod.db.session.add(user)
    mod.db.session.flush()
    run_model = mod.incontrol_rejections.model
    if link_source in ('previous_run', 'both'):
        mod.db.session.add(run_model(
            id='prior-run', task_id=task_id, remote_id=21774480, user_id=user.id,
            state='consumed', updated_at=now, payload_encrypted='unused-by-import',
        ))
    mod.db.session.commit()

    remote = {
        'id': 21774480, 'stage': 'in_work', 'phase': {'phase_name': 'Принято.' if fresh_phase == 'Выполнено.' else fresh_phase},
        'label': {'label_name': 'Не принято СТК' if fresh_phase == 'Новое.' else 'Не пройдено СТК'},
        'type_work': {'work_name': 'Заявка на ремонт'},
        'serviced_object': {'additional_fields': [{'field_name': 'Бортовой номер', 'value': '162850'}]},
    }
    client = use_client(mod, monkeypatch)
    fetch = mod.incontrol.IncontrolClient('test', 'test')
    search_results = [
        remote,
        dict(remote, id=21774481, type_work={'work_name': 'Заявка на ремонт с линии'}),
        dict(remote, id=21774482, phase={'phase_name': 'Выполнено.'}),
    ]
    monkeypatch.setattr(fetch, '_get', Mock(return_value={'results': search_results, 'next': None}))
    applications = fetch.fetch_open_applications(1069)
    assert len(applications) == 1 and applications[0]['stk_return']
    client.applications[1069] = applications

    def fresh(remote_id, org):
        client.no_orm_transaction()
        assert (remote_id, org) == (21774480, 1069)
        return dict(remote, phase={'phase_name': fresh_phase})
    monkeypatch.setattr(client, 'get_task_by_id', fresh)

    result = mod.create_tasks_from_incontrol()
    should_reopen = link_source != 'unlinked' and fresh_phase != 'Выполнено.'
    assert result['reopened'] == int(should_reopen)
    assert result['created'] == 0
    task = mod.db.session.get(mod.Task, task_id)
    assert mod.Task.query.count() == 1
    assert task.completed_work == previous_works
    if should_reopen:
        assert (task.status, task.tag, task.incontrol_task_id) == ('in_repair', 'in_work', 21774480)
        assert task.completed_at is None and task.author_closed is None
        assert not task.reject
        assert mod.db.session.get(run_model, 'prior-run') is None
        assert mod.create_tasks_from_incontrol()['reopened'] == 0
    else:
        assert task.status == 'completed'


def test_manual_task_during_fetch_blocks_duplicate_incontrol_row(sync, monkeypatch):
    mod = sync
    add_car(mod)
    client = use_client(mod, monkeypatch, applications={1069: [incoming()]})
    def create_manually(task_id):
        with mod.application.app_context():
            add_task(mod)
    client.before_details = create_manually
    assert mod.create_tasks_from_incontrol()['created'] == 0
    assert mod.Task.query.count() == 1
    assert mod.Task.query.one().incontrol_task_id is None


@pytest.mark.parametrize(('manual', 'imported', 'extra'), [
    ('Подрулевой переключатель (Электросистема), переключател паваротника тугой адометр неправильно показывает',
     'Электросистема. Подрулевой переключатель / Электросистема. Контрольные приборы',
     '\nЭлектросистема. Контрольные приборы'),
    ('Не идет зарядка (токоприемник)', 'АКБ тяговые. Не идёт зарядка', ''),
    ('Отопители кабины и салона',
     'Отопление и климатконтроль. Отопитель (печь) кабины / Отопление и климатконтроль. Отопитель (печь) салона', ''),
    ('КУП', 'Подвеска. Кран уровня пола', ''),
    ('Ошибка Limit', 'Тяговый привод. Ошибка Limit', ''),
])
def test_import_links_existing_manual_repair_and_waits_on_second_external_id(sync, monkeypatch, manual, imported, extra):
    add_car(sync)
    task_id = add_task(sync, description=manual, author_open='Приёмщик')
    created_at = sync.db.session.get(sync.Task, task_id).created_at
    client = use_client(sync, monkeypatch, applications={1069: [incoming(123), incoming(124)]})
    monkeypatch.setattr(client, 'build_description_from_linked', lambda *_: imported)

    result = sync.create_tasks_from_incontrol()
    assert (result['created'], result['linked']) == (0, 1)
    task = sync.db.session.get(sync.Task, task_id)
    assert task.incontrol_task_id == 123
    assert task.description == manual + extra
    assert (task.author_open, task.created_at) == ('Приёмщик', created_at)
    assert sync.Task.query.filter_by(incontrol_task_id=124).count() == 0
    assert sync.create_tasks_from_incontrol()['linked'] == 0


@pytest.mark.parametrize(('manual_descriptions', 'imported'), [
    (['Не работает'], 'Электросистема. Не работает'),
    (['Фара ближнего света — левая'], 'Фара ближнего света — правая'),
    (['Ошибка по АКПП'], 'АКПП, карданная передача. Стук, вибрация карданного вала, тс дергается и стучит'),
    (['Отопитель кабины'], 'Отопление и климатконтроль. Отопитель (печь) салона'),
    (['Датчик износа тормозных колодок, задняя левая'], 'Датчик износа тормозных колодок, передняя левая'),
    (['АКБ тяговые. Не идёт зарядка'], 'Идёт зарядка'),
    (['Долив охлаждающей жидкости', 'Долив охлаждающей жидкости (ДВС)'], 'Долив охлаждающей жидкости'),
])
def test_import_leaves_unclear_manual_matches_untouched(sync, monkeypatch, manual_descriptions, imported):
    add_car(sync)
    for description in manual_descriptions:
        add_task(sync, description=description, author_open='Приёмщик')
    client = use_client(sync, monkeypatch, applications={1069: [incoming()]})
    monkeypatch.setattr(client, 'build_description_from_linked', lambda *_: imported)

    result = sync.create_tasks_from_incontrol()
    assert (result['created'], result['linked']) == (0, 0)
    assert sync.Task.query.count() == len(manual_descriptions)
    assert sync.Task.query.filter_by(incontrol_task_id=123).count() == 0


def test_known_applications_are_filtered_in_batches(sync, monkeypatch):
    mod = sync
    for i in range(80):
        mod.db.session.add(mod.Task(board_number=str(100000 + i), description='Старая',
            status='completed', park='14 парк', incontrol_task_id=1000 + i))
    mod.db.session.commit()
    client = use_client(mod, monkeypatch, applications={1069: [incoming(1000+i, str(100000+i)) for i in range(80)]})
    queries = []
    def capture(conn, cursor, sql, *args):
        queries.append(sql)
    event.listen(mod.db.engine, 'before_cursor_execute', capture)
    try:
        result = mod.create_tasks_from_incontrol()
    finally:
        event.remove(mod.db.engine, 'before_cursor_execute', capture)
    assert result['created'] == 0
    assert result['skipped'] == 80
    assert len(queries) == 1  # Один пакет известных ID.
    assert client.details == []


@pytest.mark.parametrize('reply', [None, {'stage': 'completed'}, {'phase': {'id': 4}},
    {'label': {'label_name': 'Не пройдено СТК'}},
    {'type_work': {'work_name': 'Заявка на сервисное обслуживание'}}])
def test_import_rechecks_closed_or_excluded_application(sync, monkeypatch, reply):
    add_car(sync)
    client = use_client(sync, monkeypatch, applications={1069: [incoming()]})
    def get(*args):
        client.no_orm_transaction()
        return reply
    monkeypatch.setattr(client, 'get_task_by_id', get)
    assert sync.create_tasks_from_incontrol()['created'] == 0
    assert sync.Task.query.count() == 0


def test_network_failure_cannot_create_task(sync, monkeypatch):
    add_car(sync)
    client = use_client(sync, monkeypatch, applications={1069: [incoming()]})
    def fail(*args):
        client.no_orm_transaction()
        raise RuntimeError('unavailable')
    monkeypatch.setattr(client, 'get_task_by_id', fail)
    result = sync.create_tasks_from_incontrol()
    assert result['created'] == 0
    assert result['errors'] == 1
    assert sync.Task.query.count() == 0
    client.session.close.assert_called_once()


def test_second_new_task_also_fetches_without_connection(sync, monkeypatch):
    add_car(sync)
    add_car(sync, '164077')
    use_client(sync, monkeypatch, applications={1069: [incoming(), incoming(124, '164077')]})
    assert sync.create_tasks_from_incontrol()['created'] == 2
    assert sync.Task.query.count() == 2


def test_active_board_with_legacy_number_blocks_duplicate_incontrol_row(sync, monkeypatch):
    add_car(sync)
    add_task(sync, board='164076.0')
    use_client(sync, monkeypatch, applications={1069: [incoming()]})
    assert sync.create_tasks_from_incontrol()['created'] == 0
    assert sync.Task.query.count() == 1


def test_service_applications_are_not_imported_or_rechecked(sync, monkeypatch):
    add_car(sync)
    info = incoming()
    info['work_name'] = 'Заявка на сервисное обслуживание'
    client = use_client(sync, monkeypatch, applications={1069: [info]})
    assert sync.create_tasks_from_incontrol()['created'] == 0
    assert client.details == []


def test_import_preserves_external_creation_time_and_electric_vehicle(sync, monkeypatch):
    from datetime import datetime
    mod = sync
    mod.db.session.add(mod.Elcar(bort='164076', location='14 парк', contract='МГТ0019-25', vin='electric-vin'))
    mod.db.session.commit()
    timestamp = 1794301200
    use_client(mod, monkeypatch, applications={1069: [incoming(creation_date=timestamp)]})
    assert mod.create_tasks_from_incontrol()['created'] == 1
    task = mod.Task.query.one()
    assert task.type_ts == 'Электробус'
    assert task.vin == 'electric-vin'
    assert task.created_at == datetime.fromtimestamp(timestamp, mod.tz).replace(tzinfo=None)


def test_org_failure_does_not_prevent_other_org_import(sync, monkeypatch):
    add_car(sync)
    use_client(sync, monkeypatch, failed_orgs=[1060], applications={1069: [incoming()]})
    result = sync.create_tasks_from_incontrol()
    assert result['created'] == 1
    assert result['errors'] == 1


class FakeLockConnection:
    def __init__(self, acquired=1, release_error=False, commit_error=False):
        self.acquired = acquired
        self.release_error = release_error
        self.commit_error = commit_error
        self.calls = []
        self.commits = 0
        self.invalidated = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.calls.append(('close', None))

    def execute(self, sql, parameters):
        sql = str(sql)
        self.calls.append((sql, parameters['lock_name']))
        if 'RELEASE_LOCK' in sql and self.release_error:
            raise RuntimeError('release failed')
        return SimpleNamespace(scalar=lambda: self.acquired if 'GET_LOCK' in sql else 1)

    def commit(self):
        self.commits += 1
        if self.commit_error and self.commits == 1:
            raise RuntimeError('commit failed after get_lock')

    def invalidate(self):
        self.invalidated = True


def install_lock(mod, monkeypatch, connection):
    engine = mod.db.engine
    original = engine.connect
    served = []
    def connect(*args, **kwargs):
        if not served:
            served.append(True)
            return connection
        return original(*args, **kwargs)
    monkeypatch.setattr(engine, 'connect', connect)


def test_scheduler_releases_on_same_connection_after_orm_remove(sync, monkeypatch):
    connection = FakeLockConnection()
    install_lock(sync, monkeypatch, connection)
    def job():
        sync.db.session.remove()
        add_task(sync)
        sync.db.session.remove()
        return {'status': 'ok'}
    assert sync.run_incontrol_job_once('import', job) == {'status': 'ok'}
    assert connection.calls == [
        ('SELECT GET_LOCK(:lock_name, 0)', 'site_scheduler:incontrol_sync'),
        ('SELECT RELEASE_LOCK(:lock_name)', 'site_scheduler:incontrol_sync'), ('close', None)]
    assert connection.commits == 2
    assert not connection.invalidated


def test_busy_scheduler_skips_without_waiting(sync, monkeypatch):
    connection = FakeLockConnection(acquired=0)
    install_lock(sync, monkeypatch, connection)
    job = Mock(side_effect=AssertionError('must not run'))
    assert sync.run_incontrol_job_once('cache', job) == {'status': 'skipped', 'reason': 'locked'}
    job.assert_not_called()
    assert len(connection.calls) == 2


def test_failed_job_releases_lock_and_does_not_commit_partial_write(sync, monkeypatch):
    connection = FakeLockConnection()
    install_lock(sync, monkeypatch, connection)
    def job():
        sync.db.session.add(sync.Task(board_number='164076', description='Не сохранять', status='in_repair'))
        sync.db.session.flush()
        raise RuntimeError('job failed')
    assert sync.run_incontrol_job_once('import', job)['status'] == 'error'
    assert sync.Task.query.count() == 0
    assert 'RELEASE_LOCK' in connection.calls[1][0]


def test_release_failure_discards_connection(sync, monkeypatch):
    connection = FakeLockConnection(release_error=True)
    install_lock(sync, monkeypatch, connection)
    sync.run_incontrol_job_once('cache', lambda: None)
    assert connection.invalidated


def test_acquired_lock_is_released_even_if_first_commit_fails(sync, monkeypatch):
    connection = FakeLockConnection(commit_error=True)
    install_lock(sync, monkeypatch, connection)
    assert sync.run_incontrol_job_once('cache', lambda: None)['status'] == 'error'
    assert 'RELEASE_LOCK' in connection.calls[1][0]


def test_schedule_offsets_and_common_mutex(sync, monkeypatch):
    jobs = {}
    monkeypatch.setattr(sync, 'add_scheduler_job', lambda name, fn, trigger: jobs.update({name: (fn, trigger)}))
    monkeypatch.setattr(sync.scheduler, 'start', lambda: None)
    monkeypatch.setattr(sync, 'run_startup_report_catchups', lambda: None)
    calls = []
    monkeypatch.setattr(sync, 'run_incontrol_job_once', lambda name, fn: calls.append(name))
    sync.start_scheduler()
    cache_fn, cache_trigger = jobs['refresh_incontrol_statuses']
    import_fn, import_trigger = jobs['create_tasks_from_incontrol']
    assert str(cache_trigger.fields[6]) == '7,37'
    assert str(import_trigger.fields[6]) == '*/15'
    cache_fn()
    import_fn()
    assert calls == ['refresh_incontrol_statuses', 'create_tasks_from_incontrol']


def test_mail_scheduler_processes_replies_then_import_under_one_lock(sync, monkeypatch):
    jobs = {}
    monkeypatch.setenv('YANDEX_MAIL_ENABLED', '1')
    monkeypatch.setattr(sync, 'add_scheduler_job', lambda name, fn, trigger: jobs.update({name: fn}))
    monkeypatch.setattr(sync.scheduler, 'start', lambda: None)
    monkeypatch.setattr(sync, 'run_startup_report_catchups', lambda: None)
    run = Mock(side_effect=[{'status': 'error'}, {'created': 1}])
    monkeypatch.setattr(sync.yandex_requests, 'run', run)
    def locked(name, fn, scope):
        assert (name, scope) == ('yandex_mail_sync', 'yandex_mail')
        return fn()
    monkeypatch.setattr(sync, 'run_locked_sync_job', locked)

    sync.start_scheduler()

    assert {name for name in jobs if name.startswith('yandex_mail')} == {'yandex_mail_sync'}
    assert jobs['yandex_mail_sync']() == {'created': 1}
    assert [call.kwargs for call in run.call_args_list] == [{'replies_only': True}, {'import_only': True}]


def test_two_incontrol_ids_for_same_board_are_imported_after_each_other(sync, monkeypatch):
    add_car(sync)
    use_client(sync, monkeypatch, applications={1069: [incoming(123), incoming(124)]})
    assert sync.create_tasks_from_incontrol()['created'] == 1
    assert sync.create_tasks_from_incontrol()['created'] == 0
    sync.Task.query.one().status = 'completed'
    sync.db.session.commit()
    assert sync.create_tasks_from_incontrol()['created'] == 1
    assert {t.incontrol_task_id for t in sync.Task.query.all()} == {123, 124}
    assert sync.Task.query.filter_by(status='in_repair').one().incontrol_task_id == 124
