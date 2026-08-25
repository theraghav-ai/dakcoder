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