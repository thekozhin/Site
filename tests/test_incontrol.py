"""Тесты модуля incontrol: чистая логика и клиент API."""
import pytest

import incontrol


def test_нормализация_приводит_число_и_строку_к_одному_виду():
    # API отдаёт борт то строкой, то числом — иначе совпадения теряются молча
    assert incontrol.normalize_board(460653) == incontrol.normalize_board('460653')


def test_нормализация_обрезает_пробелы():
    assert incontrol.normalize_board('  164076 ') == '164076'


def test_нормализация_пустых_значений_даёт_none():
    assert incontrol.normalize_board(None) is None
    assert incontrol.normalize_board('') is None
    assert incontrol.normalize_board('   ') is None


def test_борт_достаётся_из_объекта_обслуживания():
    task = {
        'serviced_object': {
            'additional_fields': [
                {'field_name': 'Филиал', 'value': 'ф. Южный'},
                {'field_name': 'Бортовой номер', 'value': '461606'},
                {'field_name': 'VIN', 'value': 'XTC522220SN500373'},
            ]
        }
    }
    assert incontrol.board_of(task) == '461606'


def test_борт_числом_тоже_достаётся_строкой():
    task = {'serviced_object': {'additional_fields': [
        {'field_name': 'Бортовой номер', 'value': 460653},
    ]}}
    assert incontrol.board_of(task) == '460653'


def test_борт_отсутствует_если_поля_нет():
    assert incontrol.board_of({'serviced_object': {'additional_fields': []}}) is None
    assert incontrol.board_of({'serviced_object': {}}) is None
    assert incontrol.board_of({}) is None


def test_парк_переводится_в_организацию():
    assert incontrol.park_to_org('Теплый Стан') == 1069
    assert incontrol.park_to_org('Красная Пахра') == 1060
    assert incontrol.park_to_org('Коньково') == 1059


def test_неизвестный_парк_даёт_none():
    assert incontrol.park_to_org(None) is None
    assert incontrol.park_to_org('') is None
    assert incontrol.park_to_org('Неизвестный парк') is None


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(payload)

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload


class FakeSession:
    """Подменяет requests.Session: отдаёт заранее заданные ответы по очереди."""

    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(('POST', url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        return self.responses.pop(0)


def make_client(responses):
    session = FakeSession(responses)
    client = incontrol.IncontrolClient('user@example.com', 'секрет', session=session)
    return client, session


def test_логин_сохраняет_токен_в_заголовке():
    client, session = make_client([
        FakeResponse(200, {'access': 'A1', 'refresh': 'R1'}),
    ])
    client.authenticate()
    assert session.headers['Authorization'] == 'JWT A1'
    assert session.headers['X-Incontrol-App'] == incontrol.APP_ID


def test_двухфакторка_даёт_понятную_ошибку():
    # Если заказчик включит подтверждение кодом, токенов в ответе не будет
    client, _ = make_client([FakeResponse(200, {'use_auth_code': True})])
    with pytest.raises(incontrol.IncontrolError, match='код подтверждения'):
        client.authenticate()


def test_неверный_логин_даёт_ошибку():
    client, _ = make_client([FakeResponse(401, {'detail': 'no'})])
    with pytest.raises(incontrol.IncontrolError):
        client.authenticate()


def test_при_401_токен_обновляется_и_запрос_повторяется():
    # Access-токен живёт около 5 минут, это штатная ситуация, а не сбой
    client, session = make_client([
        FakeResponse(200, {'access': 'A1', 'refresh': 'R1'}),
        FakeResponse(401, {'code': 'token_not_valid'}),
        FakeResponse(200, {'access': 'A2', 'refresh': 'R2'}),
        FakeResponse(200, {'results': []}),
    ])
    client.authenticate()
    result = client._get('/tasks/', [('search', '1')], 1060)
    assert result == {'results': []}
    assert session.headers['Authorization'] == 'JWT A2'


def test_повторный_401_не_зацикливается():
    client, session = make_client([
        FakeResponse(200, {'access': 'A1', 'refresh': 'R1'}),
        FakeResponse(401, {}),
        FakeResponse(200, {'access': 'A2', 'refresh': 'R2'}),
        FakeResponse(401, {}),
    ])
    client.authenticate()
    with pytest.raises(incontrol.IncontrolError):
        client._get('/tasks/', [], 1060)
    assert len(session.responses) == 0


def test_организация_передаётся_заголовком():
    client, session = make_client([
        FakeResponse(200, {'access': 'A1', 'refresh': 'R1'}),
        FakeResponse(200, {'results': []}),
    ])
    client.authenticate()
    client._get('/tasks/', [], 1059)
    _, _, kwargs = session.calls[-1]
    assert kwargs['headers']['X-Incontrol-Organization'] == '1059'


def задача(board, phase='Назначено.'):
    return {
        'phase': {'id': 2, 'phase_name': phase},
        'serviced_object': {'additional_fields': [
            {'field_name': 'Бортовой номер', 'value': board},
        ]},
    }


def test_борт_с_открытой_заявкой_найден():
    client, _ = make_client([
        FakeResponse(200, {'access': 'A1', 'refresh': 'R1'}),
        FakeResponse(200, {'results': [задача('162345')]}),
    ])
    client.authenticate()
    assert client.check_board('162345', 'Теплый Стан') == (True, 'Назначено.')


def test_пустая_выдача_значит_заявки_нет():
    client, _ = make_client([
        FakeResponse(200, {'access': 'A1', 'refresh': 'R1'}),
        FakeResponse(200, {'results': []}),
    ])
    client.authenticate()
    assert client.check_board('162345', 'Теплый Стан') == (False, None)


def test_запрос_идёт_с_фильтром_открытых_фаз():
    client, session = make_client([
        FakeResponse(200, {'access': 'A1', 'refresh': 'R1'}),
        FakeResponse(200, {'results': []}),
    ])
    client.authenticate()
    client.check_board('162345', 'Теплый Стан')
    _, _, kwargs = session.calls[-1]
    params = kwargs['params']
    assert ('search', '162345') in params
    assert [v for k, v in params if k == 'phase'] == [1, 2, 3]


def test_чужой_борт_в_выдаче_игнорируется():
    # Страховка на случай, если search перестанет быть точным совпадением
    client, _ = make_client([
        FakeResponse(200, {'access': 'A1', 'refresh': 'R1'}),
        FakeResponse(200, {'results': [задача('999999')]}),
    ])
    client.authenticate()
    assert client.check_board('162345', 'Теплый Стан') == (False, None)


def test_номер_числом_совпадает_с_номером_строкой():
    client, _ = make_client([
        FakeResponse(200, {'access': 'A1', 'refresh': 'R1'}),
        FakeResponse(200, {'results': [задача(460653)]}),
    ])
    client.authenticate()
    assert client.check_board('460653', 'Красная Пахра') == (True, 'Назначено.')


def test_неизвестный_парк_проверяется_по_всем_организациям():
    client, session = make_client([
        FakeResponse(200, {'access': 'A1', 'refresh': 'R1'}),
        FakeResponse(200, {'results': []}),
        FakeResponse(200, {'results': []}),
        FakeResponse(200, {'results': [задача('162345')]}),
    ])
    client.authenticate()
    assert client.check_board('162345', None) == (True, 'Назначено.')
    orgs = [c[2]['headers']['X-Incontrol-Organization'] for c in session.calls[1:]]
    assert orgs == ['1069', '1060', '1059']


def test_пустой_борт_не_вызывает_запросов():
    client, session = make_client([
        FakeResponse(200, {'access': 'A1', 'refresh': 'R1'}),
    ])
    client.authenticate()
    assert client.check_board('', 'Теплый Стан') == (False, None)
    assert len(session.calls) == 1


def test_внутренние_коды_парков_переводятся_в_организации():
    # В базе Task.park хранит коды, а не названия: см. PARK_NAME в app.py
    assert incontrol.park_to_org('1 парк') == 1059      # Коньково
    assert incontrol.park_to_org('14 парк') == 1069     # Теплый Стан
    assert incontrol.park_to_org('Красная Пахра') == 1060


def test_ясенево_не_проверяется_в_incontrol24():
    # 18 парк - Ясенево, такой организации у учётной записи нет.
    # Проверять его по всем трём организациям означало бы тратить
    # по три запроса впустую на каждый борт.
    assert incontrol.park_to_org('18 парк') is None
    assert incontrol.is_outside_incontrol('18 парк') is True
    assert incontrol.is_outside_incontrol('1 парк') is False


def test_борт_из_ясенево_не_вызывает_запросов():
    client, session = make_client([
        FakeResponse(200, {'access': 'A1', 'refresh': 'R1'}),
    ])
    client.authenticate()
    assert client.check_board('180001', '18 парк') == (False, None)
    assert len(session.calls) == 1  # только вход, ни одного GET


def стр(борта, next_page=None):
    """Страница выдачи /tasks/ с перечисленными бортами."""
    return FakeResponse(200, {
        'results': [задача(b) for b in борта],
        'next': next_page,
    })


def test_обход_собирает_борта_со_всех_страниц():
    client, session = make_client([
        FakeResponse(200, {'access': 'A1', 'refresh': 'R1'}),
        стр(['164076', '164075'], next_page='page=2'),
        стр(['162643'], next_page=None),
    ])
    client.authenticate()
    борта = client.fetch_open_boards(1069)
    assert set(борта) == {'164076', '164075', '162643'}
    assert борта['164076'] == 'Назначено.'


def test_обход_запрашивает_крупные_страницы_и_открытые_фазы():
    client, session = make_client([
        FakeResponse(200, {'access': 'A1', 'refresh': 'R1'}),
        стр([], next_page=None),
    ])
    client.authenticate()
    client.fetch_open_boards(1060)
    _, _, kwargs = session.calls[-1]
    params = kwargs['params']
    # per_page - единственный работающий параметр размера страницы
    assert ('per_page', incontrol.PAGE_SIZE) in params
    assert [v for k, v in params if k == 'phase'] == [1, 2, 3]
    assert kwargs['headers']['X-Incontrol-Organization'] == '1060'


def test_обход_останавливается_без_следующей_страницы():
    client, session = make_client([
        FakeResponse(200, {'access': 'A1', 'refresh': 'R1'}),
        стр(['162643'], next_page=None),
    ])
    client.authenticate()
    client.fetch_open_boards(1069)
    assert len(session.calls) == 2  # вход + одна страница
