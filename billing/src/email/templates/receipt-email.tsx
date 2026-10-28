/**
 * The receipt email (React Email, MIT): table-based layout that holds up in Gmail, Outlook and phones,
 * full RTL for Hebrew, dark-mode colours where the client supports them, and a plain-text twin.
 */
import { Body, Button, Column, Container, Head, Hr, Html, Preview, Row, Section, Text } from "@react-email/components";
import { render } from "@react-email/render";
import { T, type Locale } from "../../receipts/texts.js";

export interface ReceiptEmailProps {
  locale: Locale;
  /** opening line from the admin panel's settings (already filled in); null = the built-in thanks */
  intro?: string | null;
  kind: "receipt" | "refund";
  customerName: string | null;
  documentTitle: string;
  documentNumber: string;
  issued: string;
  plan: string;
  period: string;
  amount: string;
  /** licensed dealers only, e.g. { label: 'מע"מ 18%', value: "₪42.56" } */
  vat?: { label: string; value: string } | null;
  nextCharge?: string | null;
  paypalTransactionId: string;
  accountUrl: string;
  sellerName: string;
  sellerFooter: string;
}

const C = { bg: "#f4f4f5", card: "#ffffff", ink: "#18181b", muted: "#64646e", line: "#e4e4e7", accent: "#4f46e5" };
const FONT = "'Heebo','Segoe UI',Arial,Helvetica,sans-serif";

// Dark mode: Apple Mail / iOS / Outlook.com honour these; Gmail applies its own inversion.
const DARK_CSS = `
:root { color-scheme: light dark; supported-color-schemes: light dark; }
@media (prefers-color-scheme: dark) {
  .bg { background:#0d0d10 !important; }
  .card { background:#16161b !important; border-color:#2e2e38 !important; }
  .ink { color:#ececf1 !important; }
  .muted { color:#9c9caa !important; }
  .line { border-color:#2e2e38 !important; }
}
[data-ogsc] .card { background:#16161b !important; }
[data-ogsc] .ink { color:#ececf1 !important; }
[data-ogsc] .muted { color:#9c9caa !important; }`;

export function ReceiptEmail(p: ReceiptEmailProps) {
  const t = T[p.locale];
  const dir = t.dir;
  const align = dir === "rtl" ? "right" : "left";
  const end = dir === "rtl" ? "left" : "right";
  const text = (extra: object = {}) => ({ fontFamily: FONT, fontSize: "15px", lineHeight: "24px", color: C.ink, margin: 0, textAlign: align, ...extra }) as const;
  const line = (label: string, value: string, strong = false) => (
    <Row style={{ padding: "6px 0" }}>
      <Column className="muted" style={{ ...text({ color: C.muted, fontSize: "14px" }) }}>{label}</Column>
      <Column className="ink" style={{ ...text({ textAlign: end, fontWeight: strong ? 700 : 400, fontSize: strong ? "17px" : "15px" }) }}>
        <span dir="ltr">{value}</span>
      </Column>
    </Row>
  );

  return (
    <Html lang={p.locale} dir={dir}>
      <Head>
        <meta name="color-scheme" content="light dark" />
        <meta name="supported-color-schemes" content="light dark" />
        <style>{DARK_CSS}</style>
      </Head>
      <Preview>{p.kind === "refund" ? t.refundThanks : t.preview(p.amount)}</Preview>
      <Body className="bg" style={{ backgroundColor: C.bg, margin: 0, padding: "24px 0", direction: dir }}>
        <Container style={{ maxWidth: "560px", width: "100%", margin: "0 auto", padding: "0 12px" }}>
          <Text style={text({ fontSize: "22px", fontWeight: 700, color: C.accent, padding: "4px 4px 16px" })}>{p.sellerName}</Text>

          <Section className="card" style={{ backgroundColor: C.card, border: `1px solid ${C.line}`, borderRadius: "14px", padding: "28px 28px 24px" }}>
            <Text className="ink" style={text({ fontSize: "18px", fontWeight: 700, marginBottom: "8px" })}>{t.hello(p.customerName)}</Text>
            <Text className="ink" style={text({ marginBottom: "22px", whiteSpace: "pre-line" })}>{p.intro || (p.kind === "refund" ? t.refundThanks : t.thanks)}</Text>

            <Text className="muted" style={text({ fontSize: "13px", fontWeight: 700, color: C.muted, letterSpacing: "0.3px", marginBottom: "6px" })}>{t.summary}</Text>
            <Section className="line" style={{ borderTop: `1px solid ${C.line}`, borderBottom: `1px solid ${C.line}`, padding: "8px 0" }}>
              {line(p.documentTitle, p.documentNumber)}
              {line(t.issued, p.issued)}
              {line(t.description, p.plan)}
              {line(t.period, p.period)}
              {line(t.paymentMethod, "PayPal")}
              {line(t.paypalTx, p.paypalTransactionId)}
              {p.vat ? line(p.vat.label, p.vat.value) : null}
              {line(p.kind === "refund" ? t.totalRefund : t.total, p.amount, true)}
            </Section>

            {p.nextCharge ? <Text className="muted" style={text({ color: C.muted, fontSize: "14px", marginTop: "14px" })}>{`${t.nextCharge}: ${p.nextCharge}`}</Text> : null}

            <Section style={{ textAlign: "center", padding: "26px 0 6px" }}>
              <Button
                href={p.accountUrl}
                style={{ backgroundColor: C.accent, color: "#ffffff", fontFamily: FONT, fontSize: "15px", fontWeight: 700, borderRadius: "10px", padding: "13px 26px", textDecoration: "none" }}
              >
                {t.viewSubscription}
              </Button>
            </Section>
          </Section>

          <Hr style={{ border: "none", margin: "18px 0 8px" }} />
          <Text className="muted" style={text({ fontSize: "12px", lineHeight: "18px", color: C.muted, textAlign: "center" })}>{t.whyEmail}</Text>
          <Text className="muted" style={text({ fontSize: "12px", lineHeight: "18px", color: C.muted, textAlign: "center" })}>{p.sellerFooter}</Text>
        </Container>
      </Body>
    </Html>
  );
}

/** The plain-text twin, written out (an automatic HTML→text conversion glues table cells together). */
export function receiptText(p: ReceiptEmailProps): string {
  const t = T[p.locale];
  const rows: [string, string][] = [
    [p.documentTitle, p.documentNumber],
    [t.issued, p.issued],
    [t.description, p.plan],
    [t.period, p.period],
    [t.paymentMethod, "PayPal"],
    [t.paypalTx, p.paypalTransactionId],
    ...(p.vat ? ([[p.vat.label, p.vat.value]] as [string, string][]) : []),
    [p.kind === "refund" ? t.totalRefund : t.total, p.amount],
  ];
  return [
    t.hello(p.customerName),
    "",
    p.intro || (p.kind === "refund" ? t.refundThanks : t.thanks),
    "",
    `${t.summary}:`,
    ...rows.map(([label, value]) => `  ${label}: ${value}`),
    ...(p.nextCharge ? ["", `${t.nextCharge}: ${p.nextCharge}`] : []),
    "",
    `${t.viewSubscription}: ${p.accountUrl}`,
    "",
    t.signoff,
    "",
    "--",
    t.whyEmail,
    p.sellerFooter,
  ].join("\n");
}

export async function renderReceiptEmail(p: ReceiptEmailProps): Promise<{ html: string; text: string }> {
  return { html: await render(<ReceiptEmail {...p} />), text: receiptText(p) };
}

/** Sample data for `email dev` previews and tests. */
export const SAMPLE: ReceiptEmailProps = {
  locale: "he",
  kind: "receipt",
  customerName: "דנה כהן",
  documentTitle: "קבלה",
  documentNumber: "000123",
  issued: "22/10/2026",
  plan: "מנוי שנתי – Kabala Pro",
  period: "22/10/2026 – 22/10/2027",
  amount: "₪279.00",
  nextCharge: "22/10/2027",
  paypalTransactionId: "8AB12345CD678901E",
  accountUrl: "https://kabala.app/account",
  sellerName: "Kabala",
  sellerFooter: "Kabala · עוסק פטור 123456789 · תל אביב",
};

export default function Preview_() {
  return <ReceiptEmail {...SAMPLE} />;
}
