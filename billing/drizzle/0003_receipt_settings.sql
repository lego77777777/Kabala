CREATE TABLE "receipt_settings" (
	"id" integer PRIMARY KEY DEFAULT 1 NOT NULL,
	"auto_send" boolean DEFAULT true NOT NULL,
	"copy_to" text,
	"subject_he" text,
	"subject_en" text,
	"intro_he" text,
	"intro_en" text,
	"auto_retry" boolean DEFAULT true NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_by" text
);
