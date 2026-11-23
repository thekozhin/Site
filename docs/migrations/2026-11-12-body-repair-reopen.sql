-- Выполнить один раз перед обновлением файлов сайта.
-- Сохраняет приоритет ручного возврата до успешной синхронизации с Google.
ALTER TABLE body_repair
    ADD COLUMN reopen_pending TINYINT(1) NOT NULL DEFAULT 0;
