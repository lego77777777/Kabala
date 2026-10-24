/**
 * Expiring links to a receipt PDF: /r/<receiptId>/<expires>/<signature>.pdf
 * The signature is an HMAC-SHA256 of the receipt id and expiry with a server-only secret, so a link
 * can't be forged, altered to another receipt or extended. Links in emails last 30 days by default;
 * the account page mints short-lived ones on demand.
 */
import { createHmac, timingSafeEqual } from "node:crypto";

export class LinkSigner {
  constructor(private readonly secret: string, private readonly publicUrl: string) {
    if (secret.length < 32) throw new Error("RECEIPT_LINK_SECRET must be at least 32 characters");
  }

  private sign(receiptId: string, expires: number): string {
    return createHmac("sha256", this.secret).update(`receipt-pdf:${receiptId}:${expires}`).digest("base64url");
  }

  url(receiptId: string, ttlSeconds: number, now = Date.now()): string {
    const expires = Math.floor(now / 1000) + ttlSeconds;
    return `${this.publicUrl.replace(/\/$/, "")}/r/${receiptId}/${expires}/${this.sign(receiptId, expires)}.pdf`;
  }

  /** true only for an untampered, unexpired link */
  verify(receiptId: string, expires: number, signature: string, now = Date.now()): boolean {
    if (!Number.isInteger(expires) || expires < Math.floor(now / 1000)) return false;
    const expected = Buffer.from(this.sign(receiptId, expires));
    const given = Buffer.from(signature);
    return expected.length === given.length && timingSafeEqual(expected, given);
  }
}
