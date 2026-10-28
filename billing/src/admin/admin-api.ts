/**
 * Read-only API for the admin panel (server/kabala_server/admin.py), which shows PayPal subscribers and payments
 * next to Stripe's.
 *
 *   GET /api/admin/stats         counts, revenue this month per currency, signups in the last 30 days
 *   GET /api/admin/subscribers   ?q=&status=&page=   subscriptions with the user and plan
 *   GET /api/admin/payments      ?page=             payments with the user
 *
 * Every request carries a short-lived JWT the panel signs with its Ed25519 key (PyJWT on the Python side);
 * this server holds only the public key (ADMIN_PANEL_PUBLIC_KEY), so it can check requests but never forge one.
 * The token is bound to one method + path (htm/htu, as in DPoP), lives 60 seconds and is single-use (jti).
 * A viewer's token gets masked emails here too, not only in the panel.
 */
import { and, count, desc, eq, gte, ilike, sql } from "drizzle-orm";
import type { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";
import { importSPKI, jwtVerify } from "jose";
import { z } from "zod";
import type { Db } from "../db/client.js";
import { payments, plans, subscriptions, users } from "../db/schema.js";
import { isEntitled } from "../services/subscription-state.js";

export const ISSUER = "kabala-admin";
export const AUDIENCE = "kabala-billing";
const MAX_AGE_S = 60;
const PAGE = 25;

type Role = "viewer" | "admin" | "owner";
interface AdminClaims { sub: string; role: Role; htm: string; htu: string; jti: string }

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

export async function registerAdminApi(app: FastifyInstance, d: { db: Db; publicKeyPem: string; mode: string; now?: () => Date }) {
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
      const c = z.object({ sub: z.string().email(), role: z.enum(["viewer", "admin", "owner"]), htm: z.string(), htu: z.string(), jti: z.string().min(16) }).parse(payload);
      const path = req.url.split("?")[0];
      if (c.htm !== req.method || c.htu !== path) throw new Error("token is for another request");
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
}
