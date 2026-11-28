"""Сквозная проверка: бейдж incontrol появляется на странице списка задач.

Тесты на моках доказывают логику, но не то, что данные доходят до шаблона.
Здесь страница отрисовывается целиком через тестовый клиент Flask.
"""
from datetime import timedelta

from incontrol import incontrol_now

import pytest


@pytest.fixture
def клиент_с_задачей(app_ctx):
    """Пользователь, задача в ремонте и вошедшая сессия."""
    from werkzeug.security import generate_password_hash

    app_ctx.db.session.add(app_ctx.User(
        username='tester',
        password=generate_password_hash('secret', method='pbkdf2:sha256:600000'),
        full_name='Тестировщик',
        allowed_parks=['Теплый Стан'],
        default_park='Теплый Стан',
        # Без этого шаблон не отрисовывает список: он весь обёрнут
        # в {% if current_user.company == 'Сетранс' %}
        company='Сетранс',
    ))
    app_ctx.db.session.add(app_ctx.Task(
        board_number='162345', park='Теплый Стан', status='in_repair',
        description='тестовая задача', tag='in_work', type_ts='Автобус',
    ))
    app_ctx.db.session.commit()

    app_ctx.application.config['WTF_CSRF_ENABLED'] = False
    client = app_ctx.application.test_client()
    client.post('/login', data={'username': 'tester', 'password': 'secret'},
                follow_redirects=True)
    return client


#: Ищем именно вёрстку бейджа, а не имя класса: .incontrol-badge объявлен
#: в блоке стилей и присутствует на странице всегда.
МЕТКА = 'class="incontrol-badge"'


def _страница(client):
    ответ = client.get('/', follow_redirects=True)
    assert ответ.status_code == 200
    html = ответ.get_data(as_text=True)
    # Список задач должен отрисоваться, иначе тест ничего не проверяет
    assert '162345' in html, 'задача не попала на страницу'
    return html


def test_бейджа_нет_когда_заявки_нет(app_ctx, клиент_с_задачей):
    assert МЕТКА not in _страница(клиент_с_задачей)


def test_бейдж_появляется_при_открытой_заявке(app_ctx, клиент_с_задачей):
    app_ctx.db.session.add(app_ctx.incontrol_integration.status_model(
        board_number='162345', has_open=True,
        phase_name='Назначено.', checked_at=incontrol_now(),
    ))
    app_ctx.db.session.commit()

    страница = _страница(клиент_с_задачей)
    assert МЕТКА in страница
    assert 'В incontrol24 есть открытая заявка' in страница


def test_бейдж_гаснет_когда_проверка_устарела(app_ctx, клиент_с_задачей):
    # После порога данные уже ничего не утверждают
    app_ctx.db.session.add(app_ctx.incontrol_integration.status_model(
        board_number='162345', has_open=True, phase_name='Назначено.',
        checked_at=incontrol_now()
        - timedelta(minutes=31),
    ))
    app_ctx.db.session.commit()

    assert МЕТКА not in _страница(клиент_с_задачей)
