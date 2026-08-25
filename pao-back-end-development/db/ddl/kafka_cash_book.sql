-- pao.kafka_cashbook definition

-- Drop table

-- DROP TABLE pao.kafka_cashbook;

CREATE TABLE pao.kafka_cash_book (
	office_id int4 NULL,
	opening_bal numeric(25, 2) NULL,
	closing_bal numeric(25, 2) NULL,
	business_date date NULL,
	approved_by int4 NULL,
	details json NULL,
	cash_book_id int8 DEFAULT 0 NOT NULL,
	cash_book_seq varchar(50) NOT NULL,
	CONSTRAINT kafka_cash_book_pk PRIMARY KEY (cash_book_seq)
);

-- Permissions

ALTER TABLE pao.kafka_cash_book OWNER TO pao_admin;
GRANT ALL ON TABLE pao.kafka_cash_book TO pao_admin;
GRANT SELECT ON TABLE pao.kafka_cash_book TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.kafka_cash_book TO pao_rw;