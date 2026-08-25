-- pao.ddo_master definition

-- Drop table

-- DROP TABLE pao.ddo_master;

CREATE TABLE pao.ddo_master (
	pao_code varchar(6) NULL,
	ddo_code varchar(6) NOT NULL,
	pao_office_id int4 NULL,
	ddo_office_id int4 NULL,
	ddo_name varchar(90) NULL,
	pao_name varchar(50) NULL,
	ddo_type varchar(5) NULL,
	gst_number varchar(15) NULL,
	CONSTRAINT ddo_master_pk PRIMARY KEY (ddo_code)
);

-- Permissions

ALTER TABLE pao.ddo_master OWNER TO pao_admin;
GRANT ALL ON TABLE pao.ddo_master TO pao_admin;
GRANT SELECT ON TABLE pao.ddo_master TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.ddo_master TO pao_rw;