"""История dashboard: только сохранённые снимки выбранного парка."""
from datetime import datetime, timedelta
from calendar import monthrange

from sqlalchemy import func, extract


def build_dashboard_metrics(model, session, park, period, selected_date):
    start_date = selected_date
    if period == 'week':
        start_date -= timedelta(days=start_date.weekday())
    elif period == 'month':
        start_date = start_date.replace(day=1)
    elif period == 'year':
        start_date = start_date.replace(month=1, day=1)
    if period == 'year':
        end_date = start_date.replace(year=start_date.year + 1)
    elif period == 'month':
        end_date = start_date + timedelta(days=monthrange(start_date.year, start_date.month)[1])
    else:
        end_date = start_date + timedelta(days=7 if period == 'week' else 1)
    start, end = datetime.combine(start_date, datetime.min.time()), datetime.combine(end_date, datetime.min.time())
    if period == 'day':
        bucket = extract('hour', model.recorded_at)
        keys = list(range(24))
        labels = [f'{hour:02d}:00' for hour in keys]
        aggregation = 'Средние значения за час'
    elif period == 'year':
        bucket = extract('month', model.recorded_at)
        keys = list(range(1, 13))
        labels = ['Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн', 'Июл', 'Авг', 'Сен', 'Окт', 'Ноя', 'Дек']
        aggregation = 'Средние значения за месяц'
    else:
        bucket = func.date(model.recorded_at)
        days = [start_date + timedelta(days=i) for i in range((end_date - start_date).days)]
        keys = [day.isoformat() for day in days]
        labels = [day.strftime('%d.%m') for day in days]
        aggregation = 'Средние значения за сутки'
    values, latest = {}, None
    if park:
        filters = (model.park == park, model.recorded_at >= start, model.recorded_at < end)
        rows = session.query(bucket, func.avg(model.ktg_park), func.avg(model.in_repair)).filter(*filters).group_by(bucket).all()
        # MySQL возвращает DATE как date, SQLite — как строку.
        values = {(key.isoformat() if hasattr(key, 'isoformat') else key): (ktg, repair)
                  for key, ktg, repair in rows}
        latest = session.query(model).filter(*filters).order_by(model.recorded_at.desc(), model.id.desc()).first()
    return {
        'labels': labels,
        'ktg': [round(values[key][0], 2) if key in values and values[key][0] is not None else None for key in keys],
        'repair': [round(values[key][1], 2) if key in values and values[key][1] is not None else None for key in keys],
        'has_data': bool(values),
        'aggregation': aggregation,
        'range': f'{start_date:%d.%m.%Y} — {end_date - timedelta(days=1):%d.%m.%Y}',
        'latest': {'ktg': latest.ktg_park, 'repair': latest.in_repair,
                   'time': latest.recorded_at.strftime('%d.%m.%Y %H:%M')} if latest else None,
    }
