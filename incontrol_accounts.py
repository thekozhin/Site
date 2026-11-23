"""Личные учётные данные InControl. Пароли не возвращаются в HTML и не пишутся в логи."""
import base64
import hashlib
import json
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app, flash, has_request_context, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError

from incontrol import IncontrolClient
from incontrol_session import capture_session, restore_session, ensure_session


class PersonalAccountError(Exception):
    pass


def paused_closure_services():
    if not has_request_context():
        return ()
    settings = session.get('closure_pauses', {})
    if settings.get('user_id') == current_user.get_id():
        return settings.get('services', ())
    return ()


def credential_cipher():
    explicit = current_app.config.get('INCONTROL_CREDENTIALS_KEY')
    key = explicit or base64.urlsafe_b64encode(hashlib.sha256(
        b'incontrol-personal-credentials:v1:' + current_app.secret_key.encode('utf-8')
    ).digest())
    return Fernet(key)


def register_incontrol_accounts(application, db):
    class IncontrolAccount(db.Model):
        __tablename__ = 'incontrol_account'
        __table_args__ = {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
        user_id = db.Column(db.Integer, db.ForeignKey('users.id'), primary_key=True)
        credentials_encrypted = db.Column(db.Text, nullable=False)
        updated_at = db.Column(db.DateTime, nullable=False)

    def read_credentials(user_id):
        row = db.session.get(IncontrolAccount, user_id)
        if row is None:
            raise PersonalAccountError('Сначала сохраните личный логин и пароль InControl.')
        try:
            return json.loads(credential_cipher().decrypt(row.credentials_encrypted.encode()))
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise PersonalAccountError('Не удалось прочитать настройки InControl. Сохраните логин и пароль заново.') from None

    def personal_client(user_id):
        data = read_credentials(user_id)
        # Никакого fallback на общую учётную запись.
        client = IncontrolClient(data['login'], data['password'], timeout=25, interactive=True)
        if isinstance(data.get('auth'), dict) and data['auth'].get('access') and data['auth'].get('refresh'):
            restore_session(client, data['auth'])
        return client

    def remember_auth(user_id, client):
        row = db.session.get(IncontrolAccount, user_id)
        if row is None:
            return
        data = read_credentials(user_id)
        # Не возвращаем старые данные при одновременной смене личного логина/пароля.
        if data['login'] != client.login or data['password'] != client.password:
            return
        auth = capture_session(client)
        if not auth or data.get('auth') == auth:
            return
        data['auth'] = auth
        row.credentials_encrypted = credential_cipher().encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()
        db.session.commit()

    application.extensions['incontrol_accounts'] = {
        'remember_auth': remember_auth,
        'has_credentials': lambda user_id: db.session.get(IncontrolAccount, user_id) is not None,
    }

    @application.route('/settings/incontrol', methods=['GET', 'POST'])
    @login_required
    def incontrol_settings():
        if request.method == 'POST' and request.form.get('action') == 'closure_checks':
            session['closure_pauses'] = {
                'user_id': current_user.get_id(),
                'services': [service for service in ('incontrol', 'yandex_mail')
                             if request.form.get(service) != '1'],
            }
            flash('Настройки закрытия заявок сохранены.', 'success')
            return redirect(url_for('incontrol_settings'))
        login = ''
        configured = False
        error = None
        preparation = None
        try:
            row = db.session.get(IncontrolAccount, current_user.id)
            configured = row is not None
            if configured:
                try:
                    login = read_credentials(current_user.id)['login']
                except PersonalAccountError as exc:
                    error = str(exc)
            if request.method == 'POST':
                action = request.form.get('action', 'save')
                if action == 'save':
                    new_login = request.form.get('incontrol_login', '').strip()
                    password = request.form.get('incontrol_password', '')
                    if not new_login or len(new_login) > 254 or len(password) > 4096:
                        error = 'Укажите корректный логин и пароль InControl.'
                    else:
                        if not password and configured:
                            previous = read_credentials(current_user.id)
                            if new_login == previous['login']:
                                password = previous['password']
                        if not password:
                            error = 'Введите пароль для этого логина.'
                        else:
                            encrypted = credential_cipher().encrypt(json.dumps(
                                {'login': new_login, 'password': password}, ensure_ascii=False
                            ).encode()).decode()
                            if row is None:
                                row = IncontrolAccount(user_id=current_user.id)
                                db.session.add(row)
                            row.credentials_encrypted = encrypted
                            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                            db.session.commit()
                            flash('Личные настройки InControl сохранены.', 'success')
                            return redirect(url_for('incontrol_settings'))
                elif action == 'check':
                    client = personal_client(current_user.id)
                    db.session.rollback()  # Не держать транзакцию во время HTTP.
                    try:
                        ensure_session(client)
                        remember_auth(current_user.id, client)
                        flash('Вход в InControl под вашей учётной записью выполнен.', 'success')
                    except Exception:
                        error = 'Войти в InControl не удалось. Проверьте личные данные и доступность сервиса. Возможно, требуется подтверждение входа.'
                    finally:
                        client.session.close()
                elif action == 'inspect':
                    from incontrol_completion import CompletionPreparation, CompletionPreparationError
                    park = session.get('selected_park')
                    if not park or park not in session.get('available_parks', []):
                        error = 'Выберите доступный парк.'
                    else:
                        value = request.form.get('incontrol_task_id', '').strip()
                        if not value.isdecimal() or not 0 < int(value) <= 2147483647:
                            error = 'Укажите числовой номер заявки InControl.'
                        else:
                            client = personal_client(current_user.id)
                            name = current_user.full_name
                            db.session.rollback()
                            try:
                                ensure_session(client)
                                remember_auth(current_user.id, client)
                                db.session.rollback()
                                preparation = CompletionPreparation(client).inspect(int(value), park, name)
                            except CompletionPreparationError as exc:
                                error = str(exc)
                            except Exception:
                                error = 'Не удалось проверить заявку InControl. Повторите проверку позже.'
                            finally:
                                client.session.close()
                elif action == 'delete':
                    if row is not None:
                        db.session.delete(row)
                        db.session.commit()
                    flash('Личные настройки InControl удалены.', 'success')
                    return redirect(url_for('incontrol_settings'))
                else:
                    error = 'Неизвестное действие.'
        except PersonalAccountError as exc:
            db.session.rollback()
            error = str(exc)
        except SQLAlchemyError:
            db.session.rollback()
            error = 'Настройки пока недоступны. Необходимо создать таблицу incontrol_account на сервере.'
        response = application.make_response(render_template(
            'incontrol_settings.html', incontrol_login=login, configured=configured, settings_error=error,
            preparation=preparation, paused_services=paused_closure_services()))
        response.headers['Cache-Control'] = 'no-store'
        return response

    @application.cli.command('init-incontrol-accounts')
    def init_accounts():
        IncontrolAccount.__table__.create(bind=db.engine, checkfirst=True)
        print('Таблица incontrol_account готова.')

    return IncontrolAccount, personal_client
