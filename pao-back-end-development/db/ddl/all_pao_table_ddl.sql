CREATE TYPE pao.status_flag_pao_enum AS ENUM (
    'created',
    'paoupdated',
    'rejected',
    'ddoupdated',
    'closed'
);


CREATE TYPE pao.status_flag_prao_enum AS ENUM (
    'created',
    'praoupdated',
    'rejected',
    'paoupdated',
    'closed'
);

CREATE TYPE pao.verification_status_te_enum AS ENUM (
    'created',
    'verified',
    'deleted'
);
 

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

-- pao.ddo_master definition

-- Drop table

-- DROP TABLE pao.ddo_master;

CREATE TABLE pao.ddo_master (
	pao_code varchar(6) NULL,
	ddo_code varchar(6) NOT NULL,
	pao_office_id int4 NULL,
	ddo_office_id int4 NULL,
	ddo_name varchar(90) NULL,
	pao_name varchar(50) NULL,
	ddo_type varchar(5) NULL,
	gst_number varchar(15) NULL,
	CONSTRAINT ddo_master_pk PRIMARY KEY (ddo_code)
);

-- Permissions

ALTER TABLE pao.ddo_master OWNER TO pao_admin;
GRANT ALL ON TABLE pao.ddo_master TO pao_admin;
GRANT SELECT ON TABLE pao.ddo_master TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.ddo_master TO pao_rw;

-- pao.hoa_exemption definition

-- Drop table

-- DROP TABLE pao.hoa_exemption;

CREATE TABLE pao.hoa_exemption (
	hoa varchar(15) NOT NULL,
	"year" varchar(10) NOT NULL,
	circle varchar(50) NOT NULL,
	authorisation_status varchar(2) DEFAULT 1 NOT NULL,
	created_date timestamp NOT NULL,
	created_by int4 NOT NULL,
	status_flag bool DEFAULT false NOT NULL,
	updated_date timestamp NULL,
	updated_by int4 NULL,
	CONSTRAINT hoa_exemption_pk PRIMARY KEY (hoa, year, circle, status_flag)
);
CREATE UNIQUE INDEX hoa_exemption_unique_idx ON pao.hoa_exemption USING btree (hoa, year, circle) WHERE ((status_flag = true) OR ((authorisation_status)::text = '1'::text));

-- Permissions

ALTER TABLE pao.hoa_exemption OWNER TO pao_admin;
GRANT ALL ON TABLE pao.hoa_exemption TO pao_admin;
GRANT SELECT ON TABLE pao.hoa_exemption TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.hoa_exemption TO pao_rw;

-- pao.kafka_budget definition

-- Drop table

-- DROP TABLE pao.kafka_budget;

CREATE TABLE pao.kafka_budget (
	transaction_id varchar DEFAULT '00000000-0000-0000-0000-000000000000'::text NOT NULL,
	financial_year varchar(4) NULL,
	office_id int4 DEFAULT 0 NULL,
	office_type_code int4 NULL,
	hoa varchar(15) NULL,
	consumable_budget numeric(25, 2) DEFAULT 0.00 NULL,
	transferred_amount numeric(25, 2) DEFAULT 0.00 NULL,
	consumed_budget numeric(25, 2) DEFAULT 0.00 NULL,
	available_budget numeric(25, 2) DEFAULT 0.00 NULL,
	reserved_amount numeric(25, 2) DEFAULT 0.00 NULL,
	reserved_percentage numeric(3) DEFAULT 0 NULL,
	from_office_id int4 DEFAULT 0 NULL,
	creation_date timestamp NULL,
	allocation_type varchar NULL,
	amount numeric(25, 2) NULL,
	created_by int4 NULL,
	approval_date timestamp NULL,
	upload_status varchar(50) NULL,
	rejected_date timestamp NULL,
	approval_status bool NULL,
	CONSTRAINT kafka_budget_pkey PRIMARY KEY (transaction_id)
);

-- Permissions

ALTER TABLE pao.kafka_budget OWNER TO pao_admin;
GRANT ALL ON TABLE pao.kafka_budget TO pao_admin;
GRANT SELECT ON TABLE pao.kafka_budget TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.kafka_budget TO pao_rw;

-- pao.kafka_cash_account definition

-- Drop table

-- DROP TABLE pao.kafka_cash_account;

CREATE TABLE pao.kafka_cash_account (
	cash_account_id varchar NOT NULL,
	opening_bal numeric(25, 2) NOT NULL,
	closing_bal numeric(25, 2) NOT NULL,
	"period" varchar(50) NOT NULL,
	approved_by int4 NOT NULL,
	office_id int4 NULL,
	db_id int8 NULL,
	result_array json NULL,
	balance_at_ho int8 NULL,
	balance_at_so int8 NULL,
	balance_at_bo int8 NULL,
	part1_receipts int8 NULL,
	part1_payments int8 NULL,
	receipts_part1_plus_part2 int8 NULL,
	payments_part1_plus_part2 int8 NULL,
	receipts_adj_part1_plus_part2 int8 NULL,
	payments_adj_part1_plus_part2 int8 NULL,
	part2_receipts int8 NULL,
	part2_payments int8 NULL,
	CONSTRAINT cash_account_pkey PRIMARY KEY (cash_account_id)
);
CREATE INDEX cash_account_cash_account_id_idx ON pao.kafka_cash_account USING btree (cash_account_id, office_id);

-- Permissions

ALTER TABLE pao.kafka_cash_account OWNER TO pao_admin;
GRANT ALL ON TABLE pao.kafka_cash_account TO pao_admin;
GRANT SELECT ON TABLE pao.kafka_cash_account TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.kafka_cash_account TO pao_rw;

-- pao.kafka_cashbook definition

-- Drop table

-- DROP TABLE pao.kafka_cashbook;

CREATE TABLE pao.kafka_cash_book (
	office_id int4 NULL,
	opening_bal numeric(25, 2) NULL,
	closing_bal numeric(25, 2) NULL,
	business_date date NULL,
	approved_by int4 NULL,
	details json NULL,
	cash_book_id int8 DEFAULT 0 NOT NULL,
	cash_book_seq varchar(50) NOT NULL,
	CONSTRAINT kafka_cash_book_pk PRIMARY KEY (cash_book_seq)
);

-- Permissions

ALTER TABLE pao.kafka_cash_book OWNER TO pao_admin;
GRANT ALL ON TABLE pao.kafka_cash_book TO pao_admin;
GRANT SELECT ON TABLE pao.kafka_cash_book TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.kafka_cash_book TO pao_rw;

-- pao.kafka_office_master definition

-- Drop table

-- DROP TABLE pao.kafka_office_master;

CREATE TABLE pao.kafka_office_master (
	office_id int4 NOT NULL,
	office_name varchar(50) NULL,
	office_type_id int4 NULL,
	office_type_code varchar(20) NULL,
	email_id varchar(50) NULL,
	contact_number varchar(20) NULL,
	office_class varchar(50) NULL,
	pincode int4 NULL,
	reporting_office_id int4 NULL,
	pao_code varchar(20) NULL,
	ddo_code varchar(20) NULL,
	valid_from date NULL,
	valid_to date NULL,
	CONSTRAINT kafka_office_master_pkey PRIMARY KEY (office_id)
);

-- Permissions

ALTER TABLE pao.kafka_office_master OWNER TO pao_admin;
GRANT ALL ON TABLE pao.kafka_office_master TO pao_admin;
GRANT SELECT ON TABLE pao.kafka_office_master TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.kafka_office_master TO pao_rw;

-- pao.kafka_transfer_entry definition

-- Drop table

-- DROP TABLE pao.kafka_transfer_entry;

CREATE TABLE pao.kafka_transfer_entry (
	trans_id varchar(50) NULL,
	trans_date timestamp NULL,
	ddo_code varchar(50) NULL,
	account_code varchar(50) NULL,
	actual_amount numeric(25, 2) NULL,
	transfer_amount numeric(25, 2) NULL,
	current_amount numeric(25, 2) NULL,
	transfer_type varchar(50) NULL,
	created_by int4 NULL,
	created_date timestamp NULL,
	te_id int8 DEFAULT 0 NOT NULL,
	status varchar(50) NULL,
	approved_by int4 NULL,
	approved_date timestamp NULL,
	remarks varchar(200) NULL,
	office_id int4 NULL,
	CONSTRAINT kafka_transfer_entry_pkey PRIMARY KEY (te_id)
);

-- Permissions

ALTER TABLE pao.kafka_transfer_entry OWNER TO pao_admin;
GRANT ALL ON TABLE pao.kafka_transfer_entry TO pao_admin;
GRANT SELECT ON TABLE pao.kafka_transfer_entry TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.kafka_transfer_entry TO pao_rw;

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

-- pao.objection definition

-- Drop table

-- DROP TABLE pao.objection;

CREATE TABLE pao.objection (
	pao_code varchar(10) NULL,
	ddo_code varchar(10) NULL,
	description varchar(5000) NULL,
	created_by int4 NULL,
	created_date timestamp NULL,
	remarks _jsonb NULL,
	status_flag pao.status_flag_pao_enum NULL,
	objection_id varchar(27) DEFAULT pao.uuid_generate_v4() NOT NULL,
	last_updated_by int4 NULL,
	last_updated_date timestamp NULL,
	CONSTRAINT objection_pk PRIMARY KEY (objection_id)
);

-- Permissions

ALTER TABLE pao.objection OWNER TO pao_admin;
GRANT ALL ON TABLE pao.objection TO pao_admin;
GRANT SELECT ON TABLE pao.objection TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.objection TO pao_rw;

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

-- pao.pfms_detail definition

-- Drop table

-- DROP TABLE pao.pfms_detail;

CREATE TABLE pao.pfms_detail (
	pfms_ddo_id varchar(100) NOT NULL,
	hoa varchar(20) NOT NULL,
	receipt numeric(25, 2) NULL,
	payment numeric(25, 2) NULL,
	account_code_detail _jsonb NULL,
	CONSTRAINT pfms_detail_pk PRIMARY KEY (pfms_ddo_id, hoa)
);

-- Permissions

ALTER TABLE pao.pfms_detail OWNER TO pao_admin;
GRANT ALL ON TABLE pao.pfms_detail TO pao_admin;
GRANT SELECT ON TABLE pao.pfms_detail TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.pfms_detail TO pao_rw;


-- pao.pfms_detail foreign keys

ALTER TABLE pao.pfms_detail ADD CONSTRAINT pfms_detail_pfms_main_fk FOREIGN KEY (pfms_ddo_id) REFERENCES pao.pfms_main(pfms_ddo_id);

-- pao.pfms_monthly_main definition

-- Drop table

-- DROP TABLE pao.pfms_monthly_main;

CREATE TABLE pao.pfms_monthly_main (
	pfms_ddo_id varchar(100) NOT NULL,
	pao_code varchar(6) NULL,
	ddo_code varchar(6) NULL,
	ddo_name varchar(100) NULL,
	h_cash_account_receive_flag bool NULL,
	h_verification_flag bool NULL,
	verified_date timestamp NULL,
	"period" varchar(6) NULL,
	opening_bal numeric(25, 2) NULL,
	closing_bal numeric(25, 2) NULL,
	verified_by int4 NULL,
	office_id int4 NULL,
	CONSTRAINT pfms_monthly_main_pk PRIMARY KEY (pfms_ddo_id)
);

-- Permissions

ALTER TABLE pao.pfms_monthly_main OWNER TO pao_admin;
GRANT ALL ON TABLE pao.pfms_monthly_main TO pao_admin;
GRANT SELECT ON TABLE pao.pfms_monthly_main TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.pfms_monthly_main TO pao_rw;

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

-- pao.pfms_xml definition

-- Drop table

-- DROP TABLE pao.pfms_xml;

CREATE TABLE pao.pfms_xml (
	te_identifier varchar(30) NOT NULL,
	pao_code varchar(6) NULL,
	pfms_xml_data xml NULL,
	created_date timestamp DEFAULT CURRENT_TIMESTAMP NULL,
	pfms_status_flag varchar(10) NULL,
	pfms_error_description varchar(200) NULL,
	CONSTRAINT pfms_xml_pk PRIMARY KEY (te_identifier)
);

-- Permissions

ALTER TABLE pao.pfms_xml OWNER TO pao_admin;
GRANT ALL ON TABLE pao.pfms_xml TO pao_admin;
GRANT SELECT ON TABLE pao.pfms_xml TO pao_ro;
GRANT UPDATE, INSERT, SELECT, DELETE ON TABLE pao.pfms_xml TO pao_rw;


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