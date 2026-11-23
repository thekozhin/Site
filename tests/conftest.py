"""Общие фикстуры. Приложение импортируется с тестовым окружением.

Переменные выставляются ДО импорта app: он требует SECRET_KEY и стартует
планировщик прямо при импорте.
"""
import os

import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('DISABLE_SCHEDULER', '1')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-dummy')
os.environ.setdefault('SESSION_COOKIE_SECURE', '0')
os.environ.setdefault('INCONTROL_ENABLED', '0')
os.environ['YANDEX_MAIL_ENABLED'] = '0'


@pytest.fixture
def app_ctx():
    """Контекст приложения с пустой базой в памяти."""
    import app as app_module

    application = app_module.application
    application.config['TESTING'] = True
    with application.app_context():
        app_module.db.create_all()
        yield app_module
        app_module.db.session.remove()
        app_module.db.drop_all()
