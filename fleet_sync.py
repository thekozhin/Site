"""
Синхронизация количества машин в парках с Google Таблицами.

Читает значения из указанных ячеек нужных листов и пишет их
в fleet_size.count для соответствующих location.

Запуск: каждый час в :00 через APScheduler
(см. register_fleet_sync_jobs).
"""

import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from app import (
    db,
    application,
    FleetSize,
    run_scheduler_job_once,
)


# ID Google Таблицы (из URL) — общий для всех листов
SPREADSHEET_ID = "1-0dD2K79KDh1hHDmYqTbvJQFqDJC7fktgLqhv8R5qA8"

# Тот же файл ключей, что использует save_to_google_sheets в app.py
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")


# ---------------------------------------------------------------------------
# Конфигурация: что откуда читать и куда писать.
#
#   sheet_gid  — gid листа из URL (#gid=...)
#   sheet_name — имя вкладки (fallback для gspread < 5.4.0)
#   cell       — ячейка
#   location   — значение fleet_size.location в БД
# ---------------------------------------------------------------------------
SYNC_TARGETS = [
    {
        "sheet_gid": 1577198341,
        "sheet_name": "Сводка ГД",
        "cell": "AH5",
        "location": "Теплый Стан",
    },
    {
        "sheet_gid": 1479708954,
        "sheet_name": "Коньково",           # ← замените на реальное имя вкладки
        "cell": "AG5",
        "location": "Коньково электробусы",
    },
    {
        "sheet_gid": 1479708954,
        "sheet_name": "Коньково",
        "cell": "AH5",
        "location": "Коньково автобусы",
    },
    {
        "sheet_gid": 2011389958,
        "sheet_name": "Красная Пахра",      # ← замените на реальное имя вкладки
        "cell": "AG5",
        "location": "Красная Пахра электробусы",
    },
    {
        "sheet_gid": 2011389958,
        "sheet_name": "Красная Пахра",
        "cell": "AH5",
        "location": "Красная Пахра автобусы",
    },
    {
        "sheet_gid": 1217953817,
        "sheet_name": "Ясенево",            # ← замените на реальное имя вкладки
        "cell": "AH5",
        "location": "Ясенево автобусы",
    },
]


def _get_sheet(spreadsheet, target):
    """Вернуть лист по gid, с fallback на имя для старых версий gspread."""
    try:
        return spreadsheet.get_worksheet_by_id(target["sheet_gid"])
    except AttributeError:
        # gspread < 5.4.0
        return spreadsheet.worksheet(target["sheet_name"])


def _parse_int(raw):
    """Привести значение ячейки к int. None — если не получилось."""
    if raw in (None, ""):
        return None
    try:
        cleaned = str(raw).replace("\xa0", "").replace(" ", "").replace(",", ".")
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def _fetch_one(sheet, target):
    """Прочитать одну ячейку. Вернуть int или None."""
    try:
        raw = sheet.acell(target["cell"]).value
        value = _parse_int(raw)
        if value is None:
            application.logger.warning(
                "fleet_sync: пустая или нечисловая ячейка %s (gid=%s) для %s",
                target["cell"], target["sheet_gid"], target["location"],
            )
        return value
    except Exception as e:
        application.logger.exception(
            "fleet_sync: ошибка чтения %s (gid=%s) для %s: %s",
            target["cell"], target["sheet_gid"], target["location"], e,
        )
        return None


def update_fleet_size_from_sheet():
    """
    Пройти по всем SYNC_TARGETS, прочитать значения и записать в БД.

    Авторизация выполняется один раз на весь проход. Если какая-то
    ячейка недоступна — остальные всё равно записываются.
    """
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]

    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            CREDENTIALS_FILE, scope
        )
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
    except Exception as e:
        application.logger.exception(
            "fleet_sync: авторизация в Google не удалась: %s", e
        )
        return {"status": "error", "message": "auth failed"}

    # Кешируем листы: один gid может использоваться несколькими целями
    sheets_by_gid = {}
    results = []

    for target in SYNC_TARGETS:
        gid = target["sheet_gid"]

        if gid not in sheets_by_gid:
            try:
                sheets_by_gid[gid] = _get_sheet(spreadsheet, target)
            except Exception as e:
                application.logger.exception(
                    "fleet_sync: лист gid=%s недоступен: %s", gid, e
                )
                sheets_by_gid[gid] = None

        sheet = sheets_by_gid[gid]
        if sheet is None:
            results.append({
                "location": target["location"],
                "status": "error",
                "message": "sheet unavailable",
            })
            continue

        value = _fetch_one(sheet, target)
        if value is None:
            results.append({
                "location": target["location"],
                "status": "error",
                "message": "cell unavailable",
            })
            continue

        # --- запись в БД ---
        try:
            fleet = FleetSize.query.filter_by(
                location=target["location"]
            ).first()

            if fleet is None:
                fleet = FleetSize(
                    location=target["location"],
                    count=value,
                    zone="[]",
                )
                db.session.add(fleet)
                application.logger.info(
                    "fleet_sync: создана запись %s = %s",
                    target["location"], value,
                )
            else:
                old = fleet.count
                fleet.count = value
                application.logger.info(
                    "fleet_sync: %s: %s -> %s",
                    target["location"], old, value,
                )

            db.session.commit()
            results.append({
                "location": target["location"],
                "status": "ok",
                "count": value,
            })
        except Exception as e:
            db.session.rollback()
            application.logger.exception(
                "fleet_sync: ошибка записи %s: %s",
                target["location"], e,
            )
            results.append({
                "location": target["location"],
                "status": "error",
                "message": str(e),
            })

    ok = sum(1 for r in results if r["status"] == "ok")
    return {"status": "ok", "updated": ok, "total": len(results), "results": results}


def scheduled_update_fleet_size():
    """Обёртка для APScheduler — та же схема, что в ktg_history."""
    return run_scheduler_job_once(
        "update_fleet_size_from_sheet",
        update_fleet_size_from_sheet,
    )


def register_fleet_sync_jobs(add_scheduler_job, CronTrigger, tz):
    """
    Регистрация задания в APScheduler.
    Каждый час в :00 по Москве.
    """
    add_scheduler_job(
        "update_fleet_size_from_sheet",
        scheduled_update_fleet_size,
        CronTrigger(hour=6, minute=0, timezone=tz),
    )