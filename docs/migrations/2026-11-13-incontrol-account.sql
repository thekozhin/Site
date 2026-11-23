-- Выполнить перед обновлением приложения.
CREATE TABLE IF NOT EXISTS incontrol_account (
    user_id INT NOT NULL PRIMARY KEY,
    credentials_encrypted TEXT NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT fk_incontrol_account_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
