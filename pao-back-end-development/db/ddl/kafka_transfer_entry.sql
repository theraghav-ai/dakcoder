-- pao.kafka_transfer_entry definition

-- Drop table

-- DROP TABLE pao.kafka_transfer_entry;

CREATE TABLE pao.kafka_transfer_entry (
	trans_id varchar(50) NULL,
	trans_date timestamp NULL,
	ddo_code varchar(50) NULL,
	account_code varchar(50) NULL,
	actual_amount numeric(25, 2) NULL,
	transfer_amount numeric(25, 2) NULL,
	current_amount numeric(25, 2) NULL,
	transfer_type varchar(50) NULL,
	created_by int4 NULL,
	created_date timestamp NULL,
	te_id int8 DEFAULT 0 NOT NULL,
	status varchar(50) NULL,
	approved_by int4 NULL,
	approved_date timestamp NULL,
	remarks varchar(200) NULL,
	office_id int4 NULL,
	CONSTRAINT kafka_transfer_entry_pkey PRIMARY KEY (te_id)
);

-- Permissions

ALTER TABLE pao.kafka_transfer_entry OWNER TO pao_admin;
GRANT ALL ON TABLE pao.kafka_transfer_entry TO pao_admin;
GRANT SELECT ON TABLE pao.kafka_transfer_entry TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.kafka_transfer_entry TO pao_rw;