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