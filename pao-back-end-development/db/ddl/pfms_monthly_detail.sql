-- pao.pfms_monthly_detail definition

-- Drop table

-- DROP TABLE pao.pfms_monthly_detail;

CREATE TABLE pao.pfms_monthly_detail (
	pfms_ddo_id varchar(100) NOT NULL,
	hoa varchar(20) NOT NULL,
	receipt numeric(25, 2) NULL,
	payment numeric(25, 2) NULL,
	account_code_detail _jsonb NULL,
	te_payment numeric(25, 2) NULL,
	te_receipt numeric(25, 2) NULL,
	CONSTRAINT pfms_monthly_detail_pk PRIMARY KEY (pfms_ddo_id, hoa)
);

-- Permissions

ALTER TABLE pao.pfms_monthly_detail OWNER TO pao_admin;
GRANT ALL ON TABLE pao.pfms_monthly_detail TO pao_admin;
GRANT SELECT ON TABLE pao.pfms_monthly_detail TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.pfms_monthly_detail TO pao_rw;


-- pao.pfms_monthly_detail foreign keys

ALTER TABLE pao.pfms_monthly_detail ADD CONSTRAINT pfms_monthly_detail_pfms_monthly_main_fk FOREIGN KEY (pfms_ddo_id) REFERENCES pao.pfms_monthly_main(pfms_ddo_id);