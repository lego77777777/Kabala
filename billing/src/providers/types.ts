/**
 * Who issues the receipt document, chosen by RECEIPT_PROVIDER without code changes:
 *   internal → our own PDF (development, tests, customers outside Israel)
 *   morning  → Green Invoice / Morning, an Israeli certified service that issues the legal document
 * Either way Kabala sends the email; the provider only produces the document.
 */
import type { ReceiptView, Seller } from "../pdf/receipt-document.js";

export interface IssuedDocument {
  pdf: Buffer;
  /** id and number of the document at the external provider (null for internal) */
  documentId: string | null;
  documentNumber: string | null;
}

export interface ReceiptProvider {
  readonly name: string;
  /**
   * Issues the document. Must be idempotent for the same receipt (`receiptId`): a retry after a
   * timeout must not create a second legal document.
   */
  issue(input: { receiptId: string; view: ReceiptView; seller: Seller; existingDocumentId?: string | null }): Promise<IssuedDocument>;
}
