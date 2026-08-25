-- pao.kafka_cash_account definition

-- Drop table

-- DROP TABLE pao.kafka_cash_account;

CREATE TABLE pao.kafka_cash_account (
	cash_account_id varchar NOT NULL,
	opening_bal numeric(25, 2) NOT NULL,
	closing_bal numeric(25, 2) NOT NULL,
	"period" varchar(50) NOT NULL,
	approved_by int4 NOT NULL,
	office_id int4 NULL,
	db_id int8 NULL,
	result_array json NULL,
	balance_at_ho int8 NULL,
	balance_at_so int8 NULL,
	balance_at_bo int8 NULL,
	part1_receipts int8 NULL,
	part1_payments int8 NULL,
	receipts_part1_plus_part2 int8 NULL,
	payments_part1_plus_part2 int8 NULL,
	receipts_adj_part1_plus_part2 int8 NULL,
	payments_adj_part1_plus_part2 int8 NULL,
	part2_receipts int8 NULL,
	part2_payments int8 NULL,
	CONSTRAINT cash_account_pkey PRIMARY KEY (cash_account_id)
);
CREATE INDEX cash_account_cash_account_id_idx ON pao.kafka_cash_account USING btree (cash_account_id, office_id);

-- Permissions

ALTER TABLE pao.kafka_cash_account OWNER TO pao_admin;
GRANT ALL ON TABLE pao.kafka_cash_account TO pao_admin;
GRANT SELECT ON TABLE pao.kafka_cash_account TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.kafka_cash_account TO pao_rw;