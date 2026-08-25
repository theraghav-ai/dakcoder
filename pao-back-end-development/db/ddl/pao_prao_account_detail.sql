-- pao.pao_prao_account_detail definition

-- Drop table

-- DROP TABLE pao.pao_prao_account_detail;

CREATE TABLE pao.pao_prao_account_detail (
	pao_code varchar(10) NOT NULL,
	hoa varchar(20) NOT NULL,
	"period" varchar(10) NOT NULL,
	total_payment numeric(25, 2) NULL,
	total_receipt numeric(25, 2) NULL,
	ddo_array _jsonb NULL,
	CONSTRAINT pao_prao_account_detail_pk PRIMARY KEY (pao_code, hoa, period)
);

-- Permissions

ALTER TABLE pao.pao_prao_account_detail OWNER TO pao_admin;
GRANT ALL ON TABLE pao.pao_prao_account_detail TO pao_admin;
GRANT SELECT ON TABLE pao.pao_prao_account_detail TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.pao_prao_account_detail TO pao_rw;