import { api, date, linkToken, message, money, show } from "/common.js";

const $ = (id) => document.getElementById(id);
const token = linkToken();

const STATUS = {
  active: ["פעיל", "ok"],
  trial: ["תקופת ניסיון", "ok"],
  past_due: ["התשלום נכשל", "warn"],
  cancelled: ["בוטל", "warn"],
  expired: ["הסתיים", ""],
  completed: ["שולם", "ok"],
  refunded: ["הוחזר", "warn"],
  partially_refunded: ["הוחזר חלקית", "warn"],
  // receipts
  pending: ["בהכנה", ""],
  generated: ["בשליחה", ""],
  sent: ["נשלחה", "ok"],
  failed: ["תקלה בשליחה", "warn"],
};

const pad = (n) => String(n).padStart(6, "0");
const docLabel = (r) => `${r.kind === "refund" ? "החזר" : "קבלה"} ${r.documentNumber ?? pad(r.number)}`;
const smallButton = (text, onClick) => {
  const b = Object.assign(document.createElement("button"), { className: "btn btn-secondary", textContent: text, type: "button" });
  b.style.padding = "4px 10px";
  b.addEventListener("click", () => onClick(b));
  return b;
};

function badge(status) {
  const [text, kind] = STATUS[status] ?? [status, ""];
  return Object.assign(document.createElement("span"), { className: `badge ${kind}`, textContent: text });
}

function render(acc) {
  const sub = acc.subscription;
  $("tier").replaceChildren(Object.assign(document.createElement("span"), { className: `badge ${acc.tier === "pro" ? "pro" : ""}`, textContent: acc.tier === "pro" ? "Kabala Pro" : "Free" }));
  if (sub) $("tier").append(" ", badge(sub.status));

  if (sub?.status === "cancelled") {
    $("date-label").textContent = "Pro פעיל עד";
    $("date").textContent = date(sub.accessUntil);
  } else if (sub?.status === "past_due") {
    $("date-label").textContent = "Pro פעיל עד";
    $("date").textContent = date(sub.graceUntil);
    show($("notice"), `התשלום האחרון לא עבר. עדכן את אמצעי התשלום ב-PayPal עד ${date(sub.graceUntil)} כדי להמשיך ב-Pro.`, "warn");
  } else {
    $("date-label").textContent = "חיוב הבא";
    $("date").textContent = sub?.nextBillingDate ? date(sub.nextBillingDate) : "—";
  }
  $("price").textContent = sub ? `${money(sub.amount, sub.currency)} / ${sub.interval === "yearly" ? "שנה" : "חודש"}` : money(0, "ILS");
  $("cancel").classList.toggle("hidden", !sub?.canCancel);
  $("upgrade").classList.toggle("hidden", acc.tier === "pro");
  $("upgrade").href = `/checkout#t=${encodeURIComponent(token)}`;

  $("license-card").classList.toggle("hidden", !acc.licenseKey);
  $("license-title").classList.toggle("hidden", !acc.licenseKey);
  if (acc.licenseKey) {
    $("license-key").textContent = acc.licenseKey.key;
    $("license-meta").textContent = `נוצר ב-${date(acc.licenseKey.createdAt)} · הופעל ${acc.licenseKey.activations} פעמים`;
  }

  const receiptRows = acc.receipts.map((r) => {
    const tr = document.createElement("tr");
    for (const c of [date(r.issuedAt), docLabel(r), money(r.amount, r.currency)]) tr.append(Object.assign(document.createElement("td"), { textContent: c }));
    const st = document.createElement("td");
    st.append(badge(r.status));
    tr.append(st);
    const actions = document.createElement("td");
    actions.style.whiteSpace = "nowrap";
    if (r.ready) {
      actions.append(smallButton("PDF ⬇", () => download(r)), " ", smallButton("שלח שוב למייל", (b) => resend(r, b)));
    }
    tr.append(actions);
    return tr;
  });
  $("receipts").replaceChildren(...receiptRows);
  $("no-receipts").classList.toggle("hidden", receiptRows.length > 0);

  const rows = acc.payments.map((p) => {
    const tr = document.createElement("tr");
    for (const c of [date(p.paidAt), money(p.amount, p.currency)]) tr.append(Object.assign(document.createElement("td"), { textContent: c }));
    const st = document.createElement("td");
    st.append(badge(p.status));
    tr.append(st);
    return tr;
  });
  $("payments").replaceChildren(...rows);
  $("no-payments").classList.toggle("hidden", rows.length > 0);

  const s = acc.receiptSettings;
  $("receipt-email").textContent = s.email ?? "";
  $("cc-email").value = s.ccEmail ?? "";
  $("receipt-locale").value = s.locale;
}

async function download(r) {
  try {
    const res = await fetch(`/api/receipts/${r.id}/pdf`, { headers: { Authorization: `Bearer ${token}` } });
    if (!res.ok) throw { code: res.status === 401 ? "unauthorized" : "internal" };
    const url = URL.createObjectURL(await res.blob());
    Object.assign(document.createElement("a"), { href: url, download: `kabala-${r.kind}-${r.documentNumber ?? pad(r.number)}.pdf` }).click();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  } catch (e) {
    show($("error"), message(e));
  }
}

async function resend(r, button) {
  button.disabled = true;
  try {
    await api(`/api/receipts/${r.id}/resend`, { method: "POST", token });
    show($("notice"), `${docLabel(r)} נשלחה שוב למייל.`, "ok");
  } catch (e) {
    show($("error"), message(e));
  } finally {
    setTimeout(() => (button.disabled = false), 5_000);
  }
}

$("receipt-settings").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("save-settings").disabled = true;
  try {
    render(await api("/api/account/receipt-settings", { method: "POST", token, body: { ccEmail: $("cc-email").value.trim(), locale: $("receipt-locale").value } }));
    show($("notice"), "ההגדרות נשמרו. הקבלות הבאות יישלחו לפיהן.", "ok");
  } catch (e) {
    show($("error"), message(e.code === "invalid_request" ? { code: "invalid_email" } : e));
  } finally {
    $("save-settings").disabled = false;
  }
});

$("cancel").addEventListener("click", () => {
  $("cancel-text").textContent = "המנוי לא יתחדש. Kabala Pro ימשיך לעבוד עד סוף התקופה ששולמה, ואחר כך תעבור לתוכנית החינמית. הקבלות שסודרו נשארות שלך.";
  $("confirm-cancel").showModal();
});
$("keep").addEventListener("click", () => $("confirm-cancel").close());
$("do-cancel").addEventListener("click", async () => {
  $("do-cancel").disabled = true;
  try {
    render(await api("/api/account/cancel", { method: "POST", token }));
    $("confirm-cancel").close();
    show($("notice"), "המנוי בוטל. Pro ימשיך לעבוד עד סוף התקופה ששולמה.", "ok");
  } catch (e) {
    $("confirm-cancel").close();
    show($("error"), message(e));
  } finally {
    $("do-cancel").disabled = false;
  }
});

if (!token) {
  $("loading").classList.add("hidden");
  show($("error"), message({ code: "unauthorized" }));
} else {
  try {
    render(await api("/api/account", { token }));
    $("content").classList.remove("hidden");
  } catch (e) {
    show($("error"), message(e));
  } finally {
    $("loading").classList.add("hidden");
  }
}

$("copy-key").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("license-key").textContent);
    show($("notice"), "המפתח הועתק.", "ok");
  } catch {
    show($("notice"), "לא הצלחנו להעתיק. סמן את המפתח והעתק ידנית.", "warn");
  }
});

$("rotate-key").addEventListener("click", async () => {
  if (!confirm("להחליף את המפתח? המפתח הנוכחי יפסיק לעבוד מיד, וצריך יהיה להזין את החדש בתוכנה.")) return;
  try {
    render(await api("/api/account/license-key/rotate", { method: "POST", token }));
    show($("notice"), "נוצר מפתח חדש. הקודם כבר לא עובד.", "ok");
  } catch (e) {
    show($("error"), message(e));
  }
});
