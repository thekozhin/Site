ALTER TABLE incontrol_pending_closure
    ADD COLUMN service VARCHAR(16) NOT NULL DEFAULT 'incontrol',
    ADD COLUMN mail_ticket_number VARCHAR(40) NULL;
