-- pao.remuneration_rate_master definition

-- Drop table

-- DROP TABLE pao.remuneration_rate_master;

CREATE TABLE pao.remuneration_rate_master (
	financial_year varchar(50) NOT NULL,
	remuneration_item varchar(50) NOT NULL,
	remuneration_type varchar(50) NOT NULL,
	remuneration_rate float4 NULL,
	updated_by int4 NULL,
	updated_date timestamp NULL,
	status bool NULL,
	authorisation_status varchar(50) NOT NULL,
	approved_date timestamp NULL,
	approved_by int4 NULL,
	CONSTRAINT remuneration_rate_master_pk PRIMARY KEY (financial_year, remuneration_item, remuneration_type, authorisation_status)
);

-- Permissions

ALTER TABLE pao.remuneration_rate_master OWNER TO pao_admin;
GRANT ALL ON TABLE pao.remuneration_rate_master TO pao_admin;
GRANT SELECT ON TABLE pao.remuneration_rate_master TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.remuneration_rate_master TO pao_rw;