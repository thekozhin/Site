"""Сохранение согласования и счётчик заявок текущего парка."""
from datetime import date
import re

import pytest


@pytest.fixture
def body_page(app_ctx, monkeypatch):
    import body_repair_sync as body
    mod = app_ctx
    monkeypatch.setattr(body, '_body_repair_schema_ready', False)
    monkeypatch.setitem(mod.application.config, 'WTF_CSRF_ENABLED', True)
    user = mod.User(username='body-approval', password='unused', full_name='Тест',
                    company='Сетранс', allowed_parks=['14 парк'])
    mod.db.session.add(user)
    records = [
        body.BodyRepair(park='14 парк', bort='164001', reason='Ремонт', started_at=date.today()),
        body.BodyRepair(park='14 парк', bort='164001', reason='Вторая запись того же борта'),
        body.BodyRepair(park='14 парк', bort='164002', reason='Другой борт'),
        body.BodyRepair(park='14 парк', bort='164003', reason='Готовый', is_completed=True, is_approved=True),
        body.BodyRepair(park='1 парк', bort='100001', reason='Другой парк'),
    ]
    mod.db.session.add_all(records)
    mod.db.session.commit()
    record_ids = [record.id for record in records]
    client = mod.application.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
        sess['available_parks'] = ['14 парк']
        sess['selected_park'] = '14 парк'
    response = client.get('/body-repair')
    assert response.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))[1]
    return mod, body, client, record_ids, token


def post_approval(page, record_id, values=None):
    _, _, client, _, token = page
    return client.post(f'/body-repair/{record_id}/approval', data=values or {}, headers={'X-CSRFToken': token})


def test_approval_is_unchecked_by_default_and_round_trips(body_page):
    mod, body, client, ids, _ = body_page
    assert mod.db.session.get(body.BodyRepair, ids[0]).is_approved is False
    for _ in range(2):
        response = post_approval(body_page, ids[0], {'is_approved': '1', 'tab': 'in_work', 'scroll_y': '601'})
        assert response.status_code == 302
        assert 'scroll_y=601' in response.headers['Location']
        mod.db.session.expire_all()
        record = mod.db.session.get(body.BodyRepair, ids[0])
        assert record.is_approved is True
        assert record.is_completed is False
        assert record.reason == 'Ремонт'
    assert post_approval(body_page, ids[0]).status_code == 302
    mod.db.session.expire_all()
    assert mod.db.session.get(body.BodyRepair, ids[0]).is_approved is False


def test_counter_counts_all_active_requests_in_current_park_on_both_tabs(body_page):
    _, _, client, _, _ = body_page
    for tab in ['in_work', 'ready']:
        html = client.get(f'/body-repair?tab={tab}').get_data(as_text=True)
        assert 'id="bodyRepairCount">3</strong>' in html
        assert html.index('>Готовые</a>') < html.index('id="bodyRepairCount"')


def test_checked_flag_is_rendered_and_preserved_on_completion(body_page):
    mod, body, client, ids, token = body_page
    post_approval(body_page, ids[0], {'is_approved': '1'})
    response = client.post(f'/body-repair/{ids[0]}/complete', headers={'X-CSRFToken': token})
    assert response.status_code == 302
    html = client.get('/body-repair?tab=ready').get_data(as_text=True)
    checkbox = re.search(r'<input[^>]*aria-label="Заявка по борту 164001 согласована"[^>]*>', html)[0]
    assert 'checked' in checkbox
    assert 'disabled' in checkbox
    assert f'/body-repair/{ids[0]}/approval' not in html
    # Вторая активная запись 164001 остаётся — счётчик бортов не уменьшается.
    assert 'id="bodyRepairCount">2</strong>' in html
    mod.db.session.expire_all()
    assert mod.db.session.get(body.BodyRepair, ids[0]).is_approved is True


def test_other_park_and_completed_records_cannot_be_changed(body_page):
    mod, body, _, ids, _ = body_page
    assert post_approval(body_page, ids[4], {'is_approved': '1'}).status_code == 404
    assert post_approval(body_page, ids[3], {'is_approved': '0'}).status_code == 400
    mod.db.session.expire_all()
    assert mod.db.session.get(body.BodyRepair, ids[4]).is_approved is False
    assert mod.db.session.get(body.BodyRepair, ids[3]).is_approved is True


def test_approval_requires_csrf_and_valid_value(body_page):
    mod, body, client, ids, _ = body_page
    assert client.post(f'/body-repair/{ids[0]}/approval', data={'is_approved': '1'}).status_code == 400
    assert post_approval(body_page, ids[0], {'is_approved': 'unexpected'}).status_code == 400
    mod.db.session.expire_all()
    assert mod.db.session.get(body.BodyRepair, ids[0]).is_approved is False


def test_unavailable_selected_park_is_forbidden(body_page):
    _, _, client, ids, _ = body_page
    with client.session_transaction() as sess:
        sess['selected_park'] = '1 парк'
    assert post_approval(body_page, ids[0], {'is_approved': '1'}).status_code == 403


def test_empty_park_count_is_zero(body_page):
    _, _, client, _, _ = body_page
    with client.session_transaction() as sess:
        sess['selected_park'] = None
    html = client.get('/body-repair').get_data(as_text=True)
    assert 'id="bodyRepairCount">0</strong>' in html
    assert 'colspan="10"' in html


def test_google_sync_preserves_local_approval(body_page, monkeypatch):
    mod, body, _, ids, _ = body_page
    record = mod.db.session.get(body.BodyRepair, ids[0])
    record.is_approved = True
    record.source_link = 'https://example.test/repair/1'
    record.repair_status = 'in_work'
    mod.db.session.commit()
    row = [''] * 14
    row[5], row[7], row[8], row[13] = '164001', 'Теплый Стан', date.today().strftime('%d.%m.%Y'), 'В работе'
    class Worksheet:
        def get_all_values(self):
            return [[''] * 14, row]
        def batch_update(self, *args, **kwargs):
            pass
    monkeypatch.setattr(body, '_get_links_by_row', lambda *args: {2: 'https://example.test/repair/1'})
    result = body._sync_one_sheet(object(), body.SHEET_CONFIGS[0], Worksheet())
    assert result['updated'] == 1
    mod.db.session.expire_all()
    assert mod.db.session.get(body.BodyRepair, ids[0]).is_approved is True


def post_reopen(page, record_id):
    _, _, client, _, token = page
    return client.post(f'/body-repair/{record_id}/reopen', headers={'X-CSRFToken': token})


def test_reopen_preserves_approval_and_details_and_updates_counter(body_page):
    mod, body, client, ids, _ = body_page
    record = mod.db.session.get(body.BodyRepair, ids[3])
    record.work_order, record.note, record.repair_type = '12345', 'Сохранить', 'warranty'
    record.started_at = date(2026, 11, 1)
    mod.db.session.commit()
    response = post_reopen(body_page, ids[3])
    assert response.status_code == 302
    assert 'tab=in_work' in response.headers['Location']
    mod.db.session.expire_all()
    record = mod.db.session.get(body.BodyRepair, ids[3])
    assert record.is_completed is False
    assert record.completed_at is None
    assert record.repair_status == 'in_work'
    assert record.is_approved is True
    assert (record.work_order, record.note, record.repair_type, record.started_at) == ('12345', 'Сохранить', 'warranty', date(2026, 11, 1))
    assert record.reopen_pending is False
    html = client.get('/body-repair').get_data(as_text=True)
    assert 'id="bodyRepairCount">4</strong>' in html
    assert '164003' in html


def test_repeated_reopen_does_not_duplicate_or_reset_active_status(body_page):
    mod, body, _, ids, _ = body_page
    post_reopen(body_page, ids[3])
    record = mod.db.session.get(body.BodyRepair, ids[3])
    record.repair_status = 'waiting_parts'
    mod.db.session.commit()
    assert post_reopen(body_page, ids[3]).status_code == 302
    mod.db.session.expire_all()
    assert mod.db.session.get(body.BodyRepair, ids[3]).repair_status == 'waiting_parts'
    assert body.BodyRepair.query.count() == 5


def test_reopen_requires_csrf_and_park_access(body_page):
    mod, body, client, ids, _ = body_page
    assert client.post(f'/body-repair/{ids[3]}/reopen').status_code == 400
    assert post_reopen(body_page, ids[4]).status_code == 404
    with client.session_transaction() as sess:
        sess['selected_park'] = '1 парк'
    assert post_reopen(body_page, ids[3]).status_code == 403
    mod.db.session.expire_all()
    assert mod.db.session.get(body.BodyRepair, ids[3]).is_completed is True


def test_reopen_is_post_only(body_page):
    _, _, client, ids, _ = body_page
    assert client.get(f'/body-repair/{ids[3]}/reopen').status_code == 405


def test_reopen_button_is_per_ready_row_only(body_page):
    _, _, client, ids, _ = body_page
    ready = client.get('/body-repair?tab=ready').get_data(as_text=True)
    assert f'/body-repair/{ids[3]}/reopen' in ready
    assert '<span>Возврат</span>' in ready
    assert 'aria-label="Вернуть борт 164003 в работу"' in ready
    active = client.get('/body-repair?tab=in_work').get_data(as_text=True)
    assert '/reopen' not in active


def prepare_reopened_google_record(page, monkeypatch, fail=False):
    mod, body, _, ids, _ = page
    record = mod.db.session.get(body.BodyRepair, ids[3])
    record.source_link = 'https://example.test/repair/reopen'
    mod.db.session.commit()
    post_reopen(page, ids[3])
    row = [''] * 14
    row[5], row[7], row[13] = '164003', 'Теплый Стан', 'Готово'
    class Worksheet:
        def __init__(self):
            self.updates = []
        def get_all_values(self):
            return [[''] * 14, row]
        def batch_update(self, updates, **kwargs):
            if fail:
                raise RuntimeError('Google unavailable')
            self.updates.extend(updates)
            for update in updates:
                if update['range'] == 'N2':
                    row[13] = update['values'][0][0]
    monkeypatch.setattr(body, '_get_links_by_row', lambda *args: {2: record.source_link})
    return Worksheet(), row


def test_reopen_is_sent_to_google_before_accepting_old_ready_status(body_page, monkeypatch):
    mod, body, _, ids, _ = body_page
    worksheet, row = prepare_reopened_google_record(body_page, monkeypatch)
    body._sync_one_sheet(object(), body.SHEET_CONFIGS[0], worksheet)
    mod.db.session.expire_all()
    record = mod.db.session.get(body.BodyRepair, ids[3])
    assert record.is_completed is False
    assert record.reopen_pending is False
    assert record.is_approved is True
    assert {'range': 'N2', 'values': [['В работе']]} in worksheet.updates
    # После успешной отправки новое завершение заказчиком снова принимается.
    row[13] = 'Готово'
    body._sync_one_sheet(object(), body.SHEET_CONFIGS[0], worksheet)
    mod.db.session.expire_all()
    assert mod.db.session.get(body.BodyRepair, ids[3]).is_completed is True


def test_failed_google_write_keeps_reopened_record_in_work(body_page, monkeypatch):
    mod, body, _, ids, _ = body_page
    worksheet, _ = prepare_reopened_google_record(body_page, monkeypatch, fail=True)
    with pytest.raises(RuntimeError, match='Google unavailable'):
        body._sync_one_sheet(object(), body.SHEET_CONFIGS[0], worksheet)
    mod.db.session.remove()
    record = mod.db.session.get(body.BodyRepair, ids[3])
    assert record.is_completed is False
    assert record.reopen_pending is True


def test_completing_again_cancels_pending_reopen(body_page, monkeypatch):
    mod, body, client, ids, token = body_page
    prepare_reopened_google_record(body_page, monkeypatch)
    response = client.post(f'/body-repair/{ids[3]}/complete', headers={'X-CSRFToken': token})
    assert response.status_code == 302
    mod.db.session.expire_all()
    record = mod.db.session.get(body.BodyRepair, ids[3])
    assert record.is_completed is True
    assert record.reopen_pending is False


@pytest.mark.parametrize('config_index,target_cell', [(0, 'P2'), (1, 'P2')])
@pytest.mark.parametrize('ready', [False, True])
def test_google_sync_exports_reason_to_matching_sheet_column(body_page, monkeypatch, config_index, target_cell, ready):
    mod, body, _, ids, _ = body_page
    config = body.SHEET_CONFIGS[config_index]
    record = mod.db.session.get(body.BodyRepair, ids[0])
    record.source_link = 'https://example.test/repair/reason'
    record.reason = 'Замена повреждённой панели'
    record.is_completed = ready
    mod.db.session.commit()
    row = [''] * 16
    row[5], row[7] = '164001', 'Теплый Стан'
    row[config['status_col']] = 'Готово' if ready else 'В работе'
    row[config['reason_col']] = 'Прежняя причина'

    class Worksheet:
        def __init__(self):
            self.updates = []

        def get_all_values(self):
            return [[''] * 16, row]

        def batch_update(self, updates, **kwargs):
            self.updates.extend(updates)

    worksheet = Worksheet()
    monkeypatch.setattr(body, '_get_links_by_row', lambda *args: {2: record.source_link})
    body._sync_one_sheet(object(), config, worksheet)
    assert {'range': target_cell, 'values': [['Замена повреждённой панели']]} in worksheet.updates
    assert record.reason == 'Замена повреждённой панели'

    # Совпадающее значение и пустая причина на сайте не перезаписывают Google.
    row[config['reason_col']] = record.reason
    worksheet.updates.clear()
    body._sync_one_sheet(object(), config, worksheet)
    assert not any(update['range'] == target_cell for update in worksheet.updates)
    record.reason = ''
    mod.db.session.commit()
    row[config['reason_col']] = 'Причина в Google'
    worksheet.updates.clear()
    body._sync_one_sheet(object(), config, worksheet)
    assert not any(update['range'] == target_cell for update in worksheet.updates)


@pytest.mark.parametrize('park,source', [('1 парк', 'Коньково'), ('Красная Пахра', 'Красная Пахра')])
def test_google_sync_new_parks_only_import_matching_rows(body_page, monkeypatch, park, source):
    mod, body, _, _, _ = body_page
    rows = [[''] * 16]
    links = {}
    for number, name in enumerate(['Теплый Стан', 'Коньково', 'Красная Пахра', 'Другой парк'], start=2):
        row = [''] * 16
        row[5], row[7], row[13] = str(200000 + number), name, 'В работе'
        rows.append(row)
        links[number] = f'https://example.test/repair/park/{number}'

    class Worksheet:
        def get_all_values(self):
            return rows

        def batch_update(self, updates, **kwargs):
            pass

    monkeypatch.setattr(body, '_get_links_by_row', lambda *args: links)
    body._sync_one_sheet(object(), body.SHEET_CONFIGS[0], Worksheet(), park=park)
    records = body.BodyRepair.query.filter(body.BodyRepair.source_link.isnot(None)).all()
    assert len(records) == 1
    record = records[0]
    assert record.park == park
    source_row = next(number for number, row in enumerate(rows) if row[7] == source)
    assert record.bort == rows[source_row][5]
    assert record.source_link == links[source_row + 1]


@pytest.mark.parametrize('park', ['1 парк', 'Красная Пахра'])
def test_new_park_sync_only_opens_first_sheet(body_page, monkeypatch, park):
    _, body, client, _, token = body_page
    opened, synced = [], []
    monkeypatch.setattr(body.ServiceAccountCredentials, 'from_json_keyfile_name', lambda *args: object())

    class GoogleClient:
        def open_by_key(self, key):
            return object()

    monkeypatch.setattr(body.gspread, 'authorize', lambda *args: GoogleClient())
    monkeypatch.setattr(body, '_get_sheet', lambda sheet, gid: opened.append(gid) or object())
    def sync(sheet, config, worksheet, park):
        synced.append((config['gid'], park))
        return {'imported': 0, 'written': 0}
    monkeypatch.setattr(body, '_sync_one_sheet', sync)
    with client.session_transaction() as sess:
        sess['selected_park'] = park
        sess['available_parks'] = [park]
    assert 'Синхронизировать с Google' in client.get('/body-repair').get_data(as_text=True)
    response = client.post('/body-repair/sync', headers={'X-CSRFToken': token})
    assert response.status_code == 302
    assert opened == [870612608]
    assert synced == [(870612608, park)]


@pytest.mark.parametrize('status',['Готов','Готово',' ГОТОВ\u00a0'])
def test_ready_rows_on_shifted_second_sheet_are_never_imported(body_page,monkeypatch,status):
    mod,body,_,_,_=body_page
    config={**body.SHEET_CONFIGS[1],'status_col':11,'reason_col':13,'work_order_col':9}
    headers=['']*16
    headers[10],headers[11],headers[13],headers[15]='Заказ-наряд','Дата готовности ТС','Статус','Комментарий СЭТранс'
    row=['']*16;row[5]='164076';row[7]='Теплый Стан';row[11]='15.11.2026';row[13]=status
    class Worksheet:
        def get_all_values(self):return [headers,row]
        def batch_update(self,*args,**kwargs):raise AssertionError('Готовая новая строка не должна записываться')
    monkeypatch.setattr(body,'_get_links_by_row',lambda *a:{2:'https://example.test/ready-164076'})
    result=body._sync_one_sheet(object(),config,Worksheet())
    assert result['imported']==0
    assert body.BodyRepair.query.filter_by(bort='164076').count()==0


def test_shifted_headers_mark_existing_ready_and_do_not_overwrite_status_with_reason(body_page,monkeypatch):
    mod,body,_,ids,_=body_page
    record=mod.db.session.get(body.BodyRepair,ids[0]);record.source_link='https://example.test/ready-existing'
    record.reason='Ремонт панели';record.work_order='ТС123';mod.db.session.commit()
    config={**body.SHEET_CONFIGS[1],'status_col':11,'reason_col':13,'work_order_col':9}
    headers=['']*16
    headers[10],headers[13],headers[15]='Заказ-наряд','Статус','Комментарий СЭТранс'
    row=['']*16;row[5]='164001';row[7]='Теплый Стан';row[13]='Готов'
    class Worksheet:
        def __init__(self):self.updates=[]
        def get_all_values(self):return [headers,row]
        def batch_update(self,updates,**kwargs):self.updates.extend(updates)
    worksheet=Worksheet();monkeypatch.setattr(body,'_get_links_by_row',lambda *a:{2:record.source_link})
    body._sync_one_sheet(object(),config,worksheet)
    assert record.is_completed
    assert {'range':'P2','values':[['Ремонт панели']]} in worksheet.updates
    assert {'range':'K2','values':[['ТС123']]} in worksheet.updates
    assert all(update['values']!=[['Ремонт панели']] for update in worksheet.updates if update['range']=='N2')


def test_missing_status_header_stops_sync_before_import(body_page,monkeypatch):
    _,body,_,_,_=body_page
    class Worksheet:
        def get_all_values(self):return [['Бортовой номер','Неизвестная структура']]
    monkeypatch.setattr(body,'_get_links_by_row',lambda *a:{})
    with pytest.raises(ValueError,match='Статус'):body._sync_one_sheet(object(),body.SHEET_CONFIGS[0],Worksheet())
