ALTER TABLE yandex_mail_request
    ADD COLUMN imap_uid BIGINT NULL,
    ADD COLUMN imap_uid_validity BIGINT NULL;
