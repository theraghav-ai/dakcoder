-- pao.account_hoa_mapping definition

-- Drop table

-- DROP TABLE pao.account_hoa_mapping;

CREATE TABLE pao.account_hoa_mapping (
	hoa varchar(15) NOT NULL,
	account_code varchar(10) NOT NULL,
	account_code_description varchar(100) NOT NULL,
	receipt_payment varchar(1) NOT NULL,
	value_sign varchar(1) NOT NULL,
	brief_description varchar(1500) NULL,
	part varchar(2) NOT NULL,
	data_source_module varchar(50) NULL,
	created_by int4 NOT NULL,
	updated_by int4 NULL,
	created_date timestamp NOT NULL,
	updated_date timestamp NULL,
	status_flag bool DEFAULT false NOT NULL,
	grant_no varchar(10) NULL,
	category varchar(10) NULL,
	authorisation_status varchar(1) DEFAULT 1 NOT NULL,
	fund_type varchar(15) NULL,
	hoa_description varchar(150) NULL,
	remark varchar(50) NULL,
	CONSTRAINT account_hoa_mapping_pk PRIMARY KEY (hoa, account_code, status_flag, authorisation_status)
);
CREATE INDEX idx_account_hoa_mapping_status_flag ON pao.account_hoa_mapping USING btree (status_flag);
CREATE INDEX idx_status_flag_hoa ON pao.account_hoa_mapping USING btree (status_flag, hoa, hoa_description);

-- Permissions

ALTER TABLE pao.account_hoa_mapping OWNER TO pao_admin;
GRANT ALL ON TABLE pao.account_hoa_mapping TO pao_admin;
GRANT SELECT ON TABLE pao.account_hoa_mapping TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.account_hoa_mapping TO pao_rw;