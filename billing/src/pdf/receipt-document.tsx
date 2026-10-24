/**
 * The receipt PDF (@react-pdf/renderer, MIT). Heebo (OFL) is embedded, so Hebrew renders the same on
 * every machine; react-pdf runs the Unicode bidi algorithm, so Hebrew, Latin and numbers mix correctly.
 */
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Document, Font, Image, Page, StyleSheet, Text, View, renderToBuffer } from "@react-pdf/renderer";
import type { BusinessType } from "../receipts/amounts.js";
import { formatNumber } from "../receipts/numbering.js";
import { T, date, documentTitle, money, type Locale } from "../receipts/texts.js";

const FONTS = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../assets/fonts");
Font.register({
  family: "Heebo",
  fonts: [
    { src: path.join(FONTS, "Heebo-Regular.ttf"), fontWeight: 400 },
    { src: path.join(FONTS, "Heebo-Bold.ttf"), fontWeight: 700 },
  ],
});
Font.registerHyphenationCallback((word) => [word]); // never hyphenate Hebrew words or ids

export interface Seller {
  name: string;
  taxId: string;
  address: string;
  email: string;
  businessType: BusinessType;
  /** PNG/JPG file path or data URI */
  logo?: string | null;
}

export interface ReceiptView {
  kind: "receipt" | "refund";
  number: number;
  /** the legal number at an Israeli provider, when it issued the document */
  providerDocumentNumber?: string | null;
  issuedAt: Date;
  locale: Locale;
  customerName: string | null;
  customerEmail: string;
  planInterval: "monthly" | "yearly";
  periodStart: Date;
  periodEnd: Date;
  amountMinor: number;
  netMinor: number;
  vatMinor: number;
  vatRateBp: number;
  currency: string;
  paypalTransactionId: string;
  /** for a refund: the number of the receipt it credits */
  creditsNumber?: number | null;
  /** for a refund: the credited document's id at the external provider (Morning links them) */
  creditsProviderDocumentId?: string | null;
}

const C = { ink: "#18181b", muted: "#64646e", line: "#e4e4e7", soft: "#f4f4f5", accent: "#4f46e5" };

export function ReceiptDocument({ r, seller }: { r: ReceiptView; seller: Seller }) {
  const t = T[r.locale];
  const rtl = r.locale === "he";
  const row = rtl ? "row-reverse" : "row";
  const align = rtl ? "right" : "left";
  const end = rtl ? "left" : "right";
  const s = StyleSheet.create({
    page: { fontFamily: "Heebo", fontSize: 10, color: C.ink, padding: 44, lineHeight: 1.45 },
    row: { flexDirection: row, justifyContent: "space-between" },
    muted: { color: C.muted, textAlign: align },
    // big text needs its own line height, or it overlaps the next line
    h1: { fontSize: 20, fontWeight: 700, textAlign: align, lineHeight: 1.25, marginBottom: 4 },
    brand: { fontSize: 16, fontWeight: 700, color: C.accent, textAlign: align, lineHeight: 1.25, marginBottom: 2 },
    label: { color: C.muted, fontSize: 9, textAlign: align },
    value: { textAlign: align },
    th: { backgroundColor: C.soft, paddingVertical: 7, paddingHorizontal: 10, fontWeight: 700, fontSize: 9 },
    td: { paddingVertical: 9, paddingHorizontal: 10, borderBottomWidth: 1, borderBottomColor: C.line },
    totals: { marginTop: 14, alignSelf: rtl ? "flex-start" : "flex-end", width: 230 },
  });
  const title = documentTitle(r.kind, seller.businessType, r.locale);
  const pct = (r.vatRateBp / 100).toString();
  const number = r.providerDocumentNumber ?? formatNumber(r.number);

  const Field = ({ label, value }: { label: string; value: string }) => (
    <View style={{ marginBottom: 8 }}>
      <Text style={s.label}>{label}</Text>
      <Text style={s.value}>{value}</Text>
    </View>
  );
  const TotalLine = ({ label, value, strong }: { label: string; value: string; strong?: boolean }) => (
    <View style={[s.row, { paddingVertical: 4 }, strong ? { borderTopWidth: 1, borderTopColor: C.ink, marginTop: 4, paddingTop: 8 } : {}]}>
      <Text style={strong ? { fontWeight: 700, fontSize: 12 } : { color: C.muted }}>{label}</Text>
      <Text style={strong ? { fontWeight: 700, fontSize: 12 } : {}}>{value}</Text>
    </View>
  );

  return (
    <Document title={`${title} ${number}`} author={seller.name} language={r.locale}>
      <Page size="A4" style={s.page}>
        {/* header: seller on the start side, document title on the end side */}
        <View style={[s.row, { alignItems: "flex-start", marginBottom: 26 }]}>
          <View style={{ maxWidth: 280 }}>
            {seller.logo ? <Image src={seller.logo} style={{ width: 44, height: 44, marginBottom: 8, alignSelf: rtl ? "flex-end" : "flex-start" }} /> : null}
            <Text style={s.brand}>{seller.name}</Text>
            <Text style={s.muted}>{t.businessType[seller.businessType]}{seller.taxId ? ` · ${t.taxId} ${seller.taxId}` : ""}</Text>
            {seller.address ? <Text style={s.muted}>{seller.address}</Text> : null}
            {seller.email ? <Text style={s.muted}>{seller.email}</Text> : null}
          </View>
          <View>
            <Text style={[s.h1, { textAlign: end }]}>{title}</Text>
            <Text style={[s.muted, { textAlign: end }]}>{`${t.number} ${number}`}</Text>
            <Text style={[s.muted, { textAlign: end }]}>{`${t.issued}: ${date(r.issuedAt, r.locale)}`}</Text>
          </View>
        </View>

        <View style={[s.row, { marginBottom: 18 }]}>
          <View style={{ width: "48%" }}>
            <Field label={t.billedTo} value={r.customerName ? `${r.customerName}\n${r.customerEmail}` : r.customerEmail} />
          </View>
          <View style={{ width: "48%" }}>
            <Field label={t.paymentMethod} value="PayPal" />
            <Field label={t.paypalTx} value={r.paypalTransactionId} />
            {r.kind === "refund" && r.creditsNumber ? <Field label={t.credits} value={formatNumber(r.creditsNumber)} /> : null}
          </View>
        </View>

        {/* line items */}
        <View style={[s.row, s.th]}>
          <Text style={{ width: "50%", textAlign: align }}>{t.description}</Text>
          <Text style={{ width: "30%", textAlign: align }}>{t.period}</Text>
          <Text style={{ width: "20%", textAlign: end }}>{t.amount}</Text>
        </View>
        <View style={[s.row, s.td]}>
          <Text style={{ width: "50%", textAlign: align }}>{t.plan[r.planInterval]}</Text>
          <Text style={{ width: "30%", textAlign: align }}>{`${date(r.periodStart, r.locale)} – ${date(r.periodEnd, r.locale)}`}</Text>
          <Text style={{ width: "20%", textAlign: end }}>{money(r.netMinor, r.currency)}</Text>
        </View>

        <View style={s.totals}>
          {r.vatRateBp > 0 ? (
            <>
              <TotalLine label={t.subtotal} value={money(r.netMinor, r.currency)} />
              <TotalLine label={t.vat(pct)} value={money(r.vatMinor, r.currency)} />
            </>
          ) : null}
          <TotalLine label={r.kind === "refund" ? t.totalRefund : t.total} value={money(r.amountMinor, r.currency)} strong />
        </View>

        {seller.businessType === "exempt" ? <Text style={[s.muted, { marginTop: 18, fontSize: 9 }]}>{t.exemptNote}</Text> : null}
        <Text style={[s.muted, { position: "absolute", bottom: 32, left: 44, right: 44, fontSize: 8, textAlign: "center" }]} fixed>
          {t.footer}
        </Text>
      </Page>
    </Document>
  );
}

export async function renderReceiptPdf(r: ReceiptView, seller: Seller): Promise<Buffer> {
  return renderToBuffer(<ReceiptDocument r={r} seller={seller} />);
}
