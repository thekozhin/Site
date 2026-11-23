ALTER TABLE task
    ADD COLUMN yandex_reply_lookup BOOLEAN NOT NULL DEFAULT FALSE,
    ADD INDEX ix_task_yandex_reply_lookup (yandex_reply_lookup);
