-- Users table for simple CRUD demo
-- public.user_details definition

-- Drop table

-- DROP TABLE public.user_details;

CREATE TABLE public.user_details (
	id serial4 NOT NULL,
	first_name varchar(100) NOT NULL,
	last_name varchar(100) NOT NULL,
	age int4 NOT NULL,
	city varchar(100) NOT NULL,
	email varchar(100) NOT NULL,
	created_at timestamp DEFAULT now() NOT NULL,
	updated_at timestamp DEFAULT now() NOT NULL,
	CONSTRAINT user_details_email_key UNIQUE (email),
	CONSTRAINT user_details_pkey PRIMARY KEY (id)
);