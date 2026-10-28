CREATE TYPE "public"."license_key_status" AS ENUM('active', 'revoked');--> statement-breakpoint
CREATE TABLE "license_keys" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"subscription_id" uuid,
	"key_hash" text NOT NULL,
	"key_encrypted" text NOT NULL,
	"hint" text NOT NULL,
	"status" "license_key_status" DEFAULT 'active' NOT NULL,
	"activations" integer DEFAULT 0 NOT NULL,
	"last_used_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"revoked_at" timestamp with time zone,
	"revoked_by" text,
	CONSTRAINT "license_keys_key_hash_unique" UNIQUE("key_hash")
);
--> statement-breakpoint
ALTER TABLE "license_keys" ADD CONSTRAINT "license_keys_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "license_keys" ADD CONSTRAINT "license_keys_subscription_id_subscriptions_id_fk" FOREIGN KEY ("subscription_id") REFERENCES "public"."subscriptions"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "license_keys_user_idx" ON "license_keys" USING btree ("user_id");--> statement-breakpoint
CREATE UNIQUE INDEX "license_keys_one_active_uq" ON "license_keys" USING btree ("user_id") WHERE status = 'active';