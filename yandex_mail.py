"""Яндекс Почта в Сетрансе: разбор Excel, IMAP/SMTP, OAuth, импорт и очередь ответов."""

import base64
import hashlib
import imaplib
import json
import logging
import os
import re
import smtplib
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, parsedate_to_datetime
from io import BytesIO
from pathlib import Path, PurePath
from tempfile import NamedTemporaryFile
from threading import BoundedSemaphore
from time import monotonic, sleep, time as unix_time
from uuid import UUID, uuid4
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import click
import requests
import xlrd
from apscheduler.triggers.cron import CronTrigger
from flask import flash
from openpyxl import load_workbook
from requests.certs import where as ca_bundle
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from task_import_matching import active_repairs_for_board, manual_repair_candidates, match_imported_repair


# --- Разбор писем, заявок Excel и обмен по IMAP/SMTP --------------------------

MOSCOW = ZoneInfo('Europe/Moscow')
MAIL_TASK_AUTHOR = 'Яндекс Почта'
INBOX = 'INBOX'
MAX_MESSAGE_SIZE = 4 * 1024 * 1024
MAX_WORKBOOK_SIZE = 2 * 1024 * 1024
IMAP_HOST, IMAP_PORT = 'imap.yandex.ru', 993
SMTP_HOST, SMTP_PORT = 'smtp.yandex.ru', 465
OAUTH_IMAP_HOST = 'imap.yandex.com'
OAUTH_SMTP_HOST = 'smtp.yandex.com'
BOARD = re.compile(r'(?<!\d)\d{6}(?!\d)')
TICKET = re.compile(r'(?:[А-ЯЁA-Z]{1,8}\s*)?\d[\d/-]{4,24}')
MESSAGE_ID = re.compile(r'<[^<>@\s\x00-\x1f\x7f]+@[^<>@\s\x00-\x1f\x7f]+>')
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
MAINTENANCE = re.compile(r'(?:(?:обслуживание техническое|техническое обслуживание)\s*)?'
                         r'\(?\s*(?:то[\s–—-]*(1000|2500|[12])|(отм))\s*\)?(?:\s+\d+)?(?:\s*\+\s*со)?', re.IGNORECASE)


class MailError(RuntimeError):
    pass


class ReplyPassExpired(RuntimeError):
    pass


class ImapConnectionError(MailError):
    pass


class ImapCommandError(MailError):
    pass


class UncertainDelivery(MailError):
    pass


class PermanentDeliveryError(MailError):
    pass


class InvalidRequest(ValueError):
    pass


@dataclass(frozen=True)
class OriginalMail:
    uid: bytes
    message_id: str
    received_at: datetime
    board_number: str | None = None
    uid_validity: int | None = None


@dataclass(frozen=True)
class RepairRequest:
    ticket_number: str
    board_number: str
    description: str
    recipient: str
    subject: str


def _cell(value):
    """Привести значение ячейки Excel к тексту для разбора заявки."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip() if value is not None else ''


def maintenance_kind(description):
    """Выделить и нормализовать вид планового ТО из описания."""
    match = MAINTENANCE.fullmatch(' '.join((description or '').split()))
    if match:
        return f'ТО-{match[1]}' if match[1] else 'ОТМ'
    return None


def _repair_sheet(sheet_rows):
    """Найти в строках листа номер заявки, борт и описание неисправностей."""
    rows = [tuple(map(_cell, row)) for row in sheet_rows]
    markers = {}
    for i, row in enumerate(rows):
        if any(v.casefold().startswith('технический акт') for v in row):
            break
        for j, value in enumerate(row):
            label = ' '.join(value.casefold().split()).rstrip(':')
            if label.startswith('заявка на ремонт') and '№' in value:
                markers.setdefault('ticket', (i, j))
            elif label in ('автобус', 'электробус', 'гаражный номер'):
                markers.setdefault('board', (i, j))
            elif label == 'описание неисправности':
                markers.setdefault('description', (i, j))
            elif label == 'вид ремонта':
                markers.setdefault('work_type', (i, j))
    if not {'ticket', 'board'} <= markers.keys() or not markers.keys() & {'description', 'work_type'}:
        return None

    i, j = markers['ticket']
    number = rows[i][j].split('№', 1)[1].strip() or next((v for v in rows[i][j + 1:] if v), '')
    number = number.upper()
    i, j = markers['board']
    vehicle = next((v for v in rows[i][j + 1:] if v), '')
    boards = set(BOARD.findall(vehicle))
    if not TICKET.fullmatch(number) or len(boards) != 1:
        raise InvalidRequest('Номер заявки или борта в Excel не определён однозначно.')

    description = []
    for label in ('description', 'work_type'):
        if label not in markers:
            continue
        i, j = markers[label]
        for row in rows[i + 1:]:
            if any(v.casefold() == 'заказчик' or v.casefold().startswith('технический акт') for v in row):
                break
            if j < len(row) and row[j] and (label == 'description' or maintenance_kind(row[j])):
                description.append(row[j])
        if description:
            break
    if 'description' not in markers and not description:
        return None
    text = '\n'.join(description)
    if not text or len(text.encode('utf-8')) > 32_000:
        raise InvalidRequest('В Excel нет подходящего описания неисправности.')
    return re.sub(r'\s+', '', number), boards.pop(), text


def read_repair_workbook(payload, suffix='.xlsx'):
    """Прочитать заявку из вложения XLS или XLSX с проверкой размера."""
    if not payload or len(payload) > MAX_WORKBOOK_SIZE:
        raise InvalidRequest('Размер вложения Excel превышает допустимый.')
    try:
        if suffix == '.xls':
            workbook = xlrd.open_workbook(file_contents=payload, on_demand=True)
            try:
                if workbook.nsheets > 8:
                    raise InvalidRequest('Во вложении слишком много листов.')
                requests = []
                for sheet in workbook.sheets():
                    rows = (sheet.row_values(i, 0, min(sheet.ncols, 64)) for i in range(min(sheet.nrows, 120)))
                    if parsed := _repair_sheet(rows):
                        requests.append(parsed)
            finally:
                workbook.release_resources()
        else:
            requests = _read_xlsx(payload)
    except InvalidRequest:
        raise
    except Exception as exc:
        raise InvalidRequest('Вложение не удалось прочитать как Excel.') from exc
    if len(requests) != 1:
        raise InvalidRequest('Во вложении должна быть одна заявка на ремонт.')
    return requests[0]


def _read_xlsx(payload):
    """Прочитать XLSX после проверки размера распакованного содержимого."""
    with ZipFile(BytesIO(payload)) as archive:
        entries = archive.infolist()
        if len(entries) > 300 or sum(entry.file_size for entry in entries) > 20 * 1024 * 1024:
            raise InvalidRequest('Содержимое Excel превышает допустимый размер.')
    workbook = load_workbook(BytesIO(payload), read_only=True, data_only=True, keep_links=False)
    try:
        if len(workbook.worksheets) > 8:
            raise InvalidRequest('Во вложении слишком много листов.')
        return [parsed for sheet in workbook.worksheets if (parsed := _repair_sheet(
            sheet.iter_rows(max_row=120, max_col=64, values_only=True)
        ))]
    finally:
        workbook.close()


def _message_id(message):
    """Извлечь корректный идентификатор письма из заголовков."""
    headers = message.get_all('Message-ID', [])
    value = str(headers[0]).strip() if len(headers) == 1 else ''
    return value if value.isascii() and len(value) <= 512 and MESSAGE_ID.fullmatch(value) else None


def _subject_board(subject):
    """Найти номер борта в теме письма."""
    if len(subject) > 500 or '\n' in subject or '\r' in subject:
        return None
    boards = set(BOARD.findall(subject))
    return boards.pop() if len(boards) == 1 else None


def read_repair_message(raw, original):
    """Извлечь из письма заявку Excel и адрес для ответа."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    subject = str(message.get('Subject', '')).strip()
    board = _subject_board(subject)
    if (_message_id(message) != original.message_id or not board
            or (original.board_number is not None and original.board_number != board)):
        raise InvalidRequest('Тема письма или его идентификатор не подходят для заявки.')
    attachments = []
    parts = (message,) if message.get_filename() else message.iter_attachments()
    for part in parts:
        name = PurePath(part.get_filename() or '')
        if name.suffix.lower() in ('.xls', '.xlsx') and set(BOARD.findall(name.stem)) == {board}:
            attachments.append((part, name.suffix.lower()))
    if len(attachments) != 1:
        raise InvalidRequest('Нужно одно вложение .xls или .xlsx с номером борта из темы.')
    attachment, suffix = attachments[0]
    ticket, sheet_board, description = read_repair_workbook(attachment.get_payload(decode=True), suffix)
    if sheet_board != board:
        raise InvalidRequest('Борт в письме и борт в Excel различаются.')

    address_headers = message.get_all('Reply-To') or message.get_all('From') or []
    addresses = getattr(address_headers[0], 'addresses', ()) if len(address_headers) == 1 else ()
    if len(addresses) != 1 or not addresses[0].username or not addresses[0].domain:
        raise InvalidRequest('Адрес для ответа не определён однозначно.')
    recipient = addresses[0].addr_spec
    if not recipient.isascii() or len(recipient) > 254 or any(ord(c) < 32 for c in recipient):
        raise InvalidRequest('Письмо содержит неподдерживаемый адрес для ответа.')
    return RepairRequest(ticket, board, description, recipient, subject)


def _quote(value):
    """Экранировать строковый аргумент для IMAP."""
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _imap_folder(value):
    # RFC 3501: имена папок вне ASCII в IMAP4rev1 используют modified UTF-7.
    """Преобразовать имя папки в формат IMAP modified UTF-7."""
    def encode(match):
        """Закодировать не-ASCII часть имени папки для IMAP."""
        chars = match[0]
        if chars == '&':
            return '&-'
        return '&' + base64.b64encode(chars.encode('utf-16-be')).decode().rstrip('=').replace('/', ',') + '-'

    return _quote(re.sub(r'&|[^ -~]+', encode, value))


def _imap_date(day):
    """Преобразовать дату в формат поиска IMAP."""
    return f'{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}'


def _reply_parent(headers):
    """Извлечь идентификатор письма, на которое был дан ответ."""
    direct = MESSAGE_ID.findall(str(headers.get('In-Reply-To', '')))
    references = MESSAGE_ID.findall(str(headers.get('References', '')))
    return set(direct or references[-1:])


def build_reply(sender, recipient, subject, original_id, reply_id, completed_work, *, sent_at=None):
    """Сформировать ответ с текстом работ и заголовками исходной переписки."""
    reply = EmailMessage()
    reply['From'] = sender
    reply['To'] = recipient
    reply['Subject'] = subject if subject.lower().startswith('re:') else f'Re: {subject}'
    reply['Message-ID'] = reply_id
    reply['In-Reply-To'] = original_id
    reply['References'] = original_id
    reply['Date'] = format_datetime(sent_at or datetime.now(timezone.utc))
    reply.set_content(completed_work)
    return reply


def _connection_error_code(error):
    # Ответы серверов могут содержать адреса и другие данные учётной записи.
    """Подготовить краткое описание обрыва для журнала."""
    if isinstance(error, imaplib.IMAP4.error) and '[AUTHENTICATIONFAILED]' in str(error).upper():
        return 'AUTHENTICATIONFAILED'
    if isinstance(error, smtplib.SMTPResponseException):
        return str(error.smtp_code)
    return type(error).__name__


class YandexMailbox:
    def __init__(self, address, password, sent_folder=None, *, oauth_token=None, logger=None):
        """Настроить адрес, режим авторизации и параметры соединений."""
        login, _, domain = address.partition('@')
        self.username = login if domain in ('yandex.ru', 'yandex.com', 'ya.ru') else address
        self.password = password
        self.oauth_payload = f'user={address}\x01auth=Bearer {oauth_token}\x01\x01' if oauth_token else None
        self.logger = logger if logger is not None else logging.getLogger(__name__)
        self.sent_folder = _imap_folder(sent_folder) if sent_folder else None
        self.selected = None
        self.selected_uid_validity = None
        self.original_uid_hints = {}
        self.sent_parents_since = None
        self.smtp = None
        self.smtp_failed = False
        self.reply_deadline = None

    def __enter__(self):
        """Подключиться к почте по IMAP и войти выбранным способом."""
        self.selected = None
        self.selected_uid_validity = None
        self.original_uid_hints.clear()
        self.sent_parents_since = None
        host = OAUTH_IMAP_HOST if self.oauth_payload else IMAP_HOST
        method = 'XOAUTH2' if self.oauth_payload else 'LOGIN'
        try:
            self.imap = imaplib.IMAP4_SSL(host, IMAP_PORT,
                                         ssl_context=ssl.create_default_context(cafile=ca_bundle()), timeout=self._imap_timeout())
        except (imaplib.IMAP4.error, OSError) as exc:
            error_type = ImapConnectionError if isinstance(exc, (imaplib.IMAP4.abort, OSError)) else MailError
            raise error_type(f'IMAP {host}:{IMAP_PORT}, сбой подключения ({_connection_error_code(exc)}).') from None
        try:
            if self.oauth_payload:
                self.imap.authenticate('XOAUTH2', lambda challenge: b'' if challenge else self.oauth_payload.encode('ascii'))
            else:
                self.imap.login(self.username, self.password)
        except (imaplib.IMAP4.error, OSError) as exc:
            self.__exit__()
            error_type = ImapConnectionError if isinstance(exc, (imaplib.IMAP4.abort, OSError)) else MailError
            raise error_type(f'IMAP {host}:{IMAP_PORT}, сбой авторизации {method} ({_connection_error_code(exc)}).') from None
        return self

    def __exit__(self, *_):
        """Закрыть IMAP и SMTP после обработки."""
        self._close_smtp()
        with suppress(imaplib.IMAP4.error, OSError):
            if self.reply_deadline is not None:
                self.imap.shutdown()
            else:
                self.imap.logout()

    def _imap_timeout(self):
        """Определить допустимое время операции IMAP в текущем проходе."""
        if self.reply_deadline is None:
            return 10
        remaining = self.reply_deadline - monotonic()
        if remaining <= 0:
            raise ReplyPassExpired
        return min(10, remaining)

    def _imap_call(self, operation, method, *args, **kwargs):
        """Выполнить команду IMAP и оформить ошибку с контекстом команды."""
        if self.reply_deadline is not None:
            self.imap.sock.settimeout(self._imap_timeout())
        try:
            return method(*args, **kwargs)
        except (imaplib.IMAP4.abort, OSError) as exc:
            reason = _connection_error_code(exc)
            network_error = exc
            if isinstance(exc, imaplib.IMAP4.abort):
                responses = getattr(self.imap, 'untagged_responses', None)
                if isinstance(responses, dict) and responses.get('BYE'):
                    reason += ', сервер прислал BYE'
                else:
                    # imaplib может повторно обернуть сетевую ошибку в abort.
                    for _ in range(4):
                        if str(network_error) == 'socket error: EOF':
                            reason += ', EOF от соединения'
                            break
                        if str(network_error).startswith('socket error: unterminated line:'):
                            reason += ', неполный ответ сервера'
                            break
                        underlying = network_error.__cause__ or network_error.__context__
                        if isinstance(underlying, OSError):
                            network_error = underlying
                            reason += f', {type(network_error).__name__}'
                            break
                        if not isinstance(underlying, imaplib.IMAP4.abort):
                            break
                        network_error = underlying
            if isinstance(network_error, OSError):
                if isinstance(network_error.errno, int):
                    reason += f', errno={network_error.errno}'
                winerror = getattr(network_error, 'winerror', None)
                if isinstance(winerror, int):
                    reason += f', winerror={winerror}'
            raise ImapConnectionError(f'IMAP {operation}: соединение прервано ({reason}).') from None
        except imaplib.IMAP4.error:
            raise ImapCommandError(f'Яндекс Почта отклонила команду IMAP {operation}.') from None

    def _select(self, wire_folder, readonly=True):
        """Открыть папку и получить её UIDVALIDITY для проверки сохранённых UID."""
        if self.selected != (wire_folder, readonly):
            self.selected = None
            status, _ = self._imap_call('EXAMINE' if readonly else 'SELECT', self.imap.select, wire_folder, readonly=readonly)
            if status != 'OK':
                raise ImapCommandError('Папка Яндекс Почты не доступна по IMAP.')
            response = self.imap.response('UIDVALIDITY')
            values = response[1] if isinstance(response, tuple) else None
            value = values[-1] if values else None
            self.selected_uid_validity = int(value) if isinstance(value, bytes) and value.isdigit() else None
            self.selected = wire_folder, readonly

    def _sent_folder(self):
        """Найти папку отправленных по настройке или служебному атрибуту IMAP."""
        if self.sent_folder is None:
            status, lines = self._imap_call('LIST', self.imap.list, '""', '"*"')
            folders = {}
            if status == 'OK':
                for line in lines:
                    if not isinstance(line, bytes):
                        continue
                    match = re.fullmatch(rb'\(([^)]*)\) (?:NIL|"[^"]*") (.+)', line)
                    if match and b'\\noselect' not in match[1].lower().split():
                        name = match[2].decode('ascii')
                        folders[name.strip('"')] = (name, b'\\sent' in match[1].lower().split())
            sent = [name for name, special in folders.values() if special]
            if not sent:
                sent = [folders[name][0] for name in ('Sent', _imap_folder('Отправленные')[1:-1]) if name in folders]
            if len(sent) != 1:
                raise MailError('Не найдена папка отправленных писем. Укажите YANDEX_MAIL_SENT_FOLDER.')
            self.sent_folder = sent[0]
        return self.sent_folder

    def _uid(self, command, *args):
        """Выполнить IMAP-команду по UID письма."""
        status, values = self._imap_call(f'UID {command}', self.imap.uid, command, *args)
        if status != 'OK':
            raise ImapCommandError(f'Яндекс Почта не выполнила команду IMAP {command}.')
        return values

    def _search(self, *args):
        """Найти письма по критериям IMAP с обработкой ошибок."""
        started = monotonic()
        criteria = ', '.join(term for term in ('SUBJECT', 'HEADER', 'SINCE', 'BEFORE', 'UNANSWERED') if term in args)
        try:
            values = self._uid('SEARCH', None, *args)
        except (ImapConnectionError, ImapCommandError) as exc:
            raise type(exc)(f'{exc} SEARCH: {criteria or "общий"}, команда={monotonic() - started:.2f} с.') from None
        uids = values[0].split() if values and values[0] else []
        elapsed = monotonic() - started
        if elapsed >= 2:
            self.logger.info('yandex-mail: медленный IMAP UID SEARCH, фильтры=%s, найдено=%s, команда=%.2f с',
                             criteria or 'общий', len(uids), elapsed)
        return uids

    def _fetch(self, uids, fields, *, purpose):
        """Считать заголовки или содержимое письма пакетами по UID."""
        batch_size = 64
        for start in range(0, len(uids), batch_size):
            batch = uids[start:start + batch_size]
            started = monotonic()
            try:
                values = self._uid('FETCH', b','.join(batch), f'({fields})')
            except (ImapConnectionError, ImapCommandError) as exc:
                raise type(exc)(
                    f'{exc} FETCH: {purpose}, писем в пакете={len(batch)}, '
                    f'обработано до сбоя={start}/{len(uids)}, команда={monotonic() - started:.2f} с.'
                ) from None
            literal = None
            for value in values:
                if literal is not None:
                    info, body = literal
                    if isinstance(value, bytes) and value.startswith((b' ', b')')):
                        yield info + value, body
                        literal = None
                        continue
                    yield info, body
                literal = value if isinstance(value, tuple) else None
            if literal is not None:
                yield literal

    def unanswered(self, folder, start_at, *, board_number=None):
        """Выбрать входящие письма без ответа по периоду и при необходимости по борту."""
        inbox = folder == INBOX
        wire_folder = _imap_folder(folder)
        try:
            self._select(wire_folder)
        except ImapCommandError:
            raise MailError(f'Папка «{folder}» недоступна по IMAP.') from None
        search = ['UNANSWERED', 'UNDELETED']
        if start_at is not None:
            if not isinstance(start_at, datetime):
                start_at = datetime.combine(start_at, time.min)
            start_at = start_at.astimezone(MOSCOW) if start_at.tzinfo else start_at.replace(tzinfo=MOSCOW)
            day = start_at.date()
            # IMAP отбирает по дню, точное время сверяем по INTERNALDATE ниже.
            search_day = day - timedelta(days=1) if day > date.min else day
            search.extend(('SINCE', _imap_date(search_day)))
        uids = self._search(*search)
        headers = 'MESSAGE-ID SUBJECT' if inbox or board_number else 'MESSAGE-ID'
        fields = f'UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({headers})]'
        for start in range(0, len(uids), 64):
            self._select(wire_folder)
            originals = []
            for flags, header in self._fetch(uids[start:start + 64], fields, purpose='заголовки входящих'):
                uid = re.search(rb'\bUID (\d+)', flags)
                stamp = re.search(rb'\bINTERNALDATE "([^"]+)"', flags)
                size = re.search(rb'\bRFC822\.SIZE (\d+)', flags)
                mail_flags = {flag.lower() for flag in imaplib.ParseFlags(flags)}
                if not uid or not stamp or not size or int(size[1]) > MAX_MESSAGE_SIZE or mail_flags & {b'\\answered', b'\\deleted'}:
                    continue
                received_at = parsedate_to_datetime(stamp[1].decode('ascii')).astimezone(MOSCOW)
                if start_at is not None and received_at < start_at:
                    continue
                message = BytesParser(policy=policy.default).parsebytes(header, headersonly=True)
                message_id = _message_id(message)
                board = _subject_board(str(message.get('Subject', '')).strip()) if inbox or board_number else None
                if message_id and (not inbox or board) and (not board_number or board == board_number):
                    originals.append(OriginalMail(uid[1], message_id, received_at, board, self.selected_uid_validity))
            yield originals

    def download(self, folder, original):
        """Загрузить исходное письмо из выбранной папки."""
        self._select(_imap_folder(folder))
        values = list(self._fetch([original.uid], f'BODY.PEEK[]<0.{MAX_MESSAGE_SIZE + 1}>',
                                  purpose='чтение письма с вложением'))
        if len(values) != 1 or len(values[0][1]) > MAX_MESSAGE_SIZE:
            raise InvalidRequest('Письмо с вложением недоступно или слишком большое.')
        return values[0][1]

    def has_reply(self, message_id, received_at):
        """Проверить, есть ли в отправленных ответ на исходное письмо."""
        received_day = received_at.date()
        if received_at.tzinfo is not None:
            received_day = received_at.astimezone(MOSCOW).date()
        return message_id in self.reply_parents_since(received_day)

    def reply_parents_since(self, received_day):
        """Собрать из отправленных идентификаторы писем, на которые уже ответили."""
        cached = self.sent_parents_since
        if cached is not None and received_day >= cached[0]:
            return cached[1]
        self._select(self._sent_folder())
        search_day = received_day - timedelta(days=1) if received_day > date.min else received_day
        # Яндекс может зависнуть на SEARCH по заголовкам. Ищем по дате
        # и сверяем заголовки ответов локально, без загрузки тела писем.
        uids = self._search('SINCE', _imap_date(search_day))
        parents = set()
        for flags, raw in self._fetch(uids, 'UID FLAGS BODY.PEEK[HEADER.FIELDS (IN-REPLY-TO REFERENCES)]',
                                      purpose='заголовки отправленных ответов'):
            if b'\\deleted' not in {flag.lower() for flag in imaplib.ParseFlags(flags)}:
                headers = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
                parents.update(_reply_parent(headers))
        self.sent_parents_since = received_day, parents
        return parents

    def _messages_with_id(self, message_id, day, *, purpose):
        """Найти письмо по точному Message-ID с учётом даты."""
        try:
            uids = self._search('UNDELETED', 'HEADER', 'MESSAGE-ID', _quote(message_id))
        except ImapCommandError:
            # Яндекс иногда отклоняет поиск по заголовку. Берём соседние даты IMAP
            # и проверяем точный Message-ID по заголовкам, без загрузки писем.
            uids = self._search('UNDELETED', 'SINCE', _imap_date(day - timedelta(days=1)),
                                'BEFORE', _imap_date(day + timedelta(days=2)))
        yield from self._matching_message_headers(uids, message_id, purpose)

    def _matching_message_headers(self, uids, message_id, purpose):
        """Отобрать UID по точному совпадению Message-ID."""
        for info, raw in self._fetch(uids, 'UID FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]', purpose=purpose):
            uid = re.search(rb'\bUID (\d+)', info)
            flags = {flag.lower() for flag in imaplib.ParseFlags(info)}
            if uid and b'\\deleted' not in flags and _message_id(BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)) == message_id:
                yield uid[1], b'\\answered' in flags

    def _original(self, folder, message_id, received_at, readonly=True, *, uid_hint=None):
        """Найти исходное письмо по сохранённому UID или через поиск."""
        wire_folder = _imap_folder(folder)
        self._select(wire_folder, readonly=readonly)
        key = (wire_folder, message_id)
        hint = self.original_uid_hints.get(key) or uid_hint
        if hint and hint[0] == self.selected_uid_validity:
            uid = str(hint[1]).encode('ascii')
            try:
                matches = list(self._matching_message_headers([uid], message_id, 'исходное письмо по UID'))
            except ImapCommandError:
                matches = []
            if len(matches) == 1 and matches[0][0] == uid:
                self.original_uid_hints[key] = hint
                return matches[0]
            self.original_uid_hints.pop(key, None)

        matches = list(self._messages_with_id(message_id, received_at.date(), purpose='исходное письмо'))
        if len(matches) != 1:
            return None
        if self.selected_uid_validity is not None:
            self.original_uid_hints[key] = self.selected_uid_validity, int(matches[0][0])
        return matches[0]

    def original_answered(self, folder, message_id, received_at, *, uid_hint=None):
        """Узнать, установлен ли флаг ответа у исходного письма."""
        found = self._original(folder, message_id, received_at, uid_hint=uid_hint)
        # None означает, что нельзя подтвердить наличие исходного письма в папке.
        return found[1] if found else None

    def mark_answered(self, folder, message_id, received_at, *, uid_hint=None):
        """Отметить исходное письмо отвеченным и важным после отправки."""
        found = self._original(folder, message_id, received_at, readonly=False, uid_hint=uid_hint)
        if found is None:
            raise MailError('Исходное письмо не найдено однозначно для отметки об ответе.')
        self._uid('STORE', found[0], '+FLAGS.SILENT', r'(\Answered \Flagged)')

    def _close_smtp(self):
        """Закрыть SMTP-соединение, если оно открыто."""
        if self.smtp is not None:
            self.smtp.close()
            self.smtp = None

    def send_reply(self, reply):
        """Отправить ответ по SMTP и различить отказ и неопределённую доставку."""
        if self.smtp_failed:
            raise MailError('Подключение SMTP завершилось ошибкой в текущем обходе.')
        if self.smtp is None:
            host = OAUTH_SMTP_HOST if self.oauth_payload else SMTP_HOST
            stage = 'подключения'
            try:
                self.smtp = smtplib.SMTP_SSL(host, SMTP_PORT,
                                            context=ssl.create_default_context(cafile=ca_bundle()), timeout=10)
                stage = 'авторизации'
                if self.oauth_payload:
                    self.smtp.ehlo_or_helo_if_needed()
                    self.smtp.auth('XOAUTH2', lambda challenge=None: self.oauth_payload if challenge is None else '')
                else:
                    self.smtp.login(self.username, self.password)
            except OSError as exc:
                self._close_smtp()
                self.smtp_failed = True
                error = f'SMTP {host}:{SMTP_PORT}, сбой {stage} ({_connection_error_code(exc)}).'
                self.logger.warning('yandex-mail: %s', error)
                raise MailError(error) from None
            self.logger.info('yandex-mail: вход SMTP выполнен, сервер=%s:%s', host, SMTP_PORT)
        try:
            self.smtp.send_message(reply)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPResponseException) as exc:
            codes = ([code for code, _ in exc.recipients.values()] if isinstance(exc, smtplib.SMTPRecipientsRefused)
                     else [exc.smtp_code])
            permanent = codes and all(500 <= code < 600 for code in codes)
            error_type = PermanentDeliveryError if permanent else MailError
            code_text = ', '.join(str(code) for code in sorted(set(codes)))
            raise error_type(f'SMTP отклонил ответ ({code_text}).') from None
        except OSError as exc:
            self._close_smtp()
            self.logger.warning('yandex-mail: отправка SMTP не подтверждена (%s)', _connection_error_code(exc))
            raise UncertainDelivery('SMTP не подтвердил отправку. Проверьте папку отправленных писем.') from None

    def save_sent_copy(self, reply):
        # SMTP может сам сохранить копию. Проверяем точный Message-ID.
        """Сохранить копию отправленного ответа в папке отправленных."""
        message_id = str(reply['Message-ID'])
        if self.has_sent_copy(message_id, reply['Date'].datetime):
            return
        sent_folder = self._sent_folder()
        status, _ = self._imap_call('APPEND', self.imap.append, sent_folder, r'(\Seen)', reply['Date'].datetime,
                                    reply.as_bytes(policy=policy.SMTP))
        if status != 'OK':
            raise MailError('Ответ отправлен, но копию не удалось записать в папку отправленных.')
        if self.sent_parents_since is not None:
            self.sent_parents_since[1].update(_reply_parent(reply))

    def has_sent_copy(self, message_id, sent_at):
        """Проверить, сохранена ли копия конкретного ответа."""
        self._select(self._sent_folder())
        return any(self._messages_with_id(message_id, sent_at.astimezone(MOSCOW).date(),
                                          purpose='копия отправленного ответа'))


# --- Авторизация OAuth и хранение токенов -----------------------------------

_WINDOWS = os.name == 'nt'
OAUTH_BASE = 'https://oauth.yandex.ru'
DEVICE_PAGE = f'{OAUTH_BASE}/device'
ACCOUNT_INFO_URL = 'https://login.yandex.ru/info'
MAIL_SCOPES = {'mail:imap_full', 'mail:smtp'}
OAUTH_ERRORS = {
    'access_denied', 'authorization_pending', 'bad_verification_code', 'expired_token',
    'invalid_client', 'invalid_grant', 'invalid_request', 'invalid_scope', 'slow_down',
    'temporarily_unavailable', 'unauthorized_client', 'unsupported_grant_type',
}


class OAuthError(MailError):
    def __init__(self, code):
        """Сохранить код отказа Яндекс OAuth для обработки повторов."""
        super().__init__(f'Яндекс OAuth: {code}.')
        self.code = code


class OAuthConnectionError(MailError):
    pass


def _post(endpoint, params, *, auth=None):
    """Выполнить запрос OAuth и вернуть проверенный ответ."""
    try:
        response = requests.post(f'{OAUTH_BASE}/{endpoint}', data=params, auth=auth,
                                 timeout=(5, 10), allow_redirects=False)
    except requests.RequestException as exc:
        raise OAuthConnectionError(f'Не удалось связаться с Яндекс OAuth ({type(exc).__name__}).') from None
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or not 200 <= response.status_code < 300 or 'error' in data:
        code = data.get('error') if isinstance(data, dict) else None
        if not isinstance(code, str) or code not in OAUTH_ERRORS:
            code = f'HTTP {response.status_code}'
        raise OAuthError(code)
    return data


def _valid_secret(value):
    """Проверить формат токена или секрета перед использованием."""
    return isinstance(value, str) and bool(value) and all(32 < ord(char) < 127 for char in value)


def _token_values(data):
    """Проверить полученные токены, разрешения и срок обновления."""
    access = data.get('access_token')
    refresh = data.get('refresh_token')
    lifetime = data.get('expires_in')
    scope = data.get('scope')
    if not (_valid_secret(access) and _valid_secret(refresh) and isinstance(lifetime, int) and lifetime > 0):
        raise MailError('Яндекс OAuth вернул неполные данные токена.')
    if scope is not None and (not isinstance(scope, str) or not MAIL_SCOPES <= set(scope.split())):
        raise MailError('Яндекс не выдал все права на чтение и отправку почты.')
    return dict(access_token=access, refresh_token=refresh,
                refresh_at=int(unix_time()) + min(max(0, lifetime - 60), 90 * 86400))


class YandexMailOAuth:
    def __init__(self, address):
        """Определить файл токенов и ожидаемый почтовый аккаунт."""
        self.address = address
        default_path = Path.home() / '.config' / 'setrans' / 'yandex-mail-oauth.json'
        self.path = Path(os.getenv('YANDEX_MAIL_OAUTH_FILE') or default_path).expanduser()

    @contextmanager
    def _locked(self):
        """Заблокировать хранилище токенов между процессами на Windows или Unix."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + '.lock')
        with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), 'r+b') as lock:
            if _WINDOWS:
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX)
                yield

    def _write(self, data, *, path=None):
        """Атомарно записать данные OAuth в локальный файл."""
        path = path or self.path
        temp = NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False)
        temp_path = Path(temp.name)
        try:
            with temp:
                json.dump(data, temp)
                temp.flush()
                os.fsync(temp.fileno())
            os.replace(temp_path, path)
        finally:
            with suppress(FileNotFoundError):
                temp_path.unlink()

    def _device_id(self):
        """Прочитать или создать постоянный ID для данного хранилища OAuth."""
        path = self.path.with_suffix(self.path.suffix + '.device.json')
        try:
            with self._locked():
                try:
                    value = json.loads(path.read_text(encoding='utf-8'))
                    if isinstance(value, str):
                        return UUID(value).hex
                except (FileNotFoundError, ValueError):
                    pass
                device_id = uuid4().hex
                self._write(device_id, path=path)
                return device_id
        except OSError as exc:
            raise MailError(f'Не удалось сохранить идентификатор устройства Яндекс OAuth ({type(exc).__name__}).') from None

    def _verify_account(self, access_token):
        """Проверить, что токен относится к настроенному почтовому ящику."""
        try:
            response = requests.get(ACCOUNT_INFO_URL, headers={'Authorization': f'OAuth {access_token}'},
                                    timeout=(5, 10), allow_redirects=False)
            data = response.json() if response.status_code == 200 else None
        except (requests.RequestException, ValueError):
            data = None
        login = data.get('login') if isinstance(data, dict) else None
        if not isinstance(login, str) or not login:
            raise MailError('Не удалось проверить владельца OAuth-токена в Яндекс ID.')
        address = self.address.casefold()
        username, _, domain = address.partition('@')
        expected_login = username if domain in ('yandex.ru', 'yandex.com', 'ya.ru') else address
        if login.casefold() not in (address, expected_login):
            raise MailError('OAuth-токен выдан для другого аккаунта Яндекса. Запустите yandex-mail-authorize и выберите нужный ящик.')

    def access_token(self):
        """Получить действующий токен доступа, обновив его при необходимости."""
        try:
            with self._locked():
                with self.path.open(encoding='utf-8') as file:
                    if not _WINDOWS and os.fstat(file.fileno()).st_mode & 0o077:
                        raise MailError('Установите права 600 на файл OAuth для Яндекс Почты.')
                    values = json.load(file)
                required = ('client_id', 'client_secret', 'access_token', 'refresh_token')
                if (not isinstance(values, dict) or values.get('address') != self.address
                        or not all(_valid_secret(values.get(key)) for key in required)
                        or not isinstance(values.get('refresh_at'), int)):
                    raise MailError('Файл OAuth не подходит для этого ящика. Запустите yandex-mail-authorize.')
                if values['refresh_at'] <= unix_time():
                    payload = _post('token', {'grant_type': 'refresh_token', 'refresh_token': values['refresh_token']},
                                    auth=(values['client_id'], values['client_secret']))
                    values.update(_token_values(payload))
                    self._write(values)
                if values.get('account_verified') is not True:
                    self._verify_account(values['access_token'])
                    values['account_verified'] = True
                    self._write(values)
                return values['access_token']
        except (FileNotFoundError, ValueError):
            raise MailError('Файл OAuth отсутствует или повреждён. Запустите yandex-mail-authorize.') from None
        except OSError as exc:
            raise MailError(f'Файл OAuth недоступен ({type(exc).__name__}).') from None

    def authorize(self, client_id, client_secret, show_code, logger):
        """Получить токены через подтверждение одноразовым кодом и проверить IMAP."""
        if not (_valid_secret(client_id) and _valid_secret(client_secret)):
            raise MailError('Укажите Client ID и Client Secret из карточки приложения.')
        device_name = (os.getenv('YANDEX_MAIL_OAUTH_DEVICE_NAME') or '').strip() or f'Сетранс · {socket.gethostname()}'
        device = _post('device/code', {
            'client_id': client_id,
            'scope': ' '.join(sorted(MAIL_SCOPES)),
            'device_id': self._device_id(),
            'device_name': device_name[:100],
        })
        code = device.get('device_code')
        user_code = device.get('user_code')
        interval = device.get('interval', 5)
        lifetime = device.get('expires_in')
        if not (_valid_secret(code) and _valid_secret(user_code) and isinstance(interval, int) and interval > 0
                and isinstance(lifetime, int) and lifetime > 0):
            raise MailError('Яндекс OAuth вернул неполные данные для подтверждения.')
        deadline = monotonic() + lifetime
        show_code(DEVICE_PAGE, user_code)
        while monotonic() + interval < deadline:
            sleep(interval)
            try:
                payload = _post('token', {'grant_type': 'device_code', 'code': code}, auth=(client_id, client_secret))
            except OAuthConnectionError:
                interval *= 2
                continue
            except OAuthError as exc:
                if exc.code in ('slow_down', 'temporarily_unavailable'):
                    interval += 5
                    continue
                if exc.code == 'authorization_pending':
                    continue
                raise
            values = _token_values(payload)
            self._verify_account(values['access_token'])
            with YandexMailbox(self.address, None, oauth_token=values['access_token'], logger=logger):
                pass  # Проверяем доступ к IMAP после проверки аккаунта в Яндекс ID.
            values.update(address=self.address, account_verified=True, client_id=client_id, client_secret=client_secret)
            try:
                with self._locked():
                    self._write(values)
            except OSError as exc:
                raise MailError(f'Не удалось сохранить файл OAuth ({type(exc).__name__}).') from None
            return
        raise MailError('Время подтверждения Яндекс OAuth истекло. Запустите yandex-mail-authorize снова.')


# --- Импорт в Сетранс, отложенная отправка и повторы --------------------------

# Для других парков добавьте пары «парк Сетранс — папка IMAP» в YANDEX_MAIL_FOLDERS.
PILOT_FOLDERS = {'14 парк': INBOX}
REPLY_PASS_SECONDS = 45
RETRY_MINUTES = (1, 5, 15)
SENT_STEPS = ('sent_copy', 'sent_mark')
MAINTENANCE_LOOKBACK = timedelta(days=1)
REPAIR_REPLY_LOOKBACK = timedelta(days=7)


def utcnow():
    """Вернуть время UTC без часового пояса для записей фоновых процессов."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _error_summary(exc):
    """Подготовить краткое сообщение о сбое для журнала."""
    if isinstance(exc, MailError):
        return str(exc)
    error = type(exc).__name__
    if (isinstance(exc, ModuleNotFoundError) and isinstance(exc.name, str)
            and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]{0,127}', exc.name)):
        return f'{error} ({exc.name})'
    return error


class YandexRequests:
    def __init__(self, app, db, Task, Car, Elcar):
        """Подключить модель, пул отправки, команды Flask и функцию для шаблонов."""
        self.app, self.db, self.Task = app, db, Task
        self.vehicle_models = (Car, Elcar)
        self.reply_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='yandex-mail-reply')
        self.reply_slots = BoundedSemaphore(8)

        class MailRequest(db.Model):
            __tablename__ = 'yandex_mail_request'
            __table_args__ = (
                db.UniqueConstraint('mailbox', 'park', 'ticket_number', name='uq_yandex_mail_ticket'),
                db.Index('ix_yandex_mail_replies', 'reply_state', 'reply_attempt_at'),
                db.Index('ix_yandex_mail_retry', 'reply_state', 'reply_next_at'),
                {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'},
            )

            task_id = db.Column(db.Integer, nullable=False, index=True)
            mailbox = db.Column(db.String(254), nullable=False)
            park = db.Column(db.String(50), nullable=False)
            board_number = db.Column(db.String(20), nullable=False)
            ticket_number = db.Column(db.String(40), nullable=False)
            folder = db.Column(db.String(255), nullable=False)
            message_key = db.Column(db.String(64), primary_key=True)
            message_id = db.Column(db.String(512), nullable=False)
            recipient = db.Column(db.String(254), nullable=False)
            subject = db.Column(db.String(500), nullable=False)
            received_at = db.Column(db.DateTime, nullable=False)
            imap_uid = db.Column(db.BigInteger)
            imap_uid_validity = db.Column(db.BigInteger)
            revision_received_at = db.Column(db.DateTime)
            revision_message_key = db.Column(db.String(64), index=True)
            reply_state = db.Column(db.String(16), nullable=False, default='open')
            reply_text = db.Column(db.Text)
            reply_attempt_at = db.Column(db.DateTime)
            reply_sent_at = db.Column(db.DateTime)
            reply_error = db.Column(db.String(255))
            reply_retry_count = db.Column(db.Integer, nullable=False, server_default='0')
            reply_next_at = db.Column(db.DateTime)

            @property
            def original_uid_hint(self):
                """Вернуть сохранённые UID и UIDVALIDITY для исходного письма."""
                if self.imap_uid_validity is not None and self.imap_uid is not None:
                    return self.imap_uid_validity, self.imap_uid
                return None


        self.model = MailRequest
        self._register_commands()
        app.add_template_global(self.link_for, 'yandex_request_for')

    def stage_after_closure(self, task, queued, *, paused, is_reject):
        """В той же транзакции подготовить ручную отписку или поиск письма."""
        if paused:
            task.yandex_reply_lookup = False
            for link in queued or ():
                self.outbox.stage_mail_closure(
                    task, task.completed_at, task.completed_work, is_reject, link.ticket_number,
                )
        elif not queued:
            self.queue_reply_lookup(task)
        return task.yandex_reply_lookup

    def notify_closed(self, task_id, board_number, queued, lookup_queued, *, paused):
        """После коммита запустить почтовый ответ и сообщить о состоянии очереди."""
        if queued:
            if paused:
                flash('Автоотправка в Яндекс Почту приостановлена. Письмо сохранено в «К отписке» для ручного ответа.', 'success')
            else:
                self.submit_reply(task_id, board_number)
                flash(f'Письма Яндекс Почты в очереди на ответ: {len(queued)}.', 'success')
        elif lookup_queued:
            self.submit_reply(task_id, board_number)

    def run_cycle(self):
        """Сначала обработать ответы, затем импортировать новые письма."""
        self.run(replies_only=True)
        return self.run(import_only=True)

    def register_scheduler_jobs(self, add_job, run_locked_job, *, minute='*/1'):
        """Зарегистрировать общий цикл почты под блокировкой планировщика."""
        if self.enabled():
            add_job(
                'yandex_mail_sync',
                lambda: run_locked_job('yandex_mail_sync', self.run_cycle, 'yandex_mail'),
                CronTrigger(minute=minute, timezone=MOSCOW),
            )

    @staticmethod
    def enabled():
        """Проверить, включена ли почтовая интеграция в настройках."""
        return os.getenv('YANDEX_MAIL_ENABLED') == '1'

    @staticmethod
    def address():
        """Прочитать и проверить настроенный адрес почты."""
        address = os.getenv('YANDEX_MAIL_ADDRESS', '').strip().lower()
        if not re.fullmatch(r'[a-z0-9_.+-]+@[a-z0-9.-]+\.[a-z]{2,}', address):
            raise MailError('Укажите YANDEX_MAIL_ADDRESS в .env.')
        return address

    def _auth_settings(self):
        """Выбрать OAuth или пароль приложения для подключения."""
        address = self.address()
        auth = os.getenv('YANDEX_MAIL_AUTH', 'password').strip().lower()
        if auth not in ('password', 'oauth'):
            raise MailError('Укажите YANDEX_MAIL_AUTH=password или YANDEX_MAIL_AUTH=oauth.')
        return address, auth

    def _mailbox(self, address, auth):
        """Создать почтовый клиент с выбранным способом входа."""
        password = oauth_token = None
        if auth == 'oauth':
            oauth_token = YandexMailOAuth(address).access_token()
        else:
            password = os.getenv('YANDEX_MAIL_APP_PASSWORD', '').strip()
            if not password:
                raise MailError('Укажите YANDEX_MAIL_APP_PASSWORD в .env.')
        return YandexMailbox(address, password, os.getenv('YANDEX_MAIL_SENT_FOLDER'),
                             oauth_token=oauth_token, logger=self.app.logger)

    @staticmethod
    def folders():
        """Прочитать соответствие парков и папок IMAP."""
        try:
            folders = json.loads(os.getenv('YANDEX_MAIL_FOLDERS', json.dumps(PILOT_FOLDERS)))
        except ValueError:
            raise MailError('Проверьте формат YANDEX_MAIL_FOLDERS.') from None
        if not isinstance(folders, dict) or not folders or any(
            not park or len(park) > 50 or not isinstance(folder, str) or not folder.strip()
            or len(folder) > 255 or any(ord(c) < 32 for c in folder)
            for park, folder in folders.items()
        ):
            raise MailError('YANDEX_MAIL_FOLDERS должен содержать отдельную папку IMAP для каждого парка.')
        folders = {park: INBOX if folder.strip().upper() == INBOX else folder for park, folder in folders.items()}
        if len(set(folders.values())) != len(folders):
            raise MailError('У парков должны быть разные папки IMAP.')
        return folders

    @staticmethod
    def _import_start():
        """Прочитать начальную дату и время импорта писем."""
        start_value = os.getenv('YANDEX_MAIL_IMPORT_FROM', '').strip()
        try:
            return datetime.fromisoformat(start_value) if start_value else None
        except ValueError:
            raise MailError('Проверьте формат YANDEX_MAIL_IMPORT_FROM (ГГГГ-ММ-ДДTЧЧ:ММ:СС).') from None

    def settings(self):
        """Собрать настройки подключения, папок и периода импорта."""
        address, auth = self._auth_settings()
        return address, auth, self.folders(), self._import_start()

    def _eligible_task(self, task):
        """Проверить, участвует ли заявка этого парка в почтовой обработке."""
        return (self.enabled() or task.author_open == MAIL_TASK_AUTHOR) and task.park in self.folders()

    def link_for(self, task):
        """Получить последнюю почтовую привязку заявки для шаблона или закрытия."""
        if self._eligible_task(task):
            return self._links(task.id).first()
        return None

    def _links(self, task_id):
        """Построить запрос почтовых привязок от новых к старым."""
        link = self.model
        return link.query.filter_by(task_id=task_id).order_by(
            func.coalesce(link.revision_received_at, link.received_at).desc(), link.message_key.desc(),
        )

    def queue_reply(self, task, *, deferred=False):
        """Записать текст работ и поставить привязанные письма в очередь либо на ручную отписку."""
        if not self._eligible_task(task):
            return None
        links = self._links(task.id).with_for_update().all()
        if not links and task.author_open != MAIL_TASK_AUTHOR:
            return []
        if not links or any((link.park, link.board_number) != (task.park, task.board_number) for link in links):
            raise MailError('Не удалось подтвердить привязку заявки к письму Яндекс Почты.')
        queued = [link for link in links if link.reply_state in ('open', 'pending')]
        for link in queued:
            link.reply_text = task.completed_work
            link.reply_state = 'deferred' if deferred else 'pending'
            link.reply_error = None
            link.reply_retry_count = 0
            link.reply_next_at = None
        return queued

    @staticmethod
    def _planned_mail_kind(task):
        """Определить вид планового ТО для подбора письма."""
        if task.event_type or not (task.id_to or task.tag in ('to', 'otm')):
            return None
        description = (task.description or '').partition('\n')[0]
        return maintenance_kind(description)

    def queue_reply_lookup(self, task):
        """Пометить карточку без привязки для поиска исходного письма после закрытия."""
        if not self.enabled() or task.park not in self.folders() or task.event_type:
            return
        planned_kind = self._planned_mail_kind(task)
        if not planned_kind and (not task.author_open or task.id_to or task.tag in ('to', 'otm')
                                 or task.author_open in (MAIL_TASK_AUTHOR, 'InControl', 'Система')):
            return
        if self.link_for(task) is None:
            task.yandex_reply_lookup = True

    def confirm_manual_reply(self, task_id, ticket_number, reported_at):
        """Зафиксировать ручной ответ в почтовой записи."""
        self.model.query.filter(self.model.task_id == task_id, self.model.ticket_number == ticket_number,
                                self.model.reply_state.in_(('deferred', 'review'))).update({
            'reply_state': 'answered', 'reply_sent_at': reported_at, 'reply_error': None, 'reply_next_at': None,
        }, synchronize_session=False)

    def submit_reply(self, task_id, board_number):
        """Поставить немедленную отправку в фоновый пул, если есть место."""
        if not self.enabled() or not self.reply_slots.acquire(blocking=False):
            return
        try:
            self.reply_executor.submit(self._run_reply, task_id, board_number)
        except RuntimeError:
            self.reply_slots.release()
            self.app.logger.warning('yandex-mail: борт=%s, task=%s, не удалось запустить отправку сразу',
                                    board_number, task_id)

    def _run_reply(self, task_id, board_number):
        """В фоне найти письмо и отправить ответ для закрытой заявки."""
        try:
            with self.app.app_context():
                address, auth = self._auth_settings()
                folders = self.folders()
                for attempt in range(2):
                    try:
                        with self._mailbox(address, auth) as mail:
                            self.resolve_reply_lookups(mail, address, folders, task_id=task_id)
                            self.deliver_replies(mail, address, task_id=task_id)
                        break
                    except ImapConnectionError:
                        self.db.session.remove()
                        if attempt:
                            raise
        except Exception as exc:
            error = _error_summary(exc)
            self.app.logger.warning('yandex-mail: борт=%s, task=%s, сбой отправки после закрытия: %s',
                                    board_number, task_id, error)
        finally:
            self.reply_slots.release()

    def _latest_unanswered(self, mail, folder, task):
        """Подобрать последнее подходящее письмо без ответа для закрытой карточки."""
        planned_kind = self._planned_mail_kind(task)
        if task.created_at is None:
            return None, None
        start_at = task.created_at - (MAINTENANCE_LOOKBACK if planned_kind else REPAIR_REPLY_LOOKBACK)
        candidates = []
        for batch in mail.unanswered(folder, start_at, board_number=task.board_number):
            for item in batch:
                received_at = item.received_at.astimezone(MOSCOW).replace(tzinfo=None)
                if (item.board_number == task.board_number and received_at <= task.completed_at
                        and received_at >= start_at):
                    candidates.append(item)
        candidates.sort(key=lambda item: (item.received_at, int(item.uid)), reverse=True)
        for item in candidates:
            if mail.has_reply(item.message_id, item.received_at):
                continue
            try:
                ticket = read_repair_message(mail.download(folder, item), item)
            except InvalidRequest:
                if not planned_kind:
                    return item, None
                continue
            if maintenance_kind(ticket.description) == planned_kind:
                return item, ticket
        return None, None

    def _reassign_unanswered_ticket(self, link, task, original, ticket, key, folder, address):
        """Перенести неиспользованную привязку при новом письме после старого закрытия."""
        if (link.message_key == key or link.reply_state != 'open' or link.reply_attempt_at is not None
                or link.reply_sent_at is not None or self._planned_mail_kind(task)
                or (link.mailbox, link.park, link.board_number, link.ticket_number)
                != (address, task.park, task.board_number, ticket.ticket_number)
                or link.recipient.casefold() != ticket.recipient.casefold()):
            return None
        received_at = original.received_at.astimezone(MOSCOW).replace(tzinfo=None)
        if received_at <= (link.revision_received_at or link.received_at):
            return None
        previous = self.db.session.get(self.Task, link.task_id, with_for_update=True)
        if (previous is None or previous.status != 'completed'
                or (previous.park, previous.board_number) != (task.park, task.board_number)
                or not isinstance(previous.completed_at, datetime) or received_at <= previous.completed_at
                or match_imported_repair([task], ticket.description)[0] is None):
            return None

        link.task_id = task.id
        link.message_key = key
        link.message_id = original.message_id
        link.folder = folder
        link.recipient = ticket.recipient
        link.subject = ticket.subject
        link.received_at = received_at
        link.imap_uid = int(original.uid)
        link.imap_uid_validity = original.uid_validity
        link.revision_message_key = None
        link.revision_received_at = None
        link.reply_state = 'pending'
        link.reply_text = task.completed_work
        link.reply_error = None
        link.reply_retry_count = 0
        link.reply_next_at = None
        return previous.id

    def resolve_reply_lookups(self, mail, address, folders, *, task_id=None):
        """Найти исходные письма для закрытых заявок и подготовить ответы."""
        tasks = self.Task
        query = tasks.query.filter(tasks.yandex_reply_lookup.is_(True), tasks.status == 'completed',
                                   tasks.completed_at.is_not(None), tasks.park.in_(folders))
        if task_id is not None:
            query = query.filter_by(id=task_id)
        pending = query.order_by(tasks.id).limit(25).all()
        self.db.session.remove()
        for task in pending:
            folder = folders[task.park]
            original, ticket = self._latest_unanswered(mail, folder, task)
            current = self.db.session.get(tasks, task.id, with_for_update=True)
            if (current is None or not current.yandex_reply_lookup or current.status != 'completed'
                    or (current.park, current.board_number, current.completed_at) != (task.park, task.board_number, task.completed_at)):
                self.db.session.remove()
                continue
            current.yandex_reply_lookup = False
            foreign_link = None
            reassigned_from = None
            if ticket and ticket.recipient.casefold() != address and not self._links(task.id).first():
                key = hashlib.sha256(f'{address}\n{original.message_id}'.encode()).hexdigest()
                link = self.model
                existing = self.db.session.get(link, key, with_for_update=True)
                if existing is None:
                    existing = link.query.filter_by(mailbox=address, park=task.park,
                                                    ticket_number=ticket.ticket_number).with_for_update().first()
                if existing is None:
                    self.db.session.add(link(
                        task_id=task.id, mailbox=address, park=task.park, folder=folder, message_key=key,
                        ticket_number=ticket.ticket_number, board_number=task.board_number,
                        message_id=original.message_id, recipient=ticket.recipient, subject=ticket.subject,
                        received_at=original.received_at.astimezone(MOSCOW).replace(tzinfo=None),
                        imap_uid=int(original.uid), imap_uid_validity=original.uid_validity,
                        reply_state='pending', reply_text=current.completed_work,
                    ))
                elif (existing.message_key == key and existing.reply_state == 'open'
                      and (existing.mailbox, existing.park, existing.board_number, existing.ticket_number)
                      == (address, current.park, current.board_number, ticket.ticket_number)
                      and self.db.session.get(tasks, existing.task_id, with_for_update=True) is None
                      and match_imported_repair([current], ticket.description)[0] is not None):
                    existing.task_id = current.id
                    existing.folder = folder
                    existing.imap_uid = int(original.uid)
                    existing.imap_uid_validity = original.uid_validity
                    existing.reply_state = 'pending'
                    existing.reply_text = current.completed_work
                else:
                    reassigned_from = self._reassign_unanswered_ticket(existing, current, original, ticket, key, folder, address)
                    if reassigned_from is None:
                        foreign_link = existing.task_id
            self.db.session.commit()
            self.db.session.remove()
            if original is None:
                self.app.logger.info('yandex-mail: борт=%s, task=%s, неотвеченное письмо не найдено',
                                     task.board_number, task.id)
            elif ticket is None:
                self.app.logger.info('yandex-mail: борт=%s, task=%s, в исходном письме нет подходящей заявки Excel',
                                     task.board_number, task.id)
            elif reassigned_from is not None:
                self.app.logger.info('yandex-mail: борт=%s, task=%s, новое письмо перенесено с закрытой заявки task=%s',
                                     task.board_number, task.id, reassigned_from)
            elif foreign_link is not None:
                self.app.logger.info('yandex-mail: борт=%s, task=%s, письмо привязано к другой записи task=%s',
                                     task.board_number, task.id, foreign_link)

    def _add_task(self, mailbox, park, folder, original, key, ticket):
        """Создать, привязать или обновить заявку из письма с проверкой дублей."""
        try:
            for model in self.vehicle_models:
                vehicle = model.query.filter_by(bort=ticket.board_number, location=park).with_for_update().first()
                if vehicle is not None:
                    break
            else:
                return 'unknown_board'
            existing = self.model.query.filter_by(
                mailbox=mailbox, park=park, ticket_number=ticket.ticket_number,
            ).first()
            received_at = original.received_at.astimezone(MOSCOW).replace(tzinfo=None)
            active_repairs = active_repairs_for_board(self.Task, ticket.board_number).with_for_update().all()
            same_board = [row for row in active_repairs if (row.park, row.board_number) == (park, ticket.board_number)]
            service_kind = maintenance_kind(ticket.description)
            if existing is not None:
                result = self._apply_ticket_revision(existing, same_board, ticket, received_at, key, service_kind)
                if result == 'updated':
                    self._log_description_update(park, ticket.board_number)
                return result
            description_changed = False
            if service_kind:
                planned = [row for row in same_board if self._planned_mail_kind(row) == service_kind
                           and row.created_at is not None and received_at >= row.created_at - MAINTENANCE_LOOKBACK]
                if len(planned) != 1:
                    return 'active_board'
                task = planned[0]
                latest = self._links(task.id).with_for_update().first()
                if latest and (latest.mailbox != mailbox or latest.recipient.casefold() != ticket.recipient.casefold()):
                    return 'active_board'
                result = 'linked'
            elif active_repairs:
                matched = []
                for row in same_board:
                    if self._planned_mail_kind(row):
                        continue
                    latest = self._links(row.id).with_for_update().first()
                    if latest is None or latest.mailbox != mailbox or latest.recipient.casefold() != ticket.recipient.casefold():
                        continue
                    matched.append((row, latest))
                manual_tasks = manual_repair_candidates(
                    active_repairs, park, excluded_authors=(MAIL_TASK_AUTHOR, 'InControl', 'Система'),
                )
                candidates = (task for task in manual_tasks if task.board_number == ticket.board_number
                              and self._links(task.id).with_for_update().first() is None)
                if len(matched) == 1:
                    task, latest = matched[0]
                    if received_at >= (latest.revision_received_at or latest.received_at):
                        description_changed = task.description != ticket.description
                        task.description = ticket.description
                    extras = []
                else:
                    task, extras = match_imported_repair(candidates, ticket.description)
                if task is None:
                    return 'active_board'
                if extras:
                    task.description = '\n'.join((task.description.rstrip(), *extras))
                    description_changed = True
                result = 'linked'
            else:
                result = 'created'
                task = self.Task(
                    board_number=ticket.board_number, description=ticket.description, park=park,
                    status='in_repair', tag='waiting', created_at=received_at,
                    type_ts=vehicle.type_ts or ('Автобус' if model is self.vehicle_models[0] else 'Электробус'),
                    author_open=MAIL_TASK_AUTHOR, contract=vehicle.contract, vin=vehicle.vin,
                )
                self.db.session.add(task)
                self.db.session.flush()
            self.db.session.add(self.model(
                task_id=task.id, mailbox=mailbox, park=park, folder=folder, message_key=key,
                ticket_number=ticket.ticket_number, board_number=ticket.board_number,
                message_id=original.message_id, recipient=ticket.recipient, subject=ticket.subject,
                received_at=received_at,
                imap_uid=int(original.uid), imap_uid_validity=original.uid_validity,
            ))
            self.db.session.commit()
            if result == 'created':
                self.app.logger.info('%s %s добавлена заявка', park, ticket.board_number)
            elif description_changed:
                self._log_description_update(park, ticket.board_number)
            return result
        except IntegrityError:
            self.db.session.rollback()
            if self.model.query.filter_by(message_key=key).first() or self.model.query.filter_by(
                mailbox=mailbox, park=park, ticket_number=ticket.ticket_number,
            ).first():
                return 'duplicate'
            raise
        finally:
            self.db.session.remove()

    def _apply_ticket_revision(self, link, active_tasks, ticket, received_at, key, service_kind):
        """Обновить описание открытой карточки по более свежей версии письма."""
        task = next((task for task in active_tasks if task.id == link.task_id), None)
        if (task is None or service_kind or self._planned_mail_kind(task) or link.board_number != ticket.board_number
                or link.recipient.casefold() != ticket.recipient.casefold()
                or key in (link.message_key, link.revision_message_key)):
            return 'duplicate'
        latest = self._links(task.id).with_for_update().first()
        if received_at <= (latest.revision_received_at or latest.received_at):
            return 'duplicate'

        changed = task.description != ticket.description
        task.description = ticket.description
        link.revision_received_at = received_at
        link.revision_message_key = key
        self.db.session.commit()
        return 'updated' if changed else 'duplicate'

    def _log_description_update(self, park, board_number):
        """Записать в журнал парк и борт с изменённым описанием."""
        self.app.logger.info('%s %s изменено описание неисправностей', park, board_number)

    def _boards_for_park(self, park, boards):
        """Проверить принадлежность бортов к парку по справочнику транспорта."""
        registered = set()
        try:
            for model in self.vehicle_models:
                rows = self.db.session.query(model.bort).filter(model.location == park, model.bort.in_(boards)).all()
                registered.update(board for board, in rows)
        finally:
            self.db.session.remove()
        return registered

    def _refresh_original_uids(self, candidates, known, mailbox, park, folder):
        """Обновить сохранённые UID исходных писем для поиска и отметки."""
        hints = {key: candidates[key] for key, in known if candidates[key].uid_validity is not None}
        if not hints:
            return
        link = self.model
        rows = link.query.filter(
            link.message_key.in_(hints), link.mailbox == mailbox, link.park == park, link.folder == folder,
        ).all()
        for row in rows:
            original = hints[row.message_key]
            if original.message_id == row.message_id and original.board_number in (None, row.board_number):
                uid = int(original.uid)
                if (row.imap_uid, row.imap_uid_validity) != (uid, original.uid_validity):
                    row.imap_uid, row.imap_uid_validity = uid, original.uid_validity
        if self.db.session.dirty:
            self.db.session.commit()

    def import_requests(self, mail, address, folders, start, apply):
        """Импортировать входящие заявки по настроенным папкам и паркам."""
        counts = dict(created=0, linked=0, updated=0, duplicate=0, active_board=0, answered=0, invalid=0, unknown_board=0, preview=0)
        sent_from = None
        replied_to = set()
        for park, folder in folders.items():
            for batch in mail.unanswered(folder, start):
                candidates = {hashlib.sha256(f'{address}\n{item.message_id}'.encode()).hexdigest(): item for item in batch}
                if apply and candidates:
                    link = self.model
                    originals = self.db.session.query(link.message_key).filter(link.message_key.in_(candidates))
                    revisions = self.db.session.query(link.revision_message_key).filter(link.revision_message_key.in_(candidates))
                    known = originals.union(revisions).all()
                    self._refresh_original_uids(candidates, known, address, park, folder)
                    self.db.session.remove()
                    for key, in known:
                        candidates.pop(key)
                        counts['duplicate'] += 1
                if folder == INBOX and candidates:
                    boards = self._boards_for_park(park, {item.board_number for item in candidates.values()})
                    eligible = {key: item for key, item in candidates.items() if item.board_number in boards}
                    counts['unknown_board'] += len(candidates) - len(eligible)
                    candidates = eligible
                if candidates:
                    first_day = min(item.received_at.date() for item in candidates.values())
                    if sent_from is None or first_day < sent_from:
                        replied_to = mail.reply_parents_since(first_day)
                        sent_from = first_day
                for key, item in sorted(candidates.items(), key=lambda pair: pair[1].received_at):
                    if item.message_id in replied_to:
                        counts['answered'] += 1
                        continue
                    try:
                        ticket = read_repair_message(mail.download(folder, item), item)
                        if ticket.recipient.casefold() == address:
                            raise InvalidRequest('Адрес ответа совпадает с ящиком импорта.')
                    except InvalidRequest:
                        counts['invalid'] += 1
                        continue
                    if apply:
                        result = self._add_task(address, park, folder, item, key, ticket)
                        counts[result] += 1
                    else:
                        counts['preview'] += 1
                        click.echo(f'{park} | {ticket.ticket_number} | {ticket.board_number} | {ticket.description}')
        return counts

    def _set_reply_state(self, message_key, state, *, expected_states, error=None, retry_count=0, next_at=None):
        """Записать новое состояние ответа при совпадении ожидаемого состояния."""
        attempted_at = utcnow()
        values = {'reply_state': state, 'reply_error': error, 'reply_retry_count': retry_count,
                  'reply_next_at': next_at}
        if state != 'verify':
            values['reply_attempt_at'] = attempted_at
        if state in ('sent_copy', 'answered'):
            values['reply_sent_at'] = attempted_at
        link = self.model
        if state == 'sent_mark':
            values['reply_sent_at'] = func.coalesce(link.reply_sent_at, attempted_at)
        link.query.filter(link.message_key == message_key, link.reply_state.in_(expected_states)).update(
            values, synchronize_session=False,
        )
        self.db.session.commit()
        self.db.session.remove()

    def _claim_reply(self, message_key):
        """Захватить запись для отправки, чтобы её одновременно не взял другой процесс."""
        link = self.model
        now = utcnow()
        claimed = link.query.filter(link.message_key == message_key, link.reply_state == 'pending',
                                    or_(link.reply_next_at.is_(None), link.reply_next_at <= now)).update({
            'reply_state': 'sending', 'reply_attempt_at': now, 'reply_error': None, 'reply_next_at': None,
        }, synchronize_session=False)
        self.db.session.commit()
        self.db.session.remove()
        return claimed == 1

    def _retry_reply(self, link, state, error, *, expected_states=None, reset=False):
        """Сохранить сбой отправки и назначить время следующей попытки."""
        count = 0 if reset else link.reply_retry_count
        next_at = utcnow() + timedelta(minutes=RETRY_MINUTES[min(count, len(RETRY_MINUTES) - 1)])
        self._set_reply_state(link.message_key, state, expected_states=expected_states or (link.reply_state,),
                              error=error, retry_count=min(count + 1, len(RETRY_MINUTES)), next_at=next_at)

    @staticmethod
    def _reply_message_id(link):
        """Сформировать постоянный Message-ID для дедупликации ответа."""
        return f'<setrans-{link.message_key}@setrans.online>'

    def _recover_sent(self, mail, message_key, state):
        # Держим этап за одним процессом, даже если быстрый ответ пересёкся с заданием планировщика.
        """Проверить следы предыдущей отправки и закончить прерванные шаги."""
        model = self.model
        now = utcnow()
        claimed = model.query.filter(model.message_key == message_key, model.reply_state == state,
                                     or_(model.reply_next_at.is_(None), model.reply_next_at <= now)).update({
            'reply_next_at': now + timedelta(minutes=5),
        }, synchronize_session=False)
        self.db.session.commit()
        if not claimed:
            self.db.session.remove()
            return
        link = self.db.session.get(model, message_key)
        self.db.session.remove()
        try:
            if state == 'sent_copy':
                reply = build_reply(link.mailbox, link.recipient, link.subject, link.message_id,
                                    self._reply_message_id(link), link.reply_text,
                                    sent_at=link.reply_sent_at.replace(tzinfo=timezone.utc))
                mail.save_sent_copy(reply)
            else:
                mail.mark_answered(link.folder, link.message_id, link.received_at, uid_hint=link.original_uid_hint)
        except (MailError, OSError) as exc:
            error = _error_summary(exc)
            stage = 'копия в отправленных' if state == 'sent_copy' else 'отметка на исходном письме'
            self._retry_reply(link, state, error)
            self.app.logger.warning('yandex-mail: борт=%s, task=%s, SMTP принял ответ, сбой IMAP (%s): %s',
                                    link.board_number, link.task_id, stage, error)
            return
        if state == 'sent_copy':
            self._set_reply_state(message_key, 'sent_mark', expected_states=(state,))
            self._recover_sent(mail, message_key, 'sent_mark')
        else:
            self._set_reply_state(message_key, 'sent', expected_states=(state,))

    def _stage_review(self, link, *, expected_state, error):
        """Перевести спорную доставку в список для ручной проверки."""
        current = self.db.session.get(self.model, link.message_key, with_for_update=True)
        staged = False
        if current is not None and current.reply_state == expected_state:
            task = self.db.session.get(self.Task, link.task_id)
            if (task is not None and task.status == 'completed' and task.completed_at is not None
                    and (task.park, task.board_number) == (current.park, current.board_number)):
                self.outbox.stage_mail_review(task, current)
                current.reply_state = 'review'
                current.reply_next_at = None
                current.reply_error = error
                staged = True
        self.db.session.commit()
        self.db.session.remove()
        if staged:
            self.app.logger.warning('yandex-mail: борт=%s, task=%s, требуется проверка в списке «К отписке»',
                                    link.board_number, link.task_id)

    def _deliver(self, mail, link):
        """Отправить ответ, сохранить копию и отметить исходное письмо."""
        task_id = link.task_id
        logger = self.app.logger
        board = link.board_number
        expected_states = (link.reply_state,)
        if link.reply_state in SENT_STEPS:
            self._recover_sent(mail, link.message_key, link.reply_state)
            return
        verifying = link.reply_state in ('sending', 'verify')
        if verifying and mail.has_sent_copy(self._reply_message_id(link),
                                            link.reply_attempt_at.replace(tzinfo=timezone.utc) if link.reply_attempt_at
                                            else utcnow().replace(tzinfo=timezone.utc)):
            self._set_reply_state(link.message_key, 'sent_mark', expected_states=expected_states)
            logger.info('%s %s ответ в почту отправлен', link.park, board)
            self._recover_sent(mail, link.message_key, 'sent_mark')
            return
        original_answered = mail.original_answered(link.folder, link.message_id, link.received_at, uid_hint=link.original_uid_hint)
        if original_answered or mail.has_reply(link.message_id, link.received_at):
            self._set_reply_state(link.message_key, 'answered', expected_states=expected_states)
            logger.info('yandex-mail: борт=%s, task=%s, ответ уже есть', board, task_id)
            return
        if verifying:
            if link.reply_state == 'verify' and link.reply_retry_count >= len(RETRY_MINUTES):
                self._stage_review(link, expected_state='verify', error='Проверьте, был ли отправлен ответ.')
            else:
                self._retry_reply(link, 'verify', 'Подтверждение отправки пока не найдено.',
                                  reset=link.reply_state == 'sending')
            return
        if original_answered is None:
            error = 'Исходное письмо не найдено в папке.'
            self._retry_reply(link, 'pending', error)
            if link.reply_error != error:
                logger.warning('yandex-mail: борт=%s, task=%s, ответ остался в очереди: %s', board, task_id, error)
            return

        reply = build_reply(link.mailbox, link.recipient, link.subject, link.message_id,
                            self._reply_message_id(link), link.reply_text)
        if not self._claim_reply(link.message_key):
            return
        expected_states = ('sending',)
        try:
            mail.send_reply(reply)
        except UncertainDelivery as exc:
            self._retry_reply(link, 'verify', str(exc), expected_states=expected_states, reset=True)
            logger.warning('yandex-mail: борт=%s, task=%s, отправка не подтверждена: %s', board, task_id, exc)
            return
        except PermanentDeliveryError as exc:
            self._stage_review(link, expected_state='sending', error=str(exc))
            logger.warning('yandex-mail: борт=%s, task=%s, %s', board, task_id, exc)
            return
        except MailError as exc:
            self._retry_reply(link, 'pending', str(exc), expected_states=expected_states)
            logger.warning('yandex-mail: борт=%s, task=%s, ответ остался в очереди: %s', board, task_id, exc)
            return
        logger.info('%s %s ответ в почту отправлен', link.park, board)
        self._set_reply_state(link.message_key, 'sent_copy', expected_states=expected_states)
        self._recover_sent(mail, link.message_key, 'sent_copy')

    def _reply_queue(self, address, *, task_id=None):
        """Выбрать ответы, которые нужно отправить или проверить."""
        link = self.model
        tasks = self.Task
        now = utcnow()
        query = link.query.join(tasks, tasks.id == link.task_id).filter(
            link.mailbox == address, link.park.in_(self.folders()),
            tasks.status == 'completed', tasks.park == link.park, tasks.board_number == link.board_number,
            or_(link.reply_next_at.is_(None), link.reply_next_at <= now),
        )
        if task_id is not None:
            query = query.filter(link.task_id == task_id, link.reply_state == 'pending')
        else:
            # Не перепроверяем письмо, пока другой процесс может отправлять его по SMTP.
            stale = now - timedelta(minutes=5)
            query = query.filter(or_(
                link.reply_state.in_(('pending', 'verify', *SENT_STEPS)),
                and_(link.reply_state == 'sending', or_(link.reply_attempt_at.is_(None), link.reply_attempt_at <= stale)),
            ))
        return query

    def deliver_replies(self, mail, address, *, task_id=None):
        """Обработать очередь ответов в пределах бюджета времени."""
        link = self.model
        items = self._reply_queue(address, task_id=task_id).order_by(
            link.reply_attempt_at.is_not(None), link.reply_attempt_at, link.task_id,
        ).limit(25).all()
        self.db.session.remove()
        for item in items:
            try:
                self._deliver(mail, item)
            except ImapConnectionError as exc:
                if task_id is None:
                    now = utcnow()
                    link.query.filter(
                        link.message_key == item.message_key, link.reply_state == item.reply_state,
                        or_(link.reply_next_at.is_(None), link.reply_next_at <= now),
                    ).update({'reply_next_at': now + timedelta(minutes=RETRY_MINUTES[0]),
                              'reply_error': str(exc)}, synchronize_session=False)
                    self.db.session.commit()
                    self.db.session.remove()
                self.app.logger.warning('yandex-mail: борт=%s, task=%s, сбой проверки письма: %s',
                                        item.board_number, item.task_id, exc)
                raise
            except MailError as exc:
                if item.reply_state in ('pending', 'verify'):
                    self._retry_reply(item, item.reply_state, str(exc))
                self.app.logger.warning('yandex-mail: борт=%s, task=%s, проверка ответа не выполнена: %s',
                                        item.board_number, item.task_id, exc)
        return len(items)

    def _sync(self, *, apply, scheduled=False, replies_only=False, import_only=False, deadline=None):
        """Выполнить импорт, отправку или оба шага в одном подключении."""
        logger = self.app.logger
        mode = 'по расписанию' if scheduled else ('импорт вручную' if apply else 'просмотр')
        started = monotonic()
        stage = 'настройки'
        try:
            if scheduled:
                address, auth = self._auth_settings()
                folders = self.folders()
            else:
                address, auth, folders, start = self.settings()
            if auth == 'oauth':
                stage = 'OAuth'
            mailbox = self._mailbox(address, auth)
            stage = 'IMAP'
            if deadline is not None:
                mailbox.reply_deadline = deadline
            with mailbox as mail:
                stage = 'очередь ответов'
                if scheduled and not import_only:
                    self.deliver_replies(mail, address)
                    self.resolve_reply_lookups(mail, address, folders)
                    if replies_only:
                        return
                stage = 'загрузка заявок'
                if scheduled:
                    start = self._import_start()
                counts = self.import_requests(mail, address, folders, start, apply)
        except ReplyPassExpired:
            logger.info('yandex-mail: лимит времени очереди ответов, остаток обработаем следующим проходом')
            return
        except Exception as exc:
            # Форматируем только сообщения, созданные в интеграции, и коды ошибок.
            # Ответы серверов и параметры SQL могут содержать данные письма.
            error = _error_summary(exc)
            if isinstance(exc, SQLAlchemyError):
                args = getattr(getattr(exc, 'orig', None), 'args', ())
                if args and isinstance(args[0], int):
                    error = f'{error}, код БД {args[0]}'
            logger.warning('yandex-mail: сбой, режим=%s, этап=%s, причина=%s, время=%.2f с',
                           mode, stage, error, monotonic() - started)
            if isinstance(exc, MailError):
                raise
            raise MailError(f'{stage}: {error}.') from None
        return counts

    def run(self, *, replies_only=False, import_only=False):
        """Запустить почтовый обход с ограничением времени и повтором при обрыве."""
        try:
            if replies_only and not self._has_reply_work():
                return
            deadline = monotonic() + REPLY_PASS_SECONDS if replies_only else None
            try:
                return self._sync(apply=True, scheduled=True, replies_only=replies_only, import_only=import_only, deadline=deadline)
            except ImapConnectionError:
                if not (import_only or replies_only):
                    raise
                self.db.session.remove()
                return self._sync(apply=True, scheduled=True, replies_only=replies_only, import_only=import_only, deadline=deadline)
        except MailError as exc:
            self.db.session.rollback()
            return {'status': 'error', 'message': str(exc)}

    def _has_reply_work(self):
        """Проверить наличие ответов или поиска писем, готовых к обработке."""
        try:
            if self._reply_queue(self.address()).first() is not None:
                return True
            tasks = self.Task
            return tasks.query.filter(tasks.status == 'completed', tasks.yandex_reply_lookup.is_(True),
                                      tasks.completed_at.is_not(None), tasks.park.in_(self.folders())).first() is not None
        finally:
            self.db.session.remove()

    def _register_commands(self):
        """Зарегистрировать команды Flask для авторизации, импорта и диагностики."""
        @self.app.cli.command('yandex-mail-authorize')
        def authorize():
            """Авторизовать Яндекс Почту через OAuth и сохранить токены."""
            try:
                oauth = YandexMailOAuth(self.address())
                client_id = os.getenv('YANDEX_MAIL_OAUTH_CLIENT_ID') or click.prompt('Client ID')
                client_secret = os.getenv('YANDEX_MAIL_OAUTH_CLIENT_SECRET') or click.prompt('Client Secret', hide_input=True)

                def show_code(url, code):
                    """Показать оператору ссылку и одноразовый код подтверждения OAuth."""
                    click.echo(f'Откройте {url} под нужным аккаунтом, введите код {code} и подтвердите доступ.')

                oauth.authorize(client_id.strip(), client_secret.strip(), show_code, self.app.logger)
            except MailError as exc:
                raise click.ClickException(str(exc)) from None
            click.echo('Вход по IMAP проверен. Токены сохранены. Для синхронизации используйте YANDEX_MAIL_AUTH=oauth.')

        @self.app.cli.command('init-yandex-mail')
        def init_table():
            """Создать таблицу связи с письмами и ответами."""
            self.model.__table__.create(bind=self.db.engine, checkfirst=True)
            click.echo('Таблица yandex_mail_request готова.')

        @self.app.cli.command('yandex-mail-sync')
        @click.option('--apply', is_flag=True, help='Импортировать в базу. Без флага только просмотр.')
        def manual_import(apply):
            """Проверить входящие заявки по IMAP."""
            try:
                counts = self._sync(apply=apply)
            except MailError as exc:
                raise click.ClickException(str(exc)) from None
            click.echo(json.dumps(counts, ensure_ascii=False))

        @self.app.cli.command('yandex-mail-status')
        def status():
            """Показать ответы, по которым ещё нужно проверить отправку."""
            entries = self.model.query.filter(self.model.reply_state.in_(
                ('pending', 'sending', 'verify', 'review', *SENT_STEPS),
            )).order_by(self.model.task_id).all()
            for item in entries:
                click.echo(f'{item.park} | борт {item.board_number} | {item.ticket_number} | {item.reply_state} | {item.reply_error or ""}')
            click.echo(f'Записей в очереди: {len(entries)}')
