"""
Наполнение локальной тестовой базы (SQLite) демо-данными.
Запуск: см. run_local.sh. Только для локальной разработки, не для прода.
"""
import os

# Гарантируем локальные настройки, даже если .env.local не подгрузили
os.environ.setdefault('DATABASE_URL', 'sqlite:///local_dev.db')
os.environ.setdefault('DISABLE_SCHEDULER', '1')
os.environ.setdefault('SECRET_KEY', 'local-dev-secret-not-for-prod')

from app import application, db, User, Task


def seed():
    with application.app_context():
        db.create_all()

        # Тестовые пользователи (пароли пока в открытом виде - как в текущей логике входа)
        if not User.query.filter_by(username='admin').first():
            db.session.add(User(
                username='admin',
                password='admin123',
                full_name='Тестовый Администратор',
                allowed_parks=['14 парк', '1 парк', 'Красная Пахра', '18 парк'],
                default_park='1 парк',
                company='Сетранс',
            ))
        if not User.query.filter_by(username='user').first():
            db.session.add(User(
                username='user',
                password='user123',
                full_name='Тестовый Пользователь',
                allowed_parks=['1 парк'],
                default_park='1 парк',
                company='МГТ',
            ))

        # Пара тестовых заявок
        if Task.query.count() == 0:
            db.session.add(Task(
                board_number='12345',
                description='Замена тормозных колодок',
                status='new',
                tag='in_work',
                park='1 парк',
                contract='Тестовый контракт',
            ))
            db.session.add(Task(
                board_number='67890',
                description='Диагностика двигателя',
                status='new',
                tag='waiting',
                park='1 парк',
                contract='Тестовый контракт',
            ))

        db.session.commit()
        print('Готово. Пользователи: admin/admin123, user/user123')
        print(f'Заявок в базе: {Task.query.count()}')


if __name__ == '__main__':
    seed()
