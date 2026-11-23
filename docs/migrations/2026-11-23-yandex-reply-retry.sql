ALTER TABLE yandex_mail_request
    ADD COLUMN reply_retry_count INT NOT NULL DEFAULT 0,
    ADD COLUMN reply_next_at DATETIME NULL,
    ADD KEY ix_yandex_mail_retry (reply_state, reply_next_at);

UPDATE yandex_mail_request
SET reply_state = 'verify'
WHERE reply_state = 'review';
