-- pao.transfer_entry definition

-- Drop table

-- DROP TABLE pao.transfer_entry;

CREATE TABLE pao.transfer_entry (
	pao_code varchar(10) NULL,
	hoa varchar(20) NOT NULL,
	transfer_amount numeric(25, 2) NULL,
	transfer_type varchar(5) NULL,
	created_by int4 NULL,
	created_date timestamp NULL,
	ddo_code varchar(10) NULL,
	transfer_entry_id varchar(25) NOT NULL,
	xml_generation_status varchar(10) DEFAULT 'pending'::character varying NULL,
	te_source_office_type varchar(5) NULL,
	remarks varchar(200) NULL,
	verified_by int4 NULL,
	verified_date timestamp NULL,
	verification_status pao.verification_status_te_enum NULL,
	xml_unique_id varchar(30) NULL,
	approver_remarks varchar(200) NULL,
	budget_id varchar(50) NULL,
	CONSTRAINT transfer_entry_pk PRIMARY KEY (hoa, transfer_entry_id)
);

-- Permissions

ALTER TABLE pao.transfer_entry OWNER TO pao_admin;
GRANT ALL ON TABLE pao.transfer_entry TO pao_admin;
GRANT SELECT ON TABLE pao.transfer_entry TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.transfer_entry TO pao_rw;