CREATE TABLE "gmail_account" (
	"id" integer PRIMARY KEY DEFAULT 1 NOT NULL,
	"email" text NOT NULL,
	"client_id" text NOT NULL,
	"secrets_encrypted" text NOT NULL,
	"connected_at" timestamp with time zone DEFAULT now() NOT NULL,
	"connected_by" text NOT NULL
);
