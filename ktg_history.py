"""
Почасовая история КТГ.

Каждый час сохраняется снимок текущего состояния парка:
    - ktg_park  — текущий процент КТГ;
    - in_repair — текущее количество машин в ремонте.

Предыдущие записи не используются для расчёта.
Каждая запись является самостоятельным снимком.
"""

from datetime import datetime

from sqlalchemy import func

from app import (
    db,
    application,
    Task,
    FleetSize,
    KtgSnapshot,
    PARK_TO_LOCATION,
    tz,
    run_scheduler_job_once,
)


# Парки, для которых сохраняем историю
KTG_SNAPSHOT_PARKS = list(PARK_TO_LOCATION.keys())


def _get_fleet_size(park):
    """
    Общее количество машин в парке.
    Используется та же привязка PARK_TO_LOCATION,
    что и в текущем расчёте КТГ.
    """
    loc_map = PARK_TO_LOCATION.get(park, {})

    locations = [
        loc
        for loc in (
            loc_map.get('bus'),
            loc_map.get('elbus'),
        )
        if loc
    ]

    if not locations:
        return 0

    total_fleet = db.session.query(
        func.coalesce(func.sum(FleetSize.count), 0)
    ).filter(
        FleetSize.location.in_(locations)
    ).scalar()

    return int(total_fleet or 0)


def _get_in_repair(park):
    """
    Текущее количество машин в ремонте.

    Учитываем только задачи со статусом in_repair.
    Заявки с тегами to и no_request исключаем.
    """
    tasks_in_repair = Task.query.filter(
        Task.status == 'in_repair',
        Task.park == park,
    ).all()

    return len([
        task
        for task in tasks_in_repair
        if task.tag not in ('to', 'no_request')
    ])


def _calc_ktg(park, in_repair):
    """
    Рассчитать текущее КТГ парка.
    """
    total_fleet = _get_fleet_size(park)

    if not total_fleet:
        return None

    return round(
        ((total_fleet - in_repair) / total_fleet) * 100,
        2
    )


def collect_ktg_snapshot():
    """
    Сохранить текущий снимок КТГ по всем паркам.

    Например:

        14:00
        КТГ = 96.5%
        В ремонте = 8

        15:00
        КТГ = 95.8%
        В ремонте = 10

    В БД будут две независимые записи.
    """
    now = datetime.now(tz)

    # В БД сохраняем время без timezone,
    # в московском времени.
    recorded_at = now.replace(tzinfo=None)

    rows = []

    for park in KTG_SNAPSHOT_PARKS:
        try:
            # Текущее количество машин в ремонте
            in_repair = _get_in_repair(park)

            # Текущее КТГ
            ktg_park = _calc_ktg(park, in_repair)

            rows.append(
                KtgSnapshot(
                    park=park,
                    recorded_at=recorded_at,
                    ktg_park=ktg_park,
                    in_repair=in_repair,
                )
            )

            application.logger.info(
                'ktg_snapshot: %s — КТГ=%s%%, в ремонте=%s',
                park,
                ktg_park,
                in_repair,
            )

        except Exception as e:
            db.session.rollback()

            application.logger.exception(
                'ktg_snapshot: ошибка по парку %s: %s',
                park,
                e,
            )

    if rows:
        db.session.add_all(rows)
        db.session.commit()

        application.logger.info(
            'ktg_snapshot: сохранено %s записей за %s',
            len(rows),
            now.strftime('%Y-%m-%d %H:%M:%S'),
        )

    return {
        'status': 'ok',
        'saved': len(rows),
    }


def scheduled_collect_ktg_snapshot():
    """
    Обёртка для APScheduler.
    """
    return run_scheduler_job_once(
        'collect_ktg_snapshot',
        collect_ktg_snapshot,
    )


def register_ktg_scheduler_jobs(add_scheduler_job, CronTrigger):
    """
    Регистрация задания в APScheduler.

    Запуск каждый час в :00:
        14:00
        15:00
        16:00
        17:00
        ...
    """
    add_scheduler_job(
        'collect_ktg_snapshot',
        scheduled_collect_ktg_snapshot,
        CronTrigger(
            minute=0,
            #minute='*/1',
            timezone=tz,
        ),
    )