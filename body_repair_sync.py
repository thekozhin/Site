"""Manual two-way synchronization for park body repair lists."""

import os
import re
from datetime import datetime

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.utils import rowcol_to_a1
from io import BytesIO

from flask import abort, flash, redirect, render_template, request, send_file, session, url_for
from flask_login import current_user
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from sqlalchemy import inspect, text

# Do not start the application's other scheduled jobs when this file is run manually.
if __name__ == '__main__':
    os.environ['RUN_SCHEDULER'] = '0'
    import app as _app  # noqa: F401
    from body_repair_sync import sync_body_repair_from_sheets

    print(sync_body_repair_from_sheets())
    raise SystemExit(0)

from app import (  # noqa: E402
    Car,
    Elcar,
    application,
    db,
    normalize_task_board,
    tz,
)


class BodyRepair(db.Model):
    """Кузовной ремонт: активные и завершённые записи по паркам."""

    __tablename__ = 'body_repair'
    __table_args__ = {
        'mysql_charset': 'utf8mb4',
        'mysql_collate': 'utf8mb4_unicode_ci',
    }

    id = db.Column(db.Integer, primary_key=True)
    park = db.Column(db.String(50), nullable=False, index=True)
    bort = db.Column(db.String(20), nullable=False, index=True)
    vin = db.Column(db.String(50), nullable=True)
    work_order = db.Column(db.String(50), nullable=True)
    reason = db.Column(db.Text, nullable=True)
    repair_status = db.Column(db.String(30), nullable=True, index=True)
    started_at = db.Column(db.Date, nullable=True)
    note = db.Column(db.Text, nullable=True)
    repair_type = db.Column(db.String(30), nullable=True)
    source_link = db.Column(db.String(1000), nullable=True)
    is_approved = db.Column(db.Boolean, nullable=False, default=False, server_default=text('0'))
    reopen_pending = db.Column(db.Boolean, nullable=False, default=False, server_default=text('0'))
    is_completed = db.Column(db.Boolean, nullable=False, default=False, index=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_by = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


SPREADSHEET_ID = '1HaqhdD2qZuC3iimiEdtl4xmZ2pjw9ngEzKX_tSXF2WU'
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), 'credentials.json')
GOOGLE_SCOPES = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive',
]
PARK_NAME = '14 парк'
SHEET_CONFIGS = (
    {
        'gid': 870612608,
        'parks': {'14 парк': 'Теплый Стан', '1 парк': 'Коньково', 'Красная Пахра': 'Красная Пахра'},
        'status_col': 13,  # N: incoming status and outgoing status
        'work_order_col': 11,  # L
        'reason_col': 15,  # P: причина обращения с сайта
        'note': 'МГТ0058',
    },
    {
        'gid': 1297725234,
        'parks': {'14 парк': 'Теплый Стан'},
        'status_col': 13,  # N: статус (актуальная структура листа)
        'work_order_col': 10,  # K
        'reason_col': 15,  # P: комментарий СЭТранс, не колонка статуса
        'note': 'МГТ0142',
    },
)
STATUS_TO_SHEET = {
    'in_work': 'В работе',
    'approval_mgt': 'Согласование с МГТ',
    'waiting_parts': 'Ожидание ЗЧ',
}



BODY_REPAIR_STATUSES = {
    'in_work': 'В работе',
    'approval_mgt': 'Согласование с МГТ',
    'waiting_parts': 'Ожидание ЗЧ',
}
BODY_REPAIR_TYPES = {
    'commercial': 'Коммерческий',
    'warranty': 'Гарантийный',
}


_body_repair_schema_ready = False


def ensure_body_repair_table():
    """Создаёт таблицу и приводит уже созданную таблицу к utf8mb4."""
    global _body_repair_schema_ready
    if _body_repair_schema_ready:
        return

    BodyRepair.__table__.create(bind=db.engine, checkfirst=True)
    if db.engine.dialect.name == 'mysql':
        with db.engine.begin() as connection:
            table_info = connection.execute(
                text("SHOW TABLE STATUS LIKE 'body_repair'")
            ).mappings().first()
            collation = table_info.get('Collation') if table_info else None
            if collation and not collation.lower().startswith('utf8mb4_'):
                connection.execute(text(
                    'ALTER TABLE body_repair '
                    'CONVERT TO CHARACTER SET utf8mb4 '
                    'COLLATE utf8mb4_unicode_ci'
                ))

    column_names = {
        column['name'] for column in inspect(db.engine).get_columns('body_repair')
    }
    missing_columns = []
    if 'repair_type' not in column_names:
        missing_columns.append(
            "ADD COLUMN repair_type VARCHAR(30) NULL DEFAULT 'commercial'"
        )
    if 'source_link' not in column_names:
        missing_columns.append('ADD COLUMN source_link VARCHAR(1000) NULL')
    if missing_columns:
        with db.engine.begin() as connection:
            for definition in missing_columns:
                connection.execute(text(f'ALTER TABLE body_repair {definition}'))

    if db.engine.dialect.name == 'mysql':
        with db.engine.begin() as connection:
            connection.execute(text(
                'ALTER TABLE body_repair '
                'MODIFY COLUMN reason TEXT NULL, '
                'MODIFY COLUMN repair_status VARCHAR(30) NULL, '
                'MODIFY COLUMN started_at DATE NULL, '
                'MODIFY COLUMN repair_type VARCHAR(30) NULL'
            ))

    _body_repair_schema_ready = True

def register_body_repair_routes(application):
    @application.route('/body-repair')
    def body_repair():
        if not current_user.is_authenticated:
            return redirect(url_for('login'))

        ensure_body_repair_table()
        available_parks = session.get('available_parks', [])
        selected_park = session.get('selected_park')
        if selected_park and selected_park not in available_parks:
            flash(f'Доступ к парку {selected_park} запрещен', 'error')
            selected_park = available_parks[0] if available_parks else None
            session['selected_park'] = selected_park

        tab = request.args.get('tab', 'in_work')
        if tab not in ('in_work', 'ready'):
            tab = 'in_work'

        repairs = []
        in_repair_count = 0
        if selected_park:
            in_repair_count = BodyRepair.query.filter(
                BodyRepair.park == selected_park,
                BodyRepair.is_completed.is_(False),
            ).count()
            repairs = BodyRepair.query.filter_by(
                park=selected_park,
                is_completed=(tab == 'ready')
            ).order_by(
                BodyRepair.completed_at.desc() if tab == 'ready' else BodyRepair.started_at.desc(),
                BodyRepair.id.desc()
            ).all()

        return render_template(
            'body_repair.html',
            repairs=repairs,
            in_repair_count=in_repair_count,
            selected_park=selected_park,
            active_tab=tab,
            statuses=BODY_REPAIR_STATUSES,
            repair_types=BODY_REPAIR_TYPES,
            today=datetime.now(tz).date().isoformat(),
            scroll_y=request.args.get('scroll_y', 0, type=int),
        )


    @application.route('/body-repair/add', methods=['POST'])
    def add_body_repair():
        if not current_user.is_authenticated:
            return redirect(url_for('login'))

        ensure_body_repair_table()
        available_parks = session.get('available_parks', [])
        selected_park = session.get('selected_park')
        if not selected_park or selected_park not in available_parks:
            abort(403)

        bort = request.form.get('bort', '').strip()
        reason = request.form.get('reason', '').strip()
        repair_type = request.form.get('repair_type', '')
        started_at_raw = request.form.get('started_at', '').strip()
        if not bort or not reason or repair_type not in BODY_REPAIR_TYPES:
            flash('Укажите борт, причину обращения и вид кузовного ремонта.', 'error')
            return redirect(url_for('body_repair'))
        try:
            started_at = datetime.strptime(started_at_raw, '%Y-%m-%d').date()
        except ValueError:
            flash('Укажите корректную дату начала ремонта.', 'error')
            return redirect(url_for('body_repair'))

        vehicle = Car.query.filter_by(bort=bort, location=selected_park).first()
        if vehicle is None:
            vehicle = Elcar.query.filter_by(bort=bort, location=selected_park).first()

        repair = BodyRepair(
            park=selected_park,
            bort=bort,
            vin=vehicle.vin if vehicle else None,
            work_order=request.form.get('work_order', '').strip() or None,
            reason=reason,
            repair_status='in_work',
            started_at=started_at,
            note=request.form.get('note', '').strip() or None,
            repair_type=repair_type,
            created_by=current_user.username,
        )
        db.session.add(repair)
        db.session.commit()
        flash(f'Борт {bort} добавлен в кузовной ремонт.', 'success')
        return redirect(url_for('body_repair'))


    @application.route('/body-repair/<int:repair_id>/approval', methods=['POST'])
    def update_body_repair_approval(repair_id):
        if not current_user.is_authenticated:
            return redirect(url_for('login'))

        ensure_body_repair_table()
        selected_park = session.get('selected_park')
        if not selected_park or selected_park not in session.get('available_parks', []):
            abort(403)
        repair = BodyRepair.query.filter_by(id=repair_id, park=selected_park).first_or_404()
        if repair.is_completed:
            abort(400)
        approved = request.form.get('is_approved', '0')
        if approved not in ('0', '1'):
            abort(400)
        # Сохраняем конкретное значение, повторная отправка не меняет его обратно.
        repair.is_approved = approved == '1'
        db.session.commit()
        flash(
            f'Заявка по борту {repair.bort} согласована.' if repair.is_approved else
            f'Отметка согласования по борту {repair.bort} снята.',
            'success',
        )
        return redirect(_body_repair_return_url())


    @application.route('/body-repair/<int:repair_id>/status', methods=['POST'])
    def update_body_repair_status(repair_id):
        if not current_user.is_authenticated:
            return redirect(url_for('login'))

        ensure_body_repair_table()
        available_parks = session.get('available_parks', [])
        selected_park = session.get('selected_park')
        if not selected_park or selected_park not in available_parks:
            abort(403)
        repair = BodyRepair.query.filter_by(id=repair_id, park=selected_park).first_or_404()
        if repair.is_completed:
            abort(400)
        status = request.form.get('repair_status', '')
        if status and status not in BODY_REPAIR_STATUSES:
            abort(400)
        repair.repair_status = status or None
        db.session.commit()
        flash(f'Статус борта {repair.bort} обновлён.', 'success')
        return redirect(_body_repair_return_url())


    @application.route('/body-repair/<int:repair_id>/complete', methods=['POST'])
    def complete_body_repair(repair_id):
        if not current_user.is_authenticated:
            return redirect(url_for('login'))

        ensure_body_repair_table()
        available_parks = session.get('available_parks', [])
        selected_park = session.get('selected_park')
        if not selected_park or selected_park not in available_parks:
            abort(403)
        repair = BodyRepair.query.filter_by(id=repair_id, park=selected_park).first_or_404()
        repair.is_completed = True
        repair.completed_at = datetime.now(tz)
        repair.reopen_pending = False
        db.session.commit()
        flash(f'Ремонт борта {repair.bort} завершён.', 'success')
        return redirect(url_for('body_repair', tab='ready'))


    @application.route('/body-repair/<int:repair_id>/reopen', methods=['POST'])
    def reopen_body_repair(repair_id):
        if not current_user.is_authenticated:
            return redirect(url_for('login'))

        ensure_body_repair_table()
        selected_park = session.get('selected_park')
        if not selected_park or selected_park not in session.get('available_parks', []):
            abort(403)
        repair = BodyRepair.query.filter_by(id=repair_id, park=selected_park).with_for_update().first_or_404()
        if not repair.is_completed:
            flash(f'Борт {repair.bort} уже в работе.', 'info')
            return redirect(url_for('body_repair', tab='in_work'))

        repair.is_completed = False
        repair.completed_at = None
        repair.repair_status = 'in_work'
        # Локальный возврат имеет приоритет над прежним «Готово» в Google
        # до успешной отправки обновлённого статуса при синхронизации.
        repair.reopen_pending = bool(repair.source_link and repair.park in {'14 парк', '1 парк', 'Красная Пахра'})
        db.session.commit()
        flash(f'Борт {repair.bort} возвращён в работу.', 'success')
        return redirect(url_for('body_repair', tab='in_work'))


    @application.route('/body-repair/<int:repair_id>/edit', methods=['POST'])
    def update_body_repair(repair_id):
        if not current_user.is_authenticated:
            return redirect(url_for('login'))

        ensure_body_repair_table()
        selected_park = session.get('selected_park')
        if not selected_park or selected_park not in session.get('available_parks', []):
            abort(403)
        repair = BodyRepair.query.filter_by(
            id=repair_id, park=selected_park, is_completed=False
        ).first_or_404()

        bort = request.form.get('bort', '').strip()
        reason = request.form.get('reason', '').strip()
        status = request.form.get('repair_status', '')
        repair_type = request.form.get('repair_type', '')
        started_at_raw = request.form.get('started_at', '').strip()
        try:
            started_at = datetime.strptime(started_at_raw, '%Y-%m-%d').date() if started_at_raw else None
        except (TypeError, ValueError):
            started_at = None
        if (not bort
                or (status and status not in BODY_REPAIR_STATUSES)
                or (repair_type and repair_type not in BODY_REPAIR_TYPES)):
            flash('Проверьте заполнение редактируемой записи.', 'error')
            return redirect(url_for('body_repair'))

        vehicle = Car.query.filter_by(bort=bort, location=selected_park).first()
        if vehicle is None:
            vehicle = Elcar.query.filter_by(bort=bort, location=selected_park).first()
        repair.bort = bort
        repair.vin = vehicle.vin if vehicle else None
        repair.work_order = request.form.get('work_order', '').strip() or None
        repair.reason = reason or None
        repair.repair_status = status or None
        repair.started_at = started_at
        repair.note = request.form.get('note', '').strip() or None
        repair.repair_type = repair_type or None
        db.session.commit()
        flash(f'Запись по борту {bort} обновлена.', 'success')
        return redirect(_body_repair_return_url())


    def _body_repair_return_url():
        tab = request.form.get('tab', 'in_work')
        if tab not in ('in_work', 'ready'):
            tab = 'in_work'
        scroll_y = max(request.form.get('scroll_y', 0, type=int), 0)
        return url_for('body_repair', tab=tab, scroll_y=scroll_y)


    @application.route('/body-repair/export.xlsx')
    def export_body_repair():
        if not current_user.is_authenticated:
            return redirect(url_for('login'))

        ensure_body_repair_table()
        selected_park = session.get('selected_park')
        if not selected_park or selected_park not in session.get('available_parks', []):
            abort(403)

        in_work = BodyRepair.query.filter_by(
            park=selected_park, is_completed=False
        ).order_by(BodyRepair.started_at, BodyRepair.bort).all()
        ready = BodyRepair.query.filter_by(
            park=selected_park, is_completed=True
        ).order_by(BodyRepair.completed_at.desc(), BodyRepair.bort).all()

        workbook = Workbook()
        columns = [
            ('Бортовой', 'bort'),
            ('ВИН номер', 'vin'),
            ('Номер заказ-наряда', 'work_order'),
            ('Причина обращения', 'reason'),
            ('Статус', 'repair_status'),
            ('Дата начала', 'started_at'),
            ('Примечание', 'note'),
            ('Вид ремонта', 'repair_type'),
            ('Ссылка', 'source_link'),
        ]
        header_fill = PatternFill(fill_type='solid', fgColor='D9EAF7')
        header_font = Font(bold=True)

        def fill_sheet(sheet, title, records, include_completion_date=False):
            sheet.title = title
            headers = [label for label, _ in columns]
            if include_completion_date:
                headers.append('Дата завершения')
            sheet.append(headers)
            for cell in sheet[1]:
                cell.fill = header_fill
                cell.font = header_font
            for repair in records:
                row = []
                for _, attribute in columns:
                    value = getattr(repair, attribute)
                    if attribute == 'repair_status':
                        value = BODY_REPAIR_STATUSES.get(value, value)
                    elif attribute == 'repair_type':
                        value = BODY_REPAIR_TYPES.get(value, value)
                    elif attribute == 'started_at' and value:
                        value = value.strftime('%d.%m.%Y')
                    row.append(value)
                if include_completion_date:
                    row.append(repair.completed_at.strftime('%d.%m.%Y %H:%M') if repair.completed_at else '')
                sheet.append(row)
                link_cell = sheet.cell(row=sheet.max_row, column=len(columns))
                if repair.source_link:
                    link_cell.hyperlink = repair.source_link
                    link_cell.style = 'Hyperlink'
            sheet.freeze_panes = 'A2'
            sheet.auto_filter.ref = sheet.dimensions
            for cells in sheet.columns:
                width = min(max(max(len(str(cell.value or '')) for cell in cells) + 2, 12), 50)
                sheet.column_dimensions[cells[0].column_letter].width = width

        fill_sheet(workbook.active, 'В работе', in_work)
        fill_sheet(workbook.create_sheet(), 'Готовые', ready, include_completion_date=True)

        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        filename = f"кузовной_ремонт_{selected_park.replace(' ', '_')}_{datetime.now(tz).strftime('%Y%m%d')}.xlsx"
        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )


    @application.route('/body-repair/sync', methods=['POST'])
    def sync_body_repair_now():
        if not current_user.is_authenticated:
            return redirect(url_for('login'))

        selected_park = session.get('selected_park')
        if selected_park not in {'14 парк', '1 парк', 'Красная Пахра'} or selected_park not in session.get('available_parks', []):
            abort(403)

        result = sync_body_repair_from_sheets(park=selected_park)
        if result.get('status') == 'error':
            flash('Синхронизация не выполнена. Проверьте журнал приложения.', 'error')
            return redirect(url_for('body_repair', tab=request.form.get('tab', 'in_work')))

        sheet_results = result.get('results', [])
        imported_count = sum(item.get('imported', 0) for item in sheet_results)
        written_count = sum(item.get('written', 0) for item in sheet_results)
        missing_link_count = sum(item.get('missing_link', 0) for item in sheet_results)
        if result.get('status') == 'partial':
            flash(
                f'Синхронизация выполнена частично: добавлено {imported_count}, '
                f'ячеек передано в Google: {written_count}. Проверьте журнал приложения.',
                'warning',
            )
        else:
            message = f'Синхронизация завершена: добавлено заявок — {imported_count}, изменений отправлено в Google — {written_count}.'
            if missing_link_count:
                message += f' Пропущено строк без ссылки в колонке E — {missing_link_count}.'
            flash(message, 'success')
        return redirect(url_for('body_repair', tab=request.form.get('tab', 'in_work')))

def _get_sheet(spreadsheet, gid):
    try:
        return spreadsheet.get_worksheet_by_id(gid)
    except AttributeError:
        return next(sheet for sheet in spreadsheet.worksheets() if sheet.id == gid)


def _cell(row, index):
    return str(row[index]).strip() if index < len(row) and row[index] is not None else ''


def _cell_link(cell_data, displayed_value=''):
    if not cell_data:
        return displayed_value if displayed_value.startswith(('https://', 'http://')) else ''

    link = cell_data.get('hyperlink')
    if link:
        return link.strip()

    for run in cell_data.get('textFormatRuns', []):
        link = run.get('format', {}).get('link', {}).get('uri')
        if link:
            return link.strip()

    entered = cell_data.get('userEnteredValue', {})
    formula = entered.get('formulaValue', '')
    match = re.match(r'^\s*=HYPERLINK\(\s*"([^"]+)"', formula, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    value = entered.get('stringValue') or cell_data.get('effectiveValue', {}).get('stringValue') or displayed_value
    value = str(value or '').strip()
    return value if value.startswith(('https://', 'http://')) else ''


def _get_links_by_row(spreadsheet, worksheet, last_row):
    if last_row < 2:
        return {}
    quoted_title = worksheet.title.replace("'", "''")
    metadata = spreadsheet.fetch_sheet_metadata(params={
        'includeGridData': 'true',
        'ranges': f"'{quoted_title}'!E1:E{last_row}",
    })
    sheet_metadata = next(
        (item for item in metadata.get('sheets', []) if item.get('properties', {}).get('sheetId') == worksheet.id),
        {},
    )
    grid_data = sheet_metadata.get('data', [])
    row_data = grid_data[0].get('rowData', []) if grid_data else []
    links = {}
    for index, row in enumerate(row_data, start=1):
        cells = row.get('values', [])
        cell_data = cells[0] if cells else {}
        link = _cell_link(cell_data)
        if link:
            links[index] = link
    return links


def _vehicle_vin(board, park):
    vehicle = Car.query.filter_by(bort=board, location=park).first()
    if vehicle is None:
        vehicle = Elcar.query.filter_by(bort=board, location=park).first()
    return vehicle.vin if vehicle else None


def _date_cell_matches(raw_value, started_at):
    raw_value = (raw_value or '').strip()
    if not raw_value:
        return False
    if raw_value == started_at.isoformat() or raw_value == started_at.strftime('%d.%m.%Y'):
        return True
    for fmt in ('%m/%d/%Y', '%d/%m/%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(raw_value, fmt).date() == started_at
        except ValueError:
            continue
    return False


def _outgoing_value(value):
    """Turn the dash placeholder into an empty Google Sheets cell."""
    if value is None:
        return None
    if str(value).strip() == '-':
        return ''
    return value


def _sheet_columns(rows, config):
    """Определять назначение столбцов по заголовкам, а не по старой позиции."""
    def normalized(value):
        return re.sub(r'\s+', ' ', str(value or '').strip().casefold().replace('ё', 'е'))
    headers = [normalized(value) for value in (rows[0] if rows else [])]
    columns = {name: config[name] for name in ('status_col', 'work_order_col', 'reason_col')}
    names = {'status_col': 'статус', 'work_order_col': 'заказ-наряд', 'reason_col': 'комментарий сэтранс'}
    for key, title in names.items():
        matches = [index for index, header in enumerate(headers) if header == title]
        if len(matches) > 1:
            raise ValueError(f'Неоднозначная колонка «{title}»: синхронизация остановлена.')
        if matches:
            columns[key] = matches[0]
        elif key == 'status_col' and any(headers):
            raise ValueError('Колонка «Статус» не найдена: синхронизация остановлена.')
    if len(set(columns.values())) != len(columns):
        raise ValueError('Колонки статуса, заказ-наряда и причины совпадают: синхронизация остановлена.')
    return columns


def _sync_one_sheet(spreadsheet, config, worksheet, park=PARK_NAME):
    source_park = config['parks'][park]
    rows = worksheet.get_all_values()
    links_by_row = _get_links_by_row(spreadsheet, worksheet, len(rows))
    row_numbers_by_link = {link: row_number for row_number, link in links_by_row.items()}
    columns = _sheet_columns(rows, config)
    status_col = columns['status_col']
    work_order_col = columns['work_order_col']
    reason_col = columns['reason_col']
    active_rows = {}
    ready_links = set()
    imported_count = 0
    updated_count = 0
    missing_link_count = 0
    missing_board_count = 0

    existing_records = BodyRepair.query.filter_by(park=park).filter(
        BodyRepair.source_link.isnot(None)
    ).all()
    records_by_link = {record.source_link: record for record in existing_records}

    for row_number, row in enumerate(rows[1:], start=2):
        if _cell(row, 7).casefold() != source_park.casefold():
            continue
        google_status = _cell(row, status_col).casefold()
        link = links_by_row.get(row_number)
        if google_status in {'готов', 'готово'}:
            if link:
                ready_links.add(link)
            continue
        if not link:
            missing_link_count += 1
            continue

        bort = normalize_task_board(_cell(row, 5))
        if not bort:
            missing_board_count += 1
            continue

        active_rows[link] = row_number
        repair = records_by_link.get(link)
        if repair is None:
            repair = BodyRepair(
                park=park,
                bort=bort,
                vin=_vehicle_vin(bort, park),
                work_order=None,
                reason=None,
                repair_status=None,
                started_at=None,
                repair_type=None,
                source_link=link,
                note=config['note'],
                is_completed=False,
            )
            db.session.add(repair)
            records_by_link[link] = repair
            imported_count += 1
        else:
            repair.bort = bort
            repair.note = config['note']
            current_vin = _vehicle_vin(bort, park)
            if current_vin:
                repair.vin = current_vin
            updated_count += 1

    now = datetime.now(tz)
    for link in ready_links:
        repair = records_by_link.get(link)
        if repair:
            repair.note = config['note']
            if not repair.is_completed and not repair.reopen_pending:
                repair.is_completed = True
                repair.completed_at = now

    db.session.commit()

    # Write populated fields back by the URL in column E; blank DB fields leave
    # the existing Google cell untouched.
    updates = []
    reopened_records = []
    sync_rows = dict(active_rows)
    sync_rows.update({
        link: row_numbers_by_link[link]
        for link in ready_links
        if link in row_numbers_by_link
    })
    for link, row_number in sync_rows.items():
        repair = records_by_link.get(link)
        if repair is None:
            continue
        row = rows[row_number - 1]
        reason = _outgoing_value(repair.reason)
        if reason is not None:
            reason = str(reason).strip()
            if reason and _cell(row, reason_col) != reason:
                updates.append({'range': rowcol_to_a1(row_number, reason_col + 1), 'values': [[reason]]})

        work_order = _outgoing_value(repair.work_order)
        if work_order is not None:
            work_order = str(work_order).strip()
            if _cell(row, work_order_col) != work_order:
                updates.append({'range': rowcol_to_a1(row_number, work_order_col + 1), 'values': [[work_order]]})

        if repair.is_completed:
            outbound_status = 'Готово'
        elif repair.reopen_pending:
            outbound_status = STATUS_TO_SHEET.get(repair.repair_status, 'В работе')
            reopened_records.append(repair)
        elif repair.repair_status and repair.repair_status.strip() == '-':
            outbound_status = ''
        else:
            outbound_status = _outgoing_value(STATUS_TO_SHEET.get(repair.repair_status))
        if outbound_status is not None:
            outbound_status = str(outbound_status).strip()
            if _cell(row, status_col) != outbound_status:
                updates.append({'range': rowcol_to_a1(row_number, status_col + 1), 'values': [[outbound_status]]})

        if repair.started_at and not _date_cell_matches(_cell(row, 8), repair.started_at):
            updates.append({
                'range': rowcol_to_a1(row_number, 9),
                'values': [[repair.started_at.strftime('%d.%m.%Y')]],
            })

    if updates:
        worksheet.batch_update(updates, value_input_option='USER_ENTERED')

    # При ошибке Google флаг остаётся в БД: следующий обмен повторит возврат.
    if reopened_records:
        for repair in reopened_records:
            repair.reopen_pending = False
        db.session.commit()

    application.logger.info(
        'body_repair_sync: gid=%s импортировано=%s обновлено=%s ячеек записано=%s '
        'пропущено без ссылки=%s без борта=%s',
        config['gid'], imported_count, updated_count, len(updates),
        missing_link_count, missing_board_count,
    )
    return {
        'gid': config['gid'], 'imported': imported_count, 'updated': updated_count,
        'written': len(updates), 'missing_link': missing_link_count,
        'missing_board': missing_board_count,
    }


def sync_body_repair_from_sheets(park=None):
    """Sync the selected park, or all configured parks when run manually."""
    with application.app_context():
        ensure_body_repair_table()
        try:
            credentials = ServiceAccountCredentials.from_json_keyfile_name(
                CREDENTIALS_FILE, GOOGLE_SCOPES
            )
            client = gspread.authorize(credentials)
            spreadsheet = client.open_by_key(SPREADSHEET_ID)
        except Exception:
            application.logger.exception('body_repair_sync: ошибка авторизации Google Sheets')
            return {'status': 'error', 'message': 'google authorization failed'}

        results = []
        for config in SHEET_CONFIGS:
            parks = [name for name in config['parks'] if park is None or name == park]
            if not parks:
                continue
            try:
                worksheet = _get_sheet(spreadsheet, config['gid'])
            except Exception as error:
                application.logger.exception('body_repair_sync: ошибка загрузки gid=%s', config['gid'])
                results.append({'gid': config['gid'], 'error': str(error)})
                continue
            for park_name in parks:
                try:
                    result = _sync_one_sheet(spreadsheet, config, worksheet, park=park_name)
                    results.append(dict(result, park=park_name))
                except Exception as error:
                    db.session.rollback()
                    application.logger.exception(
                        'body_repair_sync: ошибка синхронизации gid=%s park=%s: %s',
                        config['gid'], park_name, error,
                    )
                    results.append({'gid': config['gid'], 'park': park_name, 'error': str(error)})
        return {'status': 'ok' if not any('error' in item for item in results) else 'partial', 'results': results}


