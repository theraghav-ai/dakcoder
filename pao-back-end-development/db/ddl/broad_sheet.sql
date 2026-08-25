-- pao.broadsheet definition

-- Drop table

-- DROP TABLE pao.broadsheet;

CREATE TABLE pao.broad_sheet (
	broadsheet_month varchar(6) NOT NULL,
	hoa varchar(15) NOT NULL,
	ddo_code varchar(6) NOT NULL,
	opening_balance numeric(25, 2) DEFAULT 0 NULL,
	credit_amount numeric(25, 2) DEFAULT 0 NULL,
	debit_amount numeric(25, 2) DEFAULT 0 NULL,
	closing_balance numeric(25, 2) DEFAULT 0 NULL,
	created_date timestamp NULL,
	updated_by int4 NULL,
	updated_date timestamp NULL,
	created_by int4 NULL,
	CONSTRAINT broad_sheet_pk PRIMARY KEY (broadsheet_month, hoa, ddo_code)
);

-- Permissions

ALTER TABLE pao.broad_sheet OWNER TO pao_admin;
GRANT ALL ON TABLE pao.broad_sheet TO pao_admin;
GRANT SELECT ON TABLE pao.broad_sheet TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.broad_sheet TO pao_rw;