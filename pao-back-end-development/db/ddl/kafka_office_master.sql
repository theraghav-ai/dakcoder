
-- pao.kafka_office_master definition

-- Drop table

-- DROP TABLE pao.kafka_office_master;

CREATE TABLE pao.kafka_office_master (
	office_id int4 NOT NULL,
	office_name varchar(50) NULL,
	office_type_id int4 NULL,
	office_type_code varchar(20) NULL,
	email_id varchar(50) NULL,
	contact_number varchar(20) NULL,
	office_class varchar(50) NULL,
	pincode int4 NULL,
	reporting_office_id int4 NULL,
	pao_code varchar(20) NULL,
	ddo_code varchar(20) NULL,
	valid_from date NULL,
	valid_to date NULL,
	CONSTRAINT kafka_office_master_pkey PRIMARY KEY (office_id)
);

-- Permissions

ALTER TABLE pao.kafka_office_master OWNER TO pao_admin;
GRANT ALL ON TABLE pao.kafka_office_master TO pao_admin;
GRANT SELECT ON TABLE pao.kafka_office_master TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.kafka_office_master TO pao_rw;