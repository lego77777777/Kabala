import { renderReceiptPdf } from "../pdf/receipt-document.js";
import type { IssuedDocument, ReceiptProvider } from "./types.js";

/** Our own PDF. Re-rendering is deterministic, so retries are naturally idempotent. */
export class InternalProvider implements ReceiptProvider {
  readonly name = "internal";

  async issue({ view, seller }: Parameters<ReceiptProvider["issue"]>[0]): Promise<IssuedDocument> {
    return { pdf: await renderReceiptPdf(view, seller), documentId: null, documentNumber: null };
  }
}
