-- Храним дату и идентификатор последней версии заявки из почты.
ALTER TABLE yandex_mail_request
    ADD COLUMN revision_received_at DATETIME NULL,
    ADD COLUMN revision_message_key VARCHAR(64) NULL,
    ADD INDEX ix_yandex_mail_request_revision_message_key (revision_message_key);
