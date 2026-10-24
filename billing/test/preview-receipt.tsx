/**
 * npm run preview:receipt [outDir] — renders sample receipts (Hebrew/English, exempt/licensed, refund)
 * to PDF files, to look at the layout without running a payment.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { renderReceiptPdf, type ReceiptView, type Seller } from "../src/pdf/receipt-document.js";

const out = path.resolve(process.argv[2] ?? ".data/preview");
mkdirSync(out, { recursive: true });

const seller: Seller = { name: "Kabala", taxId: "123456789", address: "רחוב הרצל 1, תל אביב", email: "billing@kabala.app", businessType: "exempt" };
const base: ReceiptView = {
  kind: "receipt",
  number: 123,
  issuedAt: new Date("2026-10-22T09:30:00Z"),
  locale: "he",
  customerName: "דנה כהן",
  customerEmail: "dana@gmail.com",
  planInterval: "yearly",
  periodStart: new Date("2026-10-22T09:30:00Z"),
  periodEnd: new Date("2027-10-22T09:30:00Z"),
  amountMinor: 27900,
  netMinor: 27900,
  vatMinor: 0,
  vatRateBp: 0,
  currency: "ILS",
  paypalTransactionId: "8AB12345CD678901E",
};

const samples: [string, ReceiptView, Seller][] = [
  ["receipt-he-exempt.pdf", base, seller],
  ["receipt-en-exempt.pdf", { ...base, locale: "en", customerName: "Dana Cohen" }, seller],
  ["receipt-he-licensed.pdf", { ...base, netMinor: 23644, vatMinor: 4256, vatRateBp: 1800 }, { ...seller, businessType: "licensed" }],
  ["refund-he-exempt.pdf", { ...base, kind: "refund", number: 7, amountMinor: 10000, netMinor: 10000, creditsNumber: 123, paypalTransactionId: "REF-55X21" }, seller],
];
for (const [name, r, s] of samples) writeFileSync(path.join(out, name), await renderReceiptPdf(r, s));
console.log(`wrote ${samples.length} PDFs to ${out}`);
