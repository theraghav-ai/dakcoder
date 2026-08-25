-- pao.pao_prao_account_main definition

-- Drop table

-- DROP TABLE pao.pao_prao_account_main;

CREATE TABLE pao.pao_prao_account_main (
	pao_code varchar(10) NOT NULL,
	"period" varchar(10) NOT NULL,
	account_submissionto_prao_status varchar(10) NULL,
	CONSTRAINT pao_prao_account_main_pk PRIMARY KEY (pao_code, period)
);

-- Permissions

ALTER TABLE pao.pao_prao_account_main OWNER TO pao_admin;
GRANT ALL ON TABLE pao.pao_prao_account_main TO pao_admin;
GRANT SELECT ON TABLE pao.pao_prao_account_main TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.pao_prao_account_main TO pao_rw;