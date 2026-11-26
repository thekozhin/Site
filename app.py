import logging
from logging.handlers import RotatingFileHandler
import threading
import time as _time
import math

import io
import os
import re
from datetime import datetime, timedelta, time, date
from io import BytesIO

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from docxtpl import DocxTemplate
from flask import Flask, session, abort
from flask import jsonify, send_file, make_response, current_app
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user,login_required
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf.csrf import CSRFProtect, generate_csrf
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import and_, or_, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from flask import render_template, request, redirect, url_for, flash
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import incontrol
import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment
from sqlalchemy import JSON
from flask import Flask, request, redirect
from user_agents import parse
import json
from flask import jsonify
from sqlalchemy import func
import requests
import calendar
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from flask import send_file
from sqlalchemy import case
import holidays
from typing import Optional
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
from openpyxl.styles import Font, Alignment, Border, Side
from zoneinfo import ZoneInfo
from yandex_mail import MAIL_TASK_AUTHOR, MailError
from task_import_matching import active_repairs_for_board, manual_repair_candidates, match_imported_repair


load_dotenv()

application = Flask(__name__)
DISABLED_PARKS = os.getenv('DISABLED_PARKS', '').split(',')
# Секретный ключ обязателен: без него сессии можно подделать.
# Если переменная не задана - падаем при старте, а не работаем с известным ключом.
secret_key = os.getenv('SECRET_KEY')
if not secret_key:
    raise RuntimeError('SECRET_KEY не задан в окружении. Задайте его в .env перед запуском.')
application.secret_key = secret_key

application.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)  # Сессия истекает через 24 часа

# Флаги сессионной куки.
# SESSION_COOKIE_SECURE=1 (по умолчанию) - кука только по HTTPS; локально по HTTP ставим 0.
application.config['SESSION_COOKIE_HTTPONLY'] = True
application.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
application.config['SESSION_COOKIE_SECURE'] = os.getenv('SESSION_COOKIE_SECURE', '1') == '1'

# Защита от CSRF для всех POST-форм и AJAX-запросов
csrf = CSRFProtect(application)

# Учитываем, что приложение работает за обратным прокси (Apache/Passenger):
# берём реальный IP клиента и протокол из заголовков X-Forwarded-*.
# ВНИМАНИЕ: предполагается ровно один доверенный прокси. Проверить на боевом,
# что в логах виден настоящий IP клиента, а не адрес прокси.
application.wsgi_app = ProxyFix(application.wsgi_app, x_for=1, x_proto=1)

# Ограничение частоты запросов (защита входа от перебора паролей).
# Хранилище в памяти: на shared-хостинге достаточно как базовый тормоз.
limiter = Limiter(
    key_func=get_remote_address,
    app=application,
    default_limits=[],
    storage_uri="memory://",
)

login_manager = LoginManager()
login_manager.init_app(application)
login_manager.login_view = 'login'
scheduler = BackgroundScheduler()
today = date.today()
current_year = datetime.now().year
ru_holidays = holidays.RU(years=[current_year, current_year + 1])
tz = pytz.timezone("Europe/Moscow")

# ==================== НАСТРОЙКИ GOOGLE (взяты из 2 бота) ====================
SPREADSHEET_ID = "1nY6R2z4E3vEM0KZ4isyIWi5xeu516ZaSpbRw7g8bHD0"
DATA_SHEET_NAME = "Данные"
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# Укажите путь к вашему файлу ключей
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")


def is_workday_ru(check_date: Optional[date] = None) -> bool:
    tz = pytz.timezone("Europe/Moscow")
    if check_date is None:
        check_date = datetime.now(tz).date()

    return check_date.weekday() < 5 and check_date not in ru_holidays
# Создаем папку для логов, если её нет
log_dir = os.path.join(os.path.dirname(__file__), 'logs')
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# Настройка обработчика: файл logs/app.log, макс. размер 5МБ, хранить 5 старых копий
file_handler = RotatingFileHandler(
    os.path.join(log_dir, 'app.log'),
    maxBytes=5 * 1024 * 1024,
    backupCount=5
)

# Формат записи: Дата - Имя - Уровень - Сообщение
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)

# Привязываем к логгеру Flask
application.logger.addHandler(file_handler)
application.logger.setLevel(logging.INFO)

application.logger.info("Бот запущен и логирование настроено")


class RequestLoadMetrics:
    """Периодические process-local метрики HTTP-нагрузки."""

    WINDOW_SECONDS = 10
    ACTIVE_USER_TTL_SECONDS = 300

    def __init__(self):
        self._lock = threading.Lock()
        self._window_id = int(_time.time() // self.WINDOW_SECONDS)
        self._reset_window()
        self._active_requests = 0
        self._recent_users = {}

    def _reset_window(self):
        self._request_count = 0
        self._errors_5xx = 0
        self._latencies_ms = []
        self._requests_per_second = {}
        self._peak_active_requests = 0

    def _advance_window(self, now_wall):
        window_id = int(now_wall // self.WINDOW_SECONDS)
        if window_id == self._window_id:
            return

        if self._request_count:
            ordered_latencies = sorted(self._latencies_ms)
            p95_index = max(0, math.ceil(len(ordered_latencies) * 0.95) - 1)
            avg_latency = sum(ordered_latencies) / len(ordered_latencies)
            peak_rps = max(self._requests_per_second.values(), default=0)
            recent_cutoff = _time.monotonic() - self.ACTIVE_USER_TTL_SECONDS
            recent_user_count = sum(
                1 for last_seen in self._recent_users.values()
                if last_seen >= recent_cutoff
            )
            application.logger.info(
                'HTTP_LOAD scope=process pid=%s period=%ss requests=%s avg_rps=%.2f peak_rps=%s '
                'active_requests=%s peak_active_requests=%s users_seen_5m_process=%s '
                'avg_ms=%.1f p95_ms=%.1f 5xx=%s',
                os.getpid(), self.WINDOW_SECONDS, self._request_count,
                self._request_count / self.WINDOW_SECONDS, peak_rps,
                self._active_requests, self._peak_active_requests,
                recent_user_count, avg_latency, ordered_latencies[p95_index],
                self._errors_5xx,
            )

        self._window_id = window_id
        self._reset_window()

    def begin_request(self, user_id=None):
        now_wall = _time.time()
        now_mono = _time.monotonic()
        with self._lock:
            self._advance_window(now_wall)
            self._active_requests += 1
            self._peak_active_requests = max(
                self._peak_active_requests, self._active_requests
            )
            if user_id:
                self._recent_users[user_id] = now_mono
            cutoff = now_mono - self.ACTIVE_USER_TTL_SECONDS
            self._recent_users = {
                seen_user: last_seen
                for seen_user, last_seen in self._recent_users.items()
                if last_seen >= cutoff
            }
        return now_mono

    def finish_request(self, started_at, status_code):
        now_mono = _time.monotonic()
        duration_ms = max(0.0, (now_mono - started_at) * 1000)
        now_wall = _time.time()
        with self._lock:
            self._advance_window(now_wall)
            self._active_requests = max(0, self._active_requests - 1)
            self._request_count += 1
            self._latencies_ms.append(duration_ms)
            second = int(now_wall)
            self._requests_per_second[second] = self._requests_per_second.get(second, 0) + 1
            if status_code >= 500:
                self._errors_5xx += 1


request_load_metrics = RequestLoadMetrics()


@application.before_request
def begin_http_load_measurement():
    user_id = current_user.get_id() if current_user.is_authenticated else None
    request.environ['http_load_started_at'] = request_load_metrics.begin_request(user_id)


@application.after_request
def finish_http_load_measurement(response):
    started_at = request.environ.get('http_load_started_at')
    if started_at is not None:
        request_load_metrics.finish_request(started_at, response.status_code)
    return response



# Список админов
allowed_usernames = ['admin', 'plotnikovmi', 'orlovsv','korolevbs','bezzemelnayaay','morozovva','kossakovskyiis']

# Пример списка праздничных дней (день, месяц)
HOLIDAYS = {
    (1, 1),  # 1 января - Новый год
    (7, 1),  # 7 января - Рождество
    (23, 2),  # 23 февраля - День защитника Отечества
    (8, 3),  # 8 марта - Международный женский день
    (1, 5),  # 1 мая - Праздник Весны и Труда
    (9, 5),  # 9 мая - День Победы
    (13, 6),  # 12 июня - День России
    (4, 11),  # 4 ноября - День народного единства
    (31, 12),
}

# Настройка подключения к базе данных.
# Если задан DATABASE_URL (например, локальный SQLite для тестов) - используем его.
# Иначе собираем строку подключения к MySQL из отдельных переменных (боевое поведение).
database_url = os.getenv('DATABASE_URL')
if database_url:
    application.config['SQLALCHEMY_DATABASE_URI'] = database_url
else:
    user = os.getenv('DB_USER')
    password = os.getenv('DB_PASSWORD')
    host = os.getenv('DB_HOST')
    database = os.getenv('DB_NAME')
    application.config['SQLALCHEMY_DATABASE_URI'] = f'mysql+pymysql://{user}:{password}@{host}/{database}'

if make_url(application.config['SQLALCHEMY_DATABASE_URI']).drivername in ('mysql+pymysql', 'mariadb+pymysql'):
    application.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
        'connect_args': {'connect_timeout': 8, 'read_timeout': 30, 'write_timeout': 15},
    }

application.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(application)


PARK_TO_LOCATION = {
    "14 парк": {
        "bus": "Теплый Стан",
        "elbus": None  # Предполагается, нет электробусов
    },
    "1 парк": {
        "bus": "Коньково автобусы",
        "elbus": "Коньково электробусы"
    },
    "Красная Пахра": {
        "bus": "Красная Пахра автобусы",
        "elbus": "Красная Пахра электробусы"
    },
    "18 парк": {
        "bus": "Ясенево автобусы",
        "elbus": None  # Предполагается, нет электробусов
    },
}

TAG_CHOICES = {
    'in_work': 'В работе',
    'waiting': 'В ожидании',
    'ozch': 'ОЗЧ',
    'no_request': 'Без заявки',
    "to": "ТО",
    "gto": "ГТО",
    "otm": "ОТМ",
    "success": "ГОТОВ",
}

TO_TASK_NOTICE = 'Заявка предназначена для ТО. Тип ТО изменить нельзя, можно добавить только дополнительные работы.'
OPEN_PHASES = (1, 2, 3)

def is_to_schedule_task(task):
    return bool(task and (task.id_to or task.tag == 'to'))


def get_to_base_description(task):
    if task and task.id_to:
        schedule_id = str(task.id_to)
        if schedule_id.isdigit():
            schedule = TOSchedule.query.get(int(schedule_id))
            if schedule:
                return schedule.to_type

    description_lines = (task.description or '').splitlines() if task else []
    return description_lines[0].strip() if description_lines else ''


def get_to_additional_description(task):
    if not task or not task.description:
        return ''

    description_lines = task.description.splitlines()
    if not description_lines:
        return ''

    return '\n'.join(description_lines[1:]).strip()


def build_to_description(base_description, additional_description=''):
    base_description = (base_description or '').strip()
    additional_description = (additional_description or '').strip()
    if additional_description:
        return f"{base_description}\n{additional_description}"
    return base_description


STATUS_CHOICES = {
    'in_repair': 'В работе',
    'completed': 'Готов',
}

PARK_NAME = {
    '1 парк': 'Коньково',
    '14 парк': 'Теплый Стан',
    'Красная Пахра': "Красная Пахра",
    '18 парк': "Ясенево",
}

# для объяденения парков
PARK_GROUPS = {
    '1 парк': ['1 парк', '14 парк'],
    '14 парк': ['1 парк', '14 парк'],
    'Красная Пахра': ['Красная Пахра', '18 парк'],
    '18 парк': ['Красная Пахра', '18 парк'],
}

# Телефоны СТО по паркам
STATION_PHONES = {
    "1 парк": "8(915)370-23-20",
    "14 парк": "8(915)370-30-14",
    "Красная Пахра": "8(915)358-54-34",
    "18 парк": "8(916)330-29-46",
}

# Адреса для эвакуации по паркам
EVACUATION_ADDRESSES = {
    "1 парк": "г. Москва, улица Введенского, 6с1",
    "14 парк": "г. Москва, Малая Очаковская улица, 8с1",
    "Красная Пахра": "г. Москва, район Троицк, квартал № 92, 1с2",
    "18 парк": "г. Москва, Новоясеневский проспект, 4с2",
}

# Словарь для парков и их Telegram чатов
TELEGRAM_CHAT_IDS = {
    "1 парк": "-4523690725",   # id группы для 1 парка   -1003758695034 (ГотовностьСетранс) -4523690725 (Оригинал)
    "14 парк": "-1001956330583",  # id группы для 14 парка  -1003758695034 (ГотовностьСетранс) -1001956330583 (Оригинал)
    "Красная Пахра": "-1002959088759",
    "18 парк": "-1002729382444"
}
# чаты для отчетов на 14 и 17
TELEGRAM_14_17 = {
    # "1 парк": "",  # id группы для 1 парка -4856433871 -1002260405574
    # "14 парк": "-1001777584645",  # id группы для 14 парка-4856433871 -1001777584645
    # "Красная Пахра": "-4856433871",
    # "18 парк": "-4856433871"
}
TELEGRAM_READY = {
   # "1 парк": "",  # готовность для 1 парка -4856433871
  #  "14 парк": "",  # готовность для 14 парка -4856433871
}

ELCAR1440_CONTRACTS = (
    'МГТ234953-23',
    'МГТ0019-25',
    'МГТ0107-21'
)

# Графи ТО редактировать
ALLOWED_USERS = ['plotnikovmi', 'admin', 'kulebakinin', 'korolevbs', 'bezzemelnayaay', 'morozovva', 'kossakovskyiis','dolgopolovay', 'kirsh']

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')  # токен берём из окружения (.env)

# Словарь для русских названий месяцев
RUSSIAN_MONTHS = {
    1: 'Январь',
    2: 'Февраль',
    3: 'Март',
    4: 'Апрель',
    5: 'Май',
    6: 'Июнь',
    7: 'Июль',
    8: 'Август',
    9: 'Сентябрь',
    10: 'Октябрь',
    11: 'Ноябрь',
    12: 'Декабрь'
}

TELEGRAM_19 = {
    "1 парк": "-1003758695034",  # id группы для 1 парка
    "Красная Пахра": "-1002959088759",
}


def get_user(username):
    return User.query.filter_by(username=username).first()


def save_to_google_sheets(park_name, bus_count, ebus_count, total_count):
    """Функция записи данных в таблицу (логика 2-го бота)"""
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        sheet = spreadsheet.worksheet(DATA_SHEET_NAME)

        # Форматируем дату как во 2-м боте
        moscow_now = datetime.now(MOSCOW_TZ)
        date_str = moscow_now.strftime("%d.%m.%Y %H:%M")

        # Создаем строку из 22 элементов (A-V)
        new_row = [""] * 22
        new_row[0] = date_str  # Колонка A: Дата
        new_row[16] = bus_count  # Колонка Q: Автобусы
        new_row[17] = ebus_count  # Колонка R: Электробусы
        new_row[18] = total_count  # Колонка S: Общий ремонт
        new_row[21] = f"СТО {park_name}"  # Колонка V: Источник

        # Поиск первой свободной строки
        all_dates = sheet.col_values(1)
        next_row = len(all_dates) + 1
        target_range = f"A{next_row}:V{next_row}"

        sheet.update(target_range, [new_row], value_input_option='USER_ENTERED')
        return True
    except Exception as e:
        # Используем логгер вашего приложения
        application.logger.error(f"❌ Ошибка Google Sheets: {e}")
        return False


def verify_and_upgrade_password(user, plain_password):
    """
    Проверяет пароль и прозрачно мигрирует его на хэш.
    Если пароль в базе уже хэширован - сверяем через check_password_hash.
    Если это старый пароль в открытом виде - сверяем напрямую и при совпадении
    сразу перезаписываем хэшем (мягкая миграция без сброса паролей).
    """
    stored = user.password or ''
    looks_hashed = stored.startswith(('pbkdf2:', 'scrypt:'))

    if looks_hashed:
        return check_password_hash(stored, plain_password)

    # Старый формат: пароль в открытом виде
    if stored == plain_password:
        # ВАЖНО: метод зафиксирован как pbkdf2 (~102 символа), потому что колонка
        # users.password - VARCHAR(128). Дефолт Werkzeug 3 (scrypt, 162 символа)
        # обрезается MySQL и делает вход невозможным.
        user.password = generate_password_hash(plain_password, method='pbkdf2:sha256:600000')
        db.session.commit()
        application.logger.info(f"Password migrated to hash for user {user.username}")
        return True

    return False


@application.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()

        if user and verify_and_upgrade_password(user, password):
            login_user(user)
            session['available_parks'] = user.allowed_parks or []

            # Set selected_park to default_park if it exists and is in allowed_parks
            if user.default_park and user.default_park in session['available_parks']:
                session['selected_park'] = user.default_park
            # Fallback to first allowed park if default_park is not set or not allowed
            elif len(session['available_parks']) == 1:
                session['selected_park'] = session['available_parks'][0]
            else:
                session['selected_park'] = None  # or handle as needed

            application.logger.info(
                f"User {user.username} logged in, available_parks: {session['available_parks']}, selected_park: {session['selected_park']}")
            return redirect(url_for('index'))

        flash('Неверное имя пользователя или пароль', 'login_error')
        return redirect(url_for('login'))

    resp = make_response(render_template('index.html'))
    return resp
    
class KtgHistory(db.Model):
    __tablename__ = 'ktg_history'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    park = db.Column(db.String(64), nullable=False, index=True)
    recorded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    ktg_park = db.Column(db.Float, nullable=True)
    complete_task = db.Column(db.Integer, nullable=False, default=0)
    create_task = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        db.Index('ix_ktg_history_park_time', 'park', 'recorded_at'),
    )

    def __repr__(self):
        return f'<KtgHistory {self.park} {self.recorded_at} KTG={self.ktg_park}>'    

class Applications(db.Model):  # отработка заказ-нарядов
    __tablename__ = 'applications'
    id = db.Column(db.Integer, primary_key=True)
    report_date = db.Column(db.Date, nullable=False)
    shift_start = db.Column(db.DateTime, nullable=False)
    shift_end = db.Column(db.DateTime, nullable=False)
    park_name = db.Column(db.String(255))
    new_tasks_count = db.Column(db.Integer)
    completed_tasks_count = db.Column(db.Integer)
    completion_rate = db.Column(db.Float)

class TOSchedule(db.Model):
    __tablename__ = 'to_schedule'
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)  # Дата ТО
    day_of_week = db.Column(db.String(20), nullable=False)  # День недели (автоматически вычисляется)
    bort = db.Column(db.String(20), nullable=False)  # Номер борта
    to_type = db.Column(db.String(50), nullable=False)  # Тип ТО, например 'ТО-1 15000' или 'ТО-2 30000'
    park = db.Column(db.String(50), nullable=True)  # Парк, для фильтрации

    def __repr__(self):
        return f"<TOSchedule {self.date} {self.bort} {self.to_type}>"


class User(UserMixin, db.Model):
    __tablename__ = 'users'  # Make sure this is plural if you want it to match 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)  # хэш scrypt ~162 симв., с запасом
    full_name = db.Column(db.String(100))
    allowed_parks = db.Column(JSON, nullable=True, default=lambda: [])  # Список разрешенных парков
    default_park = db.Column(db.String(100), nullable=True)
    company = db.Column(db.String(50))


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# Эндпойнты, доступные без входа в систему.
# Всё остальное требует авторизации (глобальная защита ниже).
PUBLIC_ENDPOINTS = {'login', 'static', 'health', 'csrf_token_refresh'}


@application.before_request
def require_login_globally():
    # Не мешаем отдаче 404 по несуществующим путям
    if request.endpoint is None:
        return None
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    return None


@application.route('/logout')
def logout():
    session.pop('closure_pauses', None)
    logout_user()
    return redirect(url_for('index'))


@application.route('/health')
@csrf.exempt
def health():
    # Проверка живости: приложение отвечает и видит базу данных.
    # Используется скриптом деплоя и внешним мониторингом.
    try:
        db.session.execute(text('SELECT 1'))
        return jsonify(status='ok'), 200
    except Exception as e:
        application.logger.error(f"Health check failed: {e}")
        return jsonify(status='db_error'), 500


class Elcar1440(db.Model):
    __tablename__ = 'elcar1440'

    id = db.Column(db.Integer, primary_key=True, index=True)
    board = db.Column(db.String(20), nullable=False, index=True)
    vin = db.Column(db.String(50), index=True)
    park = db.Column(db.String(50), index=True)
    contract = db.Column(db.String(50), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    tag = db.Column(db.String(20), default='in_work')

    def __repr__(self):
        return f"<Elcar1440 {self.board} {self.contract}>"


class Task(db.Model):
    __tablename__ = 'task'
    __table_args__ = (
        db.UniqueConstraint('id_to', name='uq_task_id_to'),
        db.Index('ix_task_status_park_completed', 'status', 'park', 'completed_at'),
    )

    id = db.Column(db.Integer, primary_key=True)
    board_number = db.Column(db.String(20), nullable=False, index=True)
    vin = db.Column(db.String(50))
    description = db.Column(db.Text, nullable=False, index=True)
    completed_work = db.Column(db.Text, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(pytz.timezone('Europe/Moscow')), index=True)
    completed_at = db.Column(db.DateTime, nullable=True, index=True)
    status = db.Column(db.String(20), index=True)
    tag = db.Column(db.String(20), default='in_work', index=True)
    park = db.Column(db.String(50), nullable=True, index=True)
    type_ts = db.Column(db.String(20), nullable=True, index=True)
    comment = db.Column(db.String(20), nullable=True, index=True)
    author_open = db.Column(db.String(20), nullable=True, index=True)
    author_closed = db.Column(db.String(20), nullable=True, index=True)
    applications_from = db.Column(db.DateTime, nullable=True, index=True)
    work_in = db.Column(db.DateTime, nullable=True, index=True)
    contract = db.Column(db.String(50), nullable=True, index=True)
    reject = db.Column(db.Boolean, default=False, index=True)
    id_to = db.Column(db.String(50), nullable=True, index=True)
    event_type = db.Column(db.String(50), nullable=True, index=True)
    print = db.Column(db.Boolean, default=False, index=True)
    incontrol_task_id = db.Column(db.BigInteger, nullable=True, index=True)
    yandex_reply_lookup = db.Column(db.Boolean, nullable=False, server_default='0', index=True)


class IncontrolStatus(db.Model):
    """Кеш заявок incontrol24 по бортам.

    На один борт может приходиться несколько заявок,
    поэтому уникальность определяется парой:
    board_number + work_name.
    """

    __tablename__ = 'incontrol_status'

    __table_args__ = {
        'mysql_charset': 'utf8mb4',
        'mysql_collate': 'utf8mb4_unicode_ci',
    }

    board_number = db.Column(db.String(20), primary_key=True)
    work_name = db.Column(db.String(255), primary_key=True)

    has_open = db.Column(db.Boolean, nullable=False, default=False)
    phase_name = db.Column(db.String(50), nullable=True)
    checked_at = db.Column(db.DateTime, nullable=False, index=True)

    def __repr__(self):
        return (
            f"<IncontrolStatus "
            f"{self.board_number} "
            f"{self.work_name} "
            f"open={self.has_open}>"
        )


# --- Интеграция с incontrol24 -------------------------------------------------

INCONTROL_ENABLED = os.getenv('INCONTROL_ENABLED', '0') == '1'

# Полный обход трёх организаций занимает около 163 секунд (замер 2026-08-02:
# 38 запросов, ~4 с на страницу). Интервал меньше длительности обхода не даёт
# выигрыша: задача просто запускается встык (max_instances=1, coalesce).
INCONTROL_INTERVAL = int(os.getenv('INCONTROL_INTERVAL', '1800'))

# Порог устаревания с запасом на один сорванный проход:
# 300 с интервал + 163 с обход = ~8 минут в худшем случае, при одном
# пропущенном проходе - около 13. Пятнадцать минут переживают один сбой.
INCONTROL_STALE_MINUTES = int(os.getenv('INCONTROL_STALE_MINUTES', '30'))


def incontrol_now():
    """Текущее московское время без таймзоны — как хранится в базе проекта."""
    return datetime.now(pytz.timezone('Europe/Moscow')).replace(tzinfo=None)


def normalize_task_board(board_number):
    return incontrol.normalize_board(board_number)


def get_active_task_for_board(board_number, *, lock=False):
    board = normalize_task_board(board_number)
    if not board:
        return None
    if lock:
        return active_repairs_for_board(Task, board).order_by(Task.created_at.desc()).with_for_update().first()

    candidates = {board}
    if board.isdigit():
        candidates.add(f'{board}.0')

    board_column = func.replace(
        func.replace(func.trim(Task.board_number), '\xa0', ''),
        ' ',
        '',
    )

    return (
        Task.query
        .filter(Task.status == 'in_repair')
        .filter(board_column.in_(candidates))
        .order_by(Task.created_at.desc())
        .first()
    )


def get_incontrol_boards(boards):
    """Из переданных бортов вернуть те, у кого есть открытая заявка.

    Устаревшие проверки отбрасываются: бейдж утверждает, что заявка есть
    сейчас, и показывать по нему данные десятиминутной давности нельзя.
    Отсутствие бейджа при сбое означает «не знаем», а не «заявки нет».
    """
    normalized = {incontrol.normalize_board(board) for board in boards}
    normalized.discard(None)
    if not normalized:
        return set()

    cutoff = incontrol_now() - timedelta(minutes=INCONTROL_STALE_MINUTES)
    try:
        rows = IncontrolStatus.query.filter(
            IncontrolStatus.board_number.in_(normalized),
            IncontrolStatus.has_open.is_(True),
            IncontrolStatus.work_name != 'Заявка на мойку'
        ).all()
    except SQLAlchemyError as error:
        # Таблицы может ещё не быть (не создана на сервере) или база недоступна.
        # Бейдж - вспомогательная метка: она не имеет права ронять список задач.
        application.logger.warning(f'incontrol24: кеш недоступен: {error}')
        db.session.rollback()
        return set()
    return {row.board_number for row in rows}


def _make_incontrol_client(timeout=30, interactive=False):
    """Отдельной функцией — чтобы подменяться в тестах."""
    return incontrol.IncontrolClient(
        os.getenv('INCONTROL_LOGIN'),
        os.getenv('INCONTROL_PASSWORD'),
        timeout=timeout,
        interactive=interactive,
    )
def refresh_incontrol_status_for_board(
    board_number,
    park,
    required_work_names
):
    """Точечно проверить нужный тип заявки для одного борта."""

    if not INCONTROL_ENABLED:
        return None

    board = incontrol.normalize_board(board_number)

    if not board:
        raise incontrol.IncontrolError('Не указан борт для проверки InControl')
    if incontrol.is_outside_incontrol(park):
        return None

    import time

    started = time.monotonic()

    client = None
    try:
        t = time.monotonic()
        # Ответ списка заявок InControl на практике занимает до 20+ секунд.
        # Проверка уже фоновая: 7 секунд обрывали корректные медленные ответы.
        client = _make_incontrol_client(timeout=25, interactive=True)
        application.logger.info(
            f'incontrol24: создание клиента: {time.monotonic() - t:.2f} сек'
        )

        t = time.monotonic()
        client.authenticate()
        application.logger.info(
            f'incontrol24: authenticate: {time.monotonic() - t:.2f} сек'
        )

        t = time.monotonic()
        has_open, phase_name, work_name = client.check_board_details(
            board,
            park,
            required_work_names
        )
        application.logger.info(
            f'incontrol24: check_board_details: {time.monotonic() - t:.2f} сек'
        )

        application.logger.info(
            f'incontrol24: точечная проверка борта {board}: '
            f'Есть в БД = {has_open}, '
            f'Этап = {phase_name}, '
            f'Тип заявки = {work_name}, '
            f'общее время = {time.monotonic() - started:.2f} сек'
        )

        return work_name if has_open else None

    except Exception as error:
        application.logger.error(
            f'incontrol24: ошибка точечной проверки борта '
            f'{board}: {error}, '
            f'время = {time.monotonic() - started:.2f} сек'
        )

        raise incontrol.IncontrolError('Не удалось проверить заявку в InControl') from error
    finally:
        if client is not None:
            client.session.close()

def refresh_incontrol_statuses():
    """Получить снимок InControl без транзакции БД, затем обновить кеш."""
    if not INCONTROL_ENABLED:
        return {'status': 'disabled'}

    started = _time.monotonic()
    fetch_seconds = 0.0
    write_seconds = 0.0
    checked = skipped = 0
    failed_orgs = set()
    client = None
    try:
        has_tasks = db.session.query(Task.id).filter_by(status='in_repair').first() is not None
        db.session.remove()
        if not has_tasks:
            IncontrolStatus.query.delete(synchronize_session=False)
            db.session.commit()
            return {'status': 'ok', 'проверено': 0, 'пропущено': 0, 'ошибок': 0}

        fetch_started = _time.monotonic()
        try:
            client = _make_incontrol_client()
            client.authenticate()
            open_by_org = {}
            for org_id in incontrol.ALL_ORGS:
                try:
                    open_by_org[org_id] = client.fetch_open_boards(org_id)
                except Exception:
                    failed_orgs.add(org_id)
                    application.logger.exception('incontrol24: кеш, организация %s недоступна', org_id)
        finally:
            fetch_seconds += _time.monotonic() - fetch_started

        # Перечитываем после сетевого обхода: за это время заявку могли закрыть
        # или создать вручную. ORM-соединение не удерживалось во время обхода.
        write_started = _time.monotonic()
        pairs = db.session.query(Task.board_number, Task.park).filter(
            Task.status == 'in_repair'
        ).distinct().all()
        boards = {}
        for board_number, park in pairs:
            board = incontrol.normalize_board(board_number)
            if board:
                boards.setdefault(board, park)

        replacements = []
        rows = []
        now = incontrol_now()
        for board, park in boards.items():
            org_id = incontrol.park_to_org(park)
            orgs = ([] if incontrol.is_outside_incontrol(park) else
                    [org_id] if org_id else list(incontrol.ALL_ORGS))
            # При неизвестном парке нужен полный ответ всех организаций.
            # Частичный ответ не доказывает отсутствие заявки.
            if any(org in failed_orgs for org in orgs):
                skipped += 1
                continue
            board_tasks = {}
            for org in orgs:
                for info in open_by_org.get(org, {}).get(board, []):
                    work_name = (info.get('work_name') or '').strip()
                    if work_name:
                        board_tasks[work_name] = info.get('phase_name')
            replacements.append(board)
            rows.extend(IncontrolStatus(
                board_number=board, work_name=work_name, has_open=True,
                phase_name=phase, checked_at=now,
            ) for work_name, phase in board_tasks.items())
            checked += 1

        # Вся замена атомарна: до commit читатели видят прежний снимок.
        for chunk in _incontrol_chunks(replacements):
            IncontrolStatus.query.filter(IncontrolStatus.board_number.in_(chunk)).delete(synchronize_session=False)
        if boards:
            IncontrolStatus.query.filter(~IncontrolStatus.board_number.in_(list(boards))).delete(synchronize_session=False)
        else:
            IncontrolStatus.query.delete(synchronize_session=False)
        db.session.add_all(rows)
        db.session.commit()
        write_seconds = _time.monotonic() - write_started
        return {'status': 'ok', 'проверено': checked, 'пропущено': skipped, 'ошибок': len(failed_orgs)}
    except Exception as error:
        db.session.rollback()
        application.logger.exception('incontrol24: обновление кеша не выполнено, прежние данные сохранены')
        return {'status': 'error', 'message': str(error)}
    finally:
        db.session.remove()
        client_session = getattr(client, 'session', None)
        if client_session is not None:
            client_session.close()
        application.logger.info(
            'INCONTROL_SYNC job=cache total_ms=%.1f fetch_ms=%.1f write_ms=%.1f checked=%s skipped=%s failed_orgs=%s',
            (_time.monotonic() - started) * 1000, fetch_seconds * 1000,
            write_seconds * 1000, checked, skipped, len(failed_orgs),
        )


def _incontrol_chunks(values, size=500):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _incontrol_board_key(value):
    """Те же варианты номера, которые учитывает get_active_task_for_board."""
    board = normalize_task_board(value)
    if not board:
        return None
    board = board.replace('\xa0', '').replace(' ', '')
    return board[:-2] if board.endswith('.0') else board


def _stk_reopen_candidates(applications):
    run_model = incontrol_rejections.model
    closed = db.session.query(Task.id, Task.board_number, Task.park, Task.completed_at)
    closed = closed.filter(Task.status == 'completed')
    matches = {}
    for remote_ids in _incontrol_chunks(applications):
        direct = closed.add_columns(Task.incontrol_task_id).filter(Task.incontrol_task_id.in_(remote_ids))
        historical = closed.add_columns(run_model.remote_id).join(run_model, run_model.task_id == Task.id).filter(
            Task.incontrol_task_id.is_(None), run_model.remote_id.in_(remote_ids), run_model.state == 'consumed',
        )
        for task_id, board, park, completed_at, remote_id in (*direct.all(), *historical.all()):
            info = applications[remote_id]
            if (_incontrol_board_key(board) == _incontrol_board_key(info['board'])
                    and park == incontrol.org_to_park(info['org_id'])):
                matches.setdefault(remote_id, set()).add((task_id, completed_at))
    return {remote_id: next(iter(rows)) for remote_id, rows in matches.items() if len(rows) == 1}


def _reopen_incontrol_stk_task(task_id, completed_at, remote_id):
    task = Task.query.filter_by(id=task_id).populate_existing().with_for_update().first()
    if (task is None or task.status != 'completed' or task.completed_at != completed_at
            or task.incontrol_task_id not in (None, remote_id)):
        return False
    run_model = incontrol_rejections.model
    run = run_model.query.filter_by(task_id=task_id).with_for_update().first()
    if run and (run.state != 'consumed' or run.remote_id not in (None, remote_id)):
        return False
    if task.incontrol_task_id is None and (run is None or run.remote_id != remote_id):
        return False
    if run:
        db.session.delete(run)
    completion_checks.model.query.filter_by(task_id=task_id).delete(synchronize_session=False)
    task.incontrol_task_id = remote_id
    task.status = 'in_repair'
    task.tag = 'in_work'
    task.completed_at = None
    task.author_closed = None
    task.reject = False
    task.yandex_reply_lookup = False
    db.session.commit()
    return True


def create_tasks_from_incontrol():
    """Импорт: сетевой обход, короткий отбор, затем сеть и короткая запись."""
    if not INCONTROL_ENABLED:
        return {'status': 'disabled'}

    started = _time.monotonic()
    fetch_seconds = write_seconds = 0.0
    created = linked = reopened = skipped = errors = 0
    client = None
    import_parks = {'14 парк', '1 парк', 'Красная Пахра', '18 парк'}
    import_orgs = [org for org, park in incontrol.ORG_TO_PARK.items() if park in import_parks]
    application.logger.info('incontrol24: запуск проверки новых заявок из InControl...')
    # Функция вызывается в отдельном app_context планировщика.
    db.session.remove()
    try:
        fetch_started = _time.monotonic()
        try:
            client = _make_incontrol_client()
            client.authenticate()
            applications = []
            for org_id in import_orgs:
                try:
                    applications.extend(client.fetch_open_applications(org_id))
                except Exception:
                    errors += 1
                    application.logger.exception('incontrol24: импорт, организация %s недоступна', org_id)
        finally:
            fetch_seconds += _time.monotonic() - fetch_started

        by_id = {}
        stk_returns = {}
        for info in applications:
            if info.get('work_name') == 'Заявка на сервисное обслуживание':
                continue
            if info.get('task_id'):
                if info.get('stk_return'):
                    stk_returns.setdefault(info['task_id'], info)
                else:
                    by_id.setdefault(info['task_id'], info)

        # Уже известные id исключаем пакетно, вместо SQL-запроса на каждую
        # старую заявку в полной выдаче InControl.
        select_started = _time.monotonic()
        stk_candidates = _stk_reopen_candidates(stk_returns)
        existing_ids = set()
        for chunk in _incontrol_chunks(by_id):
            existing_ids.update(row[0] for row in db.session.query(Task.incontrol_task_id).filter(
                Task.incontrol_task_id.in_(chunk)
            ).all())
        new_info = [info for task_id, info in by_id.items() if task_id not in existing_ids]
        vehicle_boards = {normalize_task_board(info.get('board')) for info in new_info}
        vehicle_boards.discard(None)
        locations = {}
        for model in (Elcar, Car):  # При совпадении номера приоритет автобуса, как раньше.
            for chunk in _incontrol_chunks(vehicle_boards):
                locations.update(db.session.query(model.bort, model.location).filter(model.bort.in_(chunk)).all())
        candidates = []
        for info in by_id.values():
            board = normalize_task_board(info.get('board'))
            if info['task_id'] in existing_ids or not board or board not in locations:
                skipped += 1
                continue
            park = incontrol.org_to_park(info.get('org_id')) or locations[board]
            if park not in import_parks:
                skipped += 1
                continue
            candidates.append((board, dict(info)))
        db.session.remove()
        write_seconds += _time.monotonic() - select_started

        for remote_id, (task_id, completed_at) in stk_candidates.items():
            info = stk_returns[remote_id]
            fetch_started = _time.monotonic()
            try:
                fresh = client.get_task_by_id(remote_id, info['org_id'])
                if not fresh or fresh.get('stage') in ('denied', 'completed'):
                    continue
                if _incontrol_board_key(incontrol.board_of(fresh)) != _incontrol_board_key(info['board']):
                    continue
                if not incontrol.is_stk_return(
                    ((fresh.get('type_work') or {}).get('work_name') or '').strip(),
                    (fresh.get('phase') or {}).get('phase_name'),
                    ((fresh.get('label') or {}).get('label_name') or '').strip(),
                ):
                    continue
            except Exception:
                errors += 1
                application.logger.exception('incontrol24: не удалось подтвердить возврат СТК по заявке %s', remote_id)
                continue
            finally:
                fetch_seconds += _time.monotonic() - fetch_started

            write_started = _time.monotonic()
            try:
                if _reopen_incontrol_stk_task(task_id, completed_at, remote_id):
                    reopened += 1
                    application.logger.info('incontrol24: возврат СТК, заявка %s, task=%s, борт=%s, парк=%s',
                                            remote_id, task_id, info['board'], incontrol.org_to_park(info['org_id']))
            finally:
                db.session.remove()
                write_seconds += _time.monotonic() - write_started

        for board, info in candidates:
            # Предыдущая запись полностью завершена, соединение возвращено
            # в пул до любого обращения к внешнему сервису.
            fetch_started = _time.monotonic()
            try:
                fresh = client.get_task_by_id(info['task_id'], info.get('org_id'))
                if not fresh or fresh.get('stage') in ('denied', 'completed'):
                    skipped += 1
                    continue
                phase_id = (fresh.get('phase') or {}).get('id')
                label = ((fresh.get('label') or {}).get('label_name') or '').strip()
                if (phase_id is not None and phase_id not in OPEN_PHASES) or label in incontrol.IGNORED_LABELS:
                    skipped += 1
                    continue
                work_name = ((fresh.get('type_work') or {}).get('work_name') or info.get('work_name') or '').strip()
                if work_name not in {'Заявка на ремонт', 'Заявка на ремонт с линии'}:
                    skipped += 1
                    continue
                description = client.build_description_from_linked(info.get('linked_ids'), info.get('org_id'))
                description = description or info.get('work_name') or 'Заявка InControl'
            except Exception:
                skipped += 1
                errors += 1
                application.logger.exception('incontrol24: перепроверка заявки %s не выполнена', info['task_id'])
                continue
            finally:
                fetch_seconds += _time.monotonic() - fetch_started

            write_started = _time.monotonic()
            try:
                # Импорты и ручное создание используют одну блокировку строки борта.
                vehicle = Car.query.filter_by(bort=board).with_for_update().first()
                is_elbus = vehicle is None
                if vehicle is None:
                    vehicle = Elcar.query.filter_by(bort=board).with_for_update().first()
                if vehicle is None:
                    skipped += 1
                    continue
                park = incontrol.org_to_park(info.get('org_id')) or vehicle.location
                if park not in import_parks:
                    skipped += 1
                    continue
                if Task.query.filter_by(incontrol_task_id=info['task_id']).first():
                    skipped += 1
                    continue
                active_repairs = active_repairs_for_board(Task, board).with_for_update().all()
                if active_repairs:
                    candidates = manual_repair_candidates(
                        active_repairs, park, excluded_authors=(MAIL_TASK_AUTHOR, 'InControl', 'Система'),
                    )
                    task, extras = match_imported_repair(
                        (candidate for candidate in candidates if candidate.incontrol_task_id is None), description,
                    )
                    if task is None:
                        skipped += 1
                        continue
                    task_id = task.id
                    task.incontrol_task_id = info['task_id']
                    if extras:
                        task.description = '\n'.join((task.description.rstrip(), *extras))
                    db.session.commit()
                    linked += 1
                    application.logger.info('incontrol24: привязана заявка %s, task=%s, борт=%s, парк=%s',
                                            info['task_id'], task_id, board, park)
                    continue
                created_at = incontrol_now()
                if info.get('creation_date') is not None:
                    try:
                        created_at = datetime.fromtimestamp(int(info['creation_date']), tz).replace(tzinfo=None)
                    except (TypeError, ValueError, OSError):
                        application.logger.warning('incontrol24: некорректная дата заявки %s', info['task_id'])
                task = Task(
                    board_number=board, description=description, park=park,
                    status='in_repair', created_at=created_at, tag='waiting',
                    type_ts=vehicle.type_ts or ('Электробус' if is_elbus else 'Автобус'),
                    author_open='InControl', contract=vehicle.contract, vin=vehicle.vin,
                    incontrol_task_id=info['task_id'],
                )
                db.session.add(task)
                db.session.commit()
                created += 1
                application.logger.info('incontrol24: импортирована заявка %s, борт=%s, парк=%s', info['task_id'], board, park)
            finally:
                db.session.remove()
                write_seconds += _time.monotonic() - write_started

        return {'status': 'ok', 'created': created, 'linked': linked, 'reopened': reopened, 'skipped': skipped, 'errors': errors}
    except Exception as error:
        db.session.rollback()
        application.logger.exception('incontrol24: импорт прерван')
        return {'status': 'error', 'message': str(error), 'created': created, 'linked': linked,
                'reopened': reopened, 'skipped': skipped, 'errors': errors + 1}
    finally:
        db.session.remove()
        client_session = getattr(client, 'session', None)
        if client_session is not None:
            client_session.close()
        application.logger.info(
            'INCONTROL_SYNC job=import total_ms=%.1f fetch_ms=%.1f db_ms=%.1f created=%s linked=%s reopened=%s skipped=%s errors=%s',
            (_time.monotonic() - started) * 1000, fetch_seconds * 1000,
            write_seconds * 1000, created, linked, reopened, skipped, errors,
        )


def parse_datetime_value(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        value = value.strip()
        for date_format in (
            '%Y-%m-%dT%H:%M',
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M',
            '%d.%m.%Y %H:%M',
            '%d.%m.%y %H:%M',
        ):
            try:
                return datetime.strptime(value, date_format)
            except ValueError:
                continue
    return None


def as_moscow_datetime(value):
    dt_value = parse_datetime_value(value)
    if dt_value is None:
        return None
    if dt_value.tzinfo is None:
        return tz.localize(dt_value)
    return dt_value.astimezone(tz)


def format_datetime(value, date_format='%d.%m.%y %H:%M'):
    dt_value = parse_datetime_value(value)
    if dt_value is None:
        return ''
    if dt_value.tzinfo is not None:
        dt_value = dt_value.astimezone(tz)
    return dt_value.strftime(date_format)


# Создаем словарь для перевода месяцев
MONTH_NAMES = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря"
}


def get_russian_date():
    now = datetime.now(pytz.timezone('Europe/Moscow'))
    day = now.day
    month = MONTH_NAMES[now.month]
    year = now.year
    return f"{day} {month} {year}г."


# база данных автобусов
class Car(db.Model):
    __tablename__ = 'cars'
    id = db.Column(db.Integer, primary_key=True)
    bort = db.Column(db.String(20), index=True)
    gos_number = db.Column(db.String(20), index=True)
    year = db.Column(db.Integer, index=True)
    vin = db.Column(db.String(50), index=True)
    location = db.Column(db.String(100), index=True)
    contract = db.Column(db.String(100), index=True)
    mileage = db.Column(db.Integer, index=True)
    mileage_yesterday = db.Column(db.Integer, index=True)
    type_ts = db.Column(db.String(20), default='Автобус', nullable=True, index=True)
    models = db.Column(db.String(100), nullable=True, index=True)
    seasonal_service = db.Column(db.Boolean, default=False)
    comments = db.Column(db.Text, nullable=True)


class Elcar(db.Model):
    __tablename__ = 'elbuses'
    id = db.Column(db.Integer, primary_key=True, index=True)
    bort = db.Column(db.String(20), index=True)
    gos_number = db.Column(db.String(20), index=True)
    year = db.Column(db.Integer, index=True)
    vin = db.Column(db.String(50), index=True)
    location = db.Column(db.String(100), index=True)
    contract = db.Column(db.String(100), index=True)
    mileage = db.Column(db.Integer, index=True)
    mileage_yesterday = db.Column(db.Integer, index=True)
    type_ts = db.Column(db.String(20), default='Электробус', nullable=True, index=True)
    models = db.Column(db.String(100), nullable=True, index=True)
    seasonal_service = db.Column(db.Boolean, default=False)
    comments = db.Column(db.Text, nullable=True)


class History(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bort = db.Column(db.String(10), nullable=False)  # гаражный номер
    mileage = db.Column(db.Integer, nullable=False)  # пробег
    record_date = db.Column(db.Date, nullable=False)  # дата записи
    daily_mileage = db.Column(db.Integer, nullable=True)

    def __repr__(self):
        return f"<History {self.bort} {self.mileage} {self.record_date}>"


class ServiceStatus(db.Model):
    __tablename__ = 'service_status'
    id = db.Column(db.Integer, primary_key=True)
    bort = db.Column(db.String(20), nullable=False)
    park = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(20), default='не пройдено')
    type_ts = db.Column(db.String(20), nullable=False)
    __table_args__ = (db.UniqueConstraint('bort', 'park', 'type_ts', name='unique_bort_park_type'),)


# Данные из базы данных количество бортов
class FleetSize(db.Model):
    __tablename__ = 'fleet_size'
    id = db.Column(db.Integer, primary_key=True, index=True)
    location = db.Column(db.String(50), unique=True, nullable=False, index=True)
    count = db.Column(db.Integer, nullable=False, index=True)
    zone = db.Column(db.Text, nullable=True, default='[]', index=True)  # Храним как JSON строку

    @property
    def zones(self):
        try:
            # Удаляем лишние пробелы и кавычки, преобразуем в список
            zones = json.loads(self.zone.replace("'", '"'))
            return [z.strip() for z in zones] if isinstance(zones, list) else [str(zones)]
        except:
            return []


# Данные из базы данных для отчета
class Date(db.Model):
    __tablename__ = 'date'
    id = db.Column(db.Integer, primary_key=True)
    location = db.Column(db.String(50), unique=True, nullable=False)
    count = db.Column(db.Integer, nullable=False)
    park = db.Column(db.String, nullable=True)


def delit_data():
    with application.app_context():
        try:
            Date.query.filter(
                ~Date.location.in_(["СО Автобусы", "СО Электробусы"])
            ).update({"count": 0}, synchronize_session=False)
            db.session.commit()
        except Exception as e:
            db.session.rollback()


class Report7Table(db.Model):
    __tablename__ = 'report_7'
    id = db.Column(db.Integer, primary_key=True)
    col1 = db.Column(db.String(20))  # 7:00 report
    description = db.Column(db.Text, nullable=False)
    tag = db.Column(db.String(20))
    park = db.Column(db.String(50))
    contract = db.Column(db.String(50))
    date = db.Column(db.DateTime, nullable=True)


class Report830Table(db.Model):
    __tablename__ = 'report_830'
    id = db.Column(db.Integer, primary_key=True)
    col1 = db.Column(db.String(20))  # 8:30 report
    description = db.Column(db.Text, nullable=False)
    tag = db.Column(db.String(20))
    park = db.Column(db.String(50))
    contract = db.Column(db.String(50))
    date = db.Column(db.DateTime, nullable=True)


class Report17Table(db.Model):
    __tablename__ = 'report_17'
    id = db.Column(db.Integer, primary_key=True)
    col1 = db.Column(db.String(20))  # 17:00 report
    description = db.Column(db.Text, nullable=False)
    tag = db.Column(db.String(20))
    park = db.Column(db.String(50))
    contract = db.Column(db.String(50))
    date = db.Column(db.DateTime, nullable=True)


class Report19Table(db.Model):
    __tablename__ = 'report_19'
    id = db.Column(db.Integer, primary_key=True)
    col1 = db.Column(db.String(20))  # 19:00 report
    description = db.Column(db.Text, nullable=False)
    tag = db.Column(db.String(20))
    park = db.Column(db.String(50))
    contract = db.Column(db.String(50))
    date = db.Column(db.DateTime, nullable=True)


class Ktg_na_19(db.Model):
    __tablename__ = 'ktg_na_19'
    id = db.Column(db.Integer, primary_key=True)
    mark_1 = db.Column(db.String(20))  # 1 отметка
    mark_2 = db.Column(db.String(20))  # 2 отметка
    mark_3 = db.Column(db.String(20))  # 3 отметка
    mark_4 = db.Column(db.String(20))  # 4 отметка


@application.template_filter('completed_work_text')
def completed_work_text(value):
    from incontrol_child_plan import plain_completed_work
    return plain_completed_work(value)


@application.template_filter('nonone')
def nonone_filter(value, default=''):
    return value if value is not None else default


# --------ЭЛЕКТРОБУСЫ---------------------
@application.route('/electrobuses')
def electrobuses():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))

    selected_park = session.get('selected_park')
    available_parks = session.get('available_parks', [])

    # Парк из фильтра в URL (?park=...)
    park_filter = request.args.get('park')

    query = Elcar.query

    # --- Логика фильтрации ---
    if park_filter == 'all':
        # Все парки — без фильтра
        pass
    elif park_filter:
        # Фильтр выбран вручную
        query = query.filter(Elcar.location == park_filter)
    elif selected_park:
        # По умолчанию показываем парк пользователя
        query = query.filter(Elcar.location == selected_park)

    # --- Сортировка ---
    sort_by = request.args.get('sort_by', 'bort')
    order = request.args.get('order', 'asc')

    valid_sort_columns = ['bort', 'location', 'contract','comments']
    if sort_by not in valid_sort_columns:
        sort_by = 'bort'

    column = getattr(Elcar, sort_by)
    column = column.desc() if order == 'desc' else column.asc()

    elbuses = query.order_by(column, Elcar.bort.asc()).all()

    # заменить None на пусто
    for elcar in elbuses:
        for field in ['bort', 'gos_number', 'year', 'vin', 'location', 'contract', 'comments']:
            if getattr(elcar, field) is None:
                setattr(elcar, field, '')

    return render_template(
        'electrobuses.html',
        elbuses=elbuses,
        sort_by=sort_by,
        order=order,
        selected_park=selected_park
    )

@application.route('/upload_elbuses', methods=['POST'])
@login_required
def upload_elbuses():
    """
    Загрузка электробусов из Excel-файла.
    Если VIN уже существует в базе — обновляются данные (борт, госномер, год, контракт, модель, location).
    Если VIN не найден — создаётся новая запись.
    """
    if 'file' not in request.files:
        flash('Файл не выбран', 'error')
        return redirect(url_for('electrobuses'))

    file = request.files['file']
    if file.filename == '':
        flash('Файл не выбран', 'error')
        return redirect(url_for('electrobuses'))

    if not file.filename.endswith(('.xlsx', '.xls')):
        flash('Поддерживаются только файлы .xlsx и .xls', 'error')
        return redirect(url_for('electrobuses'))

    try:
        # Читаем Excel-файл
        workbook = openpyxl.load_workbook(file, data_only=True)
        sheet = workbook.active

        # Определяем заголовки
        headers = {}
        for col_idx, cell in enumerate(sheet[1], start=1):
            if cell.value:
                headers[cell.value.strip()] = col_idx

        # Определяем колонки по заголовкам
        col_location = headers.get('Площадка')
        col_bort = headers.get('Гаражный номер')
        col_gos = headers.get('Гос. номер')
        col_vin = headers.get('VIN')
        col_contract = headers.get('Договор')
        col_models = headers.get('Модификация по ПТС')
        col_year = headers.get('Год выпуска')

        if not all([col_location, col_bort, col_gos, col_vin, col_contract, col_models, col_year]):
            flash('Не найдены обязательные колонки: Площадка, Гаражный номер, Гос. номер, VIN, Договор, Модификация по ПТС, Год выпуска', 'error')
            return redirect(url_for('electrobuses'))

        # Собираем данные из файла
        data_rows = []
        for row in sheet.iter_rows(min_row=2, values_only=True):
            if not row or not any(row):
                continue

            location = str(row[col_location - 1] or '').strip()
            bort = str(row[col_bort - 1] or '').strip()
            if not bort:
                continue

            gos_number = str(row[col_gos - 1] or '').strip()
            vin = str(row[col_vin - 1] or '').strip()
            contract = str(row[col_contract - 1] or '').strip()
            models = str(row[col_models - 1] or '').strip()
            year = row[col_year - 1]

            # Преобразуем год в int
            try:
                if isinstance(year, (int, float)):
                    year = int(year)
                elif isinstance(year, str):
                    year = int(year.strip())
                else:
                    year = None
            except (ValueError, TypeError):
                year = None

            data_rows.append({
                'location': location,
                'bort': bort,
                'gos_number': gos_number,
                'vin': vin,
                'contract': contract,
                'models': models,
                'year': year
            })

        if not data_rows:
            flash('В файле нет данных для загрузки', 'warning')
            return redirect(url_for('electrobuses'))

        # Обработка данных: обновляем существующие, добавляем новые
        added = 0
        updated = 0
        errors = []

        for item in data_rows:
            vin = item['vin']
            if not vin:
                errors.append(f"Пропущен VIN для борта {item['bort']}")
                continue

            # Ищем запись по VIN
            existing = Elcar.query.filter_by(vin=vin).first()

            if existing:
                # Обновляем существующую запись
                existing.bort = item['bort']
                existing.gos_number = item['gos_number']
                existing.year = item['year']
                existing.contract = item['contract']
                existing.models = item['models']
                existing.location = item['location']  # Берем из колонки "Площадка"
                existing.type_ts = 'Электробус'
                updated += 1
            else:
                # Создаём новую запись
                new_elcar = Elcar(
                    bort=item['bort'],
                    gos_number=item['gos_number'],
                    year=item['year'],
                    vin=vin,
                    location=item['location'],  # Берем из колонки "Площадка"
                    contract=item['contract'],
                    type_ts='Электробус',
                    models=item['models'],
                    seasonal_service=0,
                    comments=None
                )
                db.session.add(new_elcar)
                added += 1

        db.session.commit()

        flash(f'Загрузка завершена: добавлено {added}, обновлено {updated}', 'success')
        if errors:
            flash(f'Ошибки: {", ".join(errors[:5])}', 'warning')

        application.logger.info(
            f"User {current_user.username} uploaded elbuses: added={added}, updated={updated}"
        )

    except Exception as e:
        db.session.rollback()
        application.logger.error(f"Error uploading elbuses: {e}", exc_info=True)
        flash(f'Ошибка при загрузке файла: {str(e)}', 'error')

    return redirect(url_for('electrobuses'))

@application.route('/add_elcar', methods=['GET', 'POST'])
def add_elcar():
    if request.method == 'POST':
        bort = request.form['bort']
        vin = request.form['vin']

        # Проверка на существование bort или vin в базе
        existing_elcar = Elcar.query.filter((Elcar.bort == bort) | (Elcar.vin == vin)).first()
        if existing_elcar:
            flash('Ошибка: Электробус с таким бортовым номером или VIN уже существует в базе!', 'error')
            return render_template('edit_elcar.html', elcar=None)

        new_elcar = Elcar(
            bort=bort,
            gos_number=request.form['gos_number'],
            year=request.form['year'],
            vin=vin,
            location=request.form['location'],
            contract=request.form['contract']
        )
        db.session.add(new_elcar)
        db.session.commit()
        flash('Электробус успешно добавлен!', 'success')
        return redirect(url_for('electrobuses'))

    return render_template('edit_elcar.html', elcar=None)


@application.route('/edit_elcar/<int:elcar_id>', methods=['GET', 'POST'])
def edit_elcar(elcar_id):
    elcar = Elcar.query.get_or_404(elcar_id)

    if request.method == 'POST':
        old_bort = elcar.bort  # Сохраняем старое значение борта
        new_bort = request.form['bort']  # Получаем новое значение

        # Обновляем данные электробуса
        elcar.bort = new_bort
        elcar.gos_number = request.form['gos_number']
        elcar.year = request.form['year']
        elcar.vin = request.form['vin']
        elcar.location = request.form['location']
        elcar.contract = request.form['contract']
        elcar.models = request.form['models']
        elcar.comments = request.form['comments']

        # Если изменился bort, обновляем связанные записи в Task
        if old_bort != new_bort:
            # Находим все задачи, связанные с этим электробусом
            tasks_to_update = Task.query.filter_by(board_number=old_bort).all()

            # Обновляем board_number в каждой задаче
            for task in tasks_to_update:
                task.board_number = new_bort

        db.session.commit()
        return redirect(url_for('electrobuses'))

    return render_template('edit_elcar.html', elcar=elcar)


@application.route('/delete_elcar/<int:elcar_id>')
def delete_elcar(elcar_id):
    elcar = Elcar.query.get_or_404(elcar_id)
    db.session.delete(elcar)
    db.session.commit()
    return redirect(url_for('electrobuses'))


# ------------------------------------------------------------------------------------------------
@application.route('/buses')
def buses():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))

    selected_park = session.get('selected_park')
    available_parks = session.get('available_parks', [])

    # Парк из фильтра в URL (?park=...)
    park_filter = request.args.get('park', 'all')

    query = Car.query

    # --- Логика фильтрации ---
    if park_filter == 'all':
        # Все парки — без фильтра
        pass
    elif park_filter:
        # Фильтр выбран вручную
        query = query.filter(Car.location == park_filter)
    elif selected_park:
        # По умолчанию показываем парк пользователя
        query = query.filter(Car.location == selected_park)

    # --- Сортировка ---
    sort_by = request.args.get('sort_by', 'bort')
    order = request.args.get('order', 'asc')

    valid_sort_columns = ['bort', 'location', 'contract','comments']
    if sort_by not in valid_sort_columns:
        sort_by = 'bort'

    column = getattr(Car, sort_by)
    column = column.desc() if order == 'desc' else column.asc()

    cars = query.order_by(column, Car.bort.asc()).all()

    # None → ""
    for car in cars:
        for field in ['bort', 'gos_number', 'year', 'vin', 'location', 'contract','comments']:
            if getattr(car, field) is None:
                setattr(car, field, '')

    return render_template(
        'buses.html',
        cars=cars,
        sort_by=sort_by,
        order=order,
        selected_park=selected_park
    )


@application.route('/add_car', methods=['GET', 'POST'])
def add_car():
    if request.method == 'POST':
        bort = request.form['bort']
        vin = request.form['vin']

        # Проверка на существование bort или vin в базе
        existing_car = Car.query.filter((Car.bort == bort) | (Car.vin == vin)).first()
        if existing_car:
            flash('Ошибка: Автобус с таким бортовым номером или VIN уже существует в базе!', 'error')
            return render_template('edit_car.html', car=None)

        new_car = Car(
            bort=bort,
            gos_number=request.form['gos_number'],
            year=request.form['year'],
            vin=vin,
            location=request.form['location'],
            contract=request.form['contract']
        )
        db.session.add(new_car)
        db.session.commit()
        flash('Автобус успешно добавлен!', 'success')
        return redirect(url_for('buses'))

    return render_template('edit_car.html', car=None)


@application.route('/edit_car/<int:car_id>', methods=['GET', 'POST'])
def edit_car(car_id):
    car = Car.query.get_or_404(car_id)

    if request.method == 'POST':
        old_bort = car.bort  # Сохраняем старое значение
        new_bort = request.form['bort']  # Получаем новое значение

        # Обновляем данные автомобиля
        car.bort = new_bort
        car.gos_number = request.form['gos_number']
        car.year = request.form['year']
        car.vin = request.form['vin']
        car.location = request.form['location']
        car.contract = request.form['contract']
        car.comments = request.form['comments']

        # Если изменился bort, обновляем связанные записи в Task
        if old_bort != new_bort:
            # Находим все связанные задачи
            tasks_to_update = Task.query.filter_by(board_number=old_bort).all()

            # Обновляем board_number в каждой задаче
            for task in tasks_to_update:
                task.board_number = new_bort

        db.session.commit()
        return redirect(url_for('buses'))

    return render_template('edit_car.html', car=car)


@application.route('/delete_car/<int:car_id>')
def delete_car(car_id):
    car = Car.query.get_or_404(car_id)
    db.session.delete(car)
    db.session.commit()
    return redirect(url_for('buses'))


# ---------------ЗАДАЧИ------------------------------------------
@application.route('/edit_description/<int:task_id>', methods=['GET', 'POST'])
def edit_description(task_id):
    task = Task.query.get_or_404(task_id)
    is_to_task = is_to_schedule_task(task)

    if request.method == 'POST':
        if is_to_task:
            base_description = get_to_base_description(task)
            additional_description = request.form.get('additional_description', '')
            task.description = build_to_description(base_description, additional_description)
        else:
            task.description = request.form['description']
        db.session.commit()
        return redirect(url_for('index'))

    return render_template(
        'edit_description.html',
        task=task,
        is_to_task=is_to_task,
        to_task_notice=TO_TASK_NOTICE,
        to_base_description=get_to_base_description(task) if is_to_task else '',
        to_additional_description=get_to_additional_description(task) if is_to_task else ''
    )


@application.route('/update_tag/<int:task_id>', methods=['POST'])
@csrf.exempt
def update_tag(task_id):
    task = Task.query.get_or_404(task_id)
    new_tag = request.form.get('tag')
    ozch_details = request.form.get('ozch_details', '').strip()

    # 1. Извлекаем параметры сортировки из запроса
    sort_by = request.form.get('sort_by', 'board_number')
    order = request.form.get('order', 'asc')

    if is_to_schedule_task(task):
        flash(TO_TASK_NOTICE, 'warning')
        return redirect(url_for('index', sort_by=sort_by, order=order))

    # --- OZCH логика ---
    if new_tag == 'ozch' and ozch_details:
        if task.description:
            task.description = f"{task.description}\n(ОЗЧ: {ozch_details})"
        else:
            task.description = f"ОЗЧ: {ozch_details}"

    # --- обновляем tag у Task ---
    task.tag = new_tag

    # --- обновляем tag у Elcar1440 ---
    Elcar1440.query.filter(
        Elcar1440.board == task.board_number
    ).update(
        {"tag": new_tag},
        synchronize_session=False
    )

    db.session.commit()

    flash(f'Cтатус изменен на "{TAG_CHOICES[new_tag]}"', 'success')
    application.logger.info(
        f"User: {current_user.username}, "
        f"Updated tag for task {task_id} to {new_tag}"
    )

    # 2. Возвращаем пользователя на index с сохранением сортировки
    return redirect(url_for('index', sort_by=sort_by, order=order))


@application.route('/csrf-token')
def csrf_token_refresh():
    # Доступен также форме входа. Обновляется подпись токена своей сессии,
    # права пользователя и срок проверки CSRF не меняются.
    response = jsonify(csrf_token=generate_csrf())
    response.headers['Cache-Control'] = 'no-store, private'
    response.vary.add('Cookie')
    return response


@application.route('/index/csrf-token')
@login_required
def index_csrf_token():
    response = csrf_token_refresh()
    response.headers['Cache-Control'] = 'no-store'
    return response


@application.route('/index')
@application.route('/')
def index():
    is_refresh = request.headers.get('X-Index-Refresh') == '1'
    if is_refresh and current_user.is_authenticated and current_user.company != 'Сетранс':
        return jsonify(error='Доступ запрещён'), 403
    # 1. Проверка устройства и авторизация
    user_agent = parse(request.headers.get('User-Agent'))
    if user_agent.is_mobile:
        return redirect("https://m.setrans.online")
    if not current_user.is_authenticated:
        return redirect(url_for('login'))

    # 2. Работа с сессией и парками
    available_parks = session.get('available_parks', [])
    selected_park = session.get('selected_park')
    if not selected_park and available_parks:
        selected_park = available_parks[0]
        if not is_refresh:
            session['selected_park'] = selected_park

    # 3. Настройка времени (Единый московский стандарт)
    tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(tz)

    # Расчет смены 08:00 - 08:00
    today_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now < today_8am:
        shift_start = today_8am - timedelta(days=1)
        shift_end = today_8am
    else:
        shift_start = today_8am
        shift_end = today_8am + timedelta(days=1)

    # 4. Базовый фильтр по парку (универсальный)
    def apply_park_filter(q):
        if selected_park:
            return q.filter(Task.park == selected_park)
        if available_parks:
            return q.filter(Task.park.in_(available_parks))
        return q.filter(Task.park == None)

    # 5. Сортировка и получение задач в ремонте
    sort_by = request.args.get('sort_by', session.get('sort_by', 'board_number'))
    order = request.args.get('order', session.get('order', 'asc'))

    query = Task.query.filter_by(status='in_repair')
    query = apply_park_filter(query)

    # Проверка допустимых значений
    valid_sort_columns = ['board_number', 'created_at', 'tag']
    valid_orders = ['asc', 'desc']
    if sort_by not in valid_sort_columns:
        sort_by = 'board_number'
    if order not in valid_orders:
        order = 'asc'

    # Сохраняем параметры сортировки в сессии
    if not is_refresh:
        session['sort_by'] = sort_by
        session['order'] = order

    if sort_by == 'tag':
        tag_priority = case(
            (Task.tag == 'in_work', 0),
            (Task.tag == 'waiting', 1),
            (Task.tag == 'to', 2),
            (Task.tag == 'ozch', 3),
            else_=99
        )
        tasks = query.order_by(tag_priority.asc(), Task.board_number.asc()).all()
    else:
        valid_cols = ['board_number', 'created_at']
        col = getattr(Task, sort_by if sort_by in valid_cols else 'board_number')
        direction = col.desc() if order == 'desc' else col.asc()
        tasks = query.order_by(direction, Task.board_number.asc()).all()

    # 6. Подчет в работе(учитывает все)
    counted_tasks = [t for t in tasks]
    in_repair_count = len(counted_tasks)
    # Подчет в работе(не учитывает без заявки и ТО)
    not_all_tasks = [t for t in tasks if t.tag not in ('to','no_request','success')]
    not_all = len(not_all_tasks)
    
    diesel_count = sum(1 for t in tasks if t.type_ts == 'Автобус')
    elbus_count = sum(1 for t in tasks if t.type_ts == 'Электробус')

    # 7. Данные о контрактах и сезонном обслуживании для бортов
    board_numbers = [t.board_number for t in tasks]

    if board_numbers:
        cars_rows = Car.query.filter(Car.bort.in_(board_numbers)).all()
        elcars_rows = Elcar.query.filter(Elcar.bort.in_(board_numbers)).all()
    else:
        cars_rows = []
        elcars_rows = []

    cars_data = {car.bort: car.contract for car in cars_rows}
    elcars_data = {elcar.bort: elcar.contract for elcar in elcars_rows}

    seasonal_vehicles = {
        vehicle.bort: True
        for vehicle in cars_rows + elcars_rows
        if getattr(vehicle, 'seasonal_service', False)
    }

    # Борта, по которым у заказчика в incontrol24 есть открытая заявка.
    # Читаем только кеш - наружу страница не ходит.
    incontrol_boards = get_incontrol_boards(board_numbers)

    # 8. Расчет заявок за смену (08:00 - 08:00)
    excluded_patterns = ['линии', 'линию', 'линия', 'предоставлен', 'кузовном']
    exclude_filter = or_(*[Task.completed_work.ilike(f'%{p}%') for p in excluded_patterns])

    shift_query = Task.query.filter(Task.created_at >= shift_start, Task.created_at < shift_end)
    shift_query = apply_park_filter(shift_query)
    shift_tasks_count = shift_query.count()

    closed_query = Task.query.filter(
    Task.status.in_(['completed']),
    Task.completed_at >= shift_start,
    Task.completed_at < shift_end
    )
    closed_query = apply_park_filter(closed_query)
    closed_tasks_count = closed_query.count()

    completed_query = Task.query.filter(
        Task.completed_at >= shift_start,
        Task.completed_at < shift_end,
        Task.completed_work.isnot(None),
        ~exclude_filter
    )
    completed_query = apply_park_filter(completed_query)
    completed_tasks_count = completed_query.count()

    completion_rate = round((completed_tasks_count / shift_tasks_count) * 100, 1) if shift_tasks_count > 0 else 0

    # 9. КТГ по контрактам
    TIME_CONTRACTS = {'МГТ234953-23', 'МГТ0019-25', "МГТ0107-21"}
    SIMPLE_CONTRACTS = {"216644", "222150", "МГТ0105-21", "МГТ0107-21"}
    ALL_TARGETS = TIME_CONTRACTS | SIMPLE_CONTRACTS

    ktg_results = {}
    start_of_day_ktg = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Minutes-based KTG
    records = Elcar1440.query.filter(
        Elcar1440.park == selected_park,
        Elcar1440.contract.in_(TIME_CONTRACTS),
        Elcar1440.tag != 'no_request'
    ).all()

    contract_minutes = {}
    for rec in records:
        c_at = rec.created_at.replace(tzinfo=tz) if rec.created_at.tzinfo is None else rec.created_at
        start_time = max(c_at, start_of_day_ktg)
        minutes = int((now - start_time).total_seconds() // 60)
        contract_minutes[rec.contract] = contract_minutes.get(rec.contract, 0) + minutes

    # Fleet data
    fleet_rows = FleetSize.query.filter(
        FleetSize.location.in_(ALL_TARGETS),
        FleetSize.zone.contains(selected_park)
    ).all()
    fleet_map = {f.location: f.count for f in fleet_rows}

    for contract in TIME_CONTRACTS:
        total_cars = fleet_map.get(contract, 0)
        if total_cars > 0:
            downtime = contract_minutes.get(contract, 0)
            ktg_results[contract] = round(((total_cars * 1440 - downtime) / (total_cars * 1440)) * 100, 2)

    # Простой КТГ (по количеству машин в работе)
    group_parks = PARK_GROUPS.get(selected_park, [selected_park])
    vehicles_in_work_counts = db.session.query(Car.contract, db.func.count(Car.id)) \
        .join(Task, Task.board_number == Car.bort) \
        .filter(
            Task.status == 'in_repair',
            Task.park.in_(group_parks),
            Task.tag != 'no_request',
            Car.contract.in_(SIMPLE_CONTRACTS)
        ).group_by(Car.contract).all()

    work_counts_map = dict(vehicles_in_work_counts)
    
    contract_counts = dict(
        db.session.query(
            func.coalesce(Elcar.contract, Car.contract).label("contract"),
            func.count(Task.id)
        )
        .outerjoin(Elcar, Elcar.bort == Task.board_number)
        .outerjoin(Car, Car.bort == Task.board_number)
        .filter(
            Task.status == 'in_repair',
            Task.park == selected_park,
            Task.tag != 'no_request',
            func.coalesce(Elcar.contract, Car.contract).in_([
                'МГТ234953-23',
                'МГТ0019-25',
                'МГТ0107-21',
                '216644',
                '222150',
                'МГТ0105-21',
                'МГТ0223-25',
                'МГТ0084-25'
            ])
        )
        .group_by(func.coalesce(Elcar.contract, Car.contract))
        .all()
    )

    contract_2_total = contract_counts.get('216644', 0)
    contract_6_total = contract_counts.get('222150', 0)
    contract_7_total = contract_counts.get('МГТ0107-21', 0)
    contract_8_total = contract_counts.get('МГТ0105-21', 0)
    contract_9_total = contract_counts.get('МГТ234953-23', 0)
    contract_11_total = contract_counts.get('МГТ0019-25', 0)
    contract_12_total = contract_counts.get('МГТ0223-25', 0)
    contract_13_total = contract_counts.get('МГТ0084-25', 0)

    ktg_map = {"216644": "ktg2", "222150": "ktg6", "МГТ0105-21": "ktg8", "МГТ0107-21": "ktg7"}
    final_simple_ktg = {}
    for code, var_name in ktg_map.items():
        total = fleet_map.get(code, 0)
        in_work = work_counts_map.get(code, 0)
        final_simple_ktg[var_name] = round(((total - in_work) / total) * 100, 2) if total > 0 else None
        
    # === КТГ парка ===
    loc_map = PARK_TO_LOCATION.get(selected_park, {})
    locations = [loc for loc in (loc_map.get('bus'), loc_map.get('elbus')) if loc]

    if locations:
        total_fleet = db.session.query(
            func.coalesce(func.sum(FleetSize.count), 0)
        ).filter(FleetSize.location.in_(locations)).scalar()
    else:
        total_fleet = 0

    if total_fleet > 0:
        ktg_park = round(((total_fleet - not_all) / total_fleet) * 100, 2)
    else:
        ktg_park = None    

    context = dict(
        index_sort_by=sort_by, index_sort_order=order,
        tasks=tasks,
        ktg_park=ktg_park,
        cars=cars_data,
        elcars=elcars_data,
        selected_park=selected_park,
        in_repair_count=in_repair_count,
        elbus_count=elbus_count,
        diesel_count=diesel_count,
        ktg_results=ktg_results,
        TAG_CHOICES=TAG_CHOICES,
        to_task_notice=TO_TASK_NOTICE,
        ktg2=final_simple_ktg.get('ktg2'),
        ktg6=final_simple_ktg.get('ktg6'),
        ktg7=final_simple_ktg.get('ktg7'),
        ktg8=final_simple_ktg.get('ktg8'),
        shift_tasks_count=shift_tasks_count,
        closed_tasks_count=closed_tasks_count,
        completed_tasks_count=completed_tasks_count,
        completion_rate=completion_rate,
        seasonal_vehicles=seasonal_vehicles,
        incontrol_boards=incontrol_boards,
        contract_2_total=contract_2_total,
        contract_6_total=contract_6_total,
        contract_7_total=contract_7_total,
        contract_8_total=contract_8_total,
        contract_11_total=contract_11_total,
        contract_9_total=contract_9_total,
        contract_12_total=contract_12_total,
        not_all=not_all,
        contract_13_total=contract_13_total
    )
    if is_refresh:
        response = jsonify(
            tasks_html=render_template('_index_tasks.html', **context),
            counters_html=render_template('_index_counters.html', **context),
            contract_counters_html=render_template('_index_contract_counters.html', **context),
            park=selected_park or '', sort_by=sort_by, order=order,
            csrf_token=generate_csrf(),
        )
    else:
        response = make_response(render_template('index.html', **context))
    response.headers['Cache-Control'] = 'no-store'
    response.vary.add('X-Index-Refresh')
    return response




@application.route('/about')
def about():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))

    available_parks = session.get('available_parks', [])
    selected_park = session.get('selected_park')

    # 1. Оптимизация проверки прав (упрощено)
    if selected_park and selected_park not in available_parks:
        flash(f'Доступ к парку {selected_park} запрещен', 'error')
        selected_park = available_parks[0] if available_parks else None
        session['selected_park'] = selected_park

    # 2. Базовый запрос с оптимизацией (загружаем только нужные столбцы, если их много в модели)
    # Используем .filter вместо .filter_by для единообразия
    query = Task.query.filter(Task.status == 'completed')

    # 3. Применение фильтров по паркам (ускорена логика условий)
    if selected_park:
        query = query.filter(Task.park == selected_park)
    elif available_parks:
        query = query.filter(Task.park.in_(available_parks))
    else:
        # Если нет доступных парков, возвращаем пустой результат без лишних запросов
        return render_template('about.html', tasks=[], pagination=None, search_term='')

    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()

    if date_from:
        try:
            date_from_dt = datetime.strptime(date_from, '%Y-%m-%d')
            # Начало дня (00:00:00)
            date_from_dt = tz.localize(date_from_dt)
            query = query.filter(Task.completed_at >= date_from_dt)
        except ValueError:
            pass

    if date_to:
        try:
            date_to_dt = datetime.strptime(date_to, '%Y-%m-%d')
            # Конец дня (23:59:59)
            date_to_dt = tz.localize(date_to_dt + timedelta(days=1) - timedelta(seconds=1))
            query = query.filter(Task.completed_at <= date_to_dt)
        except ValueError:
            pass

    # 4. Поиск (добавлен .low_priority или индексируемые поля)
    search_term = request.args.get('search', '').strip()
    if search_term:
        vehicle_search = f'%{search_term}%'
        vehicle_boards = [
            board for (board,) in Car.query.with_entities(Car.bort).filter(
                db.or_(
                    Car.vin.ilike(vehicle_search),
                    Car.gos_number.ilike(vehicle_search)
                )
            ).all()
        ]
        vehicle_boards.extend(
            board for (board,) in Elcar.query.with_entities(Elcar.bort).filter(
                db.or_(
                    Elcar.vin.ilike(vehicle_search),
                    Elcar.gos_number.ilike(vehicle_search)
                )
            ).all()
        )

        search_filters = [
            Task.board_number.ilike(f'{search_term}%'), # Поиск по началу строки быстрее, чем %term%
            Task.vin.ilike(vehicle_search),
            Task.description.ilike(vehicle_search),
            Task.completed_work.ilike(vehicle_search)
        ]
        if vehicle_boards:
            search_filters.append(Task.board_number.in_(vehicle_boards))

        query = query.filter(db.or_(*search_filters))

    # 5. Пагинация и сортировка
    page = request.args.get('page', 1, type=int)
    per_page = 50

    # order_by перед paginate обязателен для стабильной работы
    paginated_tasks = query.order_by(Task.completed_at.desc()).paginate(
        page=page,
        per_page=per_page,
        error_out=False
    )


    board_numbers = [task.board_number for task in paginated_tasks.items]
    vehicles_by_board = {}
    if board_numbers:
        cars_rows = Car.query.filter(Car.bort.in_(board_numbers)).all()
        elcars_rows = Elcar.query.filter(Elcar.bort.in_(board_numbers)).all()
        vehicles_by_board = {vehicle.bort: vehicle for vehicle in elcars_rows}
        vehicles_by_board.update({vehicle.bort: vehicle for vehicle in cars_rows})

    for task in paginated_tasks.items:
        vehicle = vehicles_by_board.get(task.board_number)
        task.vehicle_vin = vehicle.vin if vehicle else task.vin
        task.vehicle_gos_number = vehicle.gos_number if vehicle else None

    # 6. Формирование ответа без лишних переменных
    return render_template(
        'about.html',
        tasks=paginated_tasks.items,
        pagination=paginated_tasks,
        search_term=search_term,
        selected_park=selected_park
    )


@application.route('/add_task', methods=['POST'])
def add_task():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))

    try:
        board_number = normalize_task_board(request.form.get('board_number', ''))
        description = request.form.get('description', '').strip()
        selected_park = session.get('selected_park')
        available_parks = session.get('available_parks', [])
        tz = pytz.timezone('Europe/Moscow')

        # 1. Быстрые проверки сессии
        if not selected_park:
            flash("Выберите парк перед добавлением задачи.", "error")
            return redirect(url_for('index'))

        if selected_park not in available_parks and current_user.username != 'vasilev':
            application.logger.warning(
                f'add_task: доступ к парку {selected_park} запрещен '
                f'для пользователя {current_user.username}'
            )
            flash(f"Доступ к парку {selected_park} запрещен.", "error")
            return redirect(url_for('index'))

        if not board_number:
            flash("Укажите бортовой номер.", "error")
            return redirect(url_for('index'))

        # 2. Поиск борта (сначала Автобус, потом Электробус)
        vehicle = Car.query.filter_by(bort=board_number).first()
        is_elbus = False

        if not vehicle:
            vehicle = Elcar.query.filter_by(bort=board_number).first()
            is_elbus = True

        if not vehicle:
            application.logger.info(
                f'add_task: борт {board_number} не найден в Car/Elcar — пропуск '
                f'(парк={selected_park}, автор={current_user.username})'
            )
            flash(f"Борт {board_number} не найден в системе.", "error")
            return redirect(url_for('index'))

        # 3. Проверка локации (используем уже найденный объект vehicle)
        allowed_locations = PARK_GROUPS.get(selected_park, [selected_park])
        if vehicle.location not in allowed_locations:
            application.logger.warning(
                f'add_task: борт {board_number} принадлежит {vehicle.location}, '
                f'а не группе {selected_park} — отказ '
                f'(автор={current_user.username})'
            )
            flash(f"Борт {board_number} принадлежит {vehicle.location}, а не группе {selected_park}.", "error")
            return redirect(url_for('index'))

        # 4. Проверка активной задачи (одним запросом)
        active_task = get_active_task_for_board(board_number)
        if active_task:
            application.logger.info(
                f'add_task: для борта {board_number} уже существует активная задача '
                f'(task_id={getattr(active_task, "id", None)}) — пропуск '
                f'(парк={selected_park}, автор={current_user.username})'
            )
            flash(f"Для борта {board_number} уже существует активная задача.", "error")
            return redirect(url_for('index'))

        # 5. Проверка комментария у борта перед созданием задачи
        comment_checked = request.form.get('comment_checked') == '1'
        comment_action = request.form.get('comment_action')
        vehicle_comment = (getattr(vehicle, 'comments', '') or '').strip()

        # Обработка комментария
        if vehicle_comment and not comment_checked:
            session['pending_add_task'] = {
                'board_number': board_number,
                'description': description
            }
            session['pending_car_comment'] = {
                'board_number': board_number,
                'comment': vehicle_comment
            }
            application.logger.info(
                f'add_task: у борта {board_number} есть комментарий — '
                f'запрос подтверждения у пользователя {current_user.username} '
                f'(парк={selected_park})'
            )
            return redirect(url_for('index'))

        # Очистка сессии если комментарий уже был обработан
        if comment_checked:
            session.pop('pending_add_task', None)
            session.pop('pending_car_comment', None)

        # Обработка действий с комментарием
        if comment_checked and comment_action == 'delete' and hasattr(vehicle, 'comments'):
            vehicle.comments = None
            db.session.commit()
            application.logger.info(
                f'add_task: комментарий для борта {board_number} удален '
                f'(автор={current_user.username}, парк={selected_park})'
            )
            flash(f"Комментарий для борта {board_number} удален.", "success")

        elif comment_checked and comment_action == 'proceed':
            application.logger.info(
                f'add_task: наряд для борта {board_number} открыт несмотря на комментарий '
                f'(автор={current_user.username}, парк={selected_park})'
            )
            flash(f"Наряд для борта {board_number} открыт, несмотря на комментарий.", "success")

        # 6. Создание задачи (если не было отмены)
        if comment_action == 'cancel':
            application.logger.info(
                f'add_task: создание наряда для борта {board_number} отменено '
                f'(автор={current_user.username}, парк={selected_park})'
            )
            flash("Создание наряда отменено.", "info")
            return redirect(url_for('index'))

        vehicle = type(vehicle).query.filter_by(bort=board_number).populate_existing().with_for_update().first()
        if vehicle is None or vehicle.location not in allowed_locations:
            flash(f"Борт {board_number} больше недоступен в выбранном парке.", "error")
            return redirect(url_for('index'))
        if get_active_task_for_board(board_number, lock=True):
            flash(f"Для борта {board_number} уже существует активная задача.", "error")
            return redirect(url_for('index'))

        now = datetime.now(tz)
        new_task = Task(
            board_number=board_number,
            description=description,
            park=selected_park,
            status='in_repair',
            created_at=now,
            tag='waiting',
            type_ts='Электробус' if is_elbus else 'Автобус',
            author_open=current_user.full_name,
            contract=vehicle.contract,
            vin=vehicle.vin
        )

        db.session.add(new_task)
        db.session.commit()

        application.logger.info(
            f'add_task: создана Task id={new_task.id} для борта {board_number}, '
            f'парк={selected_park}, тип={new_task.type_ts}, '
            f'автор={current_user.full_name}, '
            f'Описание = {description[:120]}'
        )

        # 7. Логика Elcar1440
        if is_elbus and vehicle.contract in ELCAR1440_CONTRACTS:
            already_exists = db.session.query(Elcar1440.id).filter_by(
                board=board_number, contract=vehicle.contract
            ).first()

            if not already_exists:
                db.session.add(Elcar1440(
                    board=board_number,
                    vin=vehicle.vin,
                    park=selected_park,
                    contract=vehicle.contract,
                    created_at=now
                ))
                application.logger.info(
                    f'add_task: создана запись Elcar1440 для борта {board_number}, '
                    f'контракт={vehicle.contract}, парк={selected_park}'
                )
            else:
                application.logger.info(
                    f'add_task: запись Elcar1440 для борта {board_number}, '
                    f'контракт={vehicle.contract} уже существует — пропуск'
                )

        db.session.commit()

        # Если не было flash сообщения от обработки комментария, добавляем стандартное
        if comment_action not in ['delete', 'proceed']:
            flash(f"Новая заявка для борта {board_number} успешно добавлена.", "success")

    except Exception as e:
        db.session.rollback()
        application.logger.error(
            f'add_task: ошибка при добавлении задачи — {e} '
            f'(борт={request.form.get("board_number")}, '
            f'парк={session.get("selected_park")}, '
            f'автор={getattr(current_user, "username", None)})'
        )
        flash("Системная ошибка при добавлении задачи.", "error")

    return redirect(url_for('index'))


@application.route('/edit_task_order/<int:task_id>', methods=['GET', 'POST'])
def edit_task_order(task_id):
    if not current_user.is_authenticated:
        flash('Требуется авторизация', 'error')
        return redirect(url_for('login'))

    task = Task.query.get_or_404(task_id)

    if request.method == 'POST':
        try:
            applications_from_str = request.form.get('applications_from')
            work_in_str = request.form.get('work_in')

            applications_from = as_moscow_datetime(applications_from_str)
            work_in = as_moscow_datetime(work_in_str)
            created_at = as_moscow_datetime(task.created_at)

            if work_in and created_at and work_in < created_at:
                flash('Дата и время начала работ не могут быть раньше даты и времени подачи заявки', 'error')
                return redirect(url_for('about', open_edit_modal=task_id))

            task.applications_from = applications_from
            task.work_in = work_in

            db.session.commit()
            flash('Заказ-наряд успешно обновлен', 'success')
            application.logger.info(
                f"User {current_user.username} updated task {task_id}: "
                f"applications_from={task.applications_from}, work_in={task.work_in}"
            )
            return redirect(url_for('about'))

        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка при обновлении заказ-наряда: {str(e)}', 'error')
            application.logger.error(
                f"User {current_user.username} failed to update task {task_id}: {str(e)}"
            )
            return redirect(url_for('about'))

    return redirect(url_for('about'))


@application.context_processor
def inject_pytz():
    return {'pytz': pytz, 'format_datetime': format_datetime,
            'requires_completion_check': completion_checks.required,
            'incontrol_closure_paused': completion_checks.paused,
            'paused_closure_services': paused_closure_services}


@application.route('/check_board', methods=['POST'])
@login_required
def check_board():
    data = request.get_json(silent=True)
    value = data.get('board_number') if isinstance(data, dict) else None
    board_number = normalize_task_board(value) if isinstance(value, str) else None
    if not board_number or len(board_number) > 20:
        return jsonify(error='Укажите бортовой номер.'), 400

    existing_task = get_active_task_for_board(board_number)
    if existing_task is None:
        return jsonify(exists=False)
    description = existing_task.description if existing_task.park in session.get('available_parks', []) else ''
    return jsonify(exists=True, description=description)


@application.route('/complete_task/<int:task_id>', methods=['GET', 'POST'])
def complete_task(task_id):
    task = completion_checks._task_for_user(
        task_id, lock=request.method == 'POST' and 'print_decision' not in request.form
    )
    if task.status != 'in_repair' and 'print_decision' not in request.form:
        flash('Заявка уже закрыта.', 'info')
        return redirect(url_for('index'))
    tz = pytz.timezone('Europe/Moscow')
    selected_park = session.get('selected_park')

    # Ищем машину
    vehicle = Car.query.filter_by(bort=task.board_number).first()
    is_elbus = False
    if not vehicle:
        vehicle = Elcar.query.filter_by(bort=task.board_number).first()
        is_elbus = True

    current_mileage = vehicle.mileage if vehicle else None
    is_evacuation = task.event_type and 'эвакуация' in task.event_type.lower()

    if request.method == 'GET' and request.args.get('rejection_run'):
        run, run_task = incontrol_rejections.owned(request.args['rejection_run'])
        if run_task.id != task.id:
            abort(404)
        saved = incontrol_rejections.unpack(run)
        return render_template('complete_task.html', task=task, current_mileage=current_mileage,
                               is_evacuation=is_evacuation, completed_work_value=saved['form']['completed_work'],
                               mileage_value=saved['form']['mileage'], work_in_value=saved['form']['work_in'],
                               incontrol_main_id_value=saved['remote_id'], incontrol_mode_value=saved.get('mode', 'reject'),
                               seasonal_service_value=saved['form'].get('seasonal_service', '0'),
                               evacuation_confirmed_value=saved['form'].get('evacuation_photos_confirmed', ''),
                               child_decisions_value=saved['form'].get('child_decisions', ''))

    if request.method == 'POST':

        # Определяем действие (отклонение или выполнение)
        is_reject = (request.form.get('action') == 'reject')

        # Обработка только печати
        if 'print_decision' in request.form:
            if request.form['print_decision'] == 'yes':
                return render_template(
                    'complete_task.html',
                    task=task,
                    show_print_prompt=True,
                    show_edit_dates=True,
                    current_mileage=current_mileage,
                    mileage_value=request.form.get('mileage')
                )
            return redirect(url_for('index'))

        # Проверяем заполнение выполненных работ
        completed_work = request.form.get('completed_work', '').replace('\r\n', '\n').replace('\r', '\n').strip()
        if not completed_work:
            application.logger.warning(
                f'complete_task: не заполнено «Выполненные работы» '
                f'(task_id={task.id}, борт={task.board_number}, '
                f'парк={task.park}, автор={current_user.username})'
            )
            flash('Поле «Выполненные работы» обязательно', 'error')
            return render_template(
                'complete_task.html',
                task=task,
                current_mileage=current_mileage,
                is_evacuation=is_evacuation,
                work_in_value=request.form.get('work_in'),
                completed_work_value=completed_work,
                mileage_value=request.form.get('mileage')
            )

        # Пробег
        mileage = request.form.get('mileage', '').strip()

        # Дата начала работ
        work_in_str = request.form.get('work_in', '').strip()
        work_in = as_moscow_datetime(work_in_str)

        if work_in_str and not work_in:
            application.logger.warning(
                f'complete_task: некорректная дата начала работ '
                f'work_in={work_in_str!r} '
                f'(task_id={task.id}, борт={task.board_number}, '
                f'парк={task.park}, автор={current_user.username})'
            )
            flash('Укажите корректные дату и время начала работ', 'error')
            return render_template(
                'complete_task.html',
                task=task,
                current_mileage=current_mileage,
                is_evacuation=is_evacuation,
                work_in_value=work_in_str,
                completed_work_value=completed_work,
                mileage_value=request.form.get('mileage')
            )

        created_at = as_moscow_datetime(task.created_at)
        completed_at = datetime.now(tz)

        if work_in and created_at and not is_reject:

            # 1. Не раньше создания заявки
            if work_in < created_at:
                application.logger.warning(
                    f'complete_task: work_in={work_in} раньше created_at={created_at} '
                    f'(task_id={task.id}, борт={task.board_number}, '
                    f'парк={task.park}, автор={current_user.username})'
                )
                flash(
                    'Дата и время начала работ не могут быть раньше даты и времени подачи заявки.',
                    'error'
                )
                return render_template(
                    'complete_task.html',
                    task=task,
                    current_mileage=current_mileage,
                    is_evacuation=is_evacuation,
                    work_in_value=work_in_str,
                    completed_work_value=completed_work,
                    mileage_value=request.form.get('mileage')
                )

            # # 2. Минимум 10 минут после создания заявки
            # if work_in < created_at + timedelta(minutes=10):
            #     ...

            # 3. Не позже текущего времени
            if work_in > completed_at:
                application.logger.warning(
                    f'complete_task: work_in={work_in} позже текущего времени '
                    f'completed_at={completed_at} '
                    f'(task_id={task.id}, борт={task.board_number}, '
                    f'парк={task.park}, автор={current_user.username})'
                )
                flash(
                    'Дата и время начала работ не могут быть позже текущего времени.',
                    'error'
                )
                return render_template(
                    'complete_task.html',
                    task=task,
                    current_mileage=current_mileage,
                    is_evacuation=is_evacuation,
                    work_in_value=work_in_str,
                    completed_work_value=completed_work,
                    mileage_value=request.form.get('mileage')
                )

            # 4. НОВАЯ ПРОВЕРКА: Минимальная продолжительность работы (10 минут)
            min_duration = timedelta(minutes=10)
            if completed_at - work_in < min_duration:
                application.logger.warning(
                    f'complete_task: продолжительность работы '
                    f'{(completed_at - work_in).seconds // 60} мин < '
                    f'{min_duration.seconds // 60} мин '
                    f'(task_id={task.id}, борт={task.board_number}, '
                    f'парк={task.park}, автор={current_user.username})'
                )
                flash(
                    f'Продолжительность работы должна быть не менее {min_duration.seconds // 60} минут. '
                    f'Текущая продолжительность: {(completed_at - work_in).seconds // 60} мин.',
                    'error'
                )
                return render_template(
                    'complete_task.html',
                    task=task,
                    current_mileage=current_mileage,
                    is_evacuation=is_evacuation,
                    work_in_value=work_in_str,
                    completed_work_value=completed_work,
                    mileage_value=request.form.get('mileage')
                )

        incontrol_paused = completion_checks.paused(task)
        offline = incontrol_paused or bool(request.form.get('offline_run_id') or request.form.get('offline_check_id'))
        if offline:
            from incontrol_rejection import RejectionError
            try:
                incontrol_outbox.stage_closure(task, completed_at, completed_work, is_reject, paused=incontrol_paused)
                check_error = None
            except RejectionError as exc:
                check_error = str(exc)
        elif request.form.get('completion_check_id') and completion_checks.required(task):
            # Одноразовое подтверждение старой проверки допустимо только при реально
            # включённой проверке: оно удостоверяет отсутствие открытой заявки.
            check_error = completion_checks.consume(task, request.form['completion_check_id'])
        elif incontrol_rejections.applies(task, request.form):
            if request.form.get('incontrol_rejection_run'):
                check_error = incontrol_rejections.consume(task, request.form)
            else:
                from incontrol_rejection import RejectionError
                from incontrol_accounts import PersonalAccountError
                try:
                    return incontrol_rejections.start(task, request.form)
                except (RejectionError, PersonalAccountError) as exc:
                    check_error = str(exc)
                except SQLAlchemyError:
                    check_error = 'Не удалось начать отклонение InControl. Проверьте наличие таблиц настроек и процесса.'
        else:
            check_error = completion_checks.consume(task, request.form.get('completion_check_id'))
        if check_error:
            db.session.rollback()
            flash(check_error, 'error')
            return render_template(
                'complete_task.html', task=task, current_mileage=current_mileage,
                is_evacuation=is_evacuation, work_in_value=work_in_str,
                completed_work_value=completed_work, mileage_value=mileage,
            )

        if mileage.isdigit():
            m_val = int(mileage)
            task.mileage = m_val
            if vehicle:
                vehicle.mileage = m_val
        if not is_reject or 'work_in' in request.form:
            task.work_in = work_in
        if vehicle and is_to_schedule_task(task):
            vehicle.seasonal_service = request.form.get('seasonal_service', '0') == '1'

        # Закрываем заявку
        task.status = 'completed'
        task.reject = (request.form.get('action') == 'reject')
        task.completed_work = completed_work
        task.author_closed = current_user.full_name
        task.completed_at = completed_at

        is_mail_task = task.author_open == MAIL_TASK_AUTHOR
        mail_board_number = task.board_number
        mail_paused = 'yandex_mail' in paused_closure_services()
        mail_reply_queued = None
        try:
            mail_reply_queued = yandex_requests.queue_reply(task, deferred=mail_paused)
            if mail_paused:
                task.yandex_reply_lookup = False
                for link in mail_reply_queued or ():
                    incontrol_outbox.stage_mail_closure(task, completed_at, completed_work, is_reject, link.ticket_number)
            elif not mail_reply_queued:
                yandex_requests.queue_reply_lookup(task)
            mail_lookup_queued = task.yandex_reply_lookup
            if is_elbus and vehicle:
                Elcar1440.query.filter_by(board=task.board_number, contract=vehicle.contract).delete()
            db.session.commit()
        except MailError as exc:
            db.session.rollback()
            flash(str(exc), 'error')
            return render_template('complete_task.html', task=task, current_mileage=current_mileage,
                                   is_evacuation=is_evacuation, work_in_value=work_in_str,
                                   completed_work_value=completed_work, mileage_value=mileage), 409
        except SQLAlchemyError:
            if not offline and not (is_mail_task or mail_reply_queued):
                raise
            db.session.rollback()
            application.logger.exception('Не удалось сохранить закрытие и отписку task_id=%s', task.id)
            flash('Не удалось сохранить закрытие и запись для отписки. Повторите проверку.', 'error')
            return render_template('complete_task.html', task=task, current_mileage=current_mileage,
                                   is_evacuation=is_evacuation, work_in_value=work_in_str,
                                   completed_work_value=completed_work, mileage_value=mileage), 503
        if offline:
            flash('Заявка закрыта в Сетранс и добавлена в список на отписку в InControl.', 'success')
        if mail_reply_queued:
            if mail_paused:
                flash('Автоотправка в Яндекс Почту приостановлена. Письмо сохранено в «К отписке» для ручного ответа.', 'success')
            else:
                yandex_requests.submit_reply(task_id, mail_board_number)
                flash(f'Письма Яндекс Почты в очереди на ответ: {len(mail_reply_queued)}.', 'success')
        elif mail_lookup_queued:
            yandex_requests.submit_reply(task_id, mail_board_number)

        application.logger.info(
            f'complete_task: заявка закрыта id={task.id}, '
            f'борт={task.board_number}, парк={task.park}, '
            f'тип={task.type_ts}, tag={task.tag}, '
            f'reject={task.reject}, '
            f'автор_закрытия={current_user.full_name}, '
            f'Выполненные работы = {completed_work[:120]}'
        )

        excluded_contracts = [
            'МГТ0019-25',
            'МГТ234953-23',
            'МГТ0223-25',
            'МГТ0084-25'
        ]

        if task.reject:
            return redirect(url_for('index'))

        #if (vehicle and vehicle.contract in excluded_contracts or selected_park == '14 парк') and task.tag != 'to':
        #    return redirect(url_for('index'))

        if (vehicle and vehicle.contract in excluded_contracts) and task.tag != 'to':
            return redirect(url_for('index'))

        return render_template(
            'complete_task.html',
            task=task,
            show_print_prompt=True,
            show_edit_dates=True,
            current_mileage=current_mileage,
            completed_work_value=completed_work,
            mileage_value=request.form.get('mileage')
        )

    return render_template(
        'complete_task.html',
        task=task,
        current_mileage=current_mileage,
        is_evacuation=is_evacuation,
        completed_work_value=task.completed_work
    )


# ----------------комментрарии-----------------------------------------
@application.route('/get_comment/<int:task_id>', methods=['GET'])
def get_comment(task_id):
    if not current_user.is_authenticated:
        return jsonify({'error': 'Unauthorized'}), 401

    # Оптимизация: берем только нужные поля и проверяем доступ к парку
    task_data = db.session.query(Task.comment, Task.park).filter(Task.id == task_id).first()

    if not task_data:
        abort(404)

    return jsonify({'comment': task_data.comment or ''})


@application.route('/add_comment/<int:task_id>', methods=['POST'])
def add_comment(task_id):
    if not current_user.is_authenticated:
        return jsonify({'error': 'Unauthorized'}), 401

    task = Task.query.get_or_404(task_id)
    comment = request.form.get('comment', '').strip()
    task.comment = comment

    try:
        db.session.commit()

        # Если это AJAX-запрос, лучше вернуть JSON
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'success', 'comment': comment})

        # Если обычная форма — редирект на mobile_index (согласно вашему app.py)
        sort_by = request.form.get('sort_by', session.get('sort_by', 'board_number'))
        order = request.form.get('order', session.get('order', 'asc'))
        return redirect(url_for('index', sort_by=sort_by, order=order))

    except Exception as e:
        db.session.rollback()
        application.logger.error(f"Error adding comment: {task_id}, error={str(e)}")
        return jsonify({'error': 'Ошибка при сохранении комментария'}), 500

    # ----------------Отчет-----------------------------------------

@application.route('/reports')
def reports():
    tz = pytz.timezone("Europe/Moscow")
    now_dt = datetime.now(tz)
    selected_park = session.get('selected_park', '').strip().lower().replace('_', ' ')

    if selected_park == '1':
        selected_park = '1 парк'

    # 1. Расчет интервала
    if now_dt.hour < 8:
        end_date = now_dt.replace(hour=8, minute=0, second=0, microsecond=0)
        start_date = end_date - timedelta(days=1)
    else:
        start_date = now_dt.replace(hour=8, minute=0, second=0, microsecond=0)
        end_date = start_date + timedelta(days=1)

    # 2. Логика праздников (используем глобальный RU_HOLIDAYS)
    current_date = now_dt.date()
    is_workday = current_date.weekday() < 5 and current_date not in ru_holidays

    # 3. Оптимизированные запросы к БД (фильтрация средствами SQL)
    date_query = Date.query.order_by(Date.id)
    if selected_park:
        # Используем func.lower для регистронезависимого поиска на уровне БД
        date_query = date_query.filter(func.lower(Date.park) == selected_park)
    filtered_dates = date_query.all()

    # Для FleetSize: если zones — это JSON или связь, фильтрация зависит от структуры моделей.
    # Если перенос в SQL сложен из-за формата поля zones, оставляем фильтрацию в Python,
    # но лучше пересмотреть модель в будущем.
    filtered_fleet = []
    for fleet in FleetSize.query.all():
        zones = [z.lower().strip() for z in fleet.zones]
        if not selected_park or selected_park in zones:
            filtered_fleet.append(fleet)

    # 4. Подсчет заявок
    base_task_query = Task.query.filter(Task.created_at >= start_date, Task.created_at < end_date)
    if selected_park and selected_park != 'all':
        base_task_query = base_task_query.filter(Task.park == selected_park)

    open_tasks_count = base_task_query.count()

    # Для закрытых заявок фильтруем по completed_at
    closed_tasks_query = Task.query.filter(
        Task.status == 'completed',
        Task.completed_at >= start_date,
        Task.completed_at < end_date
    )
    if selected_park and selected_park != 'all':
        closed_tasks_query = closed_tasks_query.filter(Task.park == selected_park)

    closed_tasks_count = closed_tasks_query.count()

    # 5. Обновление статистики (Осторожно: запись в GET-запросе)
    # Рекомендуется убрать этот блок в отдельный POST-метод или фоновую задачу
    if selected_park:
        entry_in = Date.query.filter_by(location="Поступившие заявки", park=selected_park).first()
        if entry_in and entry_in.count != open_tasks_count:
            entry_in.count = open_tasks_count

        entry_out = Date.query.filter_by(location="Закрытые заявки", park=selected_park).first()
        if entry_out and entry_out.count != closed_tasks_count:
            entry_out.count = closed_tasks_count

        db.session.commit()

    return render_template('reports.html',
                           fleet_sizes=filtered_fleet,
                           date=filtered_dates,
                           open_tasks_count=open_tasks_count,
                           is_workday=is_workday,
                           allowed_usernames=allowed_usernames,
                           selected_park=selected_park,
                           available_parks=session.get('available_parks', []),
                           current_time=now_dt.strftime('%H:%M'))

@application.route('/update_fleet', methods=['POST'])
def update_fleet():
    data = request.get_json()
    for item in data:
        fleet = FleetSize.query.get(item['id'])  # обновляем по id
        if fleet:
            fleet.count = item['count']
    db.session.commit()
    return jsonify({'status': 'success'})


@application.route('/update_date', methods=['POST'])
def update_date():
    try:
        data = request.get_json()
        updated_count = 0

        special_locations = ['СО Автобусы', 'СО Электробусы']

        for item in data:
            date_record = Date.query.get(item['id'])

            if date_record and date_record.location in special_locations:
                if int(item['count']) < date_record.count:
                    flash(f"Ошибка: {date_record.location} не может быть меньше {date_record.count}", "danger")
                    return jsonify({'status': 'error'}), 400

            if date_record:
                date_record.count = item['count']
                updated_count += 1

        db.session.commit()

        if updated_count > 0:
            flash("Данные успешно обновлены", "success")
        else:
            flash("Нет данных для обновления", "info")

        return jsonify({'status': 'success'})

    except Exception as e:
        db.session.rollback()
        application.logger.error(f"Error updating date: {e}")
        flash("Ошибка сервера при сохранении", "danger")
        return jsonify({'status': 'error'}), 500



def print_elcar1440_minutes_today(selected_park):
    TARGET_CONTRACTS = {'МГТ234953-23', 'МГТ0019-25'}

    tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(tz)

    start_of_day = tz.localize(
        datetime.combine(now.date(), time.min)
    )

    # --- Elcar1440 ---
    records = Elcar1440.query.filter(
        Elcar1440.park == selected_park,
        Elcar1440.contract.in_(TARGET_CONTRACTS)
    ).all()

    contract_minutes = {}
    contract_boards = {}

    for rec in records:
        created_at = rec.created_at
        if created_at.tzinfo is None:
            created_at = tz.localize(created_at)

        start_time = created_at if created_at > start_of_day else start_of_day
        minutes = int((now - start_time).total_seconds() // 60)

        if rec.contract not in contract_minutes:
            contract_minutes[rec.contract] = 0
            contract_boards[rec.contract] = set()

        contract_minutes[rec.contract] += minutes
        contract_boards[rec.contract].add(rec.board)

    # --- FleetSize ТОЛЬКО по нужным контрактам ---
    fleet_rows = FleetSize.query.filter(
        FleetSize.location.in_(TARGET_CONTRACTS),
        FleetSize.zone.contains(selected_park)
    ).all()

    for fleet in fleet_rows:
        contract = fleet.location
        total_cars = fleet.count

        elcar_cars = len(contract_boards.get(contract, []))
        elcar_minutes = contract_minutes.get(contract, 0)

        max_minutes = 1440 * elcar_cars
        elcar_coef_sum = (
            elcar_minutes / max_minutes
            if max_minutes > 0 else 0
        )

        ok_cars = total_cars - elcar_cars
        ok_coef_sum = ok_cars * 1

        total_coef_sum = ok_coef_sum + elcar_coef_sum
        ktg_percent = round((total_coef_sum / total_cars) * 100, 2) if total_cars > 0 else 0

        print(
            f"[КТГ] Парк: {selected_park} | "
            f"Контракт: {contract} | "
            f"Всего ТС: {total_cars} | "
            f"В Elcar1440: {elcar_cars} | "
            f"КТГ: {ktg_percent}%"
        )












@application.route('/itr_7')
def itr_7():
    selected_park = session.get('selected_park')
    current_date = get_russian_date()
    location_map = PARK_TO_LOCATION.get(selected_park, {})
    park_name = PARK_NAME.get(selected_park, {})
    bus_location = location_map.get("bus")
    elbus_location = location_map.get("elbus")

    # Подсчёт общего количества автобусов в парке
    bus_count_total = 0
    if bus_location:
        fleet_size_bus = FleetSize.query.filter_by(location=bus_location).first()
        if not fleet_size_bus:
            flash(f"Нет данных о парке с локацией {bus_location}", 'error')
            bus_count_total = 0
        else:
            bus_count_total = fleet_size_bus.count
    else:
        flash(f"Локация автобусов для парка {selected_park} не найдена", 'error')


    # Подсчёт общего количества электробусов в парке
    elbus_count_total = 0
    total_count = 0
    if elbus_location:
        fleet_size_elbus = FleetSize.query.filter_by(location=elbus_location).first()
        if not fleet_size_elbus:
            flash(f"Нет данных о парке с локацией {elbus_location}", 'error')
        else:
            # Получаем общее количество из FleetSize
            total_count = fleet_size_elbus.count
            # Получаем количество МГТ0084-25 из FleetSize для этого парка
            exclude_fleet = FleetSize.query.filter(
                FleetSize.location == "МГТ0084-25",
                FleetSize.zone.contains(selected_park)
            ).first()
            exclude_count = exclude_fleet.count if exclude_fleet else 0
            elbus_count_total = total_count - exclude_count

    # ИСПОЛЬЗУЕМ МОСКОВСКУЮ ЗОНУ
    tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(tz)

    today_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)

    if now < today_8am:
        start_period = today_8am - timedelta(days=1)
        end_period = today_8am
    else:
        start_period = today_8am
        end_period = today_8am + timedelta(days=1)

    # 1. Ручные данные
    date_rows = Date.query.filter_by(park=selected_park).all()
    manual_data = {item.location: item.count for item in date_rows}

    # 2. Запросы с фильтрами
    base_filter = [Task.park == selected_park, Task.created_at >= start_period, Task.created_at < end_period]

    tasks_created = Task.query.filter(*base_filter).count()

    tasks_completed = Task.query.filter(
        Task.park == selected_park,
        Task.completed_at >= start_period,
        Task.completed_at < end_period
    ).count()

    tasks_rejected = Task.query.filter(
        Task.park == selected_park,
        Task.reject.is_(True),
        Task.completed_at >= start_period,
        Task.completed_at < end_period
    ).count()

    tasks_to = Task.query.filter(
        Task.park == selected_park,
        Task.id_to.isnot(None),
        Task.completed_at >= start_period,
        Task.completed_at < end_period
    ).count()

    in_repair_count = Task.query.filter_by(
        status='in_repair',
        park=selected_park
    ).filter(
        Task.tag.notin_(('no_request', 'to'))
    ).count()

    # ======================================================
    # РАСЧЕТ СО (СЕЗОННОЕ ОБСЛУЖИВАНИЕ)
    # ======================================================

    # 1. СО для автобусов

    so_bus = Car.query.filter(
        Car.location == selected_park,
        Car.seasonal_service == 1
    ).count()

    # Считаем процент от общего количества автобусов (из таблицы Date)
    if bus_count_total > 0:
        so_bus_percentage = (so_bus / bus_count_total) * 100
    else:
        so_bus_percentage = 0

    # 2. СО для электробусов

    so_elbus = Elcar.query.filter(
        Elcar.location == selected_park,
        Elcar.seasonal_service == 1
    ).count()

    # Считаем процент от общего количества электробусов (из таблицы Date)
    if elbus_count_total > 0:
        so_elbus_percentage = (so_elbus / elbus_count_total) * 100
    else:
        so_elbus_percentage = 0
        
    # Данные ЕТО
    eto_passed = (Date.query.filter_by(location="ЕТО Автобусы", park=selected_park).first() or type('obj', (), {'count': 0})()).count
    eto_passed_el = (Date.query.filter_by(location="ЕТО Электробусы", park=selected_park).first() or type('obj', (),{'count': 0})()).count
    eto_percentage = (eto_passed / bus_count_total) * 100 if bus_count_total > 0 else 0
    eto_percentage_el = (eto_passed_el / total_count) * 100 if total_count > 0 else 0    
        
        
        

    return render_template('itr_7.html',
                           date=current_date,
                           manual=manual_data,
                           tasks_created=tasks_created,
                           tasks_completed=tasks_completed,
                           tasks_rejected=tasks_rejected,
                           selected_park=selected_park,
                           PARK_NAME=PARK_NAME,
                           tasks_to=tasks_to,
                           in_repair_count=in_repair_count,
                           # Добавляем переменные для СО
                           so_bus=so_bus,
                           so_elbus=so_elbus,
                           so_bus_percentage=so_bus_percentage,
                           so_elbus_percentage=so_elbus_percentage,
                           bus_count_total=bus_count_total,
                           elbus_count_total=elbus_count_total,
                           eto_passed=eto_passed,
                           eto_passed_el=eto_passed_el,
                           eto_percentage=eto_percentage,
                           eto_percentage_el=eto_percentage_el)




@application.route('/itr_19')
def itr_19():
    try:
        if not current_user.is_authenticated:
            return redirect(url_for('login'))

        # Получаем выбранный парк
        selected_park = session.get('selected_park')
        if not selected_park:
            flash("Парк не выбран")
            return redirect(url_for('index'))

        park_display_name = PARK_NAME.get(selected_park, selected_park)

        # Получаем список парков группы
        db_parks = PARK_GROUPS.get(selected_park, [selected_park])

        # Текущая дата
        today = date.today()
        current_date = get_russian_date()

        # --- Проверка наличия данных report_19 для всех парков группы ---
        missing_parks = []
        for park in db_parks:
            has_data = db.session.query(Report19Table).filter(
                Report19Table.park == park,
                db.func.date(Report19Table.date) == today
            ).first()
            if not has_data:
                missing_parks.append(park)

        if missing_parks:
            flash(
                f"СТО {', '.join(missing_parks)} еще не отправил отчет за {today.strftime('%d.%m.%Y')} в СТО на 19:00")
            return redirect(url_for('reports'))

        # --- Запросы данных для отчетов ---
        report_7_data = sorted(
            db.session.query(Report7Table.col1, Report7Table.description)
            .filter(Report7Table.park.in_(db_parks))
            .all(),
            key=lambda x: x.col1
        )

        report_830_data = sorted(
            db.session.query(Report830Table.col1, Report830Table.description)
            .filter(Report830Table.park.in_(db_parks))
            .all(),
            key=lambda x: x.col1
        )

        report_17_data = sorted(
            db.session.query(Report17Table.col1, Report17Table.description)
            .filter(Report17Table.park.in_(db_parks))
            .all(),
            key=lambda x: x.col1
        )

        report_19_data = sorted(
            db.session.query(Report19Table.col1, Report19Table.description)
            .filter(Report19Table.park.in_(db_parks))
            .all(),
            key=lambda x: x.col1
        )

        return render_template(
            'itr_19.html',
            date=current_date,
            selected_park=selected_park,
            park_display_name=park_display_name,
            report_7_data=report_7_data,
            report_830_data=report_830_data,
            report_17_data=report_17_data,
            report_19_data=report_19_data
        )

    except Exception as e:
        application.logger.error(f"Ошибка в itr_19: {e}")
        flash("Произошла ошибка при генерации отчета")
        return redirect(url_for('reports'))


def save_7():
    with application.app_context():
        today = datetime.now(tz).date()

        try:
            in_work_tasks = db.session.query(
                Task,
                db.func.coalesce(Car.contract, Elcar.contract).label('contract')
            ).outerjoin(
                Car, Task.board_number == Car.bort
            ).outerjoin(
                Elcar, Task.board_number == Elcar.bort
            ).filter(
                Task.status == "in_repair",
                Task.tag.in_(["in_work", "waiting", "ozch"])
            ).all()

            saved_count = 0

            for task, contract in in_work_tasks:

                existing = Report7Table.query.filter(
                    Report7Table.col1 == task.board_number,
                    Report7Table.description == task.description,
                    Report7Table.park == task.park,
                    db.func.date(Report7Table.date) == today
                ).first()

                if existing:
                    continue

                db.session.add(Report7Table(
                    col1=task.board_number,
                    description=task.description,
                    tag=task.tag,
                    park=task.park,
                    contract=contract,
                    date=datetime.now(tz)
                ))

                saved_count += 1

            db.session.commit()
            application.logger.info(f"save_7 completed: saved={saved_count}, date={today}")

            return {
                "status": "success",
                "count": saved_count
            }

        except Exception as e:
            db.session.rollback()
            application.logger.error(f"Error in save_7: {str(e)}")
            return {
                "status": "error",
                "message": str(e)
            }


def save_830():
    with application.app_context():
        today = datetime.now(tz).date()
        # Рабочий день
        if not is_workday_ru(today):
            return {
                "status": "skipped",
                "reason": "holiday_or_weekend"
            }
        try:
            in_work_tasks = db.session.query(
                Task,
                db.func.coalesce(Car.contract, Elcar.contract).label('contract')
            ).outerjoin(
                Car, Task.board_number == Car.bort
            ).outerjoin(
                Elcar, Task.board_number == Elcar.bort
            ).filter(
                Task.status == "in_repair",
                Task.tag.in_(["in_work", "waiting", "ozch"])
            ).all()

            saved_count = 0
            for task, contract in in_work_tasks:

                existing = Report830Table.query.filter(
                    Report830Table.col1 == task.board_number,
                    Report830Table.description == task.description,
                    Report830Table.park == task.park,
                    db.func.date(Report830Table.date) == today
                ).first()

                if existing:
                    continue

                db.session.add(Report830Table(
                    col1=task.board_number,
                    description=task.description,
                    tag=task.tag,
                    park=task.park,
                    contract=contract,
                    date=datetime.now(tz)
                ))
                saved_count += 1

            db.session.commit()
            application.logger.info(f"save_830 completed: saved={saved_count}, date={today}")

            return {"status": "success", "count": saved_count}

        except Exception as e:
            db.session.rollback()
            application.logger.error(f"Error in save_830: {str(e)}")
            return {"status": "error", "message": str(e)}


def save_830_custom(contracts_filter=None):
    with application.app_context():
        today = datetime.now(tz).date()

        # Для выходного дня
        if is_workday_ru(today):
            return {
                "status": "skipped",
                "reason": "holiday_or_weekend"
            }

        try:
            query = db.session.query(
                Task,
                db.func.coalesce(Car.contract, Elcar.contract).label('contract')
            ).outerjoin(Car, Task.board_number == Car.bort
                        ).outerjoin(Elcar, Task.board_number == Elcar.bort
                                    ).filter(
                Task.status == "in_repair",
                Task.tag.in_(['in_work', 'waiting', 'ozch'])
            )

            if contracts_filter:
                query = query.filter(
                    db.func.coalesce(Car.contract, Elcar.contract).in_(contracts_filter)
                )

            in_work_tasks = query.all()
            saved_count = 0

            for task, contract in in_work_tasks:

                # --- ПРОВЕРКА НА ДУБЛИ ---
                existing = Report830Table.query.filter(
                    Report830Table.col1 == task.board_number,
                    Report830Table.description == task.description,
                    Report830Table.park == task.park,
                    db.func.date(Report830Table.date) == today
                ).first()

                if existing:
                    continue

                db.session.add(Report830Table(
                    col1=task.board_number,
                    description=task.description,
                    tag=task.tag,
                    park=task.park,
                    contract=contract,
                    date=datetime.now(tz)
                ))
                saved_count += 1

            db.session.commit()
            application.logger.info(
                f"save_830_custom completed: saved={saved_count}, date={today}, contracts={contracts_filter}"
            )

            return {
                'status': 'success',
                'message': f'Добавлено {saved_count} новых записей',
                'count': saved_count
            }

        except Exception as e:
            db.session.rollback()
            application.logger.error(f"Error in save_830_custom: {str(e)}")
            return {'status': 'error', 'message': str(e)}


def save_17():
    with application.app_context():
        today = datetime.now(tz).date()

        try:
            in_work_tasks = db.session.query(
                Task,
                db.func.coalesce(Car.contract, Elcar.contract).label('contract')
            ).outerjoin(
                Car, Task.board_number == Car.bort
            ).outerjoin(
                Elcar, Task.board_number == Elcar.bort
            ).filter(
                Task.status == "in_repair",
                Task.tag.in_(["in_work", "waiting", "ozch"])
            ).all()

            saved_count = 0

            for task, contract in in_work_tasks:

                existing = Report17Table.query.filter(
                    Report17Table.col1 == task.board_number,
                    Report17Table.description == task.description,
                    Report17Table.park == task.park,
                    db.func.date(Report17Table.date) == today
                ).first()

                if existing:
                    continue

                db.session.add(Report17Table(
                    col1=task.board_number,
                    description=task.description,
                    tag=task.tag,
                    park=task.park,
                    contract=contract,
                    date=datetime.now(tz)
                ))

                saved_count += 1

            db.session.commit()
            application.logger.info(f"save_17 completed: saved={saved_count}, date={today}")

            return {
                "status": "success",
                "count": saved_count
            }

        except Exception as e:
            db.session.rollback()
            application.logger.error(f"Error in save_17: {str(e)}")

            return {
                "status": "error",
                "message": str(e)
            }


def save_19():
    with application.app_context():
        today = datetime.now(tz).date()

        try:
            in_work_tasks = db.session.query(
                Task,
                db.func.coalesce(Car.contract, Elcar.contract).label('contract')
            ).outerjoin(
                Car, Task.board_number == Car.bort
            ).outerjoin(
                Elcar, Task.board_number == Elcar.bort
            ).filter(
                Task.status == "in_repair",
                Task.tag.in_(["in_work", "waiting", "ozch"])
            ).all()

            saved_count = 0

            for task, contract in in_work_tasks:

                existing = Report19Table.query.filter(
                    Report19Table.col1 == task.board_number,
                    Report19Table.description == task.description,
                    Report19Table.park == task.park,
                    db.func.date(Report19Table.date) == today
                ).first()

                if existing:
                    continue

                db.session.add(Report19Table(
                    col1=task.board_number,
                    description=task.description,
                    tag=task.tag,
                    park=task.park,
                    contract=contract,
                    date=datetime.now(tz)
                ))

                saved_count += 1

            db.session.commit()
            application.logger.info(f"save_19 completed: saved={saved_count}, date={today}")

            return {
                "status": "success",
                "count": saved_count
            }

        except Exception as e:
            db.session.rollback()
            application.logger.error(f"Error in save_19: {str(e)}")

            return {
                "status": "error",
                "message": str(e)
            }


def run_incontrol_job_once(job_name, job_func):
    """Общий mutex двух обходов, не привязанный к ORM-транзакции сайта."""
    return run_locked_sync_job(job_name, job_func, 'incontrol_sync')


def run_locked_sync_job(job_name, job_func, lock_scope):
    with application.app_context():
        # GET_LOCK принадлежит физическому соединению, а не транзакции.
        # Не брать его через db.session: remove()/commit() возвращают её
        # соединение в пул и RELEASE_LOCK может попасть на другое соединение.
        try:
            connection = db.engine.connect()
        except SQLAlchemyError as exc:
            application.logger.error('%s: сбой подключения к БД перед запуском (%s)',
                                     job_name, type(exc).__name__)
            return {'status': 'error', 'message': 'Сбой подключения к БД.'}
        with connection as lock_connection:
            lock_name = f'site_scheduler:{lock_scope}'
            acquired = False
            try:
                locked = lock_connection.execute(
                    text('SELECT GET_LOCK(:lock_name, 0)'), {'lock_name': lock_name}
                ).scalar()
                acquired = locked == 1
                lock_connection.commit()
                if locked != 1:
                    application.logger.info('%s: пропущено, другой обход ещё выполняется', job_name)
                    return {'status': 'skipped', 'reason': 'locked'}
                return job_func()
            except Exception as error:
                db.session.rollback()
                application.logger.exception('Сбой задания %s', job_name)
                return {'status': 'error', 'message': str(error)}
            finally:
                db.session.remove()
                if acquired:
                    try:
                        released = lock_connection.execute(
                            text('SELECT RELEASE_LOCK(:lock_name)'), {'lock_name': lock_name}
                        ).scalar()
                        lock_connection.commit()
                        if released != 1:
                            raise RuntimeError('Соединение потеряло блокировку задания')
                    except Exception:
                        # Не возвращать в пул соединение, которое могло
                        # сохранить именованную блокировку после сбоя.
                        lock_connection.invalidate()
                        application.logger.exception('Не удалось снять блокировку задания %s', job_name)


def run_scheduler_job_once(job_name, job_func, *args, **kwargs):
    lock_name = f"site_scheduler:{job_name}"

    with application.app_context():
        locked = db.session.execute(
            text("SELECT GET_LOCK(:lock_name, 0)"),
            {"lock_name": lock_name}
        ).scalar()

        if locked != 1:
            application.logger.warning(f"{job_name}: пропущено, так как этот процесс выполняется другим процессом")
            db.session.remove()
            return {"status": "skipped", "reason": "locked"}

        try:
            result = job_func(*args, **kwargs)
            application.logger.info(f"_____________________")
            return result
        except Exception as e:
            application.logger.exception(f"{job_name}: failed")
            return {"status": "error", "message": str(e)}
        finally:
            try:
                db.session.execute(
                    text("SELECT RELEASE_LOCK(:lock_name)"),
                    {"lock_name": lock_name}
                )
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                application.logger.error(f"{job_name}: failed to release lock {lock_name}: {e}")
            finally:
                db.session.remove()


def scheduled_save_7():
    return run_scheduler_job_once("save_7", save_7)


def scheduled_save_830():
    return run_scheduler_job_once("save_830", save_830)


def scheduled_save_830_contracts_8():
    return run_scheduler_job_once("save_830_contracts_8", save_830_custom, ["216644", "222150"])


def scheduled_save_830_contracts_10():
    return run_scheduler_job_once(
        "save_830_contracts_10",
        save_830_custom,
        ["МГТ0105-21", "МГТ0107-21", "МГТ234953-23", "МГТ0019-25"]
    )


def scheduled_save_17():
    return run_scheduler_job_once("save_17", save_17)


def scheduled_save_19():
    return run_scheduler_job_once("save_19", save_19)


def has_report_rows(report_model, report_date):
    return db.session.query(report_model.id).filter(
        db.func.date(report_model.date) == report_date
    ).first() is not None


def time_in_window(now_time, start_hour, start_minute, end_hour, end_minute):
    return time(start_hour, start_minute) <= now_time <= time(end_hour, end_minute)


def run_startup_report_catchups():
    with application.app_context():
        now = datetime.now(tz)
        today = now.date()
        now_time = now.time()

        if time_in_window(now_time, 7, 15, 7, 45):
            scheduled_save_7()

        if is_workday_ru(today) and time_in_window(now_time, 8, 30, 9, 0):
            scheduled_save_830()

        if time_in_window(now_time, 17, 0, 17, 30):
            scheduled_save_17()

        if time_in_window(now_time, 19, 0, 19, 30) and not has_report_rows(Report19Table, today):
            application.logger.warning("startup catchup: report_19 is empty after 19:00, running save_19")
            scheduled_save_19()


@application.route('/print_list')
def print_list():
    return render_template('print_list.html')


@application.route('/print_task/<int:task_id>', methods=['GET', 'POST'])
def print_task(task_id):
    try:
        if request.method != 'POST':
            return redirect(url_for('index'))

        task = Task.query.get_or_404(task_id)
        task.print = True
        db.session.commit()
        car = Car.query.filter_by(bort=task.board_number).first()
        elcar = Elcar.query.filter_by(bort=task.board_number).first()
        selected_park = session.get('selected_park')
        park_name = PARK_NAME.get(selected_park, selected_park if selected_park else 'Не указан')
        vehicle = car if car else elcar
        contract = car.contract if car else elcar.contract if elcar else None

        if request.method != 'POST':
            return redirect(url_for('index'))

        template_path = "template.docx"
        if not os.path.exists(template_path):
            flash('Шаблон документа не найден', 'error')
            application.logger.error(f'Файл шаблона не найден: {template_path}')
            return redirect(url_for('complete_task', task_id=task_id))

        doc = DocxTemplate(template_path)

        description_lines = task.description.split('\n')[:5] if task.description else []
        description_lines.extend([''] * (5 - len(description_lines)))

        completed_lines = task.completed_work.split('\n')[:5] if task.completed_work else []
        completed_lines.extend([''] * (5 - len(completed_lines)))

        mileage = task.mileage if hasattr(task, 'mileage') and task.mileage is not None else \
            vehicle.mileage if vehicle and hasattr(vehicle, 'mileage') else None

        context = {
            'contract': contract,
            'park_name': park_name,
            'task_id': task.id,
            'board_number': task.board_number,
            'created_at': f'«{task.work_in.day}» {MONTH_NAMES[task.work_in.month]} {task.work_in.year}',
            'h': task.work_in.hour,
            'm': task.work_in.minute,
            'date_go': task.work_in.strftime('%d.%m.%y'),
            'time_go': (task.work_in + timedelta(minutes=7)).strftime('%H:%M'),
            'date_end': task.completed_at.strftime('%d.%m.%y') if task.completed_at else '',
            'time_end': task.completed_at.strftime('%H:%M') if task.completed_at else '',
            'mileage': mileage if mileage is not None else 'Не указан',
            'type_ts': vehicle.type_ts if vehicle else 'Автобус',
            'completed_at': task.completed_at.strftime('%d.%m.%Y %H:%M') if task.completed_at else '',
            'status': task.status,
            'master': ' '.join((current_user.full_name or '').split()) if current_user.is_authenticated else 'Не указан',
            'description_1': description_lines[0],
            'description_2': description_lines[1],
            'description_3': description_lines[2],
            'description_4': description_lines[3],
            'description_5': description_lines[4],
            'completed_work_1': completed_lines[0],
            'completed_work_2': completed_lines[1],
            'completed_work_3': completed_lines[2],
            'completed_work_4': completed_lines[3],
            'completed_work_5': completed_lines[4],
            'vehicle': {
                'bort': vehicle.bort if vehicle else 'Не указан',
                'gos_number': vehicle.gos_number if vehicle else 'Не указан',
                'year': vehicle.year if vehicle else 'Не указан',
                'vin': vehicle.vin if vehicle else 'Не указан',
                'location': vehicle.location if vehicle else 'Не указан',
                'contract': vehicle.contract if vehicle else 'Не указан',
                'mileage': getattr(vehicle, 'mileage', 'Не указан') if vehicle else 'Не указан'
            } if vehicle else None,
        }

        file_stream = io.BytesIO()
        doc.render(context)
        doc.save(file_stream)
        file_stream.seek(0)

        filename = f"Заявка_{task.id}_{task.board_number}.docx"
        response = make_response(send_file(
            file_stream,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        ))
        response.headers['Refresh'] = '3; url=' + url_for('index')
        return response

    except Exception as e:
        flash('Произошла непредвиденная ошибка', 'error')
        application.logger.error(f'Критическая ошибка в print_task: {str(e)}')
        return redirect(url_for('index'))


# ТОЛЬКО ДЛЯ ABOUT!!!!

@application.route('/print_completed_task/<int:task_id>', methods=['GET'])
def print_completed_task(task_id):
    task = Task.query.get_or_404(task_id)
    car = Car.query.filter_by(bort=task.board_number).first()
    elcar = Elcar.query.filter_by(bort=task.board_number).first()
    vehicle = car if car else elcar
    selected_park = session.get('selected_park')
    park_name = PARK_NAME.get(selected_park, selected_park if selected_park else 'Не указан')
    contract = car.contract if car else elcar.contract if elcar else None

    description_lines = task.description.split('\n') if task.description else []
    while len(description_lines) < 5:
        description_lines.append('')

    completed_lines = task.completed_work.split('\n') if task.completed_work else []
    while len(completed_lines) < 5:
        completed_lines.append('')

    doc = DocxTemplate("template.docx")

    # Безопасное получение пробега
    mileage = None
    if hasattr(task, 'mileage') and task.mileage is not None:
        mileage = task.mileage
    elif vehicle and hasattr(vehicle, 'mileage'):
        mileage = vehicle.mileage

    context = {
        'contract': contract,
        'park_name': park_name,
        'task_id': task.id,
        'board_number': task.board_number,
        'created_at': f'«{task.work_in.day}» {MONTH_NAMES[task.work_in.month]} {task.work_in.year}',
        'h': task.work_in.hour,
        'm': task.work_in.minute,
        "date_go": task.work_in.strftime('%d.%m.%y'),
        "time_go": task.work_in.strftime('%H:%M'),
        "date_end": task.completed_at.strftime('%d.%m.%y') if task.completed_at else '',
        "time_end": task.completed_at.strftime('%H:%M') if task.completed_at else '',
        'mileage': mileage if mileage is not None else 'Не указан',
        'type_ts': vehicle.type_ts if vehicle else 'Автобус',
        'completed_at': task.completed_at.strftime('%d.%m.%Y %H:%M') if task.completed_at else '',
        'status': task.status,
        'master': ' '.join((task.author_closed or '').split()),
        'description_1': description_lines[0],
        'description_2': description_lines[1],
        'description_3': description_lines[2],
        'description_4': description_lines[3],
        'description_5': description_lines[4],
        'completed_work_1': completed_lines[0],
        'completed_work_2': completed_lines[1],
        'completed_work_3': completed_lines[2],
        'completed_work_4': completed_lines[3],
        'completed_work_5': completed_lines[4],
        'vehicle': {
            'bort': vehicle.bort if vehicle else 'Не указан',
            'gos_number': vehicle.gos_number if vehicle else 'Не указан',
            'year': vehicle.year if vehicle else 'Не указан',
            'vin': vehicle.vin if vehicle else 'Не указан',
            'location': vehicle.location if vehicle else 'Не указан',
            'contract': vehicle.contract if vehicle else 'Не указан',
            'mileage': vehicle.mileage if vehicle and hasattr(vehicle, 'mileage') else 'Не указан'
        } if vehicle else None,
    }

    doc.render(context)
    file_stream = io.BytesIO()
    doc.save(file_stream)
    file_stream.seek(0)

    filename = f"Заявка_{task.id}_{task.board_number}.docx"

    return send_file(
        file_stream,
        as_attachment=True,
        download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )


# -------------ЕТО--------------------------------------------------------------------------------------------

@application.route('/eto', methods=['GET', 'POST'])
def eto():
    selected_park = session.get('selected_park')
    if not selected_park:
        flash('Пожалуйста, выберите парк в шапке.', 'warning')
        return redirect(url_for('some_default_page'))  # или куда нужно

    if request.method == 'POST':
        if request.form.get('copy_mileage_to_yesterday'):
            try:
                cars = Car.query.filter_by(location=selected_park).all()
                for car in cars:
                    car.mileage_yesterday = car.mileage
                db.session.commit()
                flash('Пробеги успешно скопированы в «Пробег за вчера»', 'success')
            except Exception as e:
                db.session.rollback()
                flash(f'Ошибка при копировании пробега: {e}', 'error')
            return redirect(url_for('eto'))

        else:
            # Сохранение пробега
            try:
                for key, value in request.form.items():
                    if key.startswith('mileage_'):
                        try:
                            car_id = int(key.split('_')[1])
                            mileage = int(value) if value.isdigit() else None
                            car = Car.query.get(car_id)
                            if car and mileage is not None and car.location == selected_park:
                                car.mileage = mileage
                        except (ValueError, IndexError):
                            pass
                db.session.commit()
                flash('Пробег успешно сохранён', 'success')
            except Exception as e:
                db.session.rollback()
                flash(f'Ошибка при сохранении пробега: {e}', 'error')
            return redirect(url_for('eto'))

    # GET — выводим машины только из выбранного парка
    cars = Car.query.filter_by(location=selected_park).order_by(Car.bort).all()

    for car in cars:
        mileage = car.mileage or 0
        mileage_yesterday = getattr(car, 'mileage_yesterday', 0) or 0
        car.mileage_diff = mileage - mileage_yesterday

    return render_template('eto.html', cars=cars, selected_park=selected_park)


# -----------для сохранения пробега автоматом---------------------
def copy_mileage():
    try:
        cars = Car.query.filter_by(location="14 парк").all()
        for car in cars:
            car.mileage_yesterday = car.mileage
        db.session.commit()
        return True, 'Пробеги успешно скопированы в «Пробег за вчера»'
    except Exception as e:
        db.session.rollback()
        return False, f'Ошибка при копировании пробега: {e}'


def scheduled_copy_mileage():
    with application.app_context():
        success, message = copy_mileage()
        if success:
            application.logger.info(message)
        else:
            application.logger.error(message)


# --------------------------------------------------------------------


@application.route('/update_mileage', methods=['POST'])
def update_mileage():
    try:
        data = request.get_json()
        car_id = int(data.get('car_id'))
        mileage = int(data.get('mileage'))

        with db.session.no_autoflush:
            car = Car.query.get(car_id)
            if not car:
                return jsonify({'status': 'error', 'message': 'Машина не найдена'}), 404

            car.mileage = mileage
            db.session.commit()
            return jsonify({'status': 'success'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@application.route('/eto/export_xlsx')
def eto_export_xlsx():
    cars = Car.query.filter_by(location="14 парк").order_by(Car.bort).all()
    today = date.today()

    # Создаём книгу и лист
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Пробеги"

    # Заголовки
    ws['A1'] = 'дата начала'
    ws['B1'] = 'дата окончания'
    ws['C1'] = 'пробег'
    ws['D1'] = 'гаражный номер'

    # Текущая дата
    today = datetime.now().date()
    date_start = datetime.combine(today, time(0, 5))
    date_end = datetime.combine(today, time(3, 5))

    # Формат даты dd.mm.yyyy HH:MM
    date_format = 'DD.MM.YYYY HH:mm'

    # Заполняем строки
    for idx, car in enumerate(cars, start=2):
        ws[f'A{idx}'] = date_start
        ws[f'B{idx}'] = date_end
        ws[f'C{idx}'] = car.mileage or 0
        ws[f'D{idx}'] = car.bort

        ws[f'A{idx}'].number_format = date_format
        ws[f'B{idx}'].number_format = date_format

    # Сохраняем в байтовый поток
    output = BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"probeg_{today.strftime('%Y%m%d')}.xlsx"

    return send_file(output,
                     download_name=filename,
                     as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# -----------ИСТОРИЯ ПРОБЕГОВ ЕТО ----------------------------------------------------------
@application.route('/eto/save_history')
def eto_save_history():
    try:
        save_history()
        flash("Пробеги и суточный пробег сохранены.", "success")
    except Exception as e:
        flash(f"Ошибка при сохранении истории пробега: {e}", "error")
    return redirect(url_for('eto'))


def save_history():
    cars = Car.query.filter_by(location="14 парк").order_by(Car.bort).all()
    today = date.today()
    yesterday = today - timedelta(days=1)

    for car in cars:
        mileage_today = car.mileage or 0
        hist_yesterday = History.query.filter_by(bort=car.bort, record_date=yesterday).first()
        mileage_yesterday = hist_yesterday.mileage if hist_yesterday else None

        if mileage_yesterday is not None:
            daily_mileage = mileage_today - mileage_yesterday
            if daily_mileage < 0:
                daily_mileage = 0
        else:
            daily_mileage = None

        existing = History.query.filter_by(bort=car.bort, record_date=today).first()
        if existing:
            existing.mileage = mileage_today
            existing.daily_mileage = daily_mileage
        else:
            new_hist = History(bort=car.bort, mileage=mileage_today, daily_mileage=daily_mileage, record_date=today)
            db.session.add(new_hist)

    db.session.commit()


def scheduled_save_history():
    with application.app_context():
        try:
            save_history()
            application.logger.info("История пробега успешно сохранена планировщиком.")
        except Exception as e:
            application.logger.error(f"Ошибка при сохранении истории пробега планировщиком: {e}")


@application.route('/get_history/<bort>')
def get_history(bort):
    records = History.query.filter_by(bort=bort).order_by(History.record_date.desc()).all()
    history = [{
        'record_date': r.record_date.strftime('%Y-%m-%d'),
        'mileage': r.mileage,
        'daily_mileage': r.daily_mileage
    } for r in records]
    return jsonify(history=history)


# -------------ВЫБОР ПАРКА-------------------------------------------------------------------------------------------@
@application.route('/set_park', methods=['POST'])
def set_park():
    if not current_user.is_authenticated:
        return jsonify({'success': False, 'message': 'Пользователь не авторизован'}), 401

    data = request.get_json()
    requested_park = data.get('park')
    available_parks = current_user.allowed_parks or []

    if requested_park in available_parks:
        session['available_parks'] = available_parks
        session['selected_park'] = requested_park
        application.logger.info(f"User {current_user.username} selected park: {requested_park}")
        return jsonify({'success': True})

    return jsonify({'success': False, 'message': f'Доступ к парку {requested_park} запрещен'}), 403


# -------------ВЫГРУЗКА СПИСКА В ЭКСЕЛЬ-------------------------------------------------------------------------------------------
@application.route('/export_tasks')
def export_tasks():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))

    selected_park = session.get('selected_park')
    available_parks = session.get('available_parks', [])

    # Проверка выбранного парка
    if not selected_park or selected_park not in available_parks:
        flash(f'Выберите один из разрешенных парков: {", ".join(available_parks)}', 'error')
        return redirect(url_for('index'))

    moscow_tz = pytz.timezone('Europe/Moscow')

    def format_repair_downtime(start_dt, end_dt):
        if not start_dt:
            return ''

        if start_dt.tzinfo is None or start_dt.tzinfo.utcoffset(start_dt) is None:
            start_dt = moscow_tz.localize(start_dt)
        else:
            start_dt = start_dt.astimezone(moscow_tz)

        delta = end_dt - start_dt
        total_seconds = max(0, int(delta.total_seconds()))

        if total_seconds < 24 * 60 * 60:
            hours = max(1, (total_seconds + 3599) // 3600)
            return f'{hours} ч'

        days = total_seconds / (24 * 60 * 60)
        days_text = f'{days:.1f}'.rstrip('0').rstrip('.').replace('.', ',')
        return f'{days_text} дн.'

    # Текущая дата и время в Мск
    now = datetime.now(moscow_tz)
    today_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now < today_8am:
        today_8am = today_8am - timedelta(days=1)  # Если сейчас раньше 8:00 — берём вчерашние с 8 утра

    # 1. Задачи в ремонте (первый лист)
    in_repair_tasks = Task.query.filter_by(
        status='in_repair',
        park=selected_park
    ).order_by(Task.created_at.asc()).all()

    # 2. Готовые машины с 8:00 утра сегодняшнего дня (второй лист)
    completed_tasks = Task.query.filter(
        Task.status == 'completed',
        Task.park == selected_park,
        Task.completed_at >= today_8am
    ).order_by(Task.completed_at.asc()).all()

    open_tasks_count = Task.query.filter(
        and_(
            Task.status.in_(['in_repair', 'completed']),
            Task.created_at >= today_8am,
        )
    )
    if selected_park and selected_park != 'all':
        open_tasks_count = open_tasks_count.filter(Task.park == selected_park)
    open_tasks_count = open_tasks_count.count()

    closed_tasks_query = Task.query.filter(
        and_(
            Task.status.in_(['completed']),
            Task.completed_at >= today_8am,
        )
    )
    if selected_park and selected_park != 'all':
        closed_tasks_query = closed_tasks_query.filter(Task.park == selected_park)
    closed_tasks_count = closed_tasks_query.count()

    # Создаём книгу Excel
    wb = openpyxl.Workbook()

    # ——— ЛИСТ 1: Задачи в ремонте ———
    ws1 = wb.active
    ws1.title = "Задачи в ремонте"

    # Настройки страницы
    ws1.page_setup.orientation = ws1.ORIENTATION_PORTRAIT
    ws1.page_setup.paperSize = ws1.PAPERSIZE_A4
    ws1.page_margins.left = ws1.page_margins.right = 0.5
    ws1.page_margins.top = ws1.page_margins.bottom = 0.75
    ws1.page_margins.header = ws1.page_margins.footer = 0.3

    # Заголовок
    ws1['C1'] = f"Выгрузка заявок ({selected_park}) на {now.strftime('%d.%m.%Y %H:%M')}"
    ws1['C1'].font = openpyxl.styles.Font(bold=True, size=12)

    # Шапка таблицы
    headers1 = ['Номер борта', 'Описание', 'Комментарий', 'Статус', "Дата заявки", "Контракт", "Простой"]
    for col, header in enumerate(headers1, start=1):
        cell = ws1.cell(row=2, column=col, value=header)
        cell.font = openpyxl.styles.Font(bold=True)
        cell.alignment = openpyxl.styles.Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = openpyxl.styles.Border(left=openpyxl.styles.Side(style='thin'),
                                             right=openpyxl.styles.Side(style='thin'),
                                             top=openpyxl.styles.Side(style='thin'),
                                             bottom=openpyxl.styles.Side(style='thin'))

    # Данные
    for row, task in enumerate(in_repair_tasks, start=3):
        ws1.cell(row=row, column=1, value=task.board_number)
        ws1.cell(row=row, column=2, value=task.description)
        ws1.cell(row=row, column=3, value=task.comment)
        ws1.cell(row=row, column=4, value=TAG_CHOICES.get(task.tag, task.tag))
        ws1.cell(row=row, column=5, value=task.created_at)
        ws1.cell(row=row, column=6, value=task.contract)
        ws1.cell(row=row, column=7, value=format_repair_downtime(task.created_at, now))

        for col in range(1, 8):
            cell = ws1.cell(row=row, column=col)
            cell.border = openpyxl.styles.Border(left=openpyxl.styles.Side(style='thin'),
                                                 right=openpyxl.styles.Side(style='thin'),
                                                 top=openpyxl.styles.Side(style='thin'),
                                                 bottom=openpyxl.styles.Side(style='thin'))
            cell.alignment = openpyxl.styles.Alignment(wrap_text=True, vertical='top')

    # Ширина столбцов и автонастройка (оставляем как было)
    ws1.column_dimensions['A'].width = 12
    ws1.column_dimensions['B'].width = ws1.column_dimensions['C'].width = 20
    ws1.column_dimensions['D'].width = 12
    ws1.column_dimensions['E'].width = 17
    ws1.column_dimensions['F'].width = 17
    ws1.column_dimensions['G'].width = 12

    # (Твой код автоподбора ширины и высоты строк можно оставить, он работает)

    # Область печати и масштабирование
    last_row1 = len(in_repair_tasks) + 2
    ws1.print_area = f'A1:G{last_row1}'
    ws1.print_title_rows = '1:2'
    ws1.page_setup.fitToWidth = 1
    ws1.page_setup.fitToHeight = 0

    # ——— ЛИСТ 2: Готовые машины ———
    ws2 = wb.create_sheet(title="Готовые машины")

    # Те же настройки страницы
    ws2.page_setup.orientation = ws2.ORIENTATION_PORTRAIT
    ws2.page_setup.paperSize = ws2.PAPERSIZE_A4
    ws2.page_margins.left = ws2.page_margins.right = 0.5
    ws2.page_margins.top = ws2.page_margins.bottom = 0.75

    # Заголовок второго листа
    ws2[
        'A1'] = f"Готовые машины с 8:00 {today_8am.strftime('%d.%m.%Y')} по {now.strftime('%d.%m.%Y %H:%M')} ({selected_park})"
    ws2.merge_cells('A1:E1')
    ws2['A1'].font = openpyxl.styles.Font(bold=True, size=12)
    ws2['A1'].alignment = openpyxl.styles.Alignment(horizontal='center')
    # Статистика
    ws2['A2'] = 'Открытые заявки'
    ws2['B2'] = open_tasks_count

    ws2['A3'] = 'Закрытые заявки'
    ws2['B3'] = closed_tasks_count

    for row in (2, 3):
        ws2[f'A{row}'].font = openpyxl.styles.Font(bold=True)
        ws2[f'A{row}'].alignment = openpyxl.styles.Alignment(horizontal='left')
        ws2[f'B{row}'].alignment = openpyxl.styles.Alignment(horizontal='center')

    # Шапка таблицы
    TABLE_HEADER_ROW = 5
    DATA_START_ROW = 6

    headers2 = ['Номер борта', 'Выполненные работы', 'Время завершения', 'Парк']
    for col, header in enumerate(headers2, start=1):
        cell = ws2.cell(row=TABLE_HEADER_ROW, column=col, value=header)
        cell.font = openpyxl.styles.Font(bold=True)
        cell.alignment = openpyxl.styles.Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = openpyxl.styles.Border(
            left=openpyxl.styles.Side(style='thin'),
            right=openpyxl.styles.Side(style='thin'),
            top=openpyxl.styles.Side(style='thin'),
            bottom=openpyxl.styles.Side(style='thin'),
        )

    # Данные готовых машин
    for row, task in enumerate(completed_tasks, start=DATA_START_ROW):
        ws2.cell(row=row, column=1, value=task.board_number)
        ws2.cell(row=row, column=2, value=task.completed_work or '')  # предполагаю, что есть поле completed_work
        ws2.cell(row=row, column=3, value=task.completed_at.strftime('%d.%m.%Y %H:%M') if task.completed_at else '')
        ws2.cell(row=row, column=4, value=task.park)

        for col in range(1, 5):
            cell = ws2.cell(row=row, column=col)
            cell.border = openpyxl.styles.Border(left=openpyxl.styles.Side(style='thin'),
                                                 right=openpyxl.styles.Side(style='thin'),
                                                 top=openpyxl.styles.Side(style='thin'),
                                                 bottom=openpyxl.styles.Side(style='thin'))
            cell.alignment = openpyxl.styles.Alignment(wrap_text=True, vertical='top')

    # Ширина столбцов второго листа
    ws2.column_dimensions['A'].width = 14
    ws2.column_dimensions['B'].width = 35
    ws2.column_dimensions['C'].width = 18
    ws2.column_dimensions['D'].width = 12

    # Автоподбор ширины для колонки B (Выполненные работы)
    for cell in ws2['B'][DATA_START_ROW - 1:]:
        if cell.value:
            length = max(len(line) for line in str(cell.value).split('\n'))
            adjusted_width = min(max((length + 2) * 1.2, 20), 40)
            if ws2.column_dimensions['B'].width < adjusted_width:
                ws2.column_dimensions['B'].width = adjusted_width

    # Автоподбор высоты строк (по колонке B)
    for row in ws2.iter_rows(min_row=4):
        max_lines = 1
        for cell in row:
            if cell.value and cell.column_letter == 'B':
                lines = str(cell.value).split('\n')
                for line in lines:
                    if len(line) > ws2.column_dimensions['B'].width * 1.8:
                        max_lines += int(len(line) / (ws2.column_dimensions['B'].width * 1.8))
                max_lines = max(max_lines, len(lines))
        ws2.row_dimensions[row[0].row].height = max(15, 15 * max_lines)

    # Область печати второго листа
    last_row2 = len(completed_tasks) + DATA_START_ROW - 1
    if last_row2 >= 4:
        ws2.print_area = f'A1:E{last_row2}'
        ws2.print_title_rows = f'{TABLE_HEADER_ROW}:{TABLE_HEADER_ROW}'

    ws2.page_setup.fitToWidth = 1
    ws2.page_setup.fitToHeight = 0

    # Сохранение в поток
    output = BytesIO()
    wb.save(output)
    output.seek(0)

    # Имя файла
    today_str = now.strftime('%Y%m%d')
    filename = f"tasks_{selected_park}_{today_str}.xlsx"

    # Логирование
    application.logger.info(
        f"User: {current_user.username} | "
        f"Export: park={selected_park}, in_repair={len(in_repair_tasks)}, completed_since_8am={len(completed_tasks)}"
    )

    return send_file(
        output,
        download_name=filename,
        as_attachment=True,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )



# ---------------все задачи------------------------------------------

def normalize_datetime(value):
    """Преобразовать дату в московское время без tzinfo для Excel."""
    if not value:
        return None

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None

        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value

    if isinstance(value, datetime):
        moscow_tz = ZoneInfo("Europe/Moscow")

        if value.tzinfo is not None:
            return value.astimezone(moscow_tz).replace(tzinfo=None)

        # Дата без часового пояса считается московским временем
        return value

    return value


@application.route('/export_all_tasks', methods=['GET', 'POST'])
def export_all_tasks():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))

    selected_park = session.get('selected_park')
    available_parks = session.get('available_parks', [])

    if not selected_park or selected_park not in available_parks:
        flash(f'Выберите один из разрешенных парков: {", ".join(available_parks)}', 'error')
        return redirect(url_for('index'))

    # Получаем даты из формы
    start_date_str = request.form.get('export_date_start')
    end_date_str = request.form.get('export_date_end')
    start_hour = int(request.form.get('export_hour_start', 0))
    end_hour = int(request.form.get('export_hour_end', 23))
    filter_type = request.form.get('filter_type', 'created')  # Получаем тип фильтра

    # Базовый запрос
    query = Task.query.filter_by(park=selected_park)

    moscow_tz = pytz.timezone('Europe/Moscow')

    # Фильтрация по периоду
    if start_date_str and end_date_str:
        try:
            moscow_tz = pytz.timezone('Europe/Moscow')

            # Парсим только дату
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

            # Собираем datetime с учетом выбранного часа
            # Начало: указанный день, указанный час, 00 минут, 00 секунд
            start_datetime = moscow_tz.localize(datetime.combine(start_date, time(start_hour, 0, 0)))

            # Конец: указанный день, указанный час, 59 минут, 59 секунд
            base_end_dt = moscow_tz.localize(datetime.combine(end_date, time(end_hour, 0, 0)))
            end_datetime = base_end_dt - timedelta(seconds=1)

            if filter_type == 'completed':
                query = query.filter(Task.completed_at >= start_datetime, Task.completed_at <= end_datetime)
            else:
                query = query.filter(Task.created_at >= start_datetime, Task.created_at <= end_datetime)

        except ValueError:
            flash('Неверный формат даты или времени', 'error')
            return redirect(url_for('index'))

    tasks = query.order_by(Task.created_at.asc()).all()

    if not tasks:
        flash('Нет задач для выгрузки за выбранный период', 'info')
        return redirect(url_for('index'))

    wb = Workbook()
    ws = wb.active
    ws.title = "Все заявки"

    # Настройки страницы
    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.page_margins.top = ws.page_margins.bottom = 0.7

    now = datetime.now(moscow_tz)

    # Заголовок
    period_text = f" (с {start_date_str} по {end_date_str})" if start_date_str and end_date_str else ""
    title = f"ПОЛНАЯ ВЫГРУЗКА ЗАЯВОК — {selected_park.upper()}{period_text} — {now.strftime('%d.%m.%Y %H:%M')}"
    ws['A1'] = title
    ws['A1'].font = Font(bold=True, size=14)
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
    ws.merge_cells('A1:P1')

    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )

    # === Колонки (теперь даты передаются как объекты для работы фильтров Excel) ===
    columns = [
        ("Дата создания", lambda t: t.created_at.astimezone(moscow_tz).replace(tzinfo=None) if t.created_at else None),
        ("Бортовой №", lambda t: t.board_number),
        ("Vin", lambda t: t.vin),
        ("Парк", lambda t: t.park or ''),
        ("Контракт", lambda t: t.contract or ''),
        ("Тип ТС", lambda t: t.type_ts or ''),
        ("Описание", lambda t: t.description or ''),
        ("Комментарий", lambda t: t.comment or ''),
        ("Тег", lambda t: TAG_CHOICES.get(t.tag, t.tag) if 'TAG_CHOICES' in globals() else (t.tag or '')),
        ("Статус",
         lambda t: STATUS_CHOICES.get(t.status, t.status) if 'STATUS_CHOICES' in globals() else (t.status or '')),
        ("Выполненные работы", lambda t: t.completed_work or ''),
        ("Отклонено", lambda t: t.reject or ''),
        ("Кем открыта", lambda t: t.author_open or ''),
        ("Кем закрыта", lambda t: t.author_closed or ''),
        ("Дата начала", lambda t: normalize_datetime(t.work_in)),
        ("Дата завершения", lambda t: normalize_datetime(t.completed_at)),
        ("Печать", lambda t: t.print or ''),
    ]

    # Заголовки (строка 3)
    for col_num, (header, _) in enumerate(columns, start=1):
        cell = ws.cell(row=3, column=col_num, value=header)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = thin_border

    # Данные (строка 4+)
    for row_num, task in enumerate(tasks, start=4):
        for col_num, (_, getter) in enumerate(columns, start=1):
            value = getter(task)
            cell = ws.cell(row=row_num, column=col_num, value=value)

            # Форматирование дат
            if isinstance(value, datetime):
                cell.number_format = 'DD.MM.YYYY HH:MM'

            cell.alignment = Alignment(wrap_text=True, vertical='top')
            cell.border = thin_border

    # Включаем автофильтр
    from openpyxl.utils import get_column_letter
    ws.auto_filter.ref = f"A3:{get_column_letter(len(columns))}{len(tasks) + 3}"

    # Автоподбор ширины (с учетом объектов datetime)
    for col_cells in ws.columns:
        real_cells = [cell for cell in col_cells if
                      cell.row >= 3 and not isinstance(cell, openpyxl.cell.cell.MergedCell)]
        if not real_cells: continue

        column_letter = real_cells[0].column_letter
        max_length = 0
        for cell in real_cells:
            if cell.value:
                # Если дата — берем стандартную длину строки даты
                val_len = 16 if isinstance(cell.value, datetime) else len(str(cell.value))
                max_length = max(max_length, val_len)

        ws.column_dimensions[column_letter].width = min(max_length + 2, 50)

    # Заморозка и сохранение
    ws.freeze_panes = 'A4'
    output = BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"full_tasks_{selected_park}_{now.strftime('%Y%m%d_%H%M')}.xlsx"

    # Исправленное логирование (без Unicode стрелки)
    application.logger.info(f"[EXPORT] User: {current_user.username} | Park: {selected_park} | Tasks: {len(tasks)}")

    return send_file(output, download_name=filename, as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')



# ---------------СЕЗОННОЕ ОБСЛУЖИВАНИЕ------------------------------------------

@application.route('/so')
def so():
    selected_park = session.get('selected_park')
    location_map = PARK_TO_LOCATION.get(selected_park)
    if not location_map:
        return redirect(url_for('index'))

    buses = Car.query.filter_by(location=selected_park).order_by(Car.bort).all()
    elbuses = Elcar.query.filter_by(location=selected_park).order_by(Elcar.bort).all()

    borts = [bus.bort for bus in buses] + [elbus.bort for elbus in elbuses]

    statuses = ServiceStatus.query.filter(
        ServiceStatus.park == selected_park,
        ServiceStatus.bort.in_(borts)
    ).all() if borts else []

    status_map = {
        (status.bort, status.type_ts): status.status
        for status in statuses
    }

    vehicles = []

    for bus in buses:
        vehicles.append({
            'bort': bus.bort,
            'type_ts': 'Автобус',
            'status': status_map.get((bus.bort, 'Автобус'), 'не пройдено')
        })

    for elbus in elbuses:
        vehicles.append({
            'bort': elbus.bort,
            'type_ts': 'Электробус',
            'status': status_map.get((elbus.bort, 'Электробус'), 'не пройдено')
        })

    return render_template('so.html', vehicles=vehicles, selected_park=selected_park)



@application.route('/download_service_status')
def download_service_status():
    selected_park = session.get('selected_park')
    if not selected_park:
        return jsonify({'error': 'Парк не выбран'}), 400

    # Query all ServiceStatus records for the selected park
    service_statuses = ServiceStatus.query.filter_by(park=selected_park).all()

    # Create a new Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = 'ServiceStatus'

    # Define headers
    headers = ['ID', 'Номер борта', 'Парк', 'Статус', 'Тип ТС']
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num)
        cell.value = header
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal='center')

    # Populate data
    for row_num, status in enumerate(service_statuses, 2):
        ws.cell(row=row_num, column=1).value = status.id
        ws.cell(row=row_num, column=2).value = status.bort
        ws.cell(row=row_num, column=3).value = status.park
        ws.cell(row=row_num, column=4).value = status.status
        ws.cell(row=row_num, column=5).value = status.type_ts

    # Auto-adjust column widths
    for col in ws.columns:
        max_length = 0
        column = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = (max_length + 2) * 1.2
        ws.column_dimensions[column].width = adjusted_width

    # Save to BytesIO
    output = BytesIO()
    wb.save(output)
    output.seek(0)

    # Generate filename with timestamp
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'service_status_{selected_park}_{timestamp}.xlsx'

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename
    )


@application.route('/update_service_status', methods=['POST'])
def update_service_status():
    if not current_user.is_authenticated:
        return jsonify({'error': 'Требуется авторизация'}), 401

    data = request.get_json()
    bort = data.get('bort')
    status = data.get('status')
    type_ts = data.get('type_ts')
    selected_park = session.get('selected_park')

    if not bort or not status or not type_ts or not selected_park:
        return jsonify({'error': 'Недостаточно данных'}), 400

    if status not in ['пройдено', 'не пройдено']:
        return jsonify({'error': 'Недопустимый статус'}), 400

    try:
        service_status = ServiceStatus.query.filter_by(bort=bort, park=selected_park, type_ts=type_ts).first()
        if not service_status:
            service_status = ServiceStatus(bort=bort, park=selected_park, status=status, type_ts=type_ts)
            db.session.add(service_status)
        else:
            service_status.status = status
        db.session.commit()
        application.logger.info(
            f"User: {current_user.username}, Updated SO status: bort={bort}, park={selected_park}, type_ts={type_ts}, status={status}")
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        application.logger.error(f"User: {current_user.username}, Error updating SO status: {str(e)}")
        return jsonify({'error': str(e)}), 500


# -------------Эвакуация--------------------------------------------------------
@application.route('/evacuation')
def evacuation():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))

    selected_park = session.get('selected_park')
    available_parks = session.get('available_parks', [])

    # Проверяем, что выбранный парк разрешен (кроме пользователя vasilev)
    if current_user.username != 'vasilev' and selected_park and selected_park not in available_parks:
        flash(f'Доступ к парку {selected_park} запрещен', 'error')
        session.pop('selected_park', None)
        selected_park = available_parks[0] if available_parks else None
        session['selected_park'] = selected_park

    # Получаем сегодняшнюю дату в русском формате
    today = get_russian_date()

    # Получаем телефон СТО и адрес эвакуации на основе выбранного парка
    station_phone = STATION_PHONES.get(selected_park, '')
    evacuation_address = EVACUATION_ADDRESSES.get(selected_park, '')

    return render_template(
        'evacuation.html',
        today=today,
        station_phone=station_phone,
        evacuation_address=evacuation_address,
        selected_park=selected_park
    )


@application.route('/get_vehicles')
def get_vehicles():
    query = request.args.get('q', '')
    selected_park = request.args.get('park', session.get('selected_park', ''))
    available_parks = session.get('available_parks', [])

    # Проверяем доступ к парку
    if current_user.username != 'vasilev' and selected_park and selected_park not in available_parks:
        return jsonify({'vehicles': []}), 403

    # Формируем список парков для фильтрации
    filter_parks = PARK_GROUPS.get(selected_park, [selected_park]) if selected_park else available_parks

    vehicles = []
    # Запрашиваем автобусы из таблицы Car
    cars = Car.query.filter(Car.location.in_(filter_parks)).filter(Car.bort.ilike(f'%{query}%')).order_by(
        Car.bort).all()
    for car in cars:
        vehicles.append({
            'bort': car.bort,
            'gos_number': car.gos_number or '',
            'type_ts': 'Автобус'
        })

    # Запрашиваем электробусы из таблицы Elcar
    elbuses = Elcar.query.filter(Elcar.location.in_(filter_parks)).filter(Elcar.bort.ilike(f'%{query}%')).order_by(
        Elcar.bort).all()
    for elbus in elbuses:
        vehicles.append({
            'bort': elbus.bort,
            'gos_number': elbus.gos_number or '',
            'type_ts': 'Электробус'
        })

    return jsonify({'vehicles': vehicles})


@application.route('/get_gos_number/<bort>')
def get_gos_number(bort):
    # Проверяем в таблице Car
    car = Car.query.filter_by(bort=bort).first()
    if car and car.gos_number:
        return jsonify({'gos_number': car.gos_number})

    # Проверяем в таблице Elcar
    elcar = Elcar.query.filter_by(bort=bort).first()
    if elcar and elcar.gos_number:
        return jsonify({'gos_number': elcar.gos_number})

    return jsonify({'gos_number': ''}), 404


@application.route('/generate_evacuation', methods=['POST'])
def generate_evacuation():
    if not current_user.is_authenticated:
        flash('Требуется авторизация', 'error')
        return redirect(url_for('login'))

    selected_park = session.get('selected_park')
    if not selected_park:
        flash('Выберите парк перед созданием заявки', 'error')
        return redirect(url_for('evacuation'))

    # 1. Сбор и проверка данных
    data = {
        'bort': request.form.get('bort', '').strip(),
        'zakaz': request.form.get('zakaz', '').strip(),
        'opisanie': request.form.get('opisanie', '').strip(),
        'driver': request.form.get('driver', '').strip(),
        'number_driver': request.form.get('number_driver', '').strip(),
        'table': request.form.get('table', '').strip(),
        'marshrut': request.form.get('marshrut', '').strip(),
        'kuda_ehat': request.form.get('kuda_ehat', '').strip(),
        'date': get_russian_date(),
        'number_station': STATION_PHONES.get(selected_park, ''),
        'master': current_user.full_name,
        'models': '',
        'gos_number': '',
        'kuda_vezti': EVACUATION_ADDRESSES.get(selected_park, '')
    }

    required_fields = ['opisanie', 'driver', 'number_driver', 'bort', 'zakaz', 'kuda_ehat']
    for field in required_fields:
        if not data[field]:
            flash(f'Поле "{field}" обязательно для заполнения', 'error')
            return redirect(url_for('evacuation'))

    # 2. Поиск ТС для БД и документа
    vehicle = Car.query.filter_by(bort=data['bort']).first()
    is_elbus = False
    if not vehicle:
        vehicle = Elcar.query.filter_by(bort=data['bort']).first()
        is_elbus = True

    if not vehicle:
        flash(f'Борт {data["bort"]} не найден в системе', 'error')
        return redirect(url_for('evacuation'))

    # Заполняем данные ТС из найденного объекта
    data['gos_number'] = getattr(vehicle, 'gos_number', '')
    data['models'] = getattr(vehicle, 'models', '')

    # 3. Создание или обновление задачи в БД (Task)
    try:
        existing_task = (
            Task.query
            .filter_by(board_number=data['bort'], status='in_repair')
            .order_by(Task.created_at.desc())
            .first()
        )

        if existing_task:
            existing_task.description = data['opisanie']
            existing_task.comment = f"ЭВАКУАЦИЯ / Заказ-наряд {data['zakaz']}"
            existing_task.park = selected_park
            existing_task.status = 'in_repair'
            existing_task.tag = 'waiting'
            existing_task.event_type = 'эвакуация'
            existing_task.type_ts = 'Электробус' if is_elbus else 'Автобус'
            existing_task.author_open = current_user.full_name
            existing_task.contract = getattr(vehicle, 'contract', None)
            existing_task.vin = getattr(vehicle, 'vin', None)

            flash('Старая заявка обновлена под эвакуацию', 'success')
        else:
            new_task = Task(
                board_number=data['bort'],
                description=data['opisanie'],
                comment=f"ЭВАКУАЦИЯ / Заказ-наряд {data['zakaz']}",
                park=selected_park,
                status='in_repair',
                created_at=datetime.now(pytz.timezone('Europe/Moscow')),
                tag='waiting',
                event_type='эвакуация',
                type_ts='Электробус' if is_elbus else 'Автобус',
                author_open=current_user.full_name,
                contract=getattr(vehicle, 'contract', None),
                vin=getattr(vehicle, 'vin', None)
            )

            db.session.add(new_task)
            flash('Заявка создана', 'success')

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        application.logger.error(f"Ошибка БД: {e}")
        flash('Ошибка записи в БД, но файл может быть создан.', 'error')

    # 4. Генерация документа
    template_path = os.path.join(current_app.root_path, 'evacuation.docx')
    if not os.path.exists(template_path):
        flash('Шаблон документа evacuation.docx не найден', 'error')
        return redirect(url_for('evacuation'))

    template = DocxTemplate(template_path)
    template.render(data)

    output = BytesIO()
    template.save(output)
    output.seek(0)

    filename = f"evacuation_{data['bort']}_{datetime.now(tz).strftime('%Y%m%d')}.docx"

    application.logger.info(
        f"User: {current_user.username}, Task created and doc generated for bort: {data['bort']}"
    )

    return send_file(
        output,
        download_name=filename,
        as_attachment=True,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )



# -------------Телеграм-------------------------------------------------------------------------------------------
TELEGRAM_MAX_LEN = 4096


def send_telegram_message(message: str, chat_id: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    parts = [
        message[i:i + TELEGRAM_MAX_LEN]
        for i in range(0, len(message), TELEGRAM_MAX_LEN)
    ]

    last_response = None

    for part in parts:
        payload = {
            "chat_id": chat_id,
            "text": part,
            "parse_mode": "HTML"
        }
        try:
            response = requests.post(url, data=payload, timeout=5)
            response.raise_for_status()
            last_response = response
        except Exception as e:
            application.logger.error(f"Ошибка отправки в Telegram: {e}")
            application.logger.error(f"Ответ Telegram: {response.text if response else 'нет ответа'}")
            return None

    return last_response


# -------------ТО-------------------------------------------------------------------------------------------

TO_DATE_RE = re.compile(r'(\d{1,2})\.(\d{1,2})')


def clean_excel_text(value):
    if value is None:
        return ''

    if isinstance(value, float) and value.is_integer():
        return str(int(value))

    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def detect_to_park(value):
    text = clean_excel_text(value).lower().replace('ё', 'е')

    if not text:
        return None

    if '1-й' in text and 'коньково' in text:
        return '1 парк'

    if '14-й' in text and 'очаково' in text:
        return '14 парк'

    if '18-й' in text and 'ясенево' in text:
        return '18 парк'

    if 'красная пахра' in text and 'эл' not in text:
        return 'Красная Пахра'

    if 'эл-сы' in text and 'коньково' in text:
        return '1 парк'

    if 'эл-сы' in text and 'красная пахра' in text:
        return 'Красная Пахра'

    return None


def parse_schedule_date(value, year):
    if isinstance(value, datetime):
        return value.date()

    text = clean_excel_text(value)
    match = TO_DATE_RE.search(text)

    if not match:
        return None

    day = int(match.group(1))
    month = int(match.group(2))

    return datetime(year, month, day).date()


def normalize_to_type(value):
    text = clean_excel_text(value)
    upper = text.upper().replace(' ', '').replace('Ё', 'Е')

    if not upper or upper == 'РЕЗЕРВ':
        return None

    if upper == 'СО':
        return 'СО'

    if upper in ('СОНОЧЬ', 'СО-НОЧЬ'):
        return 'СО ночь'

    if 'ОБКАТ' in upper:
        return 'ТО-1000'

    if '2500' in upper:
        return 'ТО-2500'

    match = re.search(r'ТО-?([12])-?(\d+)', upper)
    if not match:
        return text

    to_num = match.group(1)
    mileage = int(match.group(2))

    if mileage < 1000:
        mileage *= 1000

    result = f'ТО{to_num} {mileage}'

    if 'СО' in upper:
        result += '+СО'

    return result


def normalize_board(value):
    text = clean_excel_text(value)

    if not text or text.lower() == 'резерв':
        return None

    return text


def parse_to_schedule_excel(file_storage, year):
    workbook = openpyxl.load_workbook(file_storage, data_only=True)

    if 'график ТО' in workbook.sheetnames:
        sheet = workbook['график ТО']
    else:
        sheet = workbook.active

    header_rows = []

    for row in range(1, sheet.max_row + 1):
        dates_by_col = {}

        for col in range(2, sheet.max_column + 1, 2):
            parsed_date = parse_schedule_date(sheet.cell(row=row, column=col).value, year)

            if parsed_date:
                dates_by_col[col] = parsed_date

        if dates_by_col:
            header_rows.append((row, dates_by_col))

    records = []

    for index, (header_row, dates_by_col) in enumerate(header_rows):
        next_header_row = header_rows[index + 1][0] if index + 1 < len(header_rows) else sheet.max_row + 1
        current_park = None

        for row in range(header_row + 1, next_header_row):
            park = detect_to_park(sheet.cell(row=row, column=1).value)

            if park:
                current_park = park

            if not current_park:
                continue

            for board_col, schedule_date in dates_by_col.items():
                board = normalize_board(sheet.cell(row=row, column=board_col).value)
                to_type = normalize_to_type(sheet.cell(row=row, column=board_col + 1).value)

                if not board or not to_type:
                    continue

                records.append({
                    'date': schedule_date,
                    'park': current_park,
                    'bort': board,
                    'to_type': to_type
                })

    return records

@application.route('/to_schedule/upload_excel', methods=['POST'])
@login_required
def upload_to_schedule_excel():
    user = db.session.query(User).filter_by(username=current_user.username).first()

    if not user or user.company not in ['Сетранс', 'МГТ']:
        flash('Нет доступа к загрузке графика ТО', 'error')
        return redirect(url_for('to_schedule'))

    file = request.files.get('excel_file')
    year = request.form.get('year', type=int) or datetime.now(pytz.timezone('Europe/Moscow')).year

    if not file or not file.filename:
        flash('Выберите Excel-файл', 'error')
        return redirect(url_for('to_schedule'))

    try:
        records = parse_to_schedule_excel(file, year)
    except Exception as e:
        application.logger.exception(f'Ошибка импорта графика ТО: {e}')
        flash('Не удалось прочитать Excel-файл', 'error')
        return redirect(url_for('to_schedule'))

    days_of_week = {
        0: 'Понедельник',
        1: 'Вторник',
        2: 'Среда',
        3: 'Четверг',
        4: 'Пятница',
        5: 'Суббота',
        6: 'Воскресенье'
    }

    added = 0
    updated = 0

    for item in records:
        existing = TOSchedule.query.filter_by(
            date=item['date'],
            park=item['park'],
            bort=item['bort']
        ).first()

        if existing:
            existing.to_type = item['to_type']
            existing.day_of_week = days_of_week[item['date'].weekday()]
            updated += 1
        else:
            db.session.add(TOSchedule(
                date=item['date'],
                day_of_week=days_of_week[item['date'].weekday()],
                bort=item['bort'],
                to_type=item['to_type'],
                park=item['park']
            ))
            added += 1

    db.session.commit()

    target_date = records[0]['date'] if records else datetime.now().date()
    flash(f'График ТО загружен: добавлено {added}, обновлено {updated}', 'success')

    return redirect(url_for('to_schedule', month=target_date.month, year=target_date.year))



@application.route('/to_schedule', methods=['GET', 'POST'])
def to_schedule():
    if not current_user.is_authenticated:
        flash('Пожалуйста, войдите в систему', 'error')
        return redirect(url_for('index'))
    # Проверка компании пользователя
    user = db.session.query(User).filter_by(username=current_user.username).first()
    if not user or user.company not in ['Сетранс', 'МГТ']:
        flash('У вас нет доступа к этой странице', 'error')
        return redirect(url_for('index'))

    # Получение доступных парков
    available_parks = user.allowed_parks if user.allowed_parks else []

    # Получение выбранного парка из сессии
    selected_park = session.get('selected_park')

    # Проверка, установлен ли selected_park и доступен ли он пользователю
    if not selected_park or selected_park not in available_parks:
        flash('Выберите доступный парк', 'error')
        session.pop('selected_park', None)
        selected_park = available_parks[0] if available_parks else None
        session['selected_park'] = selected_park
        if not selected_park:
            return redirect(url_for('select_park'))

    # Список допустимых значений to_type
    valid_to_types = [f"ТО{i} {mileage}" for i in [1, 2] for mileage in range(15000, 900001, 15000)] + ["ОТМ","СО","СО ночь", "ТО-1000", "ТО-2500"]

    # Обработка GET запроса для проверки предыдущей записи
    if request.method == 'GET':
        if request.args.get('check_previous'):
            bort = request.args.get('bort')
            date_str = request.args.get('date')

            if not bort or not date_str:
                return jsonify({'has_rejected': False})

            try:
                check_date = datetime.strptime(date_str, '%Y-%m-%d').date()

                # Ищем ПРЕДЫДУЩУЮ запись для этого борта (самую последнюю перед текущей датой)
                previous_entry = TOSchedule.query.filter(
                    TOSchedule.bort == bort,
                    TOSchedule.date < check_date,
                    TOSchedule.park == selected_park
                ).order_by(TOSchedule.date.desc()).first()

                has_rejected = False
                if previous_entry:
                    # Проверяем, есть ли связанная задача с отклонением
                    task = Task.query.filter_by(id_to=previous_entry.id).first()
                    if task and task.reject == 1:
                        has_rejected = True

                return jsonify({'has_rejected': has_rejected})
            except Exception as e:
                print(f"Error checking previous entry: {e}")
                return jsonify({'has_rejected': False})

    # Обработка POST запроса
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            date_str = request.form.get('date')
            bort = request.form.get('bort')
            to_type = request.form.get('to_type')
            is_so = 'is_so' in request.form
            force_add = request.form.get('force_add') == 'true'

            # Валидация to_type
            if to_type not in valid_to_types:
                return jsonify({'success': False, 'error': 'Недопустимый тип ТО'})

            # Валидация даты
            try:
                date = datetime.strptime(date_str, '%Y-%m-%d').date()
                # Сохраняем дату в сессии
                session['selected_date'] = date_str
            except ValueError:
                return jsonify({'success': False, 'error': 'Некорректный формат даты'})

            # Проверка предыдущей записи на отклонение
            previous_entry = TOSchedule.query.filter(
                TOSchedule.bort == bort,
                TOSchedule.date < date,
                TOSchedule.park == selected_park
            ).order_by(TOSchedule.date.desc()).first()

            has_rejected_previous = False
            if previous_entry:
                task = Task.query.filter_by(id_to=previous_entry.id).first()
                if task and task.reject == 1:
                    has_rejected_previous = True

            # Если предыдущая запись не была отклонена и не force_add, проверяем дубликаты
            if not has_rejected_previous and not force_add:
                # Проверка на дубликат борта на ту же дату
                existing_entry = TOSchedule.query.filter(
                    TOSchedule.bort == bort,
                    TOSchedule.date == date,
                    TOSchedule.park == selected_park
                ).first()

                # Также проверяем за последние 30 дней (как было раньше)
                date_threshold = datetime.now() - timedelta(days=30)
                existing_recent_entry = TOSchedule.query.filter(
                    TOSchedule.bort == bort,
                    TOSchedule.date >= date_threshold,
                    TOSchedule.date != date,  # исключаем текущую дату
                    TOSchedule.park == selected_park
                ).first()

                if existing_entry or existing_recent_entry:
                    return jsonify({
                        'success': False,
                        'duplicate': True,
                        'error': f'Борт {bort} уже есть в графике ТО'
                    })

            # Вычисление дня недели на русском
            days_of_week = {
                0: 'Понедельник',
                1: 'Вторник',
                2: 'Среда',
                3: 'Четверг',
                4: 'Пятница',
                5: 'Суббота',
                6: 'Воскресенье'
            }
            day_of_week = days_of_week[date.weekday()]

            # Добавляем "+СО" к to_type, если чекбокс отмечен
            if is_so:
                to_type = f"{to_type}+СО"

            # Добавление записи
            new_entry = TOSchedule(
                date=date,
                day_of_week=day_of_week,
                bort=bort,
                to_type=to_type,
                park=selected_park
            )
            db.session.add(new_entry)
            db.session.commit()
            return jsonify({'success': True})

        elif action == 'edit':
            entry_id = request.form.get('id')
            new_to_type = (request.form.get('to_type') or '').strip()
            passed = request.form.get('passed') == 'true'
            failed = request.form.get('failed') == 'true'

            if not new_to_type:
                return jsonify({'success': False, 'error': 'Укажите тип ТО'})

            if passed and failed:
                return jsonify({'success': False, 'error': 'Нельзя одновременно отметить прошло и не прошло'})

            entry = TOSchedule.query.get(entry_id)

            if not entry or entry.park != selected_park:
                return jsonify({'success': False, 'error': 'Запись не найдена или доступ запрещен'})

            entry.to_type = new_to_type

            task = Task.query.filter(
                Task.id_to.in_([entry.id, str(entry.id)])
            ).first()

            if passed or failed:
                if not task:
                    vehicle = Car.query.filter_by(bort=entry.bort).first()
                    if not vehicle:
                        vehicle = Elcar.query.filter_by(bort=entry.bort).first()

                    task = Task(
                        board_number=entry.bort,
                        vin=vehicle.vin if vehicle else None,
                        description=entry.to_type,
                        status='completed' if passed else 'rejected',
                        tag='to',
                        park=entry.park,
                        type_ts=vehicle.type_ts if vehicle else None,
                        contract=vehicle.contract if vehicle else None,
                        id_to=str(entry.id),
                    )
                    db.session.add(task)
                else:
                    task.board_number = entry.bort
                    task.description = build_to_description(entry.to_type, get_to_additional_description(task))
                    task.park = entry.park

                if failed:
                    task.reject = 1
                    task.status = 'rejected'
                elif passed:
                    task.reject = 0
                    task.status = 'completed'
            else:
                if task:
                    task.reject = 0
                    task.status = None

            db.session.commit()

            return jsonify({'success': True})

        elif action == 'delete':
            entry_id = request.form.get('id')
            entry = TOSchedule.query.get(entry_id)
            if entry and entry.park == selected_park:
                db.session.delete(entry)
                db.session.commit()
                flash('Запись удалена', 'success')
            else:
                flash('Запись не найдена или доступ запрещен', 'error')

            # Сохраняем текущий месяц и год для редиректа
            month = request.form.get('month', request.args.get('month', datetime.now().month, type=int))
            year = request.form.get('year', request.args.get('year', datetime.now().year, type=int))

            return redirect(url_for('to_schedule', month=month, year=year))

    # Получение данных для календаря (с выбором месяца/года)
    now = datetime.now(pytz.timezone('Europe/Moscow'))

    month = request.args.get('month', type=int, default=now.month)
    year = request.args.get('year', type=int, default=now.year)

    cal = calendar.monthcalendar(year, month)
    month_start = datetime(year, month, 1).date()

    if month == 12:
        month_end = datetime(year + 1, 1, 1).date()
    else:
        month_end = datetime(year, month + 1, 1).date()

    entries = TOSchedule.query.filter(
        TOSchedule.park == selected_park,
        TOSchedule.date >= month_start,
        TOSchedule.date < month_end
    ).order_by(
        TOSchedule.date,
        TOSchedule.bort
    ).all()

    entries_by_date = {}
    entry_ids = []

    for entry in entries:
        entries_by_date.setdefault(entry.date, []).append(entry)
        entry_ids.append(str(entry.id))

    # Проверяем, был ли этот борт в графике ТО за последние 30 дней
    check_start = month_start - timedelta(days=30)

    history_entries = TOSchedule.query.filter(
        TOSchedule.park == selected_park,
        TOSchedule.date >= check_start,
        TOSchedule.date < month_end
    ).order_by(
        TOSchedule.bort,
        TOSchedule.date
    ).all()

    history_by_bort = {}

    for history_entry in history_entries:
        history_by_bort.setdefault(history_entry.bort, []).append(history_entry)

    def get_recent_to_warning(entry):
        for history_entry in reversed(history_by_bort.get(entry.bort, [])):
            if history_entry.id == entry.id:
                continue

            history_task_state = tasks_by_to_id.get(str(history_entry.id), {})

            if history_task_state.get('has_reject', False):
                continue

            days_diff = (entry.date - history_entry.date).days

            if 0 <= days_diff <= 30:
                return (
                    f"Борт уже был в графике ТО "
                    f"{history_entry.date.strftime('%d.%m.%Y')}: {history_entry.to_type}"
                )

        return None

    all_relevant_ids = set(str(e.id) for e in entries) | set(str(e.id) for e in history_entries)

    tasks_by_to_id = {}

    if all_relevant_ids:
        tasks = Task.query.with_entities(
            Task.id_to,
            Task.reject,
            Task.status
        ).filter(
            Task.id_to.in_(all_relevant_ids)
        ).all()

        for task_id_to, reject, status in tasks:
            if not task_id_to:
                continue
            key = str(task_id_to)
            current = tasks_by_to_id.setdefault(key, {
                'has_reject': False,
                'has_completed': False
            })

            if reject == 1:
                current['has_reject'] = True
                current['has_completed'] = False
            elif not current['has_reject'] and status in ('completed', 'complited'):
                current['has_completed'] = True

    calendar_data = []

    for week in cal:
        week_data = []

        for day in week:
            if day == 0:
                week_data.append({
                    'date': None,
                    'entries': [],
                    'has_rejected_entries': False,
                    'has_completed_entries': False
                })
                continue

            current_date = datetime(year, month, day).date()
            day_entries = entries_by_date.get(current_date, [])

            processed_entries = []
            has_rejected = False
            has_completed = False

            for entry in day_entries:
                task_state = tasks_by_to_id.get(str(entry.id), {})
                has_reject = task_state.get('has_reject', False)
                has_completed_status = task_state.get('has_completed', False)

                if has_reject:
                    has_rejected = True
                elif has_completed_status:
                    has_completed = True

                warning_text = get_recent_to_warning(entry)

                processed_entries.append({
                    'id': entry.id,
                    'bort': entry.bort,
                    'to_type': entry.to_type,
                    'has_reject': has_reject,
                    'has_completed': has_completed_status,
                    'has_recent_to': warning_text is not None,
                    'recent_to_warning': warning_text
                })

            week_data.append({
                'date': current_date,
                'entries': processed_entries,
                'has_rejected_entries': has_rejected,
                'has_completed_entries': has_completed
            })

        calendar_data.append(week_data)

    month_year = f"{RUSSIAN_MONTHS[month]} {year}"

    # Извлекаем дату из сессии
    selected_date = session.get('selected_date')

    return render_template(
        'to_schedule.html',
        month_year=month_year,
        calendar_data=calendar_data,
        selected_month=month,
        selected_year=year,
        current_year=now.year,
        RUSSIAN_MONTHS=RUSSIAN_MONTHS,
        today=now.date(),
        allowed_users=ALLOWED_USERS,
        selected_date=selected_date
    )


def create_tasks_from_to_schedule():
    """Ежедневно в 8:00 создаёт заявки Task из TOSchedule на сегодня"""
    with application.app_context():
        lock_name = 'create_tasks_from_to_schedule'
        lock_acquired = False

        try:
            lock_result = db.session.execute(
                text("SELECT GET_LOCK(:lock_name, 30)"),
                {'lock_name': lock_name}
            ).scalar()

            if lock_result != 1:
                current_app.logger.warning(
                    "Создание заявок ТО уже выполняется другим процессом, повторный запуск пропущен"
                )
                return

            lock_acquired = True
            today = date.today()

            schedules = TOSchedule.query.filter_by(date=today).all()
            created_count = 0
            elcar1440_count = 0
            processed_keys = set()

            for sched in schedules:
                schedule_id = str(sched.id)
                schedule_key = (sched.park, sched.bort, today)

                if schedule_key in processed_keys:
                    current_app.logger.warning(
                        f"Дубликат в графике ТО на {today}: парк={sched.park}, борт={sched.bort}, id={sched.id}"
                    )
                    continue

                processed_keys.add(schedule_key)

                # Главная защита от дублей: у одной записи графика ТО должна быть только одна Task.
                existing_by_schedule = Task.query.filter(Task.id_to == schedule_id).first()
                if existing_by_schedule:
                    continue

                # Дополнительная защита: на один борт/парк/день не создаём вторую ТО-заявку,
                # даже если предыдущая уже закрыта или отклонена.
                existing_today_to = Task.query.filter(
                    Task.board_number == sched.bort,
                    Task.park == sched.park,
                    Task.tag == 'to',
                    func.date(Task.created_at) == today
                ).first()

                if existing_today_to:
                    if not existing_today_to.id_to:
                        existing_today_to.id_to = schedule_id
                        db.session.commit()
                    continue

                # Ищем транспорт сначала в Car
                vehicle = Car.query.filter_by(bort=sched.bort).first()
                elcar = None

                # Если не нашли — ищем в Elcar
                if not vehicle:
                    elcar = Elcar.query.filter_by(bort=sched.bort).first()
                    vehicle = elcar

                # Если вообще не нашли ТС — пропускаем
                if not vehicle:
                    current_app.logger.warning(
                        f"ТС с бортом {sched.bort} не найдено в Car и Elcar"
                    )
                    continue
                
                task_tag = 'otm' if sched.to_type == 'ОТМ' else 'to'  # Если в графике ТО вид работ ОТМ то тег будет ОТМ 

                # Создание заявки Task
                new_task = Task(
                    board_number=sched.bort,
                    type_ts=vehicle.type_ts,
                    vin=vehicle.vin,
                    contract=vehicle.contract,
                    status='in_repair',
                    description=sched.to_type,
                    park=sched.park,
                    tag=task_tag,
                    id_to=schedule_id,
                    author_open='Система',
                    created_at=datetime.combine(today, time(8, 0)),
                )

                db.session.add(new_task)
                created_count += 1

                # ===== ДОБАВЛЕНО: Elcar1440 =====
                if elcar and vehicle.contract in ELCAR1440_CONTRACTS:
                    exists = Elcar1440.query.filter_by(
                        board=sched.bort,
                        contract=vehicle.contract
                    ).first()

                    if not exists:
                        elcar1440 = Elcar1440(
                            board=sched.bort,
                            vin=elcar.vin,
                            park=sched.park,
                            contract=vehicle.contract,
                            created_at=datetime.now(pytz.timezone('Europe/Moscow'))
                        )
                        db.session.add(elcar1440)
                        elcar1440_count += 1

                db.session.commit()

            if created_count or elcar1440_count:
                current_app.logger.info(
                    f"Создано заявок ТО: {created_count}, "
                    f"добавлено в Elcar1440: {elcar1440_count} ({today})"
                )
            else:
                current_app.logger.info(
                    f"На {today} новых заявок ТО и Elcar1440 не требуется"
                )

        except Exception as e:
            db.session.rollback()
            current_app.logger.exception(f"Ошибка при создании заявок ТО: {e}")
        finally:
            if lock_acquired:
                try:
                    db.session.execute(
                        text("SELECT RELEASE_LOCK(:lock_name)"),
                        {'lock_name': lock_name}
                    )
                    db.session.commit()
                except Exception as e:
                    db.session.rollback()
                    current_app.logger.exception(f"Не удалось освободить lock создания ТО: {e}")


@application.route('/to_schedule/export', methods=['GET'])
def to_schedule_export():
    # Проверка компании пользователя
    user = db.session.query(User).filter_by(username=current_user.username).first()
    if not user or user.company not in ['Сетранс', 'МГТ']:
        flash('У вас нет доступа к этой странице', 'error')
        return redirect(url_for('index'))

    # Получение выбранного парка из сессии
    selected_park = session.get('selected_park')
    if not selected_park or selected_park not in user.allowed_parks:
        flash('Выберите доступный парк', 'error')
        return redirect(url_for('select_park'))

    # Получаем месяц и год для экспорта (по умолчанию текущие)
    export_month = request.args.get('month', type=int, default=datetime.now(pytz.timezone('Europe/Moscow')).month)
    export_year = request.args.get('year', type=int, default=datetime.now(pytz.timezone('Europe/Moscow')).year)

    # Получаем все записи за выбранный месяц/год для парка
    start_date = datetime(export_year, export_month, 1).date()
    if export_month == 12:
        end_date = datetime(export_year + 1, 1, 1).date() - timedelta(days=1)
    else:
        end_date = datetime(export_year, export_month + 1, 1).date() - timedelta(days=1)

    entries = TOSchedule.query.filter(
        TOSchedule.date >= start_date,
        TOSchedule.date <= end_date,
        TOSchedule.park == selected_park
    ).order_by(TOSchedule.date, TOSchedule.bort).all()

    # Создаём workbook
    wb = Workbook()
    ws = wb.active
    ws.title = f"График ТО {RUSSIAN_MONTHS[export_month]} {export_year}"

    # Заголовки дней недели
    days_of_week = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    for col, day in enumerate(days_of_week, 1):
        cell = ws.cell(row=2, column=col, value=day)
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color='CCCCCC', end_color='CCCCCC', fill_type='solid')
        cell.alignment = Alignment(horizontal='center', vertical='center')

    # Заголовок с месяцем и годом
    ws.merge_cells('A1:G1')
    title_cell = ws['A1']
    title_cell.value = f"График ТО {RUSSIAN_MONTHS[export_month]} {export_year} ({selected_park})"
    title_cell.font = Font(bold=True, size=14)
    title_cell.alignment = Alignment(horizontal='center', vertical='center')

    # Строим календарь
    cal = calendar.monthcalendar(export_year, export_month)
    row_offset = 3  # Начинаем с 3-й строки (после заголовков)

    # Собираем записи по датам
    entries_by_date = {}
    for entry in entries:
        date_str = entry.date.strftime('%Y-%m-%d')
        if date_str not in entries_by_date:
            entries_by_date[date_str] = []
        entries_by_date[date_str].append(f"{entry.bort} {entry.to_type}")

    # Заполняем календарь
    for week_idx, week in enumerate(cal):
        for day_idx, day in enumerate(week):
            col = day_idx + 1
            row = row_offset + week_idx
            if day == 0:
                cell = ws.cell(row=row, column=col)
                cell.fill = PatternFill(start_color='F0F0F0', end_color='F0F0F0', fill_type='solid')
            else:
                date = datetime(export_year, export_month, day).date()
                date_str = date.strftime('%Y-%m-%d')
                cell = ws.cell(row=row, column=col)
                cell.value = str(day)  # Номер дня
                cell.font = Font(bold=True)
                cell.alignment = Alignment(horizontal='left', vertical='top')

                # Добавляем задачи
                if date_str in entries_by_date:
                    tasks = "\n".join(entries_by_date[date_str])
                    cell.value = f"{day}\n{tasks}"

                # Выделяем текущий день
                if date == datetime.now(pytz.timezone('Europe/Moscow')).date():
                    cell.fill = PatternFill(start_color='D4EDDA', end_color='D4EDDA', fill_type='solid')

    # Устанавливаем высоту строк и ширину столбцов
    for row in range(2, row_offset + len(cal) + 1):
        ws.row_dimensions[row].height = 100  # Высота строк для видимости задач

    for col in range(1, 8):
        ws.column_dimensions[ws.cell(row=2, column=col).column_letter].width = 20

    # Сохраняем в BytesIO
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"график_ТО_{RUSSIAN_MONTHS[export_month]}_{export_year}.xlsx"

    return send_file(
        output,
        download_name=filename,
        as_attachment=True,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


# -------------ВИДНЫ ТОЛЬКО ДОСТУПНЫЕ ПАРКИ В BASE.HTML--------------------------------------------------------------------------------------------
@application.context_processor
def inject_allowed_parks():
    allowed_parks = []
    try:
        if current_user.is_authenticated:
            data = current_user.allowed_parks
            # если строка — парсим JSON, если уже list — оставляем
            if isinstance(data, str):
                allowed_parks = json.loads(data)
            elif isinstance(data, list):
                allowed_parks = data
    except Exception as e:
        print("Ошибка при загрузке парков:", e)
    return dict(allowed_parks=allowed_parks)


# -------------ОТПРАВКА ОТЧЕТА НА 19-00 В ТЕЛЕГУ--------------------------------------------------------------------------------------------

def send_itr_19_reports():
    with application.app_context():  # ✅ добавляем контекст Flask
        today = datetime.now(tz).date()
        formatted_date = today.strftime("%d.%m.%Y")

        has_any_report_19 = db.session.query(Report19Table.id).filter(
            db.func.date(Report19Table.date) == today
        ).first()

        if not has_any_report_19:
            application.logger.warning("send_itr_19_reports: report_19 is empty, trying save_19 before sending")
            scheduled_save_19()

        for park, chat_id in TELEGRAM_19.items():
            if not chat_id:
                application.logger.warning(f"{park}: не указан chat_id Telegram")
                continue

            db_parks = PARK_GROUPS.get(park, [park])

            # Проверка наличия данных report_19
            has_data = db.session.query(Report19Table).filter(
                Report19Table.park.in_(db_parks),
                db.func.date(Report19Table.date) == today
            ).first()

            if not has_data:
                application.logger.warning(f"{park}: нет данных report_19")
                continue

            # Получение данных с фильтром по дате
            report_7 = db.session.query(
                (Report7Table.col1 + case((park == "Красная Пахра", '  ' + Report7Table.contract),
                                          else_='')).label('col')
            ).filter(
                Report7Table.park.in_(db_parks),
                db.func.date(Report7Table.date) == today
            ).all()

            report_830 = db.session.query(
                (Report830Table.col1 + case((park == "Красная Пахра", '  ' + Report830Table.contract),
                                            else_='')).label('col')
            ).filter(
                Report830Table.park.in_(db_parks),
                db.func.date(Report830Table.date) == today
            ).all()

            report_17 = db.session.query(
                (Report17Table.col1 + case((park == "Красная Пахра", '  ' + Report17Table.contract),
                                           else_='')).label('col')
            ).filter(
                Report17Table.park.in_(db_parks),
                db.func.date(Report17Table.date) == today
            ).all()

            report_19 = db.session.query(
                (Report19Table.col1 + case((park == "Красная Пахра", '  ' + Report19Table.contract),
                                           else_='')).label('col')
            ).filter(
                Report19Table.park.in_(db_parks),
                db.func.date(Report19Table.date) == today
            ).all()

            def fmt(lst):
                return "\n".join(sorted([r.col for r in lst])) if lst else "Нет данных"

            message = (
                f"<b>Отчет в ИТР на 19-00 - {PARK_NAME.get(park, park)}</b>\n"
                f"Дата: {formatted_date} г.\n\n"
                f"<b>7-15 (8:00):</b>\n{fmt(report_7)}\n\n"
                f"<b>8-30:</b>\n{fmt(report_830)}\n\n"
                f"<b>17-00:</b>\n{fmt(report_17)}\n\n"
                f"<b>19-00:</b>\n{fmt(report_19)}"
            )

            # Отправка в Telegram
            application.logger.info(f"Сообщение для {park}: [{message}]")
            send_telegram_message(message, chat_id)
            application.logger.info(f"Отчет отправлен в Telegram для {park}")


def scheduled_send_itr_19_reports():
    return run_scheduler_job_once("send_itr_19_reports", send_itr_19_reports)


# -------------КТГ--------------------------------------------------------------------------------------------

@application.route('/reports_ktg', methods=['GET', 'POST'])
def reports_ktg():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))

    ru_holidays = holidays.RU(years=[2024, 2025, 2026, 2027])
    now = datetime.now(pytz.timezone('Europe/Moscow'))
    today = now.date()  # только дата, без времени

    contracts = sorted([
        "216644",
        "222150",
        "МГТ0105-21",
        "МГТ0107-21",
        "МГТ234953-23",
        "МГТ0019-25",
    ])

    is_workday = today.weekday() < 5 and today not in ru_holidays
    # True = будний день, False = выходной/праздник

    # Столбец 1: для ВСЕХ контрактов — 7-15 в будни, 8-00 в выходные
    time_column_1 = {
        "216644": "7:15" if is_workday else "8:00",
        "222150": "7:15" if is_workday else "8:00",
        "МГТ0105-21": "7:15" if is_workday else "8:00",
        "МГТ0107-21": "7:15" if is_workday else "8:00",
        "МГТ234953-23": "7:15" if is_workday else "8:00",
        "МГТ0019-25": "7:15" if is_workday else "8:00",
    }

    time_column_2 = {
        "216644": "8:30" if is_workday else "8:00",
        "222150": "8:30" if is_workday else "8:00",
        "МГТ0105-21": "8:30" if is_workday else "10:00",
        "МГТ0107-21": "8:30" if is_workday else "10:00",
        "МГТ234953-23": "8:30" if is_workday else "10:00",
        "МГТ0019-25": "8:30" if is_workday else "10:00",  # если он тоже должен быть как МГТ
    }

    selected_park = session.get('selected_park')
    if not selected_park:
        flash('Выберите парк', 'warning')
        return redirect(url_for('index'))

    # --- Дата ---
    date_str = request.args.get('date')
    if date_str:
        try:
            selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except:
            selected_date = date.today()
    else:
        selected_date = date.today()

    db_parks = PARK_GROUPS.get(selected_park, [selected_park])
    formatted_date = selected_date.strftime("%d.%m.%Y")
    selected_contract = request.args.get('contract', '').strip()

    # Количество бортов и проценты КТГ по контрактам
    day_start = datetime.combine(selected_date, time.min)
    day_end = day_start + timedelta(days=1)

    fleet_rows = FleetSize.query.with_entities(
        FleetSize.location,
        FleetSize.count
    ).filter(
        FleetSize.location.in_(contracts),
        FleetSize.zone.contains(selected_park)
    ).all()

    fleet_counts = {
        location: count
        for location, count in fleet_rows
    }

    def get_report_counts(table):
        rows = db.session.query(
            Car.contract,
            func.count(Car.id)
        ).join(
            table,
            Car.bort == table.col1
        ).filter(
            table.park.in_(db_parks),
            table.date >= day_start,
            table.date < day_end,
            Car.contract.in_(contracts)
        ).group_by(
            Car.contract
        ).all()

        return {
            contract: count
            for contract, count in rows
        }

    report_counts = {
        'r7': get_report_counts(Report7Table),
        'r830': get_report_counts(Report830Table),
        'r17': get_report_counts(Report17Table),
        'r19': get_report_counts(Report19Table),
    }

    def ktg_percent(contract, report_key):
        total = fleet_counts.get(contract, 0)
        in_report = report_counts[report_key].get(contract, 0)

        if total <= 0:
            return 0

        return round((total - in_report) / total * 100, 2)

    percentages_1 = {
        contract: ktg_percent(contract, 'r7')
        for contract in contracts
    }
    percentages_2 = {
        contract: ktg_percent(contract, 'r830')
        for contract in contracts
    }
    percentages_3 = {
        contract: ktg_percent(contract, 'r17')
        for contract in contracts
    }
    percentages_4 = {
        contract: ktg_percent(contract, 'r19')
        for contract in contracts
    }

    avg_percentages = {}
    for contract in contracts:
        if contract in ("216644", "222150"):
            values = [percentages_2[contract], percentages_3[contract]]
        else:
            values = [
                percentages_1[contract],
                percentages_2[contract],
                percentages_3[contract],
                percentages_4[contract],
            ]

        avg_percentages[contract] = round(sum(values) / len(values), 2)

    # Эти переменные используются ниже при генерации акта
    contract_2_count = fleet_counts.get("216644", 0)

    report_830_contract_2 = report_counts['r830'].get("216644", 0)
    report_17_contract_2 = report_counts['r17'].get("216644", 0)

    report_830_contract_2_proc = percentages_2["216644"]
    report_17_contract_2_proc = percentages_3["216644"]
    avg_percentage_2 = avg_percentages["216644"]

    report_830_1 = contract_2_count - report_830_contract_2
    report_17_2 = contract_2_count - report_17_contract_2

    # Основные списки бортов для вывода в таблицу
    def get_report_items(table):
        query = table.query.with_entities(table.col1, table.contract).filter(
            table.date >= day_start,
            table.date < day_end,
            table.park.in_(db_parks)
        )

        if selected_contract:
            query = query.filter(table.contract == selected_contract)

        return query.all()

    report_7 = get_report_items(Report7Table)
    report_830 = get_report_items(Report830Table)
    report_17 = get_report_items(Report17Table)
    report_19 = get_report_items(Report19Table)

    def format_items(items):
        result = []

        for col1, contract in items:
            if selected_park and contract:
                result.append(f"{col1}")
            else:
                result.append(col1 or "")

        return sorted([item for item in result if item])

    r7 = format_items(report_7)
    r830 = format_items(report_830)
    r17 = format_items(report_17)
    r19 = format_items(report_19)

    time1 = time_column_1.get(selected_contract)
    time2 = time_column_2.get(selected_contract)

    print_act = request.args.get('print_act') == '1'
    print_act_mgt = request.args.get('print_act_mgt') == '1'

    if (print_act or print_act_mgt) and selected_contract:

        # Выбор папки
        if print_act_mgt:
            acts_folder = os.path.join(application.template_folder, 'acts_mgt')
        else:
            acts_folder = os.path.join(application.template_folder, 'acts')

        template_path = None

        # db_parks = ['1 парк', '14 парк'] и т.п.
        for park_group in db_parks:
            park_clean = park_group.replace(" ", "_")
            filename_cp = f"{selected_contract}_{park_clean}.docx"
            path_cp = os.path.join(acts_folder, filename_cp)

            if os.path.exists(path_cp):
                template_path = path_cp
                break

        # Если в группе парков ничего не найдено — это ошибка
        if template_path is None:
            flash(
                f'Шаблон акта для контракта "{selected_contract}" '
                f'в группе парков {db_parks} не найден в папке '
                f'"{"acts_mgt" if print_act_mgt else "acts"}"!',
                'danger'
            )
            return redirect(url_for('reports_ktg', date=selected_date, contract=selected_contract))

        doc = DocxTemplate(template_path)

        context = {
            'time1': time1,
            'time2': time2,
            'report_830_1': report_830_1,
            'report_17_2': report_17_2,
            'contract_2': contract_2_count,
            'avg_1': report_830_contract_2_proc,
            'avg_2': report_17_contract_2_proc,
            'avg_percentage_2': avg_percentage_2,
            'park': selected_park.replace('_', ' ').title(),
            'data': formatted_date,  # если в шаблоне есть #data
        }
        print(report_830_contract_2_proc)
        print(report_17_contract_2_proc)
        doc.render(context)

        output = BytesIO()
        doc.save(output)
        output.seek(0)

        filename = (
            f"Акт_КТГ_МГТ_{selected_contract}_{formatted_date.replace('.', '-')}.docx"
            if print_act_mgt else
            f"Акт_КТГ_{selected_contract}_{formatted_date.replace('.', '-')}.docx"
        )

        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        )

    return render_template(
        'reports_ktg.html',
        selected_park=selected_park,
        selected_date=selected_date,
        formatted_date=formatted_date,
        selected_contract=selected_contract,
        contracts=contracts,
               r7=r7, r830=r830, r17=r17, r19=r19,
        avg_percentages=avg_percentages,
        percentages_1=percentages_1,
        percentages_2=percentages_2,
        percentages_3=percentages_3,
        percentages_4=percentages_4,
        time_column_1=time_column_1,
        time_column_2=time_column_2,
    )


# --- УДАЛЕНИЕ ЗНАЧЕНИЯ ---
@application.route('/delete_value', methods=['POST'])
def delete_value():
    table = request.form.get('table')
    value = request.form.get('value')
    date_str = request.form.get('date')
    contract = request.form.get('contract')
    park = session.get('selected_park')

    selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()

    TABLE_MAP = {
        'r7': Report7Table,
        'r830': Report830Table,
        'r17': Report17Table,
        'r19': Report19Table
    }

    model = TABLE_MAP.get(table)
    if not model:
        flash("Ошибка таблицы", "danger")
        return redirect(url_for('reports_ktg'))

    row = model.query.filter(
        db.func.date(model.date) == selected_date,
        model.park == park,
        model.col1 == value,
        model.contract == (contract if contract else None)
    ).first()

    if row:
        db.session.delete(row)
        db.session.commit()
        flash("Удалено", "success")

    return redirect(url_for('reports_ktg', date=date_str, contract=contract))


# --- ДОБАВЛЕНИЕ ЗНАЧЕНИЯ ---
@application.route('/add_value', methods=['POST'])
def add_value():
    value = request.form.get('value')
    table = request.form.get('table')
    date_str = request.form.get('date')
    contract = request.form.get('contract') or None

    park = session.get('selected_park')
    selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()

    TABLE_MAP = {
        'r7': Report7Table,
        'r830': Report830Table,
        'r17': Report17Table,
        'r19': Report19Table
    }
    model = TABLE_MAP.get(table)

    new_row = model(
        date=selected_date,
        park=park,
        contract=contract,
        col1=value
    )
    db.session.add(new_row)
    db.session.commit()

    flash("Добавлено", "success")
    return redirect(url_for('reports_ktg', date=date_str, contract=contract))


@application.route('/update_event_status', methods=['POST'])
@login_required
def update_event_status():
    task_id = request.form.get('task_id')
    event_type = request.form.get('event_type')

    # Находим задачу в базе данных
    task = Task.query.get(task_id)
    if task:
        # Если выбрали "none", записываем None или пустую строку
        task.event_type = event_type if event_type != 'none' else None
        db.session.commit()
        return jsonify({'success': True})

    return jsonify({'success': False, 'error': 'Task not found'}), 404

@application.route('/dashboard')
@login_required
def dashboard():
    from dashboard_metrics import build_dashboard_metrics

    park = session.get('selected_park')
    if park and park not in session.get('available_parks', []):
        abort(403)
    period = request.args.get('period', 'day')
    if period not in {'day', 'week', 'month', 'year'}:
        period = 'day'
    try:
        selected_date = date.fromisoformat(request.args.get('date', ''))
        if not 1900 <= selected_date.year <= 9998:
            raise ValueError('unsupported year')
    except ValueError:
        selected_date = datetime.now(tz).date()
    metrics = build_dashboard_metrics(KtgSnapshot, db.session, park, period, selected_date)
    return render_template('dashboard.html', selected_park=park, period=period,
                           selected_date=selected_date.isoformat(), metrics=metrics)
    
# ----рассчет МГТ0223-25------------------------------------------------------------------------------------------------------------------------------

def parse_excel_datetime(value):
    """Универсальный парсинг даты из Excel"""
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    if isinstance(value, (int, float)):
        try:
            return openpyxl.utils.datetime.from_excel(value)
        except:
            return None

    if isinstance(value, str):
        value = value.strip()
        for fmt in ('%d-%m-%Y %H:%M', '%Y-%m-%d %H:%M', '%d.%m.%Y %H:%M', '%d.%m.%y %H:%M'):
            try:
                return datetime.strptime(value, fmt)
            except:
                continue
    return None


@application.route('/checkpoints', methods=['GET', 'POST'])
@login_required
def checkpoints():
    if request.method == 'POST':
        session.pop('checkpoint_results', None)  # очищаем старое

        file = request.files.get('file')
        if not file or not file.filename.endswith(('.xlsx', '.xls')):
            flash('Пожалуйста, загрузите файл Excel (.xlsx или .xls)', 'error')
            return redirect(url_for('checkpoints'))

        try:
            workbook = openpyxl.load_workbook(file, data_only=True)
            sheet = workbook.active

            # === Заголовки ===
            headers = {}
            for col_idx, cell in enumerate(sheet[1], start=1):
                if cell.value:
                    headers[cell.value.strip()] = col_idx

            board_column_name = next((h for h in headers if 'бортовой номер' in h.lower()), None)
            if not board_column_name:
                flash('В файле отсутствует колонка с бортовым номером', 'error')
                return redirect(url_for('checkpoints'))

            required_cols = ['Дата создания', 'Стадия']
            for col in required_cols:
                if col not in headers:
                    flash(f'Отсутствует колонка "{col}"', 'error')
                    return redirect(url_for('checkpoints'))

            # === Читаем данные ===
            rows_data = []
            for row in sheet.iter_rows(min_row=2, values_only=True):
                if not row or not any(row):
                    continue

                stage = str(row[headers.get('Стадия', 0) - 1] or '').strip()
                if stage == 'Отклонено.':
                    continue

                board = str(row[headers.get(board_column_name, 0) - 1] or '').strip()
                if not board:
                    continue

                created_dt = parse_excel_datetime(row[headers.get('Дата создания', 0) - 1])
                end_dt = parse_excel_datetime(row[headers.get('Дата завершения', 0) - 1]) if 'Дата завершения' in headers else None

                rows_data.append({
                    'board': board,
                    'created': created_dt,
                    'end': end_dt,
                    'stage': stage
                })

            if not rows_data:
                flash('После фильтрации данных не осталось', 'warning')
                return redirect(url_for('checkpoints'))

            # === Анализ по контрольным точкам ===
            checkpoints = [
                ("7:15", 7, 15),
                ("8:30", 8, 30),
                ("17:00", 17, 0),
                ("19:00", 19, 0)
            ]

            results_by_date = {}

            for item in rows_data:
                board = item["board"]
                created = item["created"]
                end = item["end"] or datetime.now()

                if not created:
                    continue

                current_date = created.date()
                while current_date <= end.date():
                    date_key = current_date.strftime("%Y-%m-%d")

                    if date_key not in results_by_date:
                        results_by_date[date_key] = {
                            "7:15": {"boards": []},
                            "8:30": {"boards": []},
                            "17:00": {"boards": []},
                            "19:00": {"boards": []},
                        }

                    for label, hour, minute in checkpoints:
                        checkpoint_dt = datetime.combine(current_date, time(hour, minute))

                        if created <= checkpoint_dt <= end:
                            if board not in results_by_date[date_key][label]["boards"]:
                                results_by_date[date_key][label]["boards"].append(board)

                    current_date += timedelta(days=1)

            # Сортируем
            for date_key in results_by_date:
                for label in ["7:15", "8:30", "17:00", "19:00"]:
                    results_by_date[date_key][label]["boards"].sort()

            sorted_dates = sorted(results_by_date.keys(), reverse=True)

            # Берём первую дату по умолчанию
            target_date = sorted_dates[0] if sorted_dates else None

            session["checkpoint_results"] = {
                "by_date": results_by_date,
                "sorted_dates": sorted_dates,
                "target_date": target_date,
                "total_records": len(rows_data)
            }

            return redirect(url_for('checkpoints', target_date=target_date))

        except Exception as e:
            application.logger.error(f"Ошибка анализа: {e}", exc_info=True)
            flash(f'Ошибка при обработке файла: {str(e)}', 'error')
            return redirect(url_for('checkpoints'))

    # ====================== GET ======================
    results = session.get('checkpoint_results')

    if not results or not isinstance(results, dict):
        by_date = {}
        sorted_dates = []
        total_records = 0
        target_date = None
    else:
        by_date = results.get('by_date', {})
        sorted_dates = sorted(results.get('sorted_dates', []), reverse=True)
        total_records = results.get('total_records', 0)

        # Приоритет: параметр из URL → сохранённое в сессии → первая дата
        target_date = request.args.get('target_date')
        if not target_date or target_date not in by_date:
            target_date = results.get('target_date')
            if not target_date or target_date not in by_date:
                target_date = sorted_dates[0] if sorted_dates else None

        # Обновляем в сессии текущую выбранную дату
        results['target_date'] = target_date
        session['checkpoint_results'] = results

    return render_template('checkpoints.html',
                           by_date=by_date,
                           sorted_dates=sorted_dates,
                           target_date=target_date,
                           total_records=total_records)
    

# -------------ТРИГГЕРЫ--------------------------------------------------------------------------------------------
def add_scheduler_job(job_id, func, trigger):
    scheduler.add_job(
        func,
        trigger,
        id=job_id,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1800
    )


def run_yandex_mail_cycle():
    yandex_requests.run(replies_only=True)
    return yandex_requests.run(import_only=True)


def start_scheduler():
    from ktg_history import register_ktg_scheduler_jobs
    if scheduler.running:
        return
    
    from fleet_sync import register_fleet_sync_jobs
    register_fleet_sync_jobs(add_scheduler_job, CronTrigger, tz)

    scheduler.configure(timezone=tz)
    add_scheduler_job("save_7", scheduled_save_7, CronTrigger(day_of_week='*', hour=7, minute=15, timezone=tz))
    add_scheduler_job("save_830", scheduled_save_830, CronTrigger(day_of_week='*', hour=8, minute=30, timezone=tz))
    add_scheduler_job("save_830_contracts_8", scheduled_save_830_contracts_8, CronTrigger(hour=8, minute=0, timezone=tz))
    add_scheduler_job("save_830_contracts_10", scheduled_save_830_contracts_10, CronTrigger(hour=10, minute=0, timezone=tz))
    add_scheduler_job("save_17", scheduled_save_17, CronTrigger(day_of_week='*', hour=17, minute=0, timezone=tz))
    add_scheduler_job("save_19", scheduled_save_19, CronTrigger(day_of_week='*', hour=19, minute=0, timezone=tz))
    add_scheduler_job(
        "scheduled_copy_mileage",
        lambda: run_scheduler_job_once("scheduled_copy_mileage", scheduled_copy_mileage),
        CronTrigger(hour=9, minute=11, timezone=tz)
    )
    add_scheduler_job(
        "scheduled_save_history",
        lambda: run_scheduler_job_once("scheduled_save_history", scheduled_save_history),
        CronTrigger(hour=9, minute=12, timezone=tz)
    )
    add_scheduler_job(
        "send_itr_19_reports",
        scheduled_send_itr_19_reports,
        CronTrigger(day_of_week='*', hour=19, minute=30, timezone=tz)
    )
    add_scheduler_job(
        "create_tasks_from_to_schedule",
        lambda: run_scheduler_job_once("create_tasks_from_to_schedule", create_tasks_from_to_schedule),
        CronTrigger(hour=8, minute=0, timezone=tz)
    )
    add_scheduler_job(
        "delit_data",
        lambda: run_scheduler_job_once("delit_data", delit_data),
        CronTrigger(day_of_week='*', hour=8, minute=30, timezone=tz)
    )
    
    register_ktg_scheduler_jobs(add_scheduler_job, CronTrigger)

    # Два обхода используют общий mutex на выделенном соединении MySQL:
    # процессов веб-сервера несколько, и без блокировки опрос пойдёт в несколько
    # потоков, умножив число запросов к чужому API.
    if INCONTROL_ENABLED:
        add_scheduler_job(
            "refresh_incontrol_statuses",
            lambda: run_incontrol_job_once(
                "refresh_incontrol_statuses",
                refresh_incontrol_statuses
            ),
            CronTrigger(
                minute='7,37',
                timezone=tz
            )
        )
        # Импорт новых заявок InControl → Task (борта без активной заявки).
        add_scheduler_job(
            "create_tasks_from_incontrol",
            lambda: run_incontrol_job_once(
                "create_tasks_from_incontrol",
                create_tasks_from_incontrol
            ),
            CronTrigger(
                minute='*/15',
                timezone=tz
            )
        )
    if yandex_requests.enabled():
        add_scheduler_job(
            'yandex_mail_sync',
            lambda: run_locked_sync_job('yandex_mail_sync', run_yandex_mail_cycle, 'yandex_mail'),
            CronTrigger(minute='*/1', timezone=tz),
        )

    scheduler.start()
    application.logger.info(f"Scheduler started in pid={os.getpid()} with jobs={len(scheduler.get_jobs())}")
    run_startup_report_catchups()

@application.before_request
def check_disabled_parks():
    if current_user.is_authenticated:
        selected_park = session.get('selected_park')
        if selected_park in DISABLED_PARKS:
            flash(f'Парк {selected_park} временно недоступен', 'warning')
            # Переключить на первый доступный парк
            available_parks = [p for p in session.get('available_parks', []) if p not in DISABLED_PARKS]
            if available_parks:
                session['selected_park'] = available_parks[0]
            else:
                logout_user()
                return redirect(url_for('login'))

@application.route('/debug_incontrol_tasks')
def debug_incontrol_tasks():
    client = _make_incontrol_client()
    client.authenticate()

    client.debug_task_by_id(19311671)
    client.debug_task_by_id(19311705)

    return 'Проверка выполнена. Смотри консоль Flask.'
    
class KtgSnapshot(db.Model):
    __tablename__ = 'ktg_snapshot'

    id = db.Column(db.Integer, primary_key=True)
    park = db.Column(db.String(50), nullable=False, index=True)
    recorded_at = db.Column(db.DateTime, nullable=False,
                            default=datetime.now, index=True)
    ktg_park = db.Column(db.Float, nullable=True)       # КТГ, %
    in_repair = db.Column(db.Integer, nullable=False, default=0)  # машин в ремонте

    __table_args__ = (
        db.Index('ix_ktg_snapshot_park_time', 'park', 'recorded_at'),
    )    

from completion_checks import CompletionChecks
completion_checks = CompletionChecks(
    application, db, Task, refresh_incontrol_status_for_board, lambda: INCONTROL_ENABLED,
)

from body_repair_sync import BodyRepair, register_body_repair_routes
register_body_repair_routes(application)

from incontrol_accounts import paused_closure_services, register_incontrol_accounts
IncontrolAccount, personal_incontrol_client = register_incontrol_accounts(application, db)

from yandex_requests import YandexRequests
yandex_requests = YandexRequests(application, db, Task, Car, Elcar)
application.add_template_global(yandex_requests.link_for, 'yandex_request_for')

from incontrol_rejection_jobs import IncontrolRejections
incontrol_rejections = IncontrolRejections(
    application, db, Task, completion_checks, personal_incontrol_client,
    lambda: os.getenv('INCONTROL_REJECTION_ENABLED', '0') == '1',
)

from incontrol_outbox import IncontrolOutbox
incontrol_outbox = IncontrolOutbox(application, db, completion_checks, incontrol_rejections, yandex_requests)


# Планировщик запускаем только если явно разрешено.
# На продакшене с несколькими воркерами (Passenger/gunicorn)
# ставим RUN_SCHEDULER=1 только на одном процессе/инстансе,
# иначе каждый воркер поднимет свой APScheduler и будет спамить
# «пропущено, так как этот процесс выполняется другим процессом».
if os.getenv('RUN_SCHEDULER') == '1':
    start_scheduler()

if __name__ == '__main__':
    with application.app_context():
        db.create_all()
        db.session.commit()
        application.run()
