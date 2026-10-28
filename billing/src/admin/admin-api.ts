/**
 * Read-only API for the admin panel (server/kabala_server/admin.py), which shows PayPal subscribers and payments
 * next to Stripe's.
 *
 *   GET /api/admin/stats         counts, revenue this month per currency, signups in the last 30 days
 *   GET /api/admin/subscribers   ?q=&status=&page=   subscriptions with the user and plan
 *   GET /api/admin/payments      ?page=             payments with the user
 *   GET /api/admin/keys          ?q=&status=&page=   license keys (never the key: only its last group)
 *   POST /api/admin/keys/:id/revoke                  admin role and up
 *   GET /api/admin/receipts      ?q=&status=&page=   receipts with their latest email
 *   GET /api/admin/receipts/:id                      one receipt and every email sent for it
 *   GET /api/admin/receipts/:id/pdf                  the PDF (admin and up)
 *   POST /api/admin/receipts/:id/send  { to? }      email it again: to the customer, or to one address (admin+)
 *   POST /api/admin/receipts/:id/retry              a failed receipt gets another go (admin+)
 *
 * A request with a body also carries `bh`, the SHA-256 of the body (sorted keys), so a token can't be reused
 * with another body (e.g. another recipient).
 *
 * Every request carries a short-lived JWT the panel signs with its Ed25519 key (PyJWT on the Python side);
 * this server holds only the public key (ADMIN_PANEL_PUBLIC_KEY), so it can check requests but never forge one.
 * The token is bound to one method + path (htm/htu, as in DPoP), lives 60 seconds and is single-use (jti).
 * A viewer's token gets masked emails here too, not only in the panel.
 */
import { createHash } from "node:crypto";
import { and, count, desc, eq, gte, ilike, inArray, sql } from "drizzle-orm";
import type { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";
import { importSPKI, jwtVerify } from "jose";
import { z } from "zod";
import type { Db } from "../db/client.js";
import { emailLogs, payments, plans, receipts, subscriptions, users } from "../db/schema.js";
import { isMailbox, normalizeMailbox } from "../email/address.js";
import type { ReceiptPipeline } from "../receipts/pipeline.js";
import type { Storage } from "../storage/storage.js";
import type { LicenseKeyService } from "../licenses/keys.js";
import { isEntitled } from "../services/subscription-state.js";

export const ISSUER = "kabala-admin";
export const AUDIENCE = "kabala-billing";
const MAX_AGE_S = 60;
const PAGE = 25;

type Role = "viewer" | "admin" | "owner";
interface AdminClaims { sub: string; role: Role; htm: string; htu: string; jti: string; bh?: string }

/** JSON with sorted keys at every level: the same bytes the panel hashed (Python json.dumps(sort_keys=True)). */
const canonical = (v: unknown): string =>
  Array.isArray(v) ? `[${v.map(canonical).join(",")}]`
  : v && typeof v === "object" ? `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canonical((v as Record<string, unknown>)[k])}`).join(",")}}`
  : JSON.stringify(v);
export const bodyHash = (body: unknown) => createHash("sha256").update(canonical(body)).digest("base64url");

export const maskEmail = (email: string) => {
  const [name, domain] = email.split("@");
  return `${(name ?? "").slice(0, 2)}***@${domain ?? ""}`;
};

/** Remembers token ids until they expire, so a captured token can't be replayed. */
class SeenTokens {
  private readonly seen = new Map<string, number>();
  constructor(private readonly cap = 50_000) {}
  firstUse(jti: string, expires: number, now: number): boolean {
    if (this.seen.size >= this.cap) for (const [k, exp] of this.seen) if (exp < now || this.seen.size >= this.cap) this.seen.delete(k);
    if (this.seen.has(jti)) return false;
    this.seen.set(jti, expires);
    return true;
  }
}

export async function registerAdminApi(app: FastifyInstance, d: {
  db: Db; publicKeyPem: string; mode: string; keys?: LicenseKeyService; now?: () => Date;
  receipts?: { pipeline: Pick<ReceiptPipeline, "adminSend" | "retry">; storage: Storage };
}) {
  const key = await importSPKI(d.publicKeyPem.replace(/\\n/g, "\n"), "EdDSA");
  const now = d.now ?? (() => new Date());
  const seen = new SeenTokens();

  async function verify(req: FastifyRequest, reply: FastifyReply): Promise<AdminClaims | null> {
    const token = (req.headers.authorization ?? "").replace(/^Bearer\s+/i, "");
    try {
      const { payload } = await jwtVerify(token, key, {
        issuer: ISSUER, audience: AUDIENCE, algorithms: ["EdDSA"], maxTokenAge: `${MAX_AGE_S}s`, clockTolerance: 30, currentDate: now(),
        requiredClaims: ["sub", "jti", "exp", "iat"],
      });
      const c = z.object({ sub: z.string().email(), role: z.enum(["viewer", "admin", "owner"]), htm: z.string(), htu: z.string(), jti: z.string().min(16), bh: z.string().optional() }).parse(payload);
      const path = req.url.split("?")[0];
      if (c.htm !== req.method || c.htu !== path) throw new Error("token is for another request");
      const hasBody = req.body !== undefined && req.body !== null;
      if (hasBody ? c.bh !== bodyHash(req.body) : c.bh !== undefined) throw new Error("token is for another body");
      if (!seen.firstUse(c.jti, (payload.exp ?? 0) * 1000, now().getTime())) throw new Error("token already used");
      return c;
    } catch (e) {
      req.log.warn({ reason: (e as Error).message }, "admin API request rejected");
      await reply.code(401).send({ error: "unauthorized" });
      return null;
    }
  }

  const email = (c: AdminClaims, value: string) => (c.role === "viewer" ? maskEmail(value) : value);
  const limit = { config: { rateLimit: { max: 120, timeWindow: "1 minute" } } };

  app.get("/api/admin/stats", limit, async (req, reply) => {
    const c = await verify(req, reply);
    if (!c) return;
    const t = now();
    const monthStart = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), 1));
    const since = new Date(t.getTime() - 29 * 86_400_000);
    since.setUTCHours(0, 0, 0, 0);
    const [{ total } = { total: 0 }] = await d.db.select({ total: count() }).from(users);
    const subs = await d.db.select().from(subscriptions);
    const byStatus: Record<string, number> = {};
    const entitledUsers = new Set<string>();
    for (const s of subs) {
      byStatus[s.status] = (byStatus[s.status] ?? 0) + 1;
      if (isEntitled({ status: s.status, currentPeriodEnd: s.currentPeriodEnd, graceUntil: s.graceUntil, cancelledAt: s.cancelledAt }, t)) entitledUsers.add(s.userId);
    }
    const revenue = await d.db
      .select({ currency: payments.currency, gross: sql<number>`sum(${payments.amountMinor})::int`, refunded: sql<number>`sum(${payments.refundedMinor})::int`, n: count() })
      .from(payments).where(gte(payments.paidAt, monthStart)).groupBy(payments.currency);
    const signups = await d.db
      .select({ day: sql<string>`to_char(${users.createdAt} at time zone 'UTC', 'YYYY-MM-DD')`, n: count() })
      .from(users).where(gte(users.createdAt, since)).groupBy(sql`1`);
    const perDay = new Map(signups.map((r) => [r.day, Number(r.n)]));
    return {
      mode: d.mode,
      users: Number(total),
      pro: entitledUsers.size,
      subscriptions: byStatus,
      revenueThisMonth: revenue.map((r) => ({ currency: r.currency, netMinor: Number(r.gross) - Number(r.refunded), payments: Number(r.n) })),
      signups: Array.from({ length: 30 }, (_, i) => {
        const day = new Date(since.getTime() + i * 86_400_000).toISOString().slice(0, 10);
        return { day, count: perDay.get(day) ?? 0 };
      }),
    };
  });

  app.get("/api/admin/subscribers", limit, async (req, reply) => {
    const c = await verify(req, reply);
    if (!c) return;
    const q = z.object({
      q: z.string().max(100).optional(),
      status: z.enum(["pending", "trial", "active", "past_due", "cancelled", "expired"]).optional(),
      page: z.coerce.number().int().min(1).default(1),
    }).parse(req.query);
    const where = and(
      q.status ? eq(subscriptions.status, q.status) : undefined,
      // a viewer can't search: it would reveal masked addresses one guess at a time
      q.q && c.role !== "viewer" ? ilike(users.email, `%${q.q.replace(/[\\%_]/g, (m) => `\\${m}`)}%`) : undefined,
    );
    const [{ total } = { total: 0 }] = await d.db.select({ total: count() }).from(subscriptions).innerJoin(users, eq(users.id, subscriptions.userId)).where(where);
    const rows = await d.db
      .select({ s: subscriptions, u: users, p: plans }).from(subscriptions)
      .innerJoin(users, eq(users.id, subscriptions.userId)).innerJoin(plans, eq(plans.id, subscriptions.planId))
      .where(where).orderBy(desc(subscriptions.createdAt)).limit(PAGE).offset((q.page - 1) * PAGE);
    const t = now();
    return {
      total: Number(total), page: q.page, pages: Math.max(1, Math.ceil(Number(total) / PAGE)),
      subscribers: rows.map(({ s, u, p }) => ({
        email: email(c, u.email),
        status: s.status,
        entitled: isEntitled({ status: s.status, currentPeriodEnd: s.currentPeriodEnd, graceUntil: s.graceUntil, cancelledAt: s.cancelledAt }, t),
        interval: p.interval, currency: p.currency, amountMinor: p.amountMinor,
        currentPeriodEnd: s.currentPeriodEnd, graceUntil: s.graceUntil, cancelledAt: s.cancelledAt, createdAt: s.createdAt,
        paypalSubscriptionId: c.role === "viewer" ? null : s.paypalSubscriptionId,
      })),
    };
  });

  app.get("/api/admin/payments", limit, async (req, reply) => {
    const c = await verify(req, reply);
    if (!c) return;
    const { page } = z.object({ page: z.coerce.number().int().min(1).default(1) }).parse(req.query);
    const [{ total } = { total: 0 }] = await d.db.select({ total: count() }).from(payments);
    const rows = await d.db.select({ pay: payments, u: users }).from(payments).innerJoin(users, eq(users.id, payments.userId))
      .orderBy(desc(payments.paidAt)).limit(PAGE).offset((page - 1) * PAGE);
    return {
      total: Number(total), page, pages: Math.max(1, Math.ceil(Number(total) / PAGE)),
      payments: rows.map(({ pay, u }) => ({
        email: email(c, u.email), amountMinor: pay.amountMinor, refundedMinor: pay.refundedMinor, currency: pay.currency,
        status: pay.status, paidAt: pay.paidAt, paypalSaleId: c.role === "viewer" ? null : pay.paypalSaleId,
      })),
    };
  });

  app.get("/api/admin/keys", limit, async (req, reply) => {
    const c = await verify(req, reply);
    if (!c) return;
    if (!d.keys) return reply.code(404).send({ error: "keys_not_configured" });
    const q = z.object({ q: z.string().max(100).optional(), status: z.enum(["active", "revoked"]).optional(), page: z.coerce.number().int().min(1).default(1) }).parse(req.query);
    const list = await d.keys.adminList({ q: c.role === "viewer" ? undefined : q.q, status: q.status, page: q.page });
    return { ...list, keys: list.keys.map((k) => ({ ...k, email: email(c, k.email) })) };
  });

  app.post("/api/admin/keys/:id/revoke", limit, async (req, reply) => {
    const c = await verify(req, reply);
    if (!c) return;
    if (c.role === "viewer") return reply.code(403).send({ error: "forbidden" });
    const { id } = z.object({ id: z.string().uuid() }).parse(req.params);
    if (!d.keys || !(await d.keys.revoke(id, c.sub))) return reply.code(404).send({ error: "not_found" });
    req.log.info({ keyId: id, by: c.sub }, "license key revoked from the admin panel");
    return { ok: true };
  });

  // ---- receipts (the PDF + email sent to the customer after each payment) --------------------------------

  const lastEmails = async (ids: string[]) => {
    if (!ids.length) return new Map<string, typeof emailLogs.$inferSelect>();
    const logs = await d.db.select().from(emailLogs).where(inArray(emailLogs.receiptId, ids)).orderBy(desc(emailLogs.createdAt));
    const last = new Map<string, typeof emailLogs.$inferSelect>();
    for (const l of logs) if (!last.has(l.receiptId)) last.set(l.receiptId, l);
    return last;
  };
  const recipients = (c: AdminClaims, to: string) => to.split(",").map((a) => email(c, a)).join(", ");
  const receiptOut = (c: AdminClaims, r: typeof receipts.$inferSelect) => ({
    id: r.id, kind: r.kind, number: r.providerDocumentNumber ?? String(r.number).padStart(6, "0"),
    email: email(c, r.customerEmail), amountMinor: r.amountMinor, currency: r.currency, status: r.status,
    issuedAt: r.issuedAt, sentAt: r.sentAt, attempts: r.attempts, lastError: c.role === "viewer" ? null : r.lastError,
  });

  app.get("/api/admin/receipts", limit, async (req, reply) => {
    const c = await verify(req, reply);
    if (!c) return;
    const q = z.object({ q: z.string().max(100).optional(), status: z.enum(["pending", "generated", "sent", "failed"]).optional(), page: z.coerce.number().int().min(1).default(1) }).parse(req.query);
    const where = and(
      q.status ? eq(receipts.status, q.status) : undefined,
      q.q && c.role !== "viewer" ? ilike(receipts.customerEmail, `%${q.q.replace(/[\\%_]/g, (m) => `\\${m}`)}%`) : undefined,
    );
    const [{ total } = { total: 0 }] = await d.db.select({ total: count() }).from(receipts).where(where);
    const rows = await d.db.select().from(receipts).where(where).orderBy(desc(receipts.issuedAt)).limit(PAGE).offset((q.page - 1) * PAGE);
    const last = await lastEmails(rows.map((r) => r.id));
    return {
      total: Number(total), page: q.page, pages: Math.max(1, Math.ceil(Number(total) / PAGE)),
      receipts: rows.map((r) => {
        const l = last.get(r.id);
        return { ...receiptOut(c, r), lastEmail: l ? { status: l.status, trigger: l.trigger, to: recipients(c, l.toAddresses), at: l.updatedAt } : null };
      }),
    };
  });

  app.get("/api/admin/receipts/:id", limit, async (req, reply) => {
    const c = await verify(req, reply);
    if (!c) return;
    const { id } = z.object({ id: z.string().uuid() }).parse(req.params);
    const [r] = await d.db.select().from(receipts).where(eq(receipts.id, id));
    if (!r) return reply.code(404).send({ error: "not_found" });
    const logs = await d.db.select().from(emailLogs).where(eq(emailLogs.receiptId, id)).orderBy(desc(emailLogs.createdAt));
    return {
      ...receiptOut(c, r),
      emails: logs.map((l) => ({ status: l.status, trigger: l.trigger, to: recipients(c, l.toAddresses), attempts: l.attempts, error: c.role === "viewer" ? null : l.error, createdAt: l.createdAt, updatedAt: l.updatedAt })),
    };
  });

  app.get("/api/admin/receipts/:id/pdf", limit, async (req, reply) => {
    const c = await verify(req, reply);
    if (!c) return;
    if (c.role === "viewer") return reply.code(403).send({ error: "forbidden" });
    const { id } = z.object({ id: z.string().uuid() }).parse(req.params);
    const [r] = await d.db.select().from(receipts).where(eq(receipts.id, id));
    const pdf = r?.pdfKey && d.receipts ? await d.receipts.storage.get(r.pdfKey) : null;
    if (!r || !pdf) return reply.code(404).send({ error: "not_ready" });
    return reply.header("Content-Type", "application/pdf").header("Cache-Control", "private, no-store")
      .header("Content-Disposition", `attachment; filename="kabala-${r.kind}-${r.providerDocumentNumber ?? String(r.number).padStart(6, "0")}.pdf"`).send(pdf);
  });

  app.post("/api/admin/receipts/:id/send", limit, async (req, reply) => {
    const c = await verify(req, reply);
    if (!c) return;
    if (c.role === "viewer") return reply.code(403).send({ error: "forbidden" });
    const { id } = z.object({ id: z.string().uuid() }).parse(req.params);
    const { to } = z.object({ to: z.string().max(254).refine(isMailbox).optional() }).parse(req.body ?? {});
    if (!d.receipts) return reply.code(404).send({ error: "not_configured" });
    const result = await d.receipts.pipeline.adminSend(id, to ? normalizeMailbox(to) : undefined);
    if (result !== "queued") return reply.code(result === "not_found" ? 404 : 409).send({ error: result });
    req.log.info({ receipt: id, by: c.sub, custom: Boolean(to) }, "receipt email queued from the admin panel");
    return { ok: true };
  });

  app.post("/api/admin/receipts/:id/retry", limit, async (req, reply) => {
    const c = await verify(req, reply);
    if (!c) return;
    if (c.role === "viewer") return reply.code(403).send({ error: "forbidden" });
    const { id } = z.object({ id: z.string().uuid() }).parse(req.params);
    if (!d.receipts) return reply.code(404).send({ error: "not_configured" });
    const result = await d.receipts.pipeline.retry(id);
    if (result !== "queued") return reply.code(result === "not_found" ? 404 : 409).send({ error: result });
    return { ok: true };
  });
}
