-- pao.kafka_budget definition

-- Drop table

-- DROP TABLE pao.kafka_budget;

CREATE TABLE pao.kafka_budget (
	transaction_id varchar DEFAULT '00000000-0000-0000-0000-000000000000'::text NOT NULL,
	financial_year varchar(4) NULL,
	office_id int4 DEFAULT 0 NULL,
	office_type_code int4 NULL,
	hoa varchar(15) NULL,
	consumable_budget numeric(25, 2) DEFAULT 0.00 NULL,
	transferred_amount numeric(25, 2) DEFAULT 0.00 NULL,
	consumed_budget numeric(25, 2) DEFAULT 0.00 NULL,
	available_budget numeric(25, 2) DEFAULT 0.00 NULL,
	reserved_amount numeric(25, 2) DEFAULT 0.00 NULL,
	reserved_percentage numeric(3) DEFAULT 0 NULL,
	from_office_id int4 DEFAULT 0 NULL,
	creation_date timestamp NULL,
	allocation_type varchar NULL,
	amount numeric(25, 2) NULL,
	created_by int4 NULL,
	approval_date timestamp NULL,
	upload_status varchar(50) NULL,
	rejected_date timestamp NULL,
	approval_status bool NULL,
	CONSTRAINT kafka_budget_pkey PRIMARY KEY (transaction_id)
);

-- Permissions

ALTER TABLE pao.kafka_budget OWNER TO pao_admin;
GRANT ALL ON TABLE pao.kafka_budget TO pao_admin;
GRANT SELECT ON TABLE pao.kafka_budget TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.kafka_budget TO pao_rw;