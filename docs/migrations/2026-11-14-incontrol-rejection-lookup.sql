-- Только для таблицы, созданной ранним вариантом миграции с NOT NULL.
-- В текущей используемой MySQL remote_id уже допускает NULL.
ALTER TABLE incontrol_rejection_run MODIFY remote_id BIGINT NULL;
