CREATE TABLE IF NOT EXISTS incontrol_rejection_run (
    id VARCHAR(32) NOT NULL PRIMARY KEY,
    task_id INT NOT NULL,
    remote_id BIGINT NULL,
    user_id INT NOT NULL,
    state VARCHAR(20) NOT NULL,
    payload_encrypted TEXT NOT NULL,
    message VARCHAR(500) NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE KEY uq_incontrol_rejection_task (task_id),
    UNIQUE KEY uq_incontrol_rejection_remote (remote_id),
    KEY ix_incontrol_rejection_user (user_id),
    CONSTRAINT fk_incontrol_rejection_task FOREIGN KEY (task_id) REFERENCES task(id),
    CONSTRAINT fk_incontrol_rejection_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
