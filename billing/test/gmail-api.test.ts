import { describe, expect, it } from "vitest";
import { GmailApiMailer } from "../src/email/gmail-api.js";

function fakeGoogle(opts: { revokeFirstSend?: boolean } = {}) {
  const calls: { url: string; body: string }[] = [];
  let sends = 0;
  const fetchImpl = (async (url: string, init?: RequestInit) => {
    const body = String(init?.body ?? "");
    calls.push({ url, body });
    if (url.includes("oauth2.googleapis.com/token")) {
      return new Response(JSON.stringify({ access_token: `at-${calls.length}`, expires_in: 3600 }), { status: 200 });
    }
    sends++;
    if (opts.revokeFirstSend && sends === 1) return new Response(JSON.stringify({ error: { status: "UNAUTHENTICATED" } }), { status: 401 });
    return new Response(JSON.stringify({ id: "msg-123" }), { status: 200 });
  }) as typeof fetch;
  return { calls, fetchImpl };
}

describe("Gmail API sender", () => {
  it("refreshes a token and sends raw MIME with the PDF attached", async () => {
    const g = fakeGoogle();
    const mailer = new GmailApiMailer({ clientId: "cid", clientSecret: "cs", refreshToken: "rt", from: "Kabala <support@example.com>", fetchImpl: g.fetchImpl });
    const result = await mailer.send({
      to: "dana@example.com",
      cc: ["cpa@example.com"],
      bcc: ["support@example.com"],
      subject: "הקבלה שלך מ-Kabala – אוקטובר 2026",
      text: "שלום",
      html: '<div dir="rtl">שלום</div>',
      attachments: [{ filename: "kabala-receipt-000001.pdf", content: Buffer.from("%PDF-1.7 test") }],
    });
    expect(result.id).toBe("msg-123");
    expect(g.calls[0]!.url).toContain("oauth2.googleapis.com/token");
    expect(g.calls[0]!.body).toContain("grant_type=refresh_token");
    const raw = Buffer.from(JSON.parse(g.calls[1]!.body).raw, "base64url").toString();
    expect(raw).toMatch(/^From: Kabala <support@example\.com>/m);
    expect(raw).toMatch(/^To: dana@example\.com/m);
    expect(raw).toMatch(/^Cc: cpa@example.com/m);
    expect(raw).toMatch(/^Bcc: support@example\.com/m); // Gmail delivers it and strips the header
    expect(raw).toContain("=?UTF-8?"); // Hebrew subject encoded per RFC 2047
    expect(raw).toContain('filename=kabala-receipt-000001.pdf');
    expect(raw).toContain(Buffer.from("%PDF-1.7 test").toString("base64"));
    // the access token is reused for the next message
    await mailer.send({ to: "x@example.com", subject: "s", text: "t", html: "h" });
    expect(g.calls.filter((c) => c.url.includes("/token"))).toHaveLength(1);
  });

  it("re-authorizes once when Google rejects the access token", async () => {
    const g = fakeGoogle({ revokeFirstSend: true });
    const mailer = new GmailApiMailer({ clientId: "cid", clientSecret: "cs", refreshToken: "rt", from: "a@gmail.com", fetchImpl: g.fetchImpl });
    expect((await mailer.send({ to: "x@example.com", subject: "s", text: "t", html: "h" })).id).toBe("msg-123");
    expect(g.calls.filter((c) => c.url.includes("/token"))).toHaveLength(2);
  });
});
