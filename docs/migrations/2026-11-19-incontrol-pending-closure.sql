CREATE TABLE IF NOT EXISTS incontrol_pending_closure (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    task_id INT NOT NULL,
    remote_id BIGINT NULL,
    park VARCHAR(50) NOT NULL,
    board_number VARCHAR(20) NOT NULL,
    description TEXT NOT NULL,
    completed_work TEXT NOT NULL,
    child_details TEXT NULL,
    is_reject BOOLEAN NOT NULL,
    completed_at DATETIME NOT NULL,
    closed_by VARCHAR(100) NOT NULL,
    reported_at DATETIME NULL,
    reported_by VARCHAR(100) NULL,
    KEY ix_incontrol_pending_park (reported_at, park)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
