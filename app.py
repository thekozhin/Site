from flask import Flask #render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import pytz
import os
from flask import jsonify  #request
from sqlalchemy import and_
from docxtpl import DocxTemplate
from flask import send_file, make_response
import io
# from sqlalchemy.orm import joinedload



app = Flask(__name__)
# Указываем полный путь к базе данных
db_path = os.path.join(os.path.dirname(__file__), 'list.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'connect_args': {'check_same_thread': False}}
db = SQLAlchemy(app)


class Task(db.Model):
    __tablename__ = 'task'  # Явно указываем имя таблицы
    id = db.Column(db.Integer, primary_key=True)
    board_number = db.Column(db.String(20), nullable=False)
    description = db.Column(db.Text, nullable=False)
    completed_work = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(pytz.timezone('Europe/Moscow')))
    completed_at = db.Column(db.DateTime)
    status = db.Column(db.String(20), default='in_repair')
    tag = db.Column(db.String(20), default='in_work')

    def moscow_time(self, dt):
        return dt.replace(tzinfo=pytz.utc).astimezone(pytz.timezone('Europe/Moscow'))

    TAG_CHOICES = {
        'in_work': 'В работе',
        'waiting': 'В ожидании',
        'ozch': 'ОЗЧ',
        'no_request': 'Без заявки',
        "to": "ТО"
    }

# Создаем словарь для перевода месяцев
MONTH_NAMES = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "фвгуста",
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

#база данных автобусов
class Car(db.Model):
    __tablename__ = 'cars'
    id = db.Column(db.Integer, primary_key=True)
    bort = db.Column(db.String(20))
    gos_number = db.Column(db.String(20))
    year = db.Column(db.Integer)
    vin = db.Column(db.String(50))
    location = db.Column(db.String(100))
    contract = db.Column(db.String(100))

#база данных электробусов
class Elcar(db.Model):
    __tablename__ = 'elbuses'
    id = db.Column(db.Integer, primary_key=True)
    bort = db.Column(db.String(20))
    gos_number = db.Column(db.String(20))
    year = db.Column(db.Integer)
    vin = db.Column(db.String(50))
    location = db.Column(db.String(100))
    contract = db.Column(db.String(100))

@app.template_filter('nonone')
def nonone_filter(value, default=''):
    return value if value is not None else default

@app.route('/add_car', methods=['GET', 'POST'])
def add_car():
    if request.method == 'POST':
        new_car = Car(
            bort=request.form['bort'],
            gos_number=request.form['gos_number'],
            year=request.form['year'],
            vin=request.form['vin'],
            location=request.form['location'],
            contract=request.form['contract']
        )
        db.session.add(new_car)
        db.session.commit()
        return redirect(url_for('buses'))
    return render_template('edit_car.html', car=None)



#--------ЭЛЕКТРОБУСЫ---------------------
@app.route('/electrobuses')
def electrobuses():
    elbuses = Elcar.query.order_by(Elcar.bort).all()  # Исправлено на Elcar
    for bus in elbuses:
        for field in ['bort', 'gos_number', 'year', 'vin', 'location', 'contract']:
            if getattr(bus, field) is None:
                setattr(bus, field, '')
    return render_template('electrobuses.html', elbuses=elbuses)

@app.route('/add_elcar', methods=['GET', 'POST'])
def add_elcar():
    if request.method == 'POST':
        new_elcar = Elcar(
            bort=request.form['bort'],
            gos_number=request.form['gos_number'],
            year=request.form['year'],
            vin=request.form['vin'],
            location=request.form['location'],
            contract=request.form['contract']
        )
        db.session.add(new_elcar)
        db.session.commit()
        return redirect(url_for('electrobuses'))
    return render_template('edit_elcar.html', elcar=None)

@app.route('/edit_elcar/<int:elcar_id>', methods=['GET', 'POST'])
def edit_elcar(elcar_id):
    elcar = Elcar.query.get_or_404(elcar_id)
    if request.method == 'POST':
        elcar.bort = request.form['bort']
        elcar.gos_number = request.form['gos_number']
        elcar.year = request.form['year']
        elcar.vin = request.form['vin']
        elcar.location = request.form['location']
        elcar.contract = request.form['contract']
        db.session.commit()
        return redirect(url_for('electrobuses'))
    return render_template('edit_elcar.html', elcar=elcar)

@app.route('/delete_elcar/<int:elcar_id>')
def delete_elcar(elcar_id):
    elcar = Elcar.query.get_or_404(elcar_id)
    db.session.delete(elcar)
    db.session.commit()
    return redirect(url_for('electrobuses'))
#------------------------------------------------------------------------------------------------


@app.route('/buses')
def buses():
    cars = Car.query.order_by(Car.bort).all()
    # Заменяем None на пустые строки
    for car in cars:
        for field in ['bort', 'gos_number', 'year', 'vin', 'location', 'contract']:
            if getattr(car, field) is None:
                setattr(car, field, '')
    return render_template('buses.html', cars=cars)

@app.route('/edit_car/<int:car_id>', methods=['GET', 'POST'])
def edit_car(car_id):
    car = Car.query.get_or_404(car_id)
    if request.method == 'POST':
        car.bort = request.form['bort']
        car.gos_number = request.form['gos_number']
        car.year = request.form['year']
        car.vin = request.form['vin']
        car.location = request.form['location']
        car.contract = request.form['contract']
        db.session.commit()
        return redirect(url_for('buses'))
    return render_template('edit_car.html', car=car)



@app.route('/delete_car/<int:car_id>')
def delete_car(car_id):
    car = Car.query.get_or_404(car_id)
    db.session.delete(car)
    db.session.commit()
    return redirect(url_for('buses'))

#---------------ЗАДАЧИ------------------------------------------
@app.route('/edit_description/<int:task_id>', methods=['GET', 'POST'])
def edit_description(task_id):
    task = Task.query.get_or_404(task_id)


    if request.method == 'POST':
        task.description = request.form['description']
        db.session.commit()
        return redirect(url_for('index'))

    return render_template('edit_description.html', task=task)

@app.route('/update_tag/<int:task_id>', methods=['POST'])
def update_tag(task_id):
    task = Task.query.get_or_404(task_id)
    task.tag = request.form['tag']
    db.session.commit()
    return redirect(url_for('index'))


@app.route('/index')
@app.route('/')
def index():
    tasks = Task.query.filter_by(status='in_repair').order_by(Task.created_at.desc()).all()

    # Получаем размеры парков
    fleet_size_2 = FleetSize.query.filter_by(location="216644").first().count if FleetSize.query.filter_by(location="216644").first() else 0
    fleet_size_6 = FleetSize.query.filter_by(location="222150").first().count if FleetSize.query.filter_by(location="222150").first() else 0
    fleet_size_8 = FleetSize.query.filter_by(location="МГТ0105-21").first().count if FleetSize.query.filter_by(location="МГТ0105-21").first() else 0

    # Получаем информацию о контрактах для всех бортов
    board_numbers = [task.board_number for task in tasks]
    cars = {car.bort: car.contract for car in Car.query.filter(Car.bort.in_(board_numbers)).all()}
    elcars = {elcar.bort: elcar.contract for elcar in Elcar.query.filter(Elcar.bort.in_(board_numbers)).all()}

    # Считаем количество бортов в ремонте по каждому контракту
    in_repair_2 = sum(1 for task in tasks if cars.get(task.board_number) == "216644")
    in_repair_6 = sum(1 for task in tasks if cars.get(task.board_number) == "222150")
    in_repair_8 = sum(1 for task in tasks if cars.get(task.board_number) == "МГТ0105-21")

    # Рассчитываем КТГ для каждого контракта
    ktg_2 = ((fleet_size_2 - in_repair_2) / fleet_size_2 * 100) if fleet_size_2 > 0 else 0
    ktg_6 = ((fleet_size_6 - in_repair_6) / fleet_size_6 * 100) if fleet_size_6 > 0 else 0
    ktg_8 = ((fleet_size_8 - in_repair_8) / fleet_size_8 * 100) if fleet_size_8 > 0 else 0

    return render_template('index.html',
                         tasks=tasks,
                         cars=cars,
                         elcars=elcars,
                         ktg_2=round(ktg_2, 2),
                         ktg_6=round(ktg_6, 2),
                         ktg_8=round(ktg_8, 2))

@app.route('/about')
def about():
    #пока по дате завершения "completed_at"
    tasks = Task.query.filter_by(status='completed').order_by(Task.completed_at.desc()).all()
    return render_template('about.html', tasks=tasks)


@app.route('/add_task', methods=['POST'])
def add_task():
    try:
        board_number = request.form['board_number']
        description = request.form['description']

        # Проверка количества строк
        if len(description.split('\n')) > 5:
            # flash('Описание должно содержать не более 5 строк', 'error')
            return redirect(url_for('index'))


        new_task = Task(
            board_number=board_number,
            description=description,
            status='in_repair'
        )

        db.session.add(new_task)
        db.session.commit()

        # flash('Задача успешно добавлена', 'success')
        return redirect(url_for('index'))

    except Exception as e:
        db.session.rollback()
        # flash(f'Ошибка при добавлении задачи: {str(e)}', 'danger')
        return redirect(url_for('index'))
@app.context_processor
def inject_pytz():
    return {'pytz': pytz}

# проверка, что такой борт еще не в списке
@app.route('/check_board', methods=['POST'])
def check_board():
    data = request.get_json()
    board_number = data.get('board_number')

    existing_task = Task.query.filter_by(
        board_number=board_number,
        status='in_repair'
    ).first()

    if existing_task:
        return jsonify({
            'exists': True,
            'description': existing_task.description
        })
    return jsonify({'exists': False})


from flask import render_template, request, redirect, url_for, flash


@app.route('/complete_task/<int:task_id>', methods=['GET', 'POST'])
def complete_task(task_id):
    task = Task.query.get_or_404(task_id)
    # now = datetime.now()

    if request.method == 'POST':
        # Если это подтверждение печати
        if 'print_decision' in request.form:
            if request.form['print_decision'] == 'yes':
                return redirect(url_for('print_task', task_id=task_id))
            else:
                return redirect(url_for('index'))

         # Проверка количества строк
        completed_work = request.form['completed_work']
        if len(completed_work.split('\n')) > 5:
            flash('Максимум 5 строк в описании выполненных работ', 'error')
            return render_template('complete_task.html', task=task, show_print_prompt=False)

        # Если это первоначальное завершение задачи
        task.status = 'completed'
        task.completed_at = datetime.now()
        task.completed_work = request.form['completed_work']
        db.session.commit()

        # Показываем окно подтверждения печати
        return render_template('complete_task.html',
                               task=task,
                               show_print_prompt=True)

    return render_template('complete_task.html', task=task, show_print_prompt=False)

#----------------Отчет-----------------------------------------
#Данные из базы данных количество бортов
class FleetSize(db.Model):
    __tablename__ = 'fleet_size'
    id = db.Column(db.Integer, primary_key=True)
    location = db.Column(db.String(50), unique=True, nullable=False)
    count = db.Column(db.Integer, nullable=False)

#Данные из базы данных для отчета
class Date(db.Model):
    __tablename__ = 'date'
    id = db.Column(db.Integer, primary_key=True)
    location = db.Column(db.String(50), unique=True, nullable=False)
    count = db.Column(db.Integer, nullable=False)

class Report7Table(db.Model):
    __tablename__ = 'report_7'
    id = db.Column(db.Integer, primary_key=True)
    col1 = db.Column(db.String(20))  # 7:00 report
    description = db.Column(db.Text, nullable=False)

class Report830Table(db.Model):
    __tablename__ = 'report_830'
    id = db.Column(db.Integer, primary_key=True)
    col1 = db.Column(db.String(20))  # 8:30 report
    description = db.Column(db.Text, nullable=False)

class Report17Table(db.Model):
        __tablename__ = 'report_17'
        id = db.Column(db.Integer, primary_key=True)
        col1 = db.Column(db.String(20))  # 17:00 report
        description = db.Column(db.Text, nullable=False)

class Report19Table(db.Model):
    __tablename__ = 'report_19'
    id = db.Column(db.Integer, primary_key=True)
    col1 = db.Column(db.String(20))  # 19:00 report
    description = db.Column(db.Text, nullable=False)

class Ktg_na_19(db.Model):
    __tablename__ = 'ktg_na_19'
    id = db.Column(db.Integer, primary_key=True)
    mark_1 = db.Column(db.String(20))  # 1 отметка
    mark_2 = db.Column(db.String(20)) # 2 отметка
    mark_3 = db.Column(db.String(20)) # 3 отметка
    mark_4 = db.Column(db.String(20)) # 4 отметка

@app.route('/reports')
def reports():
    fleet_sizes = FleetSize.query.order_by(FleetSize.id).all()
    dates = Date.query.order_by(Date.id).all()
    now = datetime.now()

    # Фиксируем текущий момент и ровно 24 часа назад
    start_date = now
    end_date = now - timedelta(hours=24)  # строго минус 24 часа от текущего времени

    # Подсчет поступивших заявок за период
    open_tasks_count = Task.query.filter(
        and_(
            Task.status.in_(['in_repair', 'completed']),
            Task.created_at >= end_date,
            Task.created_at < start_date
        )
    ).count()
    # Подсчет поступивших заявок за период
    closed_tasks_count = Task.query.filter(
        and_(
            Task.status.in_(['completed']),
            Task.completed_at >= end_date,
            Task.completed_at < start_date
        )
    ).count()


    # Обновляем значение в таблице date (если нужно)
    date_entry = Date.query.filter_by(location="Открытые заявки").first()
    if date_entry:
        date_entry.count = open_tasks_count
        db.session.commit()

    # Обновляем значение в таблице date (если нужно)
    date_entry = Date.query.filter_by(location="Закрытые заявки").first()
    if date_entry:
        date_entry.count = closed_tasks_count
        db.session.commit()


    return render_template('reports.html', fleet_sizes=fleet_sizes, date=dates,
                         open_tasks_count=open_tasks_count)

@app.route('/update_fleet', methods=['POST'])
def update_fleet():
    data = request.get_json()
    for item in data:
        fleet = FleetSize.query.filter_by(location=item['location']).first()
        if fleet:
            fleet.count = item['count']
    db.session.commit()
    return jsonify({'status': 'success'})

@app.route('/update_date', methods=['POST'])
def update_date():
    data = request.get_json()
    for item in data:
        date_record = Date.query.filter_by(location=item['location']).first()
        if date_record:
            date_record.count = item['count']
    db.session.commit()
    return jsonify({'status': 'success'})




@app.route('/report_7')
def report_7():
    current_date = datetime.now(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y')
    # Получаем данные из базы
    fleet_size = FleetSize.query.filter_by(location="Теплый Стан").first()
    contract_8 = FleetSize.query.filter_by(location="МГТ0105-21").first()


    # Задачи в работе
    in_work = Task.query.filter_by(status='in_repair', tag='in_work').order_by(Task.board_number).all()


    # Задачи в ожидании
    waiting = Task.query.filter_by(status='in_repair').filter(
        (Task.tag == 'waiting') | (Task.tag == 'ozch')
    ).order_by(Task.board_number).all()

    # Расчет КТГ (коэффициент технической готовности)
    # Получаем номера бортов в работе по контракту
    in_work_board_numbers = [t.board_number for t in Task.query.filter_by(
        status='in_repair',
        tag='in_work'
    ).all()]

    contract_vehicles_in_work = Car.query.filter(
        Car.contract == "МГТ0105-21",
        Car.bort.in_(in_work_board_numbers)
    ).count()
    ktg = ((contract_8.count - contract_vehicles_in_work) / contract_8.count) * 100

    # Данные для кузовного ремонта (примерные значения)
    body_repair = {
        'total': 0,
        'warranty': 0,
        'commercial': 0
    }

    pending_body_repair = {
        'warranty': 0,
        'commercial': 0
    }

    # Данные ЕТО
    seasonal_service = Date.query.filter_by(location="Сезонное обслуживание").first().count
    eto_passed = Date.query.filter_by(location="ЕТО").first().count  # Получаем значение с таблицы date параметра ЕТО
    eto_percentage = (eto_passed / fleet_size.count) * 100

    return render_template('report_7.html',
                           date=current_date,
                           fleet_size=fleet_size,
                           in_work=in_work,
                           waiting=waiting,
                           ktg=ktg,
                           body_repair=body_repair,
                           pending_body_repair=pending_body_repair,
                           seasonal_service=seasonal_service,
                           eto_passed=eto_passed,
                           eto_percentage=eto_percentage)

@app.route('/report_14')
def report_14():
    current_date = get_russian_date()

    # Получаем задачи для отчета
    in_work = Task.query.filter_by(status='in_repair').filter(
        (Task.tag == 'in_work') | (Task.tag == 'no_request')
    ).order_by(Task.board_number).all()

    waiting = Task.query.filter_by(status='in_repair').filter(
        (Task.tag == 'waiting') | (Task.tag == 'ozch')
    ).order_by(Task.board_number).all()

    return render_template('report_14.html',
                           date=current_date,
                           in_work=in_work,
                           waiting=waiting)


@app.route('/report_17')
def report_17():
    current_date = datetime.now(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y')

    # Получаем задачи для отчета
    in_work = Task.query.filter_by(status='in_repair').filter(
        (Task.tag == 'in_work') | (Task.tag == 'no_request')
    ).order_by(Task.board_number).all()

    waiting = Task.query.filter_by(status='in_repair').filter(
        (Task.tag == 'waiting') | (Task.tag == 'ozch')
    ).order_by(Task.board_number).all()

    return render_template('report_17.html',
                           date=current_date,
                           in_work=in_work,
                           waiting=waiting)

@app.route('/report_19')
def report_19():
    current_date = datetime.now(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y')

    # Получаем задачи для отчета
    in_work = Task.query.filter_by(status='in_repair').filter(
        (Task.tag == 'in_work') | (Task.tag == 'no_request')
    ).order_by(Task.board_number).all()

    waiting = Task.query.filter_by(status='in_repair').filter(
        (Task.tag == 'waiting') | (Task.tag == 'ozch')
    ).order_by(Task.board_number).all()

    # Получаем данные для расчета процентов
    fleet_size_mgt0105_21 = FleetSize.query.filter_by(location="МГТ0105-21").first().count
    # fleet_size = fleet_size_mgt0105_21.count if fleet_size_mgt0105_21 else 0

    # Получаем количество бортов в каждом отчете для контракта МГТ0105-21
    report_7_count = db.session.query(Car).join(Report7Table, Car.bort == Report7Table.col1) \
        .filter(Car.contract == "МГТ0105-21").count()
    report_7 = round((fleet_size_mgt0105_21 - report_7_count) / fleet_size_mgt0105_21 * 100, 2)
    print(report_7)


    report_830_count = db.session.query(Car).join(Report830Table, Car.bort == Report830Table.col1) \
        .filter(Car.contract == "МГТ0105-21").count()
    report_830 = round((fleet_size_mgt0105_21 - report_830_count) / fleet_size_mgt0105_21 * 100, 2)
    print(report_830)

    report_17_count = db.session.query(Car).join(Report17Table, Car.bort == Report17Table.col1) \
        .filter(Car.contract == "МГТ0105-21").count()
    report_17 = round((fleet_size_mgt0105_21 - report_17_count) / fleet_size_mgt0105_21 * 100, 2)

    report_19_count = db.session.query(Car).join(Report19Table, Car.bort == Report19Table.col1) \
        .filter(Car.contract == "МГТ0105-21").count()
    report_19 = round((fleet_size_mgt0105_21 - report_19_count) / fleet_size_mgt0105_21 * 100, 2)

    # # Рассчитываем проценты для каждого отчета
    # percentages = []
    # if fleet_size > 0:
    #     percentages.append(((fleet_size - report_7_count) / fleet_size * 100))
    #     percentages.append(((fleet_size - report_830_count) / fleet_size * 100))
    #     percentages.append(((fleet_size - report_17_count) / fleet_size * 100))
    #     percentages.append(((fleet_size - report_19_count) / fleet_size * 100))

    # Рассчитываем средний процент
    avg_percentage = round((report_7+report_830+report_17+report_19) / 4, 2)

    return render_template('report_19.html',
                         date=current_date,
                         in_work=in_work,
                         waiting=waiting,
                         avg_percentage=avg_percentage,
                         # fleet_size=fleet_size,
                         report_counts={
                             '7': report_7_count,
                             '8_30': report_830_count,
                             '17': report_17_count,
                             '19': report_19_count
                         })


@app.route('/itr_7')
def itr_7():
    # Получаем все данные из таблицы date
    date_data = db.session.query(Date).order_by(Date.id).all()
    current_date = get_russian_date()

    return render_template('itr_7.html',
                           date=current_date,
                           date_data=date_data)






@app.route('/itr_19')
def itr_19():
    current_date = datetime.now(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y')

    # Получаем данные из всех таблиц
    report7_data = {item.id: item.col1 for item in Report7Table.query.all()}
    report830_data = {item.id: item.col1 for item in Report830Table.query.all()}
    report17_data = {item.id: item.col1 for item in Report17Table.query.all()}
    report19_data = {item.id: item.col1 for item in Report19Table.query.all()}

    # Получаем размер парка МГТ0105-21
    fleet_size_mgt0105_21 = FleetSize.query.filter_by(location="МГТ0105-21").first().count

    # Получаем количество бортов в каждом отчете для контракта МГТ0105-21
    report_7_count = db.session.query(Car).join(Report7Table, Car.bort == Report7Table.col1) \
        .filter(Car.contract == "МГТ0105-21").count()

    report_830_count = db.session.query(Car).join(Report830Table, Car.bort == Report830Table.col1) \
        .filter(Car.contract == "МГТ0105-21").count()

    report_17_count = db.session.query(Car).join(Report17Table, Car.bort == Report17Table.col1) \
        .filter(Car.contract == "МГТ0105-21").count()

    report_19_count = db.session.query(Car).join(Report19Table, Car.bort == Report19Table.col1) \
        .filter(Car.contract == "МГТ0105-21").count()

    # Собираем все уникальные ID из всех таблиц
    all_ids = set()
    all_ids.update(report7_data.keys())
    all_ids.update(report830_data.keys())
    all_ids.update(report17_data.keys())
    all_ids.update(report19_data.keys())

    # Создаем сводные данные
    combined_data = []
    for id in sorted(all_ids):
        combined_data.append({
            'id': id,
            'col1': report7_data.get(id, ''),
            'col2': report830_data.get(id, ''),
            'col3': report17_data.get(id, ''),
            'col4': report19_data.get(id, '')
        })

    return render_template('itr_19.html',
                           date=current_date,
                           table_data=combined_data,
                           fleet_size_mgt0105_21=fleet_size_mgt0105_21,
                           report_7_count=report_7_count,
                           report_830_count=report_830_count,
                           report_17_count=report_17_count,
                           report_19_count=report_19_count)


@app.route('/save_7', methods=['POST'])
def save_7():
    try:
        # Получаем все задачи со статусом in_work и тегом in_repair
        in_work_tasks = Task.query.filter(
            Task.status == "in_repair", # в состоянии в работе
            Task.tag.in_(['in_work', 'waiting',"ozch"]) # cо всеми статусами
        ).all()

        # Очищаем предыдущие данные
        db.session.query(Report7Table).delete()

        # Сохраняем новые данные (бортовой номер и описание)
        for task in in_work_tasks:
            new_entry = Report7Table(
                col1=task.board_number,
                description=task.description  # Добавляем описание
            )
            db.session.add(new_entry)

        db.session.commit()

        return jsonify({
            'status': 'success',
            'message': f'Сохранено {len(in_work_tasks)} бортов с описаниями в отчет 17:00',
            'count': len(in_work_tasks)
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/save_17', methods=['POST'])
def save_17():
    try:
        # Получаем все задачи со статусом in_work и тегом in_repair
        in_work_tasks = Task.query.filter(
            Task.status == "in_repair", # в состоянии в работе
            Task.tag.in_(['in_work', 'waiting',"ozch"]) # cо всеми статусами
        ).all()

        # Очищаем предыдущие данные
        db.session.query(Report17Table).delete()

        # Сохраняем новые данные (бортовой номер и описание)
        for task in in_work_tasks:
            new_entry = Report17Table(
                col1=task.board_number,
                description=task.description  # Добавляем описание
            )
            db.session.add(new_entry)

        db.session.commit()

        return jsonify({
            'status': 'success',
            'message': f'Сохранено {len(in_work_tasks)} бортов с описаниями в отчет 17:00',
            'count': len(in_work_tasks)
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500


# Маршрут для сохранения данных 19:00
@app.route('/save_19', methods=['POST'])
def save_19():
    try:
        # Получаем все задачи со статусом in_work и тегом in_repair
        in_work_tasks = Task.query.filter(
            Task.status == "in_repair", # в состоянии в работе
            Task.tag.in_(['in_work', 'waiting',"ozch"]) # cо всеми статусами
        ).all()

        # Очищаем предыдущие данные
        db.session.query(Report19Table).delete()

        # Сохраняем новые данные (бортовой номер и описание)
        for task in in_work_tasks:
            new_entry = Report19Table(
                col1=task.board_number,
                description=task.description  # Добавляем описание
            )
            db.session.add(new_entry)

        db.session.commit()

        return jsonify({
            'status': 'success',
            'message': f'Сохранено {len(in_work_tasks)} бортов с описаниями в отчет 19:00',
            'count': len(in_work_tasks)
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/print_list')
def print_list():


        return render_template('print_list.html')


@app.route('/print_task/<int:task_id>')
def print_task(task_id):
    # Получаем данные заявки
    task = Task.query.get_or_404(task_id)
    current_date = get_russian_date()

    # Ищем автомобиль по бортовому номеру
    car = Car.query.filter_by(bort=task.board_number).first()
    elcar = Elcar.query.filter_by(bort=task.board_number).first()

    # Определяем, какой транспорт (автобус или электробус)
    vehicle = car if car else elcar

    # Разделяем описание на строки
    description_lines = task.description.split('\n') if task.description else []
    # Заполняем до 5 строк пустыми значениями
    while len(description_lines) < 5:
        description_lines.append('')

    # Разделяем описание неисправности на строки
    completed_lines = task.completed_work.split('\n') if task.completed_work else []
    # Заполняем до 5 строк пустыми значениями
    while len(completed_lines) < 5:
        completed_lines.append('')

    # Загружаем шаблон
    doc = DocxTemplate("template.docx")

    # Подготавливаем контекст с данными заявки и автомобиля
    context = {
        'task_id': task.id,
        'board_number': task.board_number,
        "created_at": task.created_at.strftime('%d.%m.%Y'),
        "h": task.created_at.strftime('%H'),
        "m": task.created_at.strftime('%M'),
        "date_go": task.created_at.strftime('%d.%m.%y'),
        "time_go": task.created_at.strftime('%H:%M'),
        "date_end": task.completed_at.strftime('%d.%m.%y'),
        "time_end": task.completed_at.strftime('%H:%M'),
        'completed_at': task.completed_at.strftime('%d.%m.%Y %H:%M') if task.completed_at else '',
        'status': task.status,
        'description_1': description_lines[0] if len(description_lines) > 0 else '',
        'description_2': description_lines[1] if len(description_lines) > 1 else '',
        'description_3': description_lines[2] if len(description_lines) > 2 else '',
        'description_4': description_lines[3] if len(description_lines) > 3 else '',
        'description_5': description_lines[4] if len(description_lines) > 4 else '',
        'completed_work_1': completed_lines[0] if len(completed_lines) > 0 else '',
        'completed_work_2': completed_lines[1] if len(completed_lines) > 1 else '',
        'completed_work_3': completed_lines[2] if len(completed_lines) > 2 else '',
        'completed_work_4': completed_lines[3] if len(completed_lines) > 3 else '',
        'completed_work_5': completed_lines[4] if len(completed_lines) > 4 else '',
        'vehicle': {
            'bort': vehicle.bort if vehicle else 'Не указан',
            'gos_number': vehicle.gos_number if vehicle else 'Не указан',
            'year': vehicle.year if vehicle else 'Не указан',
            'vin': vehicle.vin if vehicle else 'Не указан',
            'location': vehicle.location if vehicle else 'Не указан',
            'contract': vehicle.contract if vehicle else 'Не указан'
        } if vehicle else None,
    }
    # Подставляем значения в шаблон
    doc.render(context)

    # Сохраняем в буфер
    file_stream = io.BytesIO()
    doc.save(file_stream)
    file_stream.seek(0)

    # Формируем имя файла
    filename = f"Заявка_{task.id}_{task.board_number}.docx"

    # Создаем ответ с файлом
    response = make_response(send_file(
        file_stream,
        as_attachment=True,
        download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    ))

    # Добавляем JavaScript для закрытия окна после загрузки
    response.headers['Refresh'] = '1; url=' + url_for('index')
    return response


if __name__ == '__main__':
    with app.app_context():
        db.create_all()  # Создаст таблицу если ее нет

    app.run(debug=True)
