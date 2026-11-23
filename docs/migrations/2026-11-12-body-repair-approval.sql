-- Выполнить один раз в рабочей базе перед обновлением файлов сайта.
ALTER TABLE body_repair
    ADD COLUMN is_approved TINYINT(1) NOT NULL DEFAULT 0;
