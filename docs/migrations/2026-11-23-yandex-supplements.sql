ALTER TABLE yandex_mail_request
    DROP PRIMARY KEY,
    ADD PRIMARY KEY (message_key),
    DROP INDEX uq_yandex_mail_message,
    ADD INDEX ix_yandex_mail_request_task_id (task_id);
