-- pao.objection definition

-- Drop table

-- DROP TABLE pao.objection;

CREATE TABLE pao.objection (
	pao_code varchar(10) NULL,
	ddo_code varchar(10) NULL,
	description varchar(5000) NULL,
	created_by int4 NULL,
	created_date timestamp NULL,
	remarks _jsonb NULL,
	status_flag pao.status_flag_pao_enum NULL,
	objection_id varchar(27) DEFAULT pao.uuid_generate_v4() NOT NULL,
	last_updated_by int4 NULL,
	last_updated_date timestamp NULL,
	CONSTRAINT objection_pk PRIMARY KEY (objection_id)
);

-- Permissions

ALTER TABLE pao.objection OWNER TO pao_admin;
GRANT ALL ON TABLE pao.objection TO pao_admin;
GRANT SELECT ON TABLE pao.objection TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.objection TO pao_rw;