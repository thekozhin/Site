"""Общий список закрытых заявок для отписки в InControl и Яндекс Почте."""
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_login import current_user, login_required
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from sqlalchemy.exc import SQLAlchemyError

from completion_checks import utcnow
from incontrol_rejection import RejectionError
from incontrol_rejection_jobs import digest, form_values


class IncontrolOutbox:
    def __init__(self, app, db, checks, rejections, mail_requests):
        self.db = db
        self.checks = checks
        self.rejections = rejections
        self.mail_requests = mail_requests

        class PendingClosure(db.Model):
            __tablename__ = 'incontrol_pending_closure'
            __table_args__ = (
                db.Index('ix_incontrol_pending_park', 'reported_at', 'park'),
                {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'},
            )
            id = db.Column(db.Integer, primary_key=True)
            task_id = db.Column(db.Integer, nullable=False)
            remote_id = db.Column(db.BigInteger, nullable=True)
            service = db.Column(db.String(16), nullable=False, server_default='incontrol')
            mail_ticket_number = db.Column(db.String(40), nullable=True)
            park = db.Column(db.String(50), nullable=False)
            board_number = db.Column(db.String(20), nullable=False)
            description = db.Column(db.Text, nullable=False)
            completed_work = db.Column(db.Text, nullable=False)
            child_details = db.Column(db.Text, nullable=True)
            is_reject = db.Column(db.Boolean, nullable=False)
            completed_at = db.Column(db.DateTime, nullable=False)
            closed_by = db.Column(db.String(100), nullable=False)
            reported_at = db.Column(db.DateTime, nullable=True)
            reported_by = db.Column(db.String(100), nullable=True)

            @property
            def service_name(self):
                return 'Яндекс Почта' if self.service == 'yandex_mail' else 'InControl'

        self.model = PendingClosure
        mail_requests.outbox = self
        app.add_url_rule('/incontrol/pending', 'incontrol_pending', login_required(self.page))
        app.add_url_rule('/incontrol/pending/count', 'incontrol_pending_count', login_required(self.count))
        app.add_url_rule('/incontrol/pending/export', 'incontrol_pending_export', login_required(self.export))
        app.add_url_rule('/incontrol/pending/<int:entry_id>/reported', 'incontrol_pending_reported',
                         login_required(self.mark_reported), methods=['POST'])

        @app.cli.command('init-incontrol-pending-closures')
        def init_table():
            PendingClosure.__table__.create(bind=db.engine, checkfirst=True)
            print('Таблица incontrol_pending_closure готова.')

    @staticmethod
    def selected_park():
        park = session.get('selected_park')
        if park and (current_user.username == 'vasilev' or park in session.get('available_parks', [])):
            return park
        return None

    def pending(self):
        return self.model.query.filter_by(park=self.selected_park(), reported_at=None)

    def page(self):
        records = self.pending().order_by(self.model.completed_at.desc(), self.model.id.desc()).all()
        return render_template('incontrol_pending.html', records=records, park=self.selected_park())

    def count(self):
        try:
            count = self.pending().count()
        except SQLAlchemyError:
            self.db.session.rollback()
            return jsonify(message='Список отписок временно недоступен.'), 503
        response = jsonify(count=count)
        response.headers['Cache-Control'] = 'no-store'
        return response

    def mark_reported(self, entry_id):
        entry = self.pending().filter_by(id=entry_id).with_for_update().first()
        if entry is None:
            abort(404)
        entry.reported_at = utcnow()
        entry.reported_by = current_user.full_name or current_user.username
        if entry.service == 'yandex_mail':
            self.mail_requests.confirm_manual_reply(entry.task_id, entry.mail_ticket_number, entry.reported_at)
        self.db.session.commit()
        flash(f'Отписка по борту {entry.board_number} отмечена.', 'success')
        return redirect(url_for('incontrol_pending'))

    def export(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'К отписке'
        sheet.append(['Борт', 'Парк', 'Заявка Сетранс', 'Заявка InControl', 'Окончание (МСК)',
                      'Описание заявки', 'Работы или причина', 'Результат', 'Закрыл', 'Связанные задачи',
                      'Сервис', 'Заявка Яндекс Почты'])
        for column, width in zip('ABCDEFGHIJKL', (14, 21, 17, 18, 22, 48, 48, 16, 25, 52, 20, 24)):
            sheet.column_dimensions[column].width = width
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for entry in self.pending().order_by(self.model.completed_at.desc(), self.model.id.desc()).all():
            sheet.append([entry.board_number, entry.park, entry.task_id, entry.remote_id, entry.completed_at.replace(microsecond=0),
                          entry.description, entry.completed_work, 'Отклонено' if entry.is_reject else 'Выполнено',
                          entry.closed_by, entry.child_details, entry.service_name, entry.mail_ticket_number])
            for cell in sheet[sheet.max_row]:
                cell.alignment = Alignment(wrap_text=True, vertical='top')
                if isinstance(cell.value, str):
                    cell.data_type = 's'  # Тексты заявок в Excel остаются текстом.
            sheet.cell(sheet.max_row, 5).number_format = 'dd.mm.yyyy hh:mm'
        sheet.freeze_panes = 'C2'
        sheet.auto_filter.ref = sheet.dimensions
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        stamp = datetime.now(ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%d')
        return send_file(output, as_attachment=True, download_name=f'К_отписке_{self.selected_park() or stamp}_{stamp}.xlsx',
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', max_age=0)

    def stage_closure(self, task, completed_at, completed_work, is_reject, *, paused=False):
        """Сохраняет отписку в транзакции закрытия после проверки сбоя или при паузе."""
        if paused:
            self._stage(task, completed_at, completed_work, is_reject, remote_id=task.incontrol_task_id)
            return
        run_id = request.form.get('offline_run_id')
        check_id = request.form.get('offline_check_id')
        if bool(run_id) == bool(check_id):
            raise RejectionError('Для закрытия без InControl повторите проверку доступности.')
        run = self.rejections.model.query.filter_by(task_id=task.id).populate_existing().with_for_update().first()
        payload = self.rejections.unpack(run) if run else {}
        if run_id:
            self.rejections.validate_outage(run, task, run_id, payload)
            if not payload.get('planning'):
                mode = 'reject' if is_reject else 'complete'
                if mode != payload['mode'] or digest(form_values(request.form)) != payload['form_hash']:
                    raise RejectionError('Данные отличаются от начатого закрытия. Вернитесь к сохранённой форме.')
        else:
            if run and run.state != 'consumed' and (run.state in ('running', 'done', 'no_remote') or payload.get('started')):
                raise RejectionError('Для заявки уже начато закрытие в InControl. Продолжите его из сохранённой формы.')
            self.checks.consume_outage(task, check_id)
        details = None
        if run_id:
            titles = {item['id']: item['title'] for item in payload.get('child_plan', [])}
            lines = []
            for item in payload.get('child_decisions', []):
                result = 'Выполнено' if item['action'] == 'complete' else 'Отклонено'
                title = titles.get(item['id'], '')
                lines.append(f"№{item['id']} {title} — {result}: {item['work']}")
            details = '\n'.join(lines) or None
            run.state = 'consumed'
        remote_id = task.incontrol_task_id
        if run_id:
            remote_id = payload.get('remote_id') or remote_id or payload.get('selected_id')
        self._stage(task, completed_at, completed_work, is_reject, remote_id=remote_id, child_details=details)

    def stage_mail_closure(self, task, completed_at, completed_work, is_reject, ticket_number):
        self._stage(task, completed_at, completed_work, is_reject, service='yandex_mail', mail_ticket_number=ticket_number)

    def stage_mail_review(self, task, link):
        if self.model.query.filter_by(task_id=task.id, service='yandex_mail',
                                      mail_ticket_number=link.ticket_number, reported_at=None).first() is None:
            self._stage(task, task.completed_at, link.reply_text, bool(task.reject), service='yandex_mail',
                        mail_ticket_number=link.ticket_number, closed_by=task.author_closed or 'Система')

    def _stage(self, task, completed_at, completed_work, is_reject, *, service='incontrol',
               remote_id=None, child_details=None, mail_ticket_number=None, closed_by=None):
        self.db.session.add(self.model(
            task_id=task.id, remote_id=remote_id, service=service, mail_ticket_number=mail_ticket_number,
            park=task.park, board_number=task.board_number, description=task.description,
            completed_work=completed_work, child_details=child_details, is_reject=is_reject,
            completed_at=completed_at.replace(tzinfo=None),
            closed_by=closed_by or current_user.full_name or current_user.username,
        ))
