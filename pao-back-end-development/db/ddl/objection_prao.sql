-- pao.objection_prao definition

-- Drop table

-- DROP TABLE pao.objection_prao;

CREATE TABLE pao.objection_prao (
	prao_code varchar(10) NULL,
	pao_code varchar(10) NULL,
	description varchar(200) NULL,
	created_by int4 NULL,
	created_date timestamptz NULL,
	remarks _jsonb NULL,
	status_flag pao.status_flag_prao_enum NULL,
	objection_id varchar(27) DEFAULT pao.uuid_generate_v4() NOT NULL,
	last_updated_by int4 NULL,
	last_updated_date timestamp NULL,
	CONSTRAINT objection_prao_pk PRIMARY KEY (objection_id)
);

-- Permissions

ALTER TABLE pao.objection_prao OWNER TO pao_admin;
GRANT ALL ON TABLE pao.objection_prao TO pao_admin;
GRANT SELECT ON TABLE pao.objection_prao TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.objection_prao TO pao_rw;