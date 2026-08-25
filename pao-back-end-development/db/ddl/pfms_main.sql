-- pao.pfms_main definition

-- Drop table

-- DROP TABLE pao.pfms_main;

CREATE TABLE pao.pfms_main (
	pfms_ddo_id varchar(100) NOT NULL,
	pao_code varchar(6) NULL,
	ddo_code varchar(6) NULL,
	ddo_name varchar(100) NULL,
	h_cash_book_receive_flag bool NULL,
	h_verification_flag bool NULL,
	h_pfms_generation_flag bool NULL,
	verified_date timestamp NULL,
	business_date timestamp NULL,
	opening_bal numeric(25, 2) NULL,
	closing_bal numeric(25, 2) NULL,
	verified_by int4 NULL,
	xml_unique_id varchar(30) NULL,
	office_id int4 NULL,
	CONSTRAINT pfms_main_pk PRIMARY KEY (pfms_ddo_id)
);

-- Permissions

ALTER TABLE pao.pfms_main OWNER TO pao_admin;
GRANT ALL ON TABLE pao.pfms_main TO pao_admin;
GRANT SELECT ON TABLE pao.pfms_main TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.pfms_main TO pao_rw;