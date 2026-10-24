/**
 * Morning (formerly Green Invoice), an Israeli service whose documents are valid for the Tax Authority.
 * Morning issues the legal document and numbers it; Kabala stores its PDF and sends the email itself.
 *
 * API facts used here (verified by the open-source GreenInvoice-MCP client, MIT, Oct 2026):
 *   token   POST https://api.morning.co/idp/v1/oauth/token {grant_type:"client_credentials", client_id, client_secret}
 *           → { accessToken } valid 60 min (sandbox: api.sandbox.morning.dev; legacy fallback: POST {api}/account/token {id, secret} → { token })
 *   create  POST {api}/documents  { type, lang, currency, vatType, client{name,emails,add}, income[], payment[], remarks, linkedDocumentIds }
 *   links   GET  {api}/documents/{id}/download/links → { he, en, origin }
 *   types   400 receipt · 320 tax invoice-receipt · 330 credit invoice;  payment type 5 = PayPal
 *   vatType 0 before VAT · 1 VAT included · 2 exempt
 * The sandbox is a separate Morning account with its own API key (production keys get 401 there).
 *
 * Idempotency: Morning has no external-id field, so the pipeline saves Morning's document id right
 * after creation and passes it back on retries; with an id we only download, never create again.
 */
import type { BusinessType } from "../receipts/amounts.js";
import { InternalProvider } from "./internal.js";
import type { IssuedDocument, ReceiptProvider } from "./types.js";

const DOC_TYPE = { receipt: { exempt: 400, licensed: 320 }, refund: { licensed: 330 } } as const;
const PAYMENT_PAYPAL = 5;
const VAT_INCLUDED = 1;
const VAT_EXEMPT = 2;

export interface MorningOptions {
  /** https://api.greeninvoice.co.il/api/v1 or https://sandbox.d.greeninvoice.co.il/api/v1 */
  apiUrl: string;
  keyId: string;
  secret: string;
  businessType: BusinessType;
  /** override the OAuth token URL (defaults follow the API URL: production or sandbox) */
  tokenUrl?: string;
  fetchImpl?: typeof fetch;
}

export class MorningError extends Error {
  constructor(message: string, readonly status: number | null) {
    super(message);
  }
}

export class MorningProvider implements ReceiptProvider {
  readonly name = "morning";
  private token: { value: string; until: number } | null = null;
  private readonly fetch: typeof fetch;
  private readonly internal = new InternalProvider();

  constructor(private readonly o: MorningOptions) {
    this.fetch = o.fetchImpl ?? fetch;
  }

  async issue({ receiptId, view, seller, existingDocumentId }: Parameters<ReceiptProvider["issue"]>[0]): Promise<IssuedDocument> {
    if (view.kind === "refund" && this.o.businessType === "exempt") {
      // An exempt dealer has no credit invoice at Morning; the refund confirmation is our own document.
      // (Confirm the right document for refunds with your accountant before going live.)
      return this.internal.issue({ receiptId, view, seller });
    }
    let id = existingDocumentId ?? null;
    let number: string | null = null;
    if (!id) {
      const created = await this.call<{ id: string; number?: string | number }>("POST", "/documents", this.document(receiptId, view));
      id = created.id;
      number = created.number != null ? String(created.number) : null;
    }
    const doc = number ? null : await this.call<{ number?: string | number }>("GET", `/documents/${id}`);
    const links = await this.call<{ he?: string; en?: string; origin?: string }>("GET", `/documents/${id}/download/links`);
    const url = (view.locale === "en" ? links.en : links.he) ?? links.origin;
    if (!url) throw new MorningError("Morning returned no download link", null);
    const pdf = await this.fetch(url);
    if (!pdf.ok) throw new MorningError(`PDF download failed (${pdf.status})`, pdf.status);
    return { pdf: Buffer.from(await pdf.arrayBuffer()), documentId: id, documentNumber: number ?? (doc?.number != null ? String(doc.number) : null) };
  }

  /** The create-document body. Amounts are what PayPal charged (VAT included for a licensed dealer). */
  document(receiptId: string, view: Parameters<ReceiptProvider["issue"]>[0]["view"]) {
    const exempt = this.o.businessType === "exempt";
    const total = view.amountMinor / 100;
    const he = view.locale === "he";
    const plan = he ? (view.planInterval === "yearly" ? "מנוי שנתי – Kabala Pro" : "מנוי חודשי – Kabala Pro") : `Kabala Pro – ${view.planInterval} plan`;
    const day = (d: Date) => d.toISOString().slice(0, 10);
    const vatType = exempt ? VAT_EXEMPT : VAT_INCLUDED;
    const type = view.kind === "refund" ? DOC_TYPE.refund.licensed : DOC_TYPE.receipt[this.o.businessType];
    return {
      type,
      lang: view.locale,
      currency: view.currency,
      vatType,
      client: { name: view.customerName ?? view.customerEmail, emails: [view.customerEmail], add: true },
      income: [{ description: `${plan} (${day(view.periodStart)} – ${day(view.periodEnd)})`, quantity: 1, price: total, currency: view.currency, vatType }],
      ...(view.kind === "receipt" ? { payment: [{ date: day(view.issuedAt), type: PAYMENT_PAYPAL, price: total, currency: view.currency }] } : {}),
      ...(view.kind === "refund" && view.creditsProviderDocumentId ? { linkedDocumentIds: [view.creditsProviderDocumentId] } : {}),
      // our ids, so a document can always be traced back (and found by hand if a create ever timed out)
      remarks: `PayPal ${view.paypalTransactionId} · Kabala ${receiptId}`,
    };
  }

  private tokenUrl() {
    if (this.o.tokenUrl) return this.o.tokenUrl;
    return this.o.apiUrl.includes("sandbox") ? "https://api.sandbox.morning.dev/idp/v1/oauth/token" : "https://api.morning.co/idp/v1/oauth/token";
  }

  private async accessToken(): Promise<string> {
    if (this.token && this.token.until > Date.now()) return this.token.value;
    let value: string | null = null;
    let lifetime = 50 * 60_000; // OAuth tokens last 60 minutes; renew early
    const oauth = await this.fetch(this.tokenUrl(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ grant_type: "client_credentials", client_id: this.o.keyId, client_secret: this.o.secret }),
    }).catch(() => null);
    if (oauth?.ok) {
      const body = (await oauth.json()) as { accessToken?: string; access_token?: string };
      value = body.accessToken ?? body.access_token ?? null;
    }
    if (!value) {
      // legacy Green Invoice token route, still answered by some environments
      const legacy = await this.fetch(`${this.o.apiUrl}/account/token`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: this.o.keyId, secret: this.o.secret }) });
      if (!legacy.ok) throw new MorningError(`Morning authentication failed (${oauth?.status ?? "network"} / ${legacy.status})`, legacy.status);
      value = ((await legacy.json()) as { token?: string }).token ?? null;
      lifetime = 20 * 60_000;
    }
    if (!value) throw new MorningError("Morning returned no token", null);
    this.token = { value, until: Date.now() + lifetime };
    return value;
  }

  private async call<T>(method: "GET" | "POST", path: string, body?: object, retried = false): Promise<T> {
    const r = await this.fetch(`${this.o.apiUrl}${path}`, {
      method,
      headers: { Authorization: `Bearer ${await this.accessToken()}`, "Content-Type": "application/json", Accept: "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (r.status === 401 && !retried) {
      this.token = null;
      return this.call(method, path, body, true);
    }
    if (!r.ok) {
      const err = (await r.json().catch(() => ({}))) as { errorCode?: number; errorMessage?: string };
      // Morning's error message is safe to log: it never contains card or secret data
      throw new MorningError(`Morning ${method} ${path} failed (${r.status}${err.errorCode ? ` / ${err.errorCode}` : ""}): ${err.errorMessage ?? ""}`.trim(), r.status);
    }
    return (await r.json()) as T;
  }
}
