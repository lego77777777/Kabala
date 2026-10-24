CREATE TYPE "public"."email_status" AS ENUM('queued', 'sent', 'failed');--> statement-breakpoint
CREATE TYPE "public"."receipt_kind" AS ENUM('receipt', 'refund');--> statement-breakpoint
CREATE TYPE "public"."receipt_status" AS ENUM('pending', 'generated', 'sent', 'failed');--> statement-breakpoint
CREATE TABLE "email_logs" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"receipt_id" uuid NOT NULL,
	"trigger" text NOT NULL,
	"to_addresses" text NOT NULL,
	"status" "email_status" DEFAULT 'queued' NOT NULL,
	"attempts" integer DEFAULT 0 NOT NULL,
	"provider_message_id" text,
	"error" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "receipt_sequences" (
	"kind" "receipt_kind" PRIMARY KEY NOT NULL,
	"next_number" integer DEFAULT 1 NOT NULL
);
--> statement-breakpoint
CREATE TABLE "receipts" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"kind" "receipt_kind" NOT NULL,
	"number" integer NOT NULL,
	"user_id" uuid NOT NULL,
	"payment_id" uuid NOT NULL,
	"paypal_transaction_id" text NOT NULL,
	"credits_receipt_id" uuid,
	"amount_minor" integer NOT NULL,
	"net_minor" integer NOT NULL,
	"vat_minor" integer NOT NULL,
	"vat_rate_bp" integer NOT NULL,
	"currency" text NOT NULL,
	"plan_interval" "billing_interval" NOT NULL,
	"period_start" timestamp with time zone NOT NULL,
	"period_end" timestamp with time zone NOT NULL,
	"customer_name" text,
	"customer_email" text NOT NULL,
	"locale" text NOT NULL,
	"status" "receipt_status" DEFAULT 'pending' NOT NULL,
	"provider" text NOT NULL,
	"provider_document_id" text,
	"provider_document_number" text,
	"pdf_key" text,
	"attempts" integer DEFAULT 0 NOT NULL,
	"last_error" text,
	"issued_at" timestamp with time zone DEFAULT now() NOT NULL,
	"generated_at" timestamp with time zone,
	"sent_at" timestamp with time zone,
	CONSTRAINT "receipts_paypal_transaction_id_unique" UNIQUE("paypal_transaction_id")
);
--> statement-breakpoint
ALTER TABLE "users" ADD COLUMN "name" text;--> statement-breakpoint
ALTER TABLE "users" ADD COLUMN "locale" text DEFAULT 'he' NOT NULL;--> statement-breakpoint
ALTER TABLE "users" ADD COLUMN "receipt_cc_email" text;--> statement-breakpoint
ALTER TABLE "email_logs" ADD CONSTRAINT "email_logs_receipt_id_receipts_id_fk" FOREIGN KEY ("receipt_id") REFERENCES "public"."receipts"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "receipts" ADD CONSTRAINT "receipts_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "receipts" ADD CONSTRAINT "receipts_payment_id_payments_id_fk" FOREIGN KEY ("payment_id") REFERENCES "public"."payments"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "email_logs_receipt_idx" ON "email_logs" USING btree ("receipt_id");--> statement-breakpoint
CREATE UNIQUE INDEX "receipts_kind_number_uq" ON "receipts" USING btree ("kind","number");--> statement-breakpoint
CREATE INDEX "receipts_user_idx" ON "receipts" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "receipts_status_idx" ON "receipts" USING btree ("status");