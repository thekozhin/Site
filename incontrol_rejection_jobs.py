"""Фоновые отклонения InControl с сохранением состояния между воркерами."""
import hashlib
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from flask import abort, flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from incontrol_accounts import credential_cipher, PersonalAccountError
from incontrol_rejection import RejectionFlow, RejectionError, find_open_main_tasks
from incontrol_completion import is_service_request, normalized_name
from incontrol_fulfillment import FulfillmentFlow
from incontrol import PARK_TO_ORG, is_incontrol_unavailable
from incontrol_session import capture_session, restore_session, ensure_session
from incontrol_child_plan import decisions_from_json, decisions_summary, read_children, validate_blanket_rejection


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


FORM_FIELDS = ('completed_work', 'mileage', 'work_in', 'seasonal_service', 'evacuation_photos_confirmed')


def form_values(form):
    values = {key: form.get(key, '').replace('\r\n','\n').replace('\r','\n').strip() for key in FORM_FIELDS}
    if form.get('main_outcome'):
        if form['main_outcome'] not in ('complete', 'reject'):
            raise RejectionError('Выберите результат основной заявки.')
        values['main_outcome'] = form['main_outcome']
    if form.get('child_decisions'):
        values['child_decisions'] = json.dumps(decisions_from_json(form['child_decisions']), ensure_ascii=False, sort_keys=True)
    return values


def digest(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def drive_flow(flow, action='prepare', code=None):
    result = flow.advance(action, code)
    for _ in range(8):
        if result in ('done', 'code'):
            return result
        if result == 'confirm':
            result = flow.advance('sign')
        elif result == 'advance':
            result = flow.advance('prepare')
        else:
            break
    raise RejectionError('Состояние документов изменилось. Повторите проверку.')


class IncontrolRejections:
    def __init__(self, app, db, Task, checks, personal_client, enabled):
        self.app, self.db, self.Task, self.checks = app, db, Task, checks
        self.personal_client, self.enabled = personal_client, enabled
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='incontrol-reject')
        self.slots = threading.BoundedSemaphore(2)

        class Run(db.Model):
            __tablename__ = 'incontrol_rejection_run'
            __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
            id = db.Column(db.String(32), primary_key=True)
            task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False, unique=True)
            remote_id = db.Column(db.BigInteger, nullable=True, unique=True)
            user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
            state = db.Column(db.String(20), nullable=False)
            payload_encrypted = db.Column(db.Text, nullable=False)
            message = db.Column(db.String(500))
            updated_at = db.Column(db.DateTime, nullable=False)

        self.model = Run
        app.add_url_rule('/incontrol/rejections/<run_id>', 'incontrol_rejection_page', login_required(self.page), methods=['GET'])
        app.add_url_rule('/incontrol/rejections/<run_id>/status', 'incontrol_rejection_status', login_required(self.status), methods=['GET'])
        app.add_url_rule('/incontrol/rejections/<run_id>/action', 'incontrol_rejection_action', login_required(self.action), methods=['POST'])

        app.add_url_rule('/complete_task/<int:task_id>/incontrol-plan', 'start_incontrol_plan', login_required(self.start_plan), methods=['POST'])

        @app.cli.command('init-incontrol-rejections')
        def init_table():
            Run.__table__.create(bind=db.engine, checkfirst=True)
            print('Таблица incontrol_rejection_run готова.')

        @app.context_processor
        def rejection_options():
            return {'incontrol_rejection_enabled': self.enabled(),
                    'uses_inline_incontrol': lambda task: self.park_enabled(task.park) and self.has_personal_account()}

    def pack(self, value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        if len(raw) > 45000:
            raise RejectionError('Слишком большой объём текста связанных задач. Сократите описания работ.')
        return credential_cipher().encrypt(raw).decode()

    def unpack(self, row):
        return json.loads(credential_cipher().decrypt(row.payload_encrypted.encode()))

    def owned(self, run_id, lock=False):
        query = self.model.query.filter_by(id=run_id, user_id=current_user.id)
        if lock:
            query = query.with_for_update()
        row = query.first_or_404()
        task = self.checks._task_for_user(row.task_id)
        return row, task

    def has_personal_account(self):
        return current_user.is_authenticated and self.app.extensions['incontrol_accounts']['has_credentials'](current_user.id)

    @staticmethod
    def park_enabled(park):
        parks = os.getenv('INCONTROL_CLOSURE_PARKS', '14 парк').split(',')
        return park in PARK_TO_ORG and park in {name.strip() for name in parks}

    def applies(self, task, form):
        return self.park_enabled(task.park) and bool(form.get('incontrol_rejection_run') or self.has_personal_account())

    def start_plan(self, task_id):
        task = self.checks._task_for_user(task_id, lock=True)
        if task.status != 'in_repair':
            abort(409)
        try:
            return self.start(task, request.form, planning=True)
        except (RejectionError, PersonalAccountError) as exc:
            self.db.session.rollback()
            return jsonify(message=str(exc)), 409

    def start(self, task, form, planning=False):
        if not self.park_enabled(task.park):
            raise RejectionError('Автоматическая отписка в InControl выключена для этого парка.')
        if self.checks.paused(task):
            raise RejectionError('Проверка InControl при закрытии приостановлена. Обновите форму закрытия.')
        if not self.enabled():
            raise RejectionError('Закрытие не выполнено: интеграция InControl выключена. Включите INCONTROL_REJECTION_ENABLED=1 и перезапустите приложение. Проверку пропустить нельзя.')
        self.personal_client(current_user.id).session.close()
        hint = int(task.incontrol_task_id) if task.incontrol_task_id else None
        values = form_values(form)
        mode = 'reject' if form.get('action') == 'reject' else 'complete'
        explicit_mode = values.get('main_outcome')
        if explicit_mode and explicit_mode != mode:
            raise RejectionError('Результат основной заявки не совпадает с выбранным действием.')
        row = self.model.query.filter_by(task_id=task.id).with_for_update().first()
        old = self.unpack(row) if row else {}
        if row and row.user_id != current_user.id:
            raise RejectionError('Закрытие этой заявки уже начато другим пользователем.')
        if row and planning and (old.get('started') or (row.state == 'running' and not old.get('planning'))):
            response = jsonify(resume_url=url_for('complete_task', task_id=task.id, rejection_run=row.id))
            response.headers['Cache-Control'] = 'no-store'
            return response, 202
        if row and row.state == 'running':
            return self.respond(row.id)
        decisions = decisions_from_json(values.get('child_decisions'))
        blanket = explicit_mode == 'reject' and not planning
        if blanket and decisions:
            raise RejectionError('Для отклонения основной заявки укажите одну общую причину.')
        if blanket and not values['completed_work']:
            raise RejectionError('Укажите причину отклонения основной заявки.')
        if decisions:
            # Проверяем состав и решения, без сравнения сводного текста.
            # Результат основной заявки выбирает приёмщик.
            decisions_summary(old.get('child_plan', []), decisions)
        elif not planning and not blanket and old.get('planning') and old.get('child_plan'):
            raise RejectionError('Заполните решение по каждой связанной задаче.')
        if row and old.get('started'):
            if old['form_hash'] != digest(values) or old.get('mode', 'reject') != mode:
                raise RejectionError('Закрытие уже начато с другими данными. Продолжите прежний процесс из формы закрытия.')
            return self.respond(row.id)
        payload = {'mode':mode, 'remote_id':None, 'hint_id':hint, 'lookup_scope':'all-main-v1',
                   'board':task.board_number, 'park':task.park, 'full_name':current_user.full_name,
                   'reason':values['completed_work'], 'form':values, 'form_hash':digest(values),
                   'fingerprint':self.checks.fingerprint(task), 'planning':planning, 'blanket_reject':blanket}
        if old.get('fingerprint') == payload['fingerprint']:
            for key in ('confirmed_remote_id', 'child_plan', 'candidates', 'unrelated_remote_ids'):
                if key in old: payload[key] = old[key]
        if decisions:
            payload['child_decisions'] = decisions
        if row:
            row.remote_id=None; row.payload_encrypted=self.pack(payload); row.state='new'; row.message=None
        else:
            row=self.model(id=uuid.uuid4().hex,task_id=task.id,user_id=current_user.id,remote_id=None,
                           state='new',payload_encrypted=self.pack(payload),updated_at=now())
            self.db.session.add(row)
        try:
            self.db.session.commit()
        except IntegrityError:
            self.db.session.rollback()
            raise RejectionError('Процесс уже запущен. Обновите страницу и продолжите его.') from None
        self.submit(row, 'inspect' if planning else 'prepare')
        return self.respond(row.id)

    def respond(self, run_id):
        if request.accept_mimetypes.best == 'application/json':
            response = jsonify(run_id=run_id, status_url=url_for('incontrol_rejection_status', run_id=run_id),
                               action_url=url_for('incontrol_rejection_action', run_id=run_id))
            response.headers['Cache-Control'] = 'no-store'
            return response, 202
        row, task = self.owned(run_id)
        return redirect(url_for('complete_task', task_id=task.id, rejection_run=run_id))

    def submit(self, row, action, code=None):
        if not self.slots.acquire(blocking=False):
            raise RejectionError('Все проверки заняты. Повторите через несколько секунд.')
        row.state = 'running'
        row.message = None
        row.updated_at = now()
        run_id = row.id
        self.db.session.commit()
        try:
            self.pool.submit(self.worker, run_id, action, code)
        except Exception:
            self.slots.release()
            row.state = 'error'
            row.message = 'Не удалось запустить обработку. Повторите проверку состояния.'
            self.db.session.commit()
            raise

    def worker(self, run_id, action, code):
        with self.app.app_context():
            client = None
            try:
                row = self.db.session.get(self.model, run_id)
                payload = self.unpack(row)
                task = self.db.session.get(self.Task, row.task_id)
                if not self.park_enabled(task.park):
                    raise RejectionError('Автоматическая отписка в InControl выключена для этого парка.')
                if task.status != 'in_repair' or self.checks.fingerprint(task) != payload['fingerprint']:
                    raise RejectionError('Локальная заявка изменилась или уже закрыта. Требуется ручная проверка.')
                scheduled_service = bool(task.id_to or task.tag == 'to')
                if not task.incontrol_task_id and not payload.get('confirmed_remote_id'):
                    # Старый план мог выбрать единственную заявку сам, до подтверждения приёмщиком.
                    payload.update(hint_id=None, remote_id=None, selected_id=None)
                user_id = row.user_id
                client = self.personal_client(user_id)
                client.auth_context = f'run:{run_id}'
                self.db.session.rollback()
                # Личные настройки содержат последний сеанс, в том числе после других подписей.
                # Снимок старой заявки используется только при отсутствии актуального JWT.
                authorization = client.session.headers.get('Authorization')
                if payload.get('auth') and not (isinstance(authorization, str) and authorization.startswith('JWT ')):
                    restore_session(client, payload['auth'])
                ensure_session(client)
                self.app.extensions['incontrol_accounts']['remember_auth'](user_id, client)
                self.db.session.rollback()
                payload['auth'] = capture_session(client)

                def save(state):
                    record = self.db.session.get(self.model, run_id)
                    record.payload_encrypted = self.pack(state)
                    record.updated_at = now()
                    self.db.session.commit()
                    self.app.extensions['incontrol_accounts']['remember_auth'](user_id, client)
                    self.db.session.remove()  # Никакой транзакции во время внешних HTTP.

                save(payload)
                if payload.get('remote_id') is None:
                    payload['lookup_scope'] = 'all-main-v1'
                    found = find_open_main_tasks(client, payload['board'], payload['park'])
                    hint = payload.get('hint_id')
                    selected = hint or payload.get('confirmed_remote_id')
                    ids = [item['id'] for item in found]
                    if selected and selected not in ids:
                        # Отсутствие в поиске само по себе не доказывает закрытие.
                        direct = RejectionFlow(client, payload, save).task(selected)
                        phase = normalized_name((direct.get('phase') or {}).get('phase_name'))
                        from incontrol import ALLOWED_WORK_NAMES
                        if (direct.get('type_work') or {}).get('work_name') not in ALLOWED_WORK_NAMES:
                            raise RejectionError('Привязанная заявка имеет неподдерживаемый тип.')
                        if phase in {'новое', 'назначено', 'принято'} and direct.get('stage') not in ('denied', 'completed'):
                            found.append(direct); ids.append(selected)
                        elif (phase in {'выполнено', 'отклонено'} or direct.get('stage') in ('denied', 'completed')) and hint == selected:
                            payload.update(no_remote=True, remote_closed=hint, checked_at=now().timestamp(), child_plan=[], candidates=[])
                            save(payload)
                            row = self.db.session.get(self.model, run_id)
                            row.state = 'plan' if action == 'inspect' else 'no_remote'
                            row.updated_at = now()
                            self.db.session.commit()
                            return
                        else:
                            raise RejectionError('Не удалось подтвердить открытый этап выбранной заявки InControl.')
                    # Выбор «нет связанной заявки» действует только на проверенный список ID.
                    unrelated = not selected and sorted(ids) == payload.get('unrelated_remote_ids')
                    if not found or unrelated:
                        if hint:
                            raise RejectionError(f'Привязанная заявка {hint} закрыта. Открытые заявки по борту не найдены.')
                        payload.update(no_remote=True, checked_at=now().timestamp(), child_plan=[], candidates=[])
                        save(payload)
                        row = self.db.session.get(self.model, run_id)
                        row.state = 'plan' if action == 'inspect' else 'no_remote'
                        row.updated_at = now()
                        self.db.session.commit()
                        return
                    if not selected and scheduled_service:
                        service_requests = [item['id'] for item in found if is_service_request(item)]
                        if len(service_requests) == 1:
                            selected = service_requests[0]
                    if not selected:
                        payload['candidates'] = [{'id':x['id'], 'title':(x.get('type_work') or {}).get('work_name','Заявка'),
                                                  'description':x.get('task_description') or ''} for x in found]
                        payload['planning'] = True
                        save(payload)
                        row = self.db.session.get(self.model, run_id); row.state='choose'; self.db.session.commit()
                        return
                    payload.update(remote_id=selected, no_remote=False, selected_id=selected)
                if action == 'inspect' or (not payload.get('started') and not payload.get('child_decisions') and not payload.get('blanket_reject')):
                    probe = RejectionFlow(client, payload, save)
                    main, children = read_children(probe)
                    payload.update(child_plan=children, planning=True)
                    save(payload)
                    row = self.db.session.get(self.model, run_id); row.state='plan'; row.updated_at=now(); self.db.session.commit()
                    return
                if not payload.get('started') and (payload.get('child_decisions') or payload.get('blanket_reject')):
                    probe = RejectionFlow(client, payload, save)
                    _, fresh_plan = read_children(probe)
                    if payload.get('blanket_reject'):
                        validate_blanket_rejection(payload.get('child_plan',[]), fresh_plan)
                    else:
                        decisions_summary(fresh_plan, payload['child_decisions'])
                    row = self.db.session.get(self.model, run_id)
                    row.remote_id=payload['remote_id']
                    local = self.db.session.get(self.Task, row.task_id, populate_existing=True, with_for_update=True)
                    if local.incontrol_task_id and int(local.incontrol_task_id) != payload['remote_id']:
                        raise RejectionError('Привязка локальной заявки изменилась.')
                    local.incontrol_task_id=payload['remote_id']
                    payload['hint_id']=payload['remote_id']
                    row.payload_encrypted=self.pack(payload)
                    self.db.session.commit()
                    self.db.session.remove()
                flow = (FulfillmentFlow if payload.get('mode') == 'complete' else RejectionFlow)(client, payload, save)
                result = drive_flow(flow, action, code)
                row = self.db.session.get(self.model, run_id)
                row.state = {'confirm': 'confirm', 'code': 'code', 'done': 'done'}[result]
                row.updated_at = now()
                self.db.session.commit()
            except Exception as exc:
                self.db.session.rollback()
                row = self.db.session.get(self.model, run_id)
                if row:
                    if is_incontrol_unavailable(exc):
                        row.state = 'unavailable'
                        row.message = 'InControl недоступен. Можно закрыть заявку в Сетранс и отписать её позже.'
                    else:
                        row.state = 'error'
                        row.message = str(exc)[:500] if isinstance(exc, (RejectionError, PersonalAccountError)) else 'Ответ InControl не получен. Проверьте состояние перед повтором; уже выполненные шаги будут учтены.'
                    row.updated_at = now()
                    self.db.session.commit()
                self.app.logger.warning('incontrol-rejection: run=%s error_type=%s', run_id, type(exc).__name__)
            finally:
                if client:
                    client.session.close()
                self.db.session.remove()
                self.slots.release()

    def page(self, run_id):
        row, task = self.owned(run_id)
        if row.state == 'running' and row.updated_at < now() - timedelta(minutes=3):
            row.state = 'error'
            row.message = 'Обработка прервалась. Проверьте состояние перед продолжением.'
            self.db.session.commit()
        response = redirect(url_for('complete_task', task_id=task.id, rejection_run=run_id))
        response.headers['Cache-Control'] = 'no-store'
        return response

    def status(self, run_id):
        row, _ = self.owned(run_id)
        stale = row.state == 'running' and row.updated_at < now() - timedelta(minutes=3)
        payload = self.unpack(row)
        if row.state == 'unavailable':
            stale = row.updated_at < now() - timedelta(seconds=self.checks.OUTAGE_SECONDS)
        response = jsonify(state='error' if stale else row.state,
                           message='Обработка прервалась. Проверьте состояние.' if stale else row.message,
                           step=payload.get('step'), document_id=(payload.get('document') or {}).get('id'),
                           signature_requested_at=(payload.get('signature_request') or {}).get('at'),
                           child_plan=payload.get('child_plan', []), decisions=payload.get('child_decisions', []),
                           candidates=payload.get('candidates', []), selected_id=payload.get('remote_id') or payload.get('selected_id'),
                           allow_unrelated=not payload.get('hint_id'),
                           unrelated_remote=bool(payload.get('no_remote') and payload.get('unrelated_remote_ids')),
                           planning=bool(payload.get('planning')),
                           no_remote=bool(payload.get('no_remote')), remote_closed=payload.get('remote_closed'))
        response.headers['Cache-Control'] = 'no-store'
        return response

    def action(self, run_id):
        row, task = self.owned(run_id, lock=True)
        if not self.park_enabled(task.park):
            return jsonify(message='Автоматическая отписка в InControl выключена для этого парка.'), 409
        if self.checks.paused(task):
            return jsonify(message='Проверка InControl при закрытии приостановлена. Обновите форму закрытия.'), 409
        if row.state == 'running' and row.updated_at >= now() - timedelta(minutes=3):
            return self.respond(run_id)
        action = request.form.get('action')
        if row.state in ('done', 'consumed'):
            return self.respond(run_id)
        payload = self.unpack(row)
        if action == 'select':
            if payload.get('started') or row.state != 'choose': abort(409)
            candidate_ids = {x['id'] for x in payload.get('candidates', [])}
            choice = request.form.get('remote_id', '')
            if choice == 'unrelated' and not task.incontrol_task_id:
                selected = None
                payload['unrelated_remote_ids'] = sorted(candidate_ids)
            else:
                try: selected = int(choice)
                except ValueError: abort(400)
                if selected not in candidate_ids: abort(409)
                payload.pop('unrelated_remote_ids', None)
            payload.update(confirmed_remote_id=selected, selected_id=selected, remote_id=None)
            row.payload_encrypted=self.pack(payload); self.db.session.commit()
            action, code = 'inspect', None
        elif action in ('sign', 'resend'):
            if row.state not in ('confirm', 'code') or request.form.get('step') != payload.get('step') or request.form.get('document_id') != str((payload.get('document') or {}).get('id')):
                abort(409)
            code = request.form.get('code', '').strip() or None
            if action == 'resend':
                if row.state != 'code':
                    abort(409)
                if datetime.now(timezone.utc).timestamp() - (payload.get('signature_request') or {}).get('at', 0) < 10:
                    return jsonify(message='Повторно запросить код можно через 10 секунд.'), 429
                code = None
            if action == 'sign' and row.state == 'code' and (not code or not code.isdecimal() or len(code) != 4):
                if request.accept_mimetypes.best == 'application/json':
                    return jsonify(message='Введите четыре цифры кода из письма.'), 400
                flash('Введите четыре цифры кода из письма.', 'error')
                return self.respond(run_id)
        elif action == 'retry':
            action, code = ('inspect' if payload.get('planning') and not payload.get('started') else 'prepare'), None
        else:
            abort(400)
        try:
            self.submit(row, action, code)
        except RejectionError as exc:
            self.db.session.rollback()
            if request.accept_mimetypes.best == 'application/json':
                return jsonify(message=str(exc)), 409
            flash(str(exc), 'error')
        return self.respond(run_id)

    def validate_outage(self, run, task, run_id, payload):
        if (run is None or run.id != run_id or run.user_id != current_user.id or run.state != 'unavailable'
                or run.updated_at < now() - timedelta(seconds=self.checks.OUTAGE_SECONDS)
                or payload['fingerprint'] != self.checks.fingerprint(task)
                or (payload.get('hint_id') or None) != (task.incontrol_task_id or None)):
            raise RejectionError('Недоступность InControl не подтверждена или проверка устарела. Повторите проверку.')

    def consume(self, task, form):
        row = self.model.query.filter_by(id=form.get('incontrol_rejection_run', ''), task_id=task.id, user_id=current_user.id).with_for_update().first()
        if row is None or row.state not in ('done', 'no_remote'):
            return 'Отклонение и подпись заявки в InControl ещё не подтверждены.'
        payload = self.unpack(row)
        if row.state == 'no_remote' and payload.get('lookup_scope') != 'all-main-v1':
            return 'Прежняя проверка учитывала не все типы заявок InControl. Начните проверку заново.'
        if row.state == 'no_remote' and now().timestamp() - payload.get('checked_at', 0) > 90:
            return 'Проверка отсутствия заявки устарела. В форме закрытия нажмите «Проверить состояние и продолжить».'
        if payload.get('mode', 'reject') != ('reject' if form.get('action') == 'reject' else 'complete'):
            return 'Способ закрытия изменён после подписания.'
        try:
            submitted = form_values(form)
        except RejectionError as exc:
            return str(exc)
        if 'main_outcome' not in payload['form'] and 'main_outcome' in submitted:
            if submitted['main_outcome'] != payload.get('mode', 'reject'):
                return 'Результат основной заявки изменён после начала закрытия.'
            submitted.pop('main_outcome')  # Совместимость с уже начатыми процессами прежней формы.
        if payload['fingerprint'] != self.checks.fingerprint(task) or payload['form_hash'] != digest(submitted):
            return 'Данные заявки изменились после подписания. Требуется проверка.'
        row.state = 'consumed'  # Коммит — в одной транзакции с локальным закрытием.
        return None
