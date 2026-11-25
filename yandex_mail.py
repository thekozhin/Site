"""Разбор заявок из вложений и обмен с Яндекс Почтой по IMAP/SMTP."""

import base64
import imaplib
import logging
import re
import smtplib
import ssl
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, parsedate_to_datetime
from io import BytesIO
from pathlib import PurePath
from time import monotonic
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import xlrd
from openpyxl import load_workbook
from requests.certs import where as ca_bundle


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


@dataclass(frozen=True)
class RepairRequest:
    ticket_number: str
    board_number: str
    description: str
    recipient: str
    subject: str


def _cell(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip() if value is not None else ''


def maintenance_kind(description):
    match = MAINTENANCE.fullmatch(' '.join((description or '').split()))
    if match:
        return f'ТО-{match[1]}' if match[1] else 'ОТМ'
    return None


def _repair_sheet(sheet_rows):
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
    headers = message.get_all('Message-ID', [])
    value = str(headers[0]).strip() if len(headers) == 1 else ''
    return value if value.isascii() and len(value) <= 512 and MESSAGE_ID.fullmatch(value) else None


def _subject_board(subject):
    if len(subject) > 500 or '\n' in subject or '\r' in subject:
        return None
    boards = set(BOARD.findall(subject))
    return boards.pop() if len(boards) == 1 else None


def read_repair_message(raw, original):
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
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _imap_folder(value):
    # RFC 3501: имена папок вне ASCII в IMAP4rev1 используют modified UTF-7.
    def encode(match):
        chars = match[0]
        if chars == '&':
            return '&-'
        return '&' + base64.b64encode(chars.encode('utf-16-be')).decode().rstrip('=').replace('/', ',') + '-'

    return _quote(re.sub(r'&|[^ -~]+', encode, value))


def _imap_date(day):
    return f'{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}'


def _reply_parent(headers):
    direct = MESSAGE_ID.findall(str(headers.get('In-Reply-To', '')))
    references = MESSAGE_ID.findall(str(headers.get('References', '')))
    return set(direct or references[-1:])


def build_reply(sender, recipient, subject, original_id, reply_id, completed_work, *, sent_at=None):
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
    if isinstance(error, imaplib.IMAP4.error) and '[AUTHENTICATIONFAILED]' in str(error).upper():
        return 'AUTHENTICATIONFAILED'
    if isinstance(error, smtplib.SMTPResponseException):
        return str(error.smtp_code)
    return type(error).__name__


class YandexMailbox:
    def __init__(self, address, password, sent_folder=None, *, oauth_token=None, logger=None):
        login, _, domain = address.partition('@')
        self.username = login if domain in ('yandex.ru', 'yandex.com', 'ya.ru') else address
        self.password = password
        self.oauth_payload = f'user={address}\x01auth=Bearer {oauth_token}\x01\x01' if oauth_token else None
        self.logger = logger if logger is not None else logging.getLogger(__name__)
        self.sent_folder = _imap_folder(sent_folder) if sent_folder else None
        self.selected = None
        self.sent_parents_since = None
        self.smtp = None
        self.smtp_failed = False
        self.reply_deadline = None

    def __enter__(self):
        self.selected = None
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
        self._close_smtp()
        with suppress(imaplib.IMAP4.error, OSError):
            if self.reply_deadline is not None:
                self.imap.shutdown()
            else:
                self.imap.logout()

    def _imap_timeout(self):
        if self.reply_deadline is None:
            return 10
        remaining = self.reply_deadline - monotonic()
        if remaining <= 0:
            raise ReplyPassExpired
        return min(10, remaining)

    def _imap_call(self, operation, method, *args, **kwargs):
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
        if self.selected != (wire_folder, readonly):
            self.selected = None
            status, _ = self._imap_call('EXAMINE' if readonly else 'SELECT', self.imap.select, wire_folder, readonly=readonly)
            if status != 'OK':
                raise ImapCommandError('Папка Яндекс Почты не доступна по IMAP.')
            self.selected = wire_folder, readonly

    def _sent_folder(self):
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
        status, values = self._imap_call(f'UID {command}', self.imap.uid, command, *args)
        if status != 'OK':
            raise ImapCommandError(f'Яндекс Почта не выполнила команду IMAP {command}.')
        return values

    def _search(self, *args):
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
        if board_number:
            subject = ('SUBJECT', _quote(board_number))
            try:
                uids = self._search(*search, *subject)
            except ImapCommandError:
                try:
                    uids = self._search(*subject)
                except ImapCommandError:
                    uids = self._search(*search)
        else:
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
                    originals.append(OriginalMail(uid[1], message_id, received_at, board))
            yield originals

    def download(self, folder, original):
        self._select(_imap_folder(folder))
        values = list(self._fetch([original.uid], f'BODY.PEEK[]<0.{MAX_MESSAGE_SIZE + 1}>',
                                  purpose='чтение письма с вложением'))
        if len(values) != 1 or len(values[0][1]) > MAX_MESSAGE_SIZE:
            raise InvalidRequest('Письмо с вложением недоступно или слишком большое.')
        return values[0][1]

    def has_reply(self, message_id, received_at):
        received_day = received_at.date()
        if received_at.tzinfo is not None:
            received_day = received_at.astimezone(MOSCOW).date()
        return message_id in self.reply_parents_since(received_day)

    def reply_parents_since(self, received_day):
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
        try:
            uids = self._search('UNDELETED', 'HEADER', 'MESSAGE-ID', _quote(message_id))
        except ImapCommandError:
            # Яндекс иногда отклоняет поиск по заголовку. Берём соседние даты IMAP
            # и проверяем точный Message-ID по заголовкам, без загрузки писем.
            uids = self._search('UNDELETED', 'SINCE', _imap_date(day - timedelta(days=1)),
                                'BEFORE', _imap_date(day + timedelta(days=2)))
        for info, raw in self._fetch(uids, 'UID FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]', purpose=purpose):
            uid = re.search(rb'\bUID (\d+)', info)
            flags = {flag.lower() for flag in imaplib.ParseFlags(info)}
            if uid and b'\\deleted' not in flags and _message_id(BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)) == message_id:
                yield uid[1], b'\\answered' in flags

    def _original(self, folder, message_id, received_at, readonly=True):
        self._select(_imap_folder(folder), readonly=readonly)
        matches = list(self._messages_with_id(message_id, received_at.date(), purpose='исходное письмо'))
        if len(matches) != 1:
            return None
        return matches[0]

    def original_answered(self, folder, message_id, received_at):
        found = self._original(folder, message_id, received_at)
        # None означает, что нельзя подтвердить наличие исходного письма в папке.
        return found[1] if found else None

    def mark_answered(self, folder, message_id, received_at):
        found = self._original(folder, message_id, received_at, readonly=False)
        if found is None:
            raise MailError('Исходное письмо не найдено однозначно для отметки об ответе.')
        self._uid('STORE', found[0], '+FLAGS.SILENT', r'(\Answered \Flagged)')

    def _close_smtp(self):
        if self.smtp is not None:
            self.smtp.close()
            self.smtp = None

    def send_reply(self, reply):
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
        self._select(self._sent_folder())
        return any(self._messages_with_id(message_id, sent_at.astimezone(MOSCOW).date(),
                                          purpose='копия отправленного ответа'))
