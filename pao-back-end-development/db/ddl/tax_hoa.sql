-- pao.tax_hoa definition

-- Drop table

-- DROP TABLE pao.tax_hoa;

CREATE TABLE pao.tax_hoa (
	hoa varchar(15) NOT NULL,
	receipt_payment varchar(1) NULL,
	tax_type varchar(10) NULL,
	type_description varchar(50) NULL,
	CONSTRAINT tax_hoa_pk PRIMARY KEY (hoa)
);

-- Permissions

ALTER TABLE pao.tax_hoa OWNER TO pao_admin;
GRANT ALL ON TABLE pao.tax_hoa TO pao_admin;
GRANT SELECT ON TABLE pao.tax_hoa TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.tax_hoa TO pao_rw;