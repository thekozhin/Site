"""Тесты кеша статусов incontrol24."""
from datetime import datetime, timedelta


def test_запись_статуса_сохраняется(app_ctx):
    статус = app_ctx.IncontrolStatus(
        board_number='162345',
        has_open=True,
        phase_name='Назначено.',
        checked_at=datetime(2026, 8, 2, 14, 30),
    )
    app_ctx.db.session.add(статус)
    app_ctx.db.session.commit()

    найдено = app_ctx.db.session.get(app_ctx.IncontrolStatus, '162345')
    assert найдено.has_open is True
    assert найдено.phase_name == 'Назначено.'


def _добавить(app_ctx, board, has_open, minutes_ago):
    app_ctx.db.session.add(app_ctx.IncontrolStatus(
        board_number=board,
        has_open=has_open,
        phase_name='Назначено.' if has_open else None,
        checked_at=app_ctx.incontrol_now() - timedelta(minutes=minutes_ago),
    ))
    app_ctx.db.session.commit()


def test_свежий_борт_с_заявкой_попадает_в_выдачу(app_ctx):
    _добавить(app_ctx, '162345', True, minutes_ago=1)
    assert app_ctx.get_incontrol_boards(['162345']) == {'162345'}


def test_борт_без_заявки_не_попадает(app_ctx):
    _добавить(app_ctx, '162345', False, minutes_ago=1)
    assert app_ctx.get_incontrol_boards(['162345']) == set()


def test_устаревшая_проверка_не_попадает(app_ctx):
    # После порога мы не знаем, а не «заявки нет»
    _добавить(app_ctx, '162345', True,
              minutes_ago=app_ctx.INCONTROL_STALE_MINUTES + 1)
    assert app_ctx.get_incontrol_boards(['162345']) == set()


def test_запрашиваются_только_переданные_борта(app_ctx):
    _добавить(app_ctx, '162345', True, minutes_ago=1)
    _добавить(app_ctx, '999999', True, minutes_ago=1)
    assert app_ctx.get_incontrol_boards(['162345']) == {'162345'}


def test_пустой_список_не_ходит_в_базу(app_ctx):
    assert app_ctx.get_incontrol_boards([]) == set()


def _задача(app_ctx, board, park='14 парк', status='in_repair'):
    app_ctx.db.session.add(app_ctx.Task(
        board_number=board, park=park, status=status,
        description='тест', tag='in_work',
    ))
    app_ctx.db.session.commit()


def _включить(app_ctx, monkeypatch, клиент):
    """В тестовом окружении интеграция выключена — включаем точечно."""
    monkeypatch.setattr(app_ctx, 'INCONTROL_ENABLED', True)
    monkeypatch.setattr(app_ctx, '_make_incontrol_client', lambda: клиент)


def test_выключённая_интеграция_ничего_не_делает(app_ctx, monkeypatch):
    _задача(app_ctx, '162345')
    monkeypatch.setattr(app_ctx, 'INCONTROL_ENABLED', False)

    итог = app_ctx.refresh_incontrol_statuses()

    assert итог['status'] == 'disabled'


def test_отсутствие_таблицы_не_роняет_страницу(app_ctx):
    """Список задач не должен падать, если кеша ещё нет.

    Так бывает до первого создания таблицы на сервере. Бейдж - вспомогательная
    метка, она не имеет права обрушивать главную страницу.
    """
    app_ctx.IncontrolStatus.__table__.drop(app_ctx.db.engine)
    assert app_ctx.get_incontrol_boards(['162345']) == set()


class FakeSweepClient:
    """Подменяет клиент: отдаёт готовые множества бортов по организациям."""

    def __init__(self, по_организациям, падать_на=()):
        self.по_организациям = по_организациям
        self.падать_на = set(падать_на)
        self.обойдено = []

    def authenticate(self):
        pass

    def fetch_open_boards(self, org_id):
        self.обойдено.append(org_id)
        if org_id in self.падать_на:
            raise RuntimeError('сеть недоступна')
        return self.по_организациям.get(org_id, {})


def test_обход_идёт_по_всем_организациям(app_ctx, monkeypatch):
    _задача(app_ctx, '164076', park='14 парк')
    клиент = FakeSweepClient({1069: {'164076': 'Назначено.'}})
    _включить(app_ctx, monkeypatch, клиент)

    итог = app_ctx.refresh_incontrol_statuses()

    assert клиент.обойдено == [1069, 1060, 1059]
    assert итог['ошибок'] == 0
    assert app_ctx.get_incontrol_boards(['164076']) == {'164076'}


def test_борт_без_заявки_помечается_закрытым(app_ctx, monkeypatch):
    _задача(app_ctx, '164076', park='14 парк')
    клиент = FakeSweepClient({1069: {}})
    _включить(app_ctx, monkeypatch, клиент)

    app_ctx.refresh_incontrol_statuses()

    assert app_ctx.get_incontrol_boards(['164076']) == set()


def test_сбой_обхода_не_гасит_бейджи_этой_организации(app_ctx, monkeypatch):
    # Иначе обрыв сети выглядел бы как закрытие заявок у заказчика
    _задача(app_ctx, '164076', park='14 парк')
    _добавить(app_ctx, '164076', True, minutes_ago=1)
    клиент = FakeSweepClient({}, падать_на=[1069])
    _включить(app_ctx, monkeypatch, клиент)

    итог = app_ctx.refresh_incontrol_statuses()

    assert итог['ошибок'] == 1
    assert app_ctx.get_incontrol_boards(['164076']) == {'164076'}


def test_сбой_одной_организации_не_мешает_другой(app_ctx, monkeypatch):
    _задача(app_ctx, '164076', park='14 парк')
    _задача(app_ctx, '460653', park='Красная Пахра')
    клиент = FakeSweepClient({1060: {'460653': 'Принято.'}}, падать_на=[1069])
    _включить(app_ctx, monkeypatch, клиент)

    app_ctx.refresh_incontrol_statuses()

    assert app_ctx.get_incontrol_boards(['460653']) == {'460653'}


def test_ясенево_помечается_без_обращения_к_api(app_ctx, monkeypatch):
    _задача(app_ctx, '180001', park='18 парк')
    клиент = FakeSweepClient({})
    _включить(app_ctx, monkeypatch, клиент)

    app_ctx.refresh_incontrol_statuses()

    assert app_ctx.get_incontrol_boards(['180001']) == set()


def test_борт_неизвестного_парка_ищется_во_всех_организациях(app_ctx, monkeypatch):
    _задача(app_ctx, '162345', park=None)
    клиент = FakeSweepClient({1059: {'162345': 'Новое.'}})
    _включить(app_ctx, monkeypatch, клиент)

    app_ctx.refresh_incontrol_statuses()

    assert app_ctx.get_incontrol_boards(['162345']) == {'162345'}


def test_в_кеш_попадают_только_задачи_в_ремонте(app_ctx, monkeypatch):
    _задача(app_ctx, '164076', park='14 парк', status='in_repair')
    _задача(app_ctx, '164077', park='14 парк', status='completed')
    клиент = FakeSweepClient({1069: {'164076': 'Назначено.', '164077': 'Новое.'}})
    _включить(app_ctx, monkeypatch, клиент)

    итог = app_ctx.refresh_incontrol_statuses()

    assert итог['проверено'] == 1
    борта = {r.board_number for r in app_ctx.IncontrolStatus.query.all()}
    assert борта == {'164076'}


def test_повторяющийся_борт_даёт_одну_запись(app_ctx, monkeypatch):
    # На один борт может быть заведено несколько задач
    _задача(app_ctx, '164076', park='14 парк')
    _задача(app_ctx, '164076', park='14 парк')
    клиент = FakeSweepClient({1069: {'164076': 'Принято.'}})
    _включить(app_ctx, monkeypatch, клиент)

    итог = app_ctx.refresh_incontrol_statuses()

    assert итог['проверено'] == 1
    assert app_ctx.IncontrolStatus.query.count() == 1


def test_сбойная_организация_попадает_в_пропущенные(app_ctx, monkeypatch):
    _задача(app_ctx, '164076', park='14 парк')
    клиент = FakeSweepClient({}, падать_на=[1069])
    _включить(app_ctx, monkeypatch, клиент)

    итог = app_ctx.refresh_incontrol_statuses()

    assert итог['пропущено'] == 1
    assert итог['проверено'] == 0
