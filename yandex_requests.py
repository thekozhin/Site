"""Импорт заявок Яндекс Почты и очередь ответов после закрытия в Сетранс."""

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import BoundedSemaphore
from time import monotonic

import click
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from completion_checks import utcnow
from task_import_matching import active_repairs_for_board, manual_repair_candidates, match_imported_repair
from yandex_mail import (
    INBOX,
    MAIL_TASK_AUTHOR,
    MOSCOW,
    ImapConnectionError,
    InvalidRequest,
    MailError,
    PermanentDeliveryError,
    UncertainDelivery,
    YandexMailbox,
    build_reply,
    read_repair_message,
)
from yandex_oauth import YandexMailOAuth


# Для других парков добавьте пары «парк Сетранс — папка IMAP» в YANDEX_MAIL_FOLDERS.
PILOT_FOLDERS = {'14 парк': INBOX}
RETRY_MINUTES = (1, 5, 15)
SENT_STEPS = ('sent_copy', 'sent_mark')


class YandexRequests:
    def __init__(self, app, db, Task, Car, Elcar):
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
            reply_state = db.Column(db.String(16), nullable=False, default='open')
            reply_text = db.Column(db.Text)
            reply_attempt_at = db.Column(db.DateTime)
            reply_sent_at = db.Column(db.DateTime)
            reply_error = db.Column(db.String(255))
            reply_retry_count = db.Column(db.Integer, nullable=False, server_default='0')
            reply_next_at = db.Column(db.DateTime)

        self.model = MailRequest
        self._register_commands()

    @staticmethod
    def enabled():
        return os.getenv('YANDEX_MAIL_ENABLED') == '1'

    @staticmethod
    def address():
        address = os.getenv('YANDEX_MAIL_ADDRESS', '').strip().lower()
        if not re.fullmatch(r'[a-z0-9_.+-]+@[a-z0-9.-]+\.[a-z]{2,}', address):
            raise MailError('Укажите YANDEX_MAIL_ADDRESS в .env.')
        return address

    def _auth_settings(self):
        address = self.address()
        auth = os.getenv('YANDEX_MAIL_AUTH', 'password').strip().lower()
        if auth not in ('password', 'oauth'):
            raise MailError('Укажите YANDEX_MAIL_AUTH=password или YANDEX_MAIL_AUTH=oauth.')
        return address, auth

    def _mailbox(self, address, auth):
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
        start_value = os.getenv('YANDEX_MAIL_IMPORT_FROM', '').strip()
        try:
            return datetime.fromisoformat(start_value) if start_value else None
        except ValueError:
            raise MailError('Проверьте формат YANDEX_MAIL_IMPORT_FROM (ГГГГ-ММ-ДДTЧЧ:ММ:СС).') from None

    def settings(self):
        address, auth = self._auth_settings()
        return address, auth, self.folders(), self._import_start()

    def _eligible_task(self, task):
        return (self.enabled() or task.author_open == MAIL_TASK_AUTHOR) and task.park in self.folders()

    def link_for(self, task):
        if self._eligible_task(task):
            return self._links(task.id).first()
        return None

    def _links(self, task_id):
        link = self.model
        return link.query.filter_by(task_id=task_id).order_by(link.received_at.desc(), link.message_key.desc())

    def queue_reply(self, task, *, deferred=False):
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

    def queue_reply_lookup(self, task):
        if (not self.enabled() or task.park not in self.folders() or not task.author_open
                or task.author_open in (MAIL_TASK_AUTHOR, 'InControl', 'Система')
                or task.id_to or task.event_type or self.link_for(task) is not None):
            return
        task.yandex_reply_lookup = True

    def confirm_manual_reply(self, task_id, ticket_number, reported_at):
        self.model.query.filter(self.model.task_id == task_id, self.model.ticket_number == ticket_number,
                                self.model.reply_state.in_(('deferred', 'review'))).update({
            'reply_state': 'answered', 'reply_sent_at': reported_at, 'reply_error': None, 'reply_next_at': None,
        }, synchronize_session=False)

    def submit_reply(self, task_id, board_number):
        if not self.enabled() or not self.reply_slots.acquire(blocking=False):
            return
        try:
            self.reply_executor.submit(self._run_reply, task_id, board_number)
        except RuntimeError:
            self.reply_slots.release()
            self.app.logger.warning('yandex-mail: борт=%s, task=%s, не удалось запустить отправку сразу',
                                    board_number, task_id)

    def _run_reply(self, task_id, board_number):
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
            error = str(exc) if isinstance(exc, MailError) else type(exc).__name__
            self.app.logger.warning('yandex-mail: борт=%s, task=%s, сбой отправки после закрытия: %s',
                                    board_number, task_id, error)
        finally:
            self.reply_slots.release()

    def _latest_unanswered(self, mail, folder, task):
        candidates = []
        for batch in mail.unanswered(folder, None, board_number=task.board_number):
            candidates.extend(item for item in batch if item.board_number == task.board_number
                              and item.received_at.astimezone(MOSCOW).replace(tzinfo=None) <= task.completed_at)
        candidates.sort(key=lambda item: (item.received_at, int(item.uid)), reverse=True)
        return next((item for item in candidates if not mail.has_reply(item.message_id, item.received_at)), None)

    def resolve_reply_lookups(self, mail, address, folders, *, task_id=None):
        tasks = self.Task
        query = tasks.query.filter(tasks.yandex_reply_lookup.is_(True), tasks.status == 'completed',
                                   tasks.completed_at.is_not(None), tasks.park.in_(folders))
        if task_id is not None:
            query = query.filter_by(id=task_id)
        pending = query.order_by(tasks.id).limit(25).all()
        self.db.session.remove()
        for task in pending:
            folder = folders[task.park]
            original = self._latest_unanswered(mail, folder, task)
            ticket = None
            if original is not None:
                try:
                    ticket = read_repair_message(mail.download(folder, original), original)
                except InvalidRequest:
                    pass
            current = self.db.session.get(tasks, task.id, with_for_update=True)
            if (current is None or not current.yandex_reply_lookup or current.status != 'completed'
                    or (current.park, current.board_number, current.completed_at) != (task.park, task.board_number, task.completed_at)):
                self.db.session.remove()
                continue
            current.yandex_reply_lookup = False
            foreign_link = None
            if ticket and ticket.recipient.casefold() != address and not self._links(task.id).first():
                key = hashlib.sha256(f'{address}\n{original.message_id}'.encode()).hexdigest()
                link = self.model
                existing = link.query.filter(or_(link.message_key == key, and_(
                    link.mailbox == address, link.park == task.park, link.ticket_number == ticket.ticket_number,
                ))).with_for_update().first()
                if existing is None:
                    self.db.session.add(link(
                        task_id=task.id, mailbox=address, park=task.park, folder=folder, message_key=key,
                        ticket_number=ticket.ticket_number, board_number=task.board_number,
                        message_id=original.message_id, recipient=ticket.recipient, subject=ticket.subject,
                        received_at=original.received_at.astimezone(MOSCOW).replace(tzinfo=None),
                        reply_state='pending', reply_text=current.completed_work,
                    ))
                elif (existing.message_key == key and existing.reply_state == 'open'
                      and (existing.mailbox, existing.park, existing.board_number, existing.ticket_number)
                      == (address, current.park, current.board_number, ticket.ticket_number)
                      and self.db.session.get(tasks, existing.task_id, with_for_update=True) is None
                      and match_imported_repair([current], ticket.description)[0] is not None):
                    existing.task_id = current.id
                    existing.folder = folder
                    existing.reply_state = 'pending'
                    existing.reply_text = current.completed_work
                else:
                    foreign_link = existing.task_id
            self.db.session.commit()
            self.db.session.remove()
            if original is None:
                self.app.logger.info('yandex-mail: борт=%s, task=%s, неотвеченное письмо не найдено',
                                     task.board_number, task.id)
            elif ticket is None:
                self.app.logger.info('yandex-mail: борт=%s, task=%s, в исходном письме нет подходящей заявки Excel',
                                     task.board_number, task.id)
            elif foreign_link is not None:
                self.app.logger.info('yandex-mail: борт=%s, task=%s, письмо привязано к другой записи task=%s',
                                     task.board_number, task.id, foreign_link)

    def _add_task(self, mailbox, park, folder, original, key, ticket):
        try:
            for model in self.vehicle_models:
                vehicle = model.query.filter_by(bort=ticket.board_number, location=park).with_for_update().first()
                if vehicle is not None:
                    break
            else:
                return 'unknown_board'
            if self.model.query.filter_by(mailbox=mailbox, park=park, ticket_number=ticket.ticket_number).first():
                return 'duplicate'
            received_at = original.received_at.replace(tzinfo=None)
            active_repairs = active_repairs_for_board(self.Task, ticket.board_number).with_for_update().all()
            if active_repairs:
                same_board = [row for row in active_repairs if (row.park, row.board_number) == (park, ticket.board_number)]
                matched = []
                for row in same_board:
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
                    if received_at >= latest.received_at:
                        task.description = ticket.description
                    extras = []
                else:
                    task, extras = match_imported_repair(candidates, ticket.description)
                if task is None:
                    return 'active_board'
                if extras:
                    task.description = '\n'.join((task.description.rstrip(), *extras))
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
            ))
            self.db.session.commit()
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

    def _boards_for_park(self, park, boards):
        registered = set()
        try:
            for model in self.vehicle_models:
                rows = self.db.session.query(model.bort).filter(model.location == park, model.bort.in_(boards)).all()
                registered.update(board for board, in rows)
        finally:
            self.db.session.remove()
        return registered

    def import_requests(self, mail, address, folders, start, apply):
        counts = dict(created=0, linked=0, duplicate=0, active_board=0, answered=0, invalid=0, unknown_board=0, preview=0)
        sent_from = None
        replied_to = set()
        for park, folder in folders.items():
            added_boards = []
            for batch in mail.unanswered(folder, start):
                candidates = {hashlib.sha256(f'{address}\n{item.message_id}'.encode()).hexdigest(): item for item in batch}
                if apply and candidates:
                    known = self.db.session.query(self.model.message_key).filter(self.model.message_key.in_(candidates)).all()
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
                        if result == 'created':
                            added_boards.append(ticket.board_number)
                    else:
                        counts['preview'] += 1
                        click.echo(f'{park} | {ticket.ticket_number} | {ticket.board_number} | {ticket.description}')
            if added_boards:
                self.app.logger.info('Яндекс Почта | %s | Добавлены борта %s', park, ', '.join(added_boards))
        return counts

    def _set_reply_state(self, message_key, state, *, expected_states, error=None, retry_count=0, next_at=None):
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
        count = 0 if reset else link.reply_retry_count
        next_at = utcnow() + timedelta(minutes=RETRY_MINUTES[min(count, len(RETRY_MINUTES) - 1)])
        self._set_reply_state(link.message_key, state, expected_states=expected_states or (link.reply_state,),
                              error=error, retry_count=min(count + 1, len(RETRY_MINUTES)), next_at=next_at)

    @staticmethod
    def _reply_message_id(link):
        return f'<setrans-{link.message_key}@setrans.online>'

    def _recover_sent(self, mail, message_key, state):
        # Держим этап за одним процессом, даже если быстрый ответ пересёкся с заданием планировщика.
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
                mail.mark_answered(link.folder, link.message_id, link.received_at)
        except (MailError, OSError) as exc:
            error = str(exc) if isinstance(exc, MailError) else type(exc).__name__
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
            self.app.logger.info('yandex-mail: борт=%s, task=%s, копия ответа и отметка сохранены',
                                 link.board_number, link.task_id)

    def _stage_review(self, link, *, expected_state, error):
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
            self._recover_sent(mail, link.message_key, 'sent_mark')
            return
        original_answered = mail.original_answered(link.folder, link.message_id, link.received_at)
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
        logger.info('yandex-mail: борт=%s, task=%s, отправляем ответ', board, task_id)
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
        logger.info('yandex-mail: борт=%s, task=%s, SMTP подтвердил отправку', board, task_id)
        self._set_reply_state(link.message_key, 'sent_copy', expected_states=expected_states)
        self._recover_sent(mail, link.message_key, 'sent_copy')

    def _reply_queue(self, address, *, task_id=None):
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
        link = self.model
        items = self._reply_queue(address, task_id=task_id).order_by(
            link.reply_attempt_at.is_not(None), link.reply_attempt_at, link.task_id,
        ).limit(25).all()
        self.db.session.remove()
        for item in items:
            try:
                self._deliver(mail, item)
            except ImapConnectionError as exc:
                self.app.logger.warning('yandex-mail: борт=%s, task=%s, сбой проверки письма: %s',
                                        item.board_number, item.task_id, exc)
                raise
            except MailError as exc:
                if item.reply_state in ('pending', 'verify'):
                    self._retry_reply(item, item.reply_state, str(exc))
                self.app.logger.warning('yandex-mail: борт=%s, task=%s, проверка ответа не выполнена: %s',
                                        item.board_number, item.task_id, exc)
        return len(items)

    def _sync(self, *, apply, scheduled=False, replies_only=False, import_only=False):
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
        except Exception as exc:
            # Форматируем только сообщения, созданные в интеграции, и коды ошибок.
            # Ответы серверов и параметры SQL могут содержать данные письма.
            error = str(exc) if isinstance(exc, MailError) else type(exc).__name__
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
        try:
            if replies_only and not self._has_reply_work():
                return
            try:
                return self._sync(apply=True, scheduled=True, replies_only=replies_only, import_only=import_only)
            except ImapConnectionError:
                if not (import_only or replies_only):
                    raise
                self.db.session.remove()
                return self._sync(apply=True, scheduled=True, replies_only=replies_only, import_only=import_only)
        except MailError as exc:
            self.db.session.rollback()
            return {'status': 'error', 'message': str(exc)}

    def _has_reply_work(self):
        try:
            if self._reply_queue(self.address()).first() is not None:
                return True
            tasks = self.Task
            return tasks.query.filter(tasks.status == 'completed', tasks.yandex_reply_lookup.is_(True),
                                      tasks.completed_at.is_not(None), tasks.park.in_(self.folders())).first() is not None
        finally:
            self.db.session.remove()

    def _register_commands(self):
        @self.app.cli.command('yandex-mail-authorize')
        def authorize():
            """Авторизовать Яндекс Почту через OAuth и сохранить токены."""
            try:
                oauth = YandexMailOAuth(self.address())
                client_id = os.getenv('YANDEX_MAIL_OAUTH_CLIENT_ID') or click.prompt('Client ID')
                client_secret = os.getenv('YANDEX_MAIL_OAUTH_CLIENT_SECRET') or click.prompt('Client Secret', hide_input=True)

                def show_code(url, code):
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
