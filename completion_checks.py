"""Короткие HTTP-запросы для фоновой проверки перед закрытием заявки.

Результаты общие для всех Passenger-процессов, исполнители ограничены
четырьмя проверками на процесс. Перезапуск процесса не разрешает закрытие:
незавершённая проверка истечёт, и пользователь сможет повторить её.
Таблица создаётся отдельной CLI-командой, никогда из HTTP-запроса.
"""

import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from flask import abort, jsonify, session, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError
from incontrol import ALLOWED_WORK_NAMES, PARK_TO_ORG, is_incontrol_unavailable
from incontrol_accounts import paused_closure_services
from incontrol_rejection import RejectionError


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CompletionChecks:
    WAIT_SECONDS = 45
    RESULT_SECONDS = 60
    OUTAGE_SECONDS = 900
    TIMEOUT_MESSAGE = 'Проверка InControl не завершилась вовремя. Заявка не закрыта. Повторите проверку.'

    def __init__(self, application, db, Task, check_board, enabled):
        self.application = application
        self.db = db
        self.Task = Task
        self.check_board = check_board
        self.enabled = enabled
        # Потоки создаются только при submit, после запуска Passenger-процесса.
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='completion-check')
        self.slots = threading.BoundedSemaphore(4)

        class CompletionCheck(db.Model):
            __tablename__ = 'completion_check'
            __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
            id = db.Column(db.String(32), primary_key=True)
            task_id = db.Column(db.Integer, nullable=False, index=True)
            user_id = db.Column(db.Integer, nullable=False, index=True)
            fingerprint = db.Column(db.String(64), nullable=False)
            state = db.Column(db.String(16), nullable=False, default='pending')
            message = db.Column(db.String(500), nullable=True)
            created_at = db.Column(db.DateTime, nullable=False, index=True)
            finished_at = db.Column(db.DateTime, nullable=True)

        self.model = CompletionCheck
        application.add_url_rule(
            '/complete_task/<int:task_id>/incontrol-check',
            'start_completion_check', login_required(self.start), methods=['POST'],
        )
        application.add_url_rule(
            '/complete_task/<int:task_id>/incontrol-check/<check_id>',
            'completion_check_status', login_required(self.status), methods=['GET'],
        )

        @application.cli.command('init-completion-checks')
        def init_table():
            """Создать таблицу результатов перед обновлением сайта."""
            CompletionCheck.__table__.create(bind=db.engine, checkfirst=True)
            print('Таблица completion_check готова.')

    def required(self, task):
        return self.enabled() and task.park in ('1 парк', '14 парк', 'Красная Пахра', '18 парк') and not self.paused(task)

    @staticmethod
    def paused(task):
        return 'incontrol' in paused_closure_services() and (task.park in PARK_TO_ORG or bool(task.incontrol_task_id))

    @staticmethod
    def fingerprint(task):
        data = [task.id, task.board_number, task.park, task.tag, task.status, task.id_to]
        return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()

    def proof_fingerprint(self, task):
        # Не принимать старые результаты поиска, который ориентировался только на tag.
        data = [self.fingerprint(task), 'board-main-v3']
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()

    def _task_for_user(self, task_id, lock=False):
        query = self.Task.query.filter_by(id=task_id)
        if lock:
            query = query.populate_existing().with_for_update()
        task = query.first_or_404()
        if task.park not in session.get('available_parks', []) and current_user.username != 'vasilev':
            abort(403)
        return task

    def _expired(self, job, now):
        if job.state == 'pending':
            return now >= job.created_at + timedelta(seconds=self.WAIT_SECONDS)
        if job.state == 'unavailable':
            return not job.finished_at or now >= job.finished_at + timedelta(seconds=self.OUTAGE_SECONDS)
        if job.state == 'ready':
            return not job.finished_at or now >= job.finished_at + timedelta(seconds=self.RESULT_SECONDS)
        return False

    def start(self, task_id):
        try:
            task = self._task_for_user(task_id, lock=True)
            if task.status != 'in_repair':
                return jsonify(state='error', message='Заявка уже закрыта. Обновите страницу.'), 409
            if not self.required(task):
                return jsonify(state='ready', check_id='')
            now = utcnow()
            fingerprint = self.proof_fingerprint(task)
            # Повтор HTTP-запроса не создаёт ещё одну активную проверку.
            job = self.model.query.filter_by(
                task_id=task.id, user_id=current_user.id,
                fingerprint=fingerprint, state='pending',
            ).filter(self.model.created_at > now - timedelta(seconds=self.WAIT_SECONDS)).with_for_update().first()
            if job is None:
                # Результаты одноразовые и не нужны дольше суток.
                self.model.query.filter(self.model.created_at < now - timedelta(days=1)).delete(synchronize_session=False)
                job = self.model(
                    id=uuid.uuid4().hex, task_id=task.id, user_id=current_user.id,
                    fingerprint=fingerprint, state='pending', created_at=now,
                )
                self.db.session.add(job)
                self.db.session.commit()
                check_id = job.id
                if not self._submit(check_id):
                    job.state = 'error'
                    job.message = 'Все проверки заняты. Заявка не закрыта. Повторите через несколько секунд.'
                    self.db.session.commit()
                    return jsonify(state='error', message=job.message), 503
            else:
                check_id = job.id
                self.db.session.commit()
            return jsonify(
                state='pending', check_id=check_id,
                status_url=url_for('completion_check_status', task_id=task_id, check_id=check_id),
            ), 202
        except SQLAlchemyError:
            self.db.session.rollback()
            self.application.logger.exception('completion-check: не удалось начать проверку task_id=%s', task_id)
            return jsonify(state='error', message='Не удалось начать проверку. Заявка не закрыта. Повторите позже.'), 503

    def status(self, task_id, check_id):
        try:
            task = self._task_for_user(task_id)
            job = self.model.query.filter_by(id=check_id, task_id=task.id, user_id=current_user.id).first_or_404()
            if self._expired(job, utcnow()):
                return jsonify(state='error', message=self.TIMEOUT_MESSAGE)
            if job.fingerprint != self.proof_fingerprint(task):
                return jsonify(state='error', message='Заявка изменилась. Обновите страницу и повторите проверку.')
            response = jsonify(state=job.state, message=job.message, check_id=job.id)
            response.headers['Cache-Control'] = 'no-store'
            return response
        except SQLAlchemyError:
            self.db.session.rollback()
            return jsonify(state='error', message='Не удалось получить результат. Заявка не закрыта. Повторите проверку.'), 503

    def _submit(self, check_id):
        if not self.slots.acquire(blocking=False):
            return False
        try:
            self.executor.submit(self._run_with_slot, check_id)
        except RuntimeError:
            self.slots.release()
            return False
        return True

    def _run_with_slot(self, check_id):
        try:
            self._run(check_id)
        finally:
            self.slots.release()

    def _run(self, check_id):
        with self.application.app_context():
            try:
                job = self.db.session.get(self.model, check_id)
                if job is None or job.state != 'pending' or self._expired(job, utcnow()):
                    return
                task = self.db.session.get(self.Task, job.task_id)
                if task is None or job.fingerprint != self.proof_fingerprint(task):
                    state, message = 'error', 'Заявка изменилась. Обновите страницу.'
                else:
                    board, park = task.board_number, task.park
                    # Не держим соединение/транзакцию MySQL во время внешнего запроса.
                    self.db.session.remove()
                    required_names = set(ALLOWED_WORK_NAMES)
                    work_name = self.check_board(board, park, required_names)
                    state = 'blocked' if work_name else 'ready'
                    message = (f'Нельзя закрыть заявку: в InControl ещё открыта «{work_name}».' if work_name else None)
                self._save_result(check_id, state, message)
            except Exception as exc:
                self.db.session.rollback()
                self.application.logger.exception('completion-check: ошибка проверки id=%s', check_id)
                try:
                    state = 'unavailable' if is_incontrol_unavailable(exc) else 'error'
                    message = ('InControl недоступен. Можно закрыть заявку в Сетранс и отписать её позже.'
                               if state == 'unavailable' else 'InControl вернул ошибку. Заявка не закрыта. Повторите проверку.')
                    self._save_result(check_id, state, message)
                except SQLAlchemyError:
                    self.db.session.rollback()
                    self.application.logger.exception('completion-check: не удалось записать ошибку id=%s', check_id)
            finally:
                self.db.session.remove()

    def _save_result(self, check_id, state, message):
        # Поздний ответ не оживляет истёкшую проверку.
        now = utcnow()
        self.model.query.filter_by(id=check_id, state='pending').filter(
            self.model.created_at > now - timedelta(seconds=self.WAIT_SECONDS)
        ).update(dict(state=state, message=message, finished_at=now), synchronize_session=False)
        self.db.session.commit()

    def consume_outage(self, task, check_id):
        job = self.model.query.filter_by(id=check_id, task_id=task.id, user_id=current_user.id).populate_existing().with_for_update().first()
        if (job is None or job.state != 'unavailable' or self._expired(job, utcnow())
                or job.fingerprint != self.proof_fingerprint(task)):
            raise RejectionError('Недоступность InControl не подтверждена или проверка устарела. Повторите проверку.')
        job.state = 'consumed'

    def consume(self, task, check_id):
        """Вызывается под блокировкой Task и в транзакции её закрытия."""
        if not self.required(task):
            if task.park in ('1 парк', '14 парк', 'Красная Пахра', '18 парк'):
                return 'Проверка InControl выключена. Проверку пропустить нельзя. Включите интеграцию и повторите закрытие.'
            return None
        job = self.model.query.filter_by(
            id=check_id, task_id=task.id, user_id=current_user.id,
        ).populate_existing().with_for_update().first() if check_id else None
        if job is None or job.state != 'ready':
            return 'Перед закрытием дождитесь успешной проверки InControl. Повторите проверку.'
        if self._expired(job, utcnow()) or job.fingerprint != self.proof_fingerprint(task):
            return 'Результат проверки устарел или заявка изменилась. Повторите проверку.'
        job.state = 'consumed'
        return None
