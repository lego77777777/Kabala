// Kabala admin panel. Plain JS, no libraries; every value from the server goes in with textContent (no innerHTML).
"use strict";

const ROLES = ["viewer", "admin", "owner"];
const ROLE_NAME = { viewer: "צופה", admin: "מנהל", owner: "בעלים" };
const STATUS_NAME = { active: "פעיל", trialing: "ניסיון", past_due: "בפיגור תשלום", canceled: "בוטל", comp: "Pro ידני" };
const ACTION_NAME = {
  login: "כניסה", login_failed: "קוד שגוי", logout: "יציאה", comp_on: "נתן Pro ידני", comp_off: "ביטל Pro ידני",
  export_csv: "ייצא CSV", admin_removed: "הסיר מנהל", unban: "שחרר IP", key_revoked: "ביטל מפתח רישיון",
  receipt_sent: "שלח קבלה במייל", receipt_retry: "ניסה שוב קבלה", receipt_downloaded: "הוריד קבלה",
  receipt_settings: "שינה הגדרות שליחה", receipt_test: "שלח מייל בדיקה",
  gmail_connect: "חיבר Gmail", gmail_disconnect: "ניתק Gmail", gmail_test: "שלח מייל בדיקה מ-Gmail", login_google: "כניסה עם Google",
  "admin_set:viewer": "הגדיר צופה", "admin_set:admin": "הגדיר מנהל", "admin_set:owner": "הגדיר בעלים",
};
const ROUTES = {
  dashboard: { title: "לוח בקרה", render: dashboard },
  users: { title: "משתמשים", render: (v) => usersView(v, false) },
  payments: { title: "תשלומים (Stripe)", render: (v) => usersView(v, true) },
  paypal: { title: "PayPal", render: paypalView, paypal: true },
  receipts: { title: "קבלות ללקוחות", render: receiptsView },
  sending: { title: "שליחה אוטומטית", render: sendingView },
  gmail: { title: "Gmail", render: gmailView },
  admins: { title: "מנהלים והרשאות", render: adminsView, min: "owner" },
  security: { title: "אבטחה", render: securityView },
  audit: { title: "יומן פעולות", render: auditView, min: "admin" },
};

let me = null;
let paypalConnected = false;
const $ = (id) => document.getElementById(id);

// ---- helpers -----------------------------------------------------------------------------------

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", credentials: "same-origin", headers: { "X-Kabala-Admin": "1" } };
  if (opts.body) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, init);
  if (r.status === 401 && !opts.allow401) {
    showLogin();
    throw new Error("signed out");
  }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) throw Object.assign(new Error(data?.detail || `HTTP ${r.status}`), { status: r.status });
  return data;
}

const can = (role) => me && ROLES.indexOf(me.role) >= ROLES.indexOf(role);
const date = (iso) => (iso ? new Date(iso).toLocaleDateString("he-IL") : "—");
const dateTime = (iso) => (iso ? new Date(iso).toLocaleString("he-IL", { dateStyle: "short", timeStyle: "short" }) : "—");
const epochDate = (s) => (s ? new Date(s * 1000).toLocaleDateString("he-IL") : "—");
const num = (n) => Number(n).toLocaleString("he-IL");

function ago(iso) {
  if (!iso) return "אף פעם";
  const min = Math.round((Date.now() - new Date(iso)) / 60000);
  if (min < 2) return "עכשיו";
  if (min < 60) return `לפני ${min} דק׳`;
  if (min < 1440) return `לפני ${Math.round(min / 60)} שע׳`;
  return `לפני ${Math.round(min / 1440)} ימים`;
}

const planPill = (plan) => h("span", { class: `pill ${plan}` }, plan === "pro" ? "Pro" : "Free");
const statusPill = (s) => (s ? h("span", { class: `pill ${s}` }, STATUS_NAME[s] || s) : h("span", { class: "muted" }, "—"));

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 3000);
}

function modal(...content) {
  const body = $("modal-body");
  body.replaceChildren(...content);
  $("modal").showModal();
}
const closeModal = () => $("modal").close();

function confirmBox(title, text, okText, danger) {
  return new Promise((resolve) => {
    const done = (v) => { closeModal(); resolve(v); };
    modal(h("h2", {}, title), h("p", { class: "muted" }, text),
      h("div", { class: "actions" },
        h("button", { class: "btn ghost", onclick: () => done(false) }, "ביטול"),
        h("button", { class: `btn ${danger ? "danger" : "primary"}`, onclick: () => done(true) }, okText)));
  });
}

function pager(page, pages, total, go) {
  return h("div", { class: "pager" }, `${num(total)} סה״כ · עמוד ${page} מתוך ${pages}`,
    h("button", { class: "btn sm ghost", disabled: page <= 1, onclick: () => go(page - 1) }, "הקודם"),
    h("button", { class: "btn sm ghost", disabled: page >= pages, onclick: () => go(page + 1) }, "הבא"));
}

const loading = () => h("div", { class: "card empty" }, "טוען…");
const failed = (e) => h("div", { class: "error" }, `שגיאה: ${e.message}`);

// ---- sign-in -----------------------------------------------------------------------------------

function showLogin() {
  me = null;
  $("app").hidden = true;
  $("login").hidden = false;
  $("login-email").hidden = false;
  $("login-code").hidden = true;
  $("login-gmail").hidden = true;
  $("email").focus();
  // every email goes through Gmail: until an account is connected no code can be sent, so offer to connect it
  api("/admin/api/mail-status", { allow401: true }).then((st) => {
    if (st.ready) return;
    $("login-email").hidden = true;
    $("login-hint").hidden = true;
    $("login-gmail").hidden = false;
    $("login-gmail-btn").hidden = !st.clientConfigured;
    if (!st.clientConfigured) $("login-gmail-text").textContent = "כל המיילים נשלחים דרך Gmail API. כדי להתחיל, הגדר בשרת את GMAIL_CLIENT_ID ו-GMAIL_CLIENT_SECRET (לקוח OAuth מסוג Web application) והפעל אותו מחדש.";
  }).catch(() => {});
}

$("login-gmail-btn").addEventListener("click", async () => {
  loginError("");
  try {
    location.href = (await api("/admin/api/gmail/setup", { method: "POST", allow401: true })).url;
  } catch (e) {
    loginError(e.status === 409 ? "Gmail כבר מחובר. רענן את הדף והיכנס עם קוד." : e.message);
  }
});

function loginError(text) {
  const e = $("login-error");
  e.textContent = text || "";
  e.hidden = !text;
}

$("login-email").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  loginError("");
  try {
    await api("/admin/api/login/start", { method: "POST", body: { email: $("email").value }, allow401: true });
    $("login-email").hidden = true;
    $("login-code").hidden = false;
    $("login-hint").textContent = "אם הכתובת מורשית, נשלח אליה קוד בן 6 ספרות. הוא תקף ל-10 דקות.";
    $("code").value = "";
    $("code").focus();
  } catch (e) {
    loginError(e.status === 429 ? "יותר מדי ניסיונות. נסה שוב בעוד כמה דקות." : e.message);
  }
});

$("login-code").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  loginError("");
  try {
    me = await api("/admin/api/login/verify", { method: "POST", body: { email: $("email").value, code: $("code").value }, allow401: true });
    start();
  } catch (e) {
    loginError(e.status === 401 ? "הקוד שגוי או שפג תוקפו." : e.status === 403 ? "הגישה נחסמה זמנית בגלל ניסיונות רבים." : e.message);
    $("code").select();
  }
});

$("login-back").addEventListener("click", () => {
  $("login-hint").textContent = "הקלד את כתובת המייל שלך. אם היא מורשית, נשלח אליה קוד כניסה.";
  showLogin();
});

$("logout").addEventListener("click", async () => {
  await api("/admin/api/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

// ---- shell -------------------------------------------------------------------------------------

function start() {
  $("login").hidden = true;
  $("app").hidden = false;
  $("me-email").textContent = me.email;
  const role = $("me-role");
  role.textContent = ROLE_NAME[me.role];
  role.className = `role ${me.role}`;
  for (const a of document.querySelectorAll(".sidebar nav a")) a.hidden = a.dataset.min ? !can(a.dataset.min) : a.hasAttribute("data-paypal");
  route();
  api("/admin/api/paypal/status").then((s) => {
    paypalConnected = s.connected;
    for (const a of document.querySelectorAll("[data-paypal]")) a.hidden = !s.connected;
    if (["#paypal", "#receipts", "#sending"].includes(location.hash)) route(); // the PayPal source appears
  }).catch(() => {});
}

function route() {
  if (!me) return;
  let name = location.hash.slice(1) || "dashboard";
  if (!ROUTES[name] || (ROUTES[name].min && !can(ROUTES[name].min)) || (ROUTES[name].paypal && !paypalConnected)) name = "dashboard";
  for (const a of document.querySelectorAll(".sidebar nav a")) a.classList.toggle("active", a.dataset.route === name);
  $("title").textContent = ROUTES[name].title;
  $("sidebar").classList.remove("open");
  if ($("modal").open) closeModal(); // a details window belongs to the page it was opened from
  const view = $("view");
  view.replaceChildren(loading());
  ROUTES[name].render(view).catch((e) => e.message !== "signed out" && view.replaceChildren(failed(e)));
}

window.addEventListener("hashchange", route);

// Keep the open page in sync: re-read it every 30 seconds while the tab is visible and nobody is typing,
// so a new user, payment or key shows up without reloading. Filters stay (viewState).
const LIVE = new Set(["dashboard", "users", "payments", "paypal", "receipts", "security"]);
setInterval(() => {
  const name = location.hash.slice(1) || "dashboard";
  const busy = $("modal").open || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);
  if (!me || document.hidden || busy || !LIVE.has(name) || !ROUTES[name]) return;
  if (ROUTES[name].paypal && !paypalConnected) return;
  const scroll = window.scrollY;
  ROUTES[name].render($("view")).then(() => window.scrollTo(0, scroll)).catch(() => {});
}, 30_000);
$("menu").addEventListener("click", () => $("sidebar").classList.toggle("open"));

function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}
try { applyTheme(localStorage.getItem("kabala-admin-theme")); } catch { /* storage blocked */ }
$("theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem("kabala-admin-theme", next); } catch { /* storage blocked */ }
});

// ---- views -------------------------------------------------------------------------------------

async function dashboard(view) {
  const s = await api("/admin/api/stats");
  const pct = s.users ? Math.round((s.pro / s.users) * 100) : 0;
  const stat = (label, value, hint, cls) => h("div", { class: `card stat ${cls || ""}` },
    h("div", { class: "label" }, label), h("div", { class: "value" }, num(value)), hint ? h("div", { class: "hint" }, hint) : null);
  const max = Math.max(1, ...s.signups.map((d) => d.count));
  const bars = s.signups.map((d) => {
    const bar = h("div", { class: "bar", title: `${date(d.day)}: ${d.count}` });
    bar.style.height = `${(d.count / max) * 100}%`; // CSSOM, allowed by the CSP (inline style attributes are not)
    return bar;
  });
  view.replaceChildren(
    h("div", { class: "cards" },
      stat("סה״כ משתמשים", s.users, `${num(s.free)} בתוכנית Free`),
      stat("משתמשי Pro", s.pro, `${pct}% המרה`, "accent"),
      stat("משלמים כרגע", s.paying, s.comp ? `+ ${num(s.comp)} Pro ידני` : "דרך Stripe"),
      stat("הצטרפו השבוע", s.new_week, "7 ימים אחרונים"),
      stat("פעילים השבוע", s.active_week, "נכנסו לתוכנה ב-7 ימים"),
      stat("בפיגור תשלום", s.past_due, "Stripe מנסה לחייב שוב", s.past_due ? "warn" : "")),
    h("div", { class: "card" },
      h("h2", {}, "הרשמות ב-30 הימים האחרונים"),
      h("div", { class: "chart" }, bars),
      h("div", { class: "chart-axis" }, h("span", {}, date(s.signups[0].day)), h("span", {}, "היום"))));
}

const viewState = {}; // filters and page per view, kept across the 30-second refresh

async function usersView(view, paidOnly) {
  const state = (viewState[paidOnly ? "payments" : "users"] ??= { q: "", plan: "", status: "", page: 1 });
  const tableBox = h("div");
  const load = async () => {
    const qs = new URLSearchParams({ q: state.q, plan: state.plan, status: state.status, page: state.page, paid: paidOnly });
    tableBox.replaceChildren(loading());
    const d = await api(`/admin/api/users?${qs}`);
    if (!d.users.length) {
      tableBox.replaceChildren(h("div", { class: "card empty" }, paidOnly ? "עוד אין לקוחות משלמים." : "לא נמצאו משתמשים."));
      return;
    }
    const rows = d.users.map((u) => h("tr", { class: "click", tabindex: "0", onclick: () => userDialog(u.id, load), onkeydown: (e) => e.key === "Enter" && userDialog(u.id, load) },
      h("td", { dir: "ltr", class: "ltr" }, u.email), h("td", {}, planPill(u.plan)), h("td", {}, statusPill(u.status)),
      h("td", {}, epochDate(u.period_end)), h("td", {}, date(u.created_at)), h("td", {}, ago(u.last_seen_at))));
    tableBox.replaceChildren(
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["מייל", "תוכנית", "סטטוס מנוי", "מתחדש ב-", "נרשם", "נראה לאחרונה"].map((t) => h("th", {}, t)))),
        h("tbody", {}, rows))),
      pager(d.page, d.pages, d.total, (p) => { state.page = p; load(); }));
  };
  const search = h("input", { type: "search", placeholder: can("admin") ? "חיפוש לפי מייל…" : "חיפוש זמין למנהל ומעלה", disabled: !can("admin"), dir: "ltr", value: state.q });
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { state.q = search.value; state.page = 1; load(); }, 250); });
  const select = (key, options) => {
    const s = h("select", { "aria-label": key }, options.map(([v, t]) => h("option", { value: v, selected: state[key] === v }, t)));
    s.addEventListener("change", () => { state[key] = s.value; state.page = 1; load(); });
    return s;
  };
  const toolbar = h("div", { class: "toolbar" }, search,
    select("plan", [["", "כל התוכניות"], ["pro", "Pro"], ["free", "Free"]]),
    select("status", [["", "כל הסטטוסים"], ["active", "פעיל"], ["trialing", "ניסיון"], ["past_due", "בפיגור תשלום"], ["canceled", "בוטל"], ["comp", "Pro ידני"], ["none", "ללא מנוי"]]),
    h("div", { class: "spacer" }),
    can("admin") ? h("a", { class: "btn ghost", href: `/admin/api/users.csv?paid=${paidOnly}`, download: "kabala-users.csv" }, "⬇ ייצוא CSV") : null);
  const intro = paidOnly
    ? h("p", { class: "note" }, "כל מי שיש לו לקוח ב-Stripe: משלמים כרגע, בניסיון, בפיגור, או מי שביטל אחרי ששילם. פרטי כרטיס האשראי נשמרים רק ב-Stripe.")
    : (!can("admin") ? h("p", { class: "note" }, "כצופה, המיילים מוצגים חלקית.") : null);
  view.replaceChildren(...[intro, toolbar, tableBox].filter(Boolean));
  await load();
}

async function userDialog(id, reload) {
  const u = await api(`/admin/api/users/${id}`);
  const stripeLink = typeof u.stripe_customer === "string"
    ? h("a", { href: `https://dashboard.stripe.com/customers/${encodeURIComponent(u.stripe_customer)}`, target: "_blank", rel: "noopener noreferrer" }, "פתח ב-Stripe ↗")
    : (u.stripe_customer ? "קיים" : "—");
  const manual = u.status === null || u.status === "comp" || u.status === "canceled";
  const actions = [];
  if (can("admin") && manual) {
    const on = u.status !== "comp";
    actions.push(h("button", {
      class: `btn ${on ? "primary" : "danger"}`,
      onclick: async () => {
        closeModal();
        if (!(await confirmBox(on ? "לתת Pro ידני?" : "לבטל Pro ידני?", on ? `${u.email} יקבל Pro ללא תשלום עד שתבטל.` : `${u.email} יחזור לתוכנית Free.`, on ? "תן Pro" : "בטל", !on))) return;
        try { await api(`/admin/api/users/${id}/comp`, { method: "POST", body: { on } }); toast("עודכן"); reload(); } catch (e) { toast(e.message); }
      },
    }, on ? "תן Pro ידני" : "בטל Pro ידני"));
  }
  modal(
    h("h2", { dir: "ltr" }, u.email),
    h("dl", {},
      h("dt", {}, "תוכנית"), h("dd", {}, planPill(u.plan)),
      h("dt", {}, "סטטוס מנוי"), h("dd", {}, statusPill(u.status)),
      h("dt", {}, "מתחדש ב-"), h("dd", {}, epochDate(u.period_end)),
      h("dt", {}, "נרשם"), h("dd", {}, dateTime(u.created_at)),
      h("dt", {}, "נראה לאחרונה"), h("dd", {}, u.last_seen_at ? dateTime(u.last_seen_at) : "אף פעם"),
      h("dt", {}, "לקוח Stripe"), h("dd", {}, stripeLink)),
    !manual && can("admin") ? h("p", { class: "note" }, "למשתמש יש מנוי ב-Stripe. שינויים בו נעשים ב-Stripe, והשרת מתעדכן אוטומטית.") : null,
    h("div", { class: "actions" }, h("button", { class: "btn ghost", onclick: closeModal }, "סגור"), ...actions));
}

async function adminsView(view) {
  const render = (list) => {
    const owners = list.filter((a) => a.role === "owner").length;
    const rows = list.map((a) => {
      const self = a.email === me.email;
      const role = h("select", { "aria-label": "תפקיד", disabled: self }, ROLES.map((r) => h("option", { value: r, selected: r === a.role }, ROLE_NAME[r])));
      role.addEventListener("change", async () => {
        try { render((await api("/admin/api/admins", { method: "POST", body: { email: a.email, role: role.value } })).admins); toast("התפקיד עודכן"); } catch (e) { toast(e.message); role.value = a.role; }
      });
      const remove = h("button", {
        class: "btn sm danger", disabled: self || (a.role === "owner" && owners <= 1),
        onclick: async () => {
          if (!(await confirmBox("להסיר מנהל?", `${a.email} לא יוכל יותר להיכנס לפאנל. הגישה שלו נחסמת מיד.`, "הסר", true))) return;
          try { render((await api("/admin/api/admins/remove", { method: "POST", body: { email: a.email } })).admins); toast("הוסר"); } catch (e) { toast(e.message); }
        },
      }, "הסר");
      return h("tr", {}, h("td", { dir: "ltr", class: "ltr" }, a.email, self ? " (את/ה)" : ""), h("td", {}, role),
        h("td", { dir: "ltr", class: "ltr" }, a.added_by || "—"), h("td", {}, date(a.created_at)), h("td", {}, remove));
    });
    const email = h("input", { type: "email", placeholder: "name@example.com", dir: "ltr", required: true });
    const role = h("select", { "aria-label": "תפקיד" }, ROLES.map((r) => h("option", { value: r, selected: r === "viewer" }, ROLE_NAME[r])));
    const add = h("form", {
      class: "toolbar",
      onsubmit: async (ev) => {
        ev.preventDefault();
        try { render((await api("/admin/api/admins", { method: "POST", body: { email: email.value, role: role.value } })).admins); toast("נוסף"); } catch (e) { toast(e.message); }
      },
    }, email, role, h("button", { class: "btn primary", type: "submit" }, "+ הוסף מנהל"));
    view.replaceChildren(
      h("div", { class: "card" }, h("h2", {}, "מי יכול להיכנס לפאנל"),
        h("dl", {},
          h("dt", {}, ROLE_NAME.viewer), h("dd", {}, "רואה סטטיסטיקות ורשימת משתמשים, עם מיילים חלקיים."),
          h("dt", {}, ROLE_NAME.admin), h("dd", {}, "כמו צופה, ובנוסף: מיילים מלאים, ייצוא, Pro ידני, שחרור IP חסום ויומן הפעולות."),
          h("dt", {}, ROLE_NAME.owner), h("dd", {}, "כמו מנהל, ובנוסף: הוספה והסרה של מנהלים ושינוי תפקידים."))),
      add,
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["מייל", "תפקיד", "נוסף על ידי", "מתאריך", ""].map((t) => h("th", {}, t)))),
        h("tbody", {}, rows))));
  };
  render((await api("/admin/api/admins")).admins);
}

async function securityView(view) {
  const render = (d) => {
    const fmt = (s) => (s >= 3600 ? `${Math.ceil(s / 3600)} שע׳` : `${Math.ceil(s / 60)} דק׳`);
    const rows = d.bans.map((b) => h("tr", {},
      h("td", { dir: "ltr", class: "ltr" }, b.ip), h("td", {}, fmt(b.seconds_left)), h("td", {}, num(b.bans)),
      h("td", {}, can("admin") ? h("button", {
        class: "btn sm ghost",
        onclick: async () => { try { render(await api("/admin/api/security/unban", { method: "POST", body: { ip: b.ip } })); toast("שוחרר"); } catch (e) { toast(e.message); } },
      }, "שחרר") : null)));
    view.replaceChildren(
      h("div", { class: "cards" },
        h("div", { class: "card stat" }, h("div", { class: "label" }, "IP חסומים כרגע"), h("div", { class: "value" }, num(d.bans.length))),
        h("div", { class: "card stat" }, h("div", { class: "label" }, "HTTPS"), h("div", { class: "value" }, d.https_required && d.proxy_hops ? "נאכף" : "לא נאכף"),
          h("div", { class: "hint" }, d.proxy_hops ? `מאחורי ${d.proxy_hops} proxy` : "ללא proxy (פיתוח)"))),
      h("p", { class: "note" }, "IP נחסם אחרי 10 \"עבירות\" ב-10 דקות: חריגה מהקצב, התחברות או קוד שגויים, webhook מזויף. החסימה מוכפלת בכל פעם, עד 24 שעות."),
      d.bans.length
        ? h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["IP", "נותר", "מספר חסימות", ""].map((t) => h("th", {}, t)))), h("tbody", {}, rows)))
        : h("div", { class: "card empty" }, "אין כרגע כתובות חסומות. 🎉"));
  };
  render(await api("/admin/api/security"));
}

async function auditView(view) {
  const load = async (page) => {
    const d = await api(`/admin/api/audit?page=${page}`);
    if (!d.entries.length) { view.replaceChildren(h("div", { class: "card empty" }, "היומן ריק.")); return; }
    view.replaceChildren(
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["מתי", "מי", "פעולה", "על מה", "IP"].map((t) => h("th", {}, t)))),
        h("tbody", {}, d.entries.map((e) => h("tr", {},
          h("td", {}, dateTime(e.at)), h("td", { dir: "ltr", class: "ltr" }, e.actor),
          h("td", {}, e.action === "login_failed" ? h("span", { class: "pill canceled" }, ACTION_NAME[e.action]) : (ACTION_NAME[e.action] || e.action)),
          h("td", { dir: "ltr", class: "ltr" }, e.target || "—"), h("td", { dir: "ltr", class: "ltr" }, e.ip || "—")))))),
      pager(d.page, d.pages, d.total, load));
  };
  await load(1);
}

const PAYPAL_STATUS = { pending: "ממתין לאישור", trial: "ניסיון", active: "פעיל", past_due: "בפיגור תשלום", cancelled: "בוטל", expired: "פג תוקף" };
const PAYPAL_PILL = { trial: "trialing", cancelled: "canceled", expired: "canceled" };
const money = (minor, cur) => (minor / 100).toLocaleString("he-IL", { style: "currency", currency: cur });

async function paypalView(view) {
  const s = await api("/admin/api/paypal/stats");
  const subsBox = h("div"), paysBox = h("div");
  const stat = (label, value, hint, cls) => h("div", { class: `card stat ${cls || ""}` },
    h("div", { class: "label" }, label), h("div", { class: "value" }, value), hint ? h("div", { class: "hint" }, hint) : null);
  const revenue = s.revenueThisMonth.length ? s.revenueThisMonth.map((r) => money(r.netMinor, r.currency)).join(" + ") : money(0, "ILS");
  const payments = s.revenueThisMonth.reduce((n, r) => n + r.payments, 0);
  const st = s.subscriptions;
  const pill = (status) => h("span", { class: `pill ${PAYPAL_PILL[status] || status}` }, PAYPAL_STATUS[status] || status);

  const state = (viewState.paypal ??= { q: "", status: "", page: 1, keysQ: "", keysStatus: "", keysPage: 1 });
  const keysBox = h("div");
  const loadSubs = async () => {
    const qs = new URLSearchParams({ q: state.q, status: state.status, page: state.page });
    const d = await api(`/admin/api/paypal/subscribers?${qs}`);
    subsBox.replaceChildren(d.subscribers.length
      ? h("div", {}, h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["מייל", "סטטוס", "מסלול", "מחיר", "בתוקף עד", "נרשם"].map((t) => h("th", {}, t)))),
          h("tbody", {}, d.subscribers.map((x) => h("tr", {},
            h("td", { dir: "ltr", class: "ltr" }, x.email), h("td", {}, pill(x.status)),
            h("td", {}, x.interval === "yearly" ? "שנתי" : "חודשי"), h("td", {}, money(x.amountMinor, x.currency)),
            h("td", {}, date(x.currentPeriodEnd)), h("td", {}, date(x.createdAt))))))),
        pager(d.page, d.pages, d.total, (p) => { state.page = p; loadSubs(); }))
      : h("div", { class: "card empty" }, "אין מנויים."));
  };
  const loadPays = async (page) => {
    const d = await api(`/admin/api/paypal/payments?page=${page}`);
    paysBox.replaceChildren(d.payments.length
      ? h("div", {}, h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["מייל", "סכום", "הוחזר", "סטטוס", "תאריך"].map((t) => h("th", {}, t)))),
          h("tbody", {}, d.payments.map((p) => h("tr", {},
            h("td", { dir: "ltr", class: "ltr" }, p.email), h("td", {}, money(p.amountMinor, p.currency)),
            h("td", {}, p.refundedMinor ? money(p.refundedMinor, p.currency) : "—"),
            h("td", {}, h("span", { class: `pill ${p.status === "completed" ? "active" : "canceled"}` }, p.status === "completed" ? "שולם" : p.status === "refunded" ? "הוחזר" : "הוחזר חלקית")),
            h("td", {}, dateTime(p.paidAt))))))),
        pager(d.page, d.pages, d.total, loadPays))
      : h("div", { class: "card empty" }, "עוד אין תשלומים."));
  };

  const loadKeys = async () => {
    const qs = new URLSearchParams({ q: state.keysQ, status: state.keysStatus, page: state.keysPage });
    let d;
    try { d = await api(`/admin/api/paypal/keys?${qs}`); } catch (e) { keysBox.replaceChildren(failed(e)); return; }
    const revoke = (k) => h("button", {
      class: "btn sm danger",
      onclick: async () => {
        if (!(await confirmBox("לבטל את מפתח הרישיון?", `המפתח ${k.hint} של ${k.email} יפסיק לעבוד מיד. המנוי עצמו לא מתבטל, והמשתמש יכול ליצור מפתח חדש בדף "המנוי שלי".`, "בטל מפתח", true))) return;
        try { await api(`/admin/api/paypal/keys/${k.id}/revoke`, { method: "POST" }); toast("המפתח בוטל"); loadKeys(); } catch (e) { toast(e.message); }
      },
    }, "בטל");
    keysBox.replaceChildren(d.keys.length
      ? h("div", {}, h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["מפתח", "מייל", "סטטוס", "הפעלות", "שימוש אחרון", "נוצר", ""].map((t) => h("th", {}, t)))),
          h("tbody", {}, d.keys.map((k) => h("tr", {},
            h("td", { dir: "ltr", class: "ltr" }, h("code", {}, k.hint)), h("td", { dir: "ltr", class: "ltr" }, k.email),
            h("td", {}, k.status === "active" ? h("span", { class: "pill active" }, "פעיל") : h("span", { class: "pill canceled", title: k.revokedBy ? `בוטל על ידי ${k.revokedBy}` : "" }, "בוטל")),
            h("td", {}, num(k.activations)), h("td", {}, k.lastUsedAt ? ago(k.lastUsedAt) : "אף פעם"), h("td", {}, date(k.createdAt)),
            h("td", {}, k.status === "active" && can("admin") ? revoke(k) : null)))))),
        pager(d.page, d.pages, d.total, (p) => { state.keysPage = p; loadKeys(); }))
      : h("div", { class: "card empty" }, "עוד לא הונפקו מפתחות. מפתח נוצר אוטומטית עם התשלום הראשון של כל מנוי."));
  };

  const search = h("input", { type: "search", placeholder: can("admin") ? "חיפוש לפי מייל…" : "חיפוש זמין למנהל ומעלה", disabled: !can("admin"), dir: "ltr", value: state.q });
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { state.q = search.value; state.page = 1; loadSubs(); }, 250); });
  const select = h("select", { "aria-label": "סטטוס" }, [["", "כל הסטטוסים"], ...Object.entries(PAYPAL_STATUS)].map(([v, t]) => h("option", { value: v, selected: state.status === v }, t)));
  const keySearch = h("input", { type: "search", placeholder: can("admin") ? "חיפוש מפתח לפי מייל…" : "חיפוש זמין למנהל ומעלה", disabled: !can("admin"), dir: "ltr", value: state.keysQ });
  let keyTimer;
  keySearch.addEventListener("input", () => { clearTimeout(keyTimer); keyTimer = setTimeout(() => { state.keysQ = keySearch.value; state.keysPage = 1; loadKeys(); }, 250); });
  const keyStatus = h("select", { "aria-label": "סטטוס מפתח" }, [["", "כל המפתחות"], ["active", "פעילים"], ["revoked", "בוטלו"]].map(([v, t]) => h("option", { value: v, selected: state.keysStatus === v }, t)));
  keyStatus.addEventListener("change", () => { state.keysStatus = keyStatus.value; state.keysPage = 1; loadKeys(); });
  select.addEventListener("change", () => { state.status = select.value; state.page = 1; loadSubs(); });

  view.replaceChildren(
    h("p", { class: "note" }, `שרת המנויים של PayPal · מצב ${s.mode === "live" ? "אמיתי (Live)" : "בדיקות (Sandbox)"}. הנתונים לקריאה בלבד; שינויים במנוי נעשים ב-PayPal.`),
    h("div", { class: "cards" },
      stat("משתמשים", num(s.users)),
      stat("עם Pro פעיל", num(s.pro), null, "accent"),
      stat("מנויים פעילים", num((st.active || 0) + (st.trial || 0)), st.trial ? `מתוכם ${num(st.trial)} בניסיון` : null),
      stat("בפיגור תשלום", num(st.past_due || 0), "בתקופת חסד", st.past_due ? "warn" : ""),
      stat("הכנסות החודש", revenue, `${num(payments)} תשלומים, אחרי החזרים`),
      stat("בוטלו / פגו", num((st.cancelled || 0) + (st.expired || 0)))),
    h("h2", {}, "מנויים"), h("div", { class: "toolbar" }, search, select), subsBox,
    h("h2", {}, "תשלומים"), paysBox,
    h("h2", {}, "מפתחות רישיון"),
    h("p", { class: "note" }, "כל קונה מקבל מפתח במייל עם התשלום הראשון. המפתח עצמו לא נשמר בשרת בטקסט גלוי (רק hash ועותק מוצפן שרק בעליו רואה), ולכן כאן מוצגת רק הקבוצה האחרונה שלו."),
    h("div", { class: "toolbar" }, keySearch, keyStatus), keysBox);
  await Promise.all([loadSubs(), loadPays(1), loadKeys()]);
}

const RECEIPT_STATUS = { pending: ["בהכנה", ""], generated: ["נוצרה, נשלחת", "trialing"], sent: ["נשלחה", "active"], failed: ["נכשלה", "canceled"] };
const EMAIL_STATUS = { queued: ["בתור", ""], sent: ["נשלח", "active"], failed: ["נכשל", "canceled"] };
const TRIGGER = { auto: "אוטומטי אחרי התשלום", resend: "הלקוח ביקש שוב", admin: "נשלח מהפאנל", retry: "ניסיון חוזר", test: "מייל בדיקה" };
const statusPillOf = (map, s) => h("span", { class: `pill ${(map[s] || [s, ""])[1]}` }, (map[s] || [s])[0]);

/** Stripe (this server) or PayPal (the billing server): the same pages and actions for both. */
function receiptSource() {
  if (viewState.source === "paypal" && !paypalConnected) viewState.source = "stripe";
  return (viewState.source ??= "stripe");
}
const sourceBase = () => `/admin/api/${receiptSource()}`;
const SOURCE_NOTE = { stripe: "תשלומים בכרטיס אשראי דרך Stripe", paypal: "תשלומים דרך PayPal" };
function sourceSwitch() {
  const options = [["stripe", "Stripe"], ...(paypalConnected ? [["paypal", "PayPal"]] : [])];
  if (options.length < 2) return null;
  return h("div", { class: "toolbar" }, h("span", { class: "muted" }, "מקור:"),
    options.map(([v, label]) => h("button", { class: `btn sm ${receiptSource() === v ? "primary" : "ghost"}`, "aria-pressed": String(receiptSource() === v),
      onclick: () => { viewState.source = v; viewState.receipts = undefined; route(); } }, label)));
}

async function receiptsView(view) {
  const base = sourceBase();
  const state = (viewState.receipts ??= { q: "", status: "", page: 1 });
  const box = h("div");
  const load = async () => {
    const qs = new URLSearchParams({ q: state.q, status: state.status, page: state.page });
    const d = await api(`${base}/receipts?${qs}`);
    if (!d.receipts.length) { box.replaceChildren(h("div", { class: "card empty" }, "אין קבלות. קבלה נוצרת ונשלחת אוטומטית אחרי כל תשלום.")); return; }
    box.replaceChildren(
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["מספר", "לקוח", "סכום", "סטטוס", "הונפקה", "מייל אחרון", ""].map((t) => h("th", {}, t)))),
        h("tbody", {}, d.receipts.map((r) => h("tr", { class: "click", tabindex: "0", onclick: () => receiptDialog(r.id, load, base), onkeydown: (e) => e.key === "Enter" && receiptDialog(r.id, load, base) },
          h("td", {}, `${r.kind === "refund" ? "החזר" : "קבלה"} ${r.number}`), h("td", { dir: "ltr", class: "ltr" }, r.email),
          h("td", {}, money(r.amountMinor, r.currency)), h("td", {}, statusPillOf(RECEIPT_STATUS, r.status)), h("td", {}, dateTime(r.issuedAt)),
          h("td", {}, r.lastEmail ? h("span", {}, statusPillOf(EMAIL_STATUS, r.lastEmail.status), " ", h("span", { class: "muted" }, ago(r.lastEmail.at))) : "—"),
          h("td", {}, h("span", { class: "muted" }, "פרטים ›"))))))),
      pager(d.page, d.pages, d.total, (p) => { state.page = p; load(); }));
  };
  const search = h("input", { type: "search", placeholder: can("admin") ? "חיפוש לפי מייל הלקוח…" : "חיפוש זמין למנהל ומעלה", disabled: !can("admin"), dir: "ltr", value: state.q });
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { state.q = search.value; state.page = 1; load(); }, 250); });
  const select = h("select", { "aria-label": "סטטוס" }, [["", "כל הקבלות"], ...Object.entries(RECEIPT_STATUS).map(([k, v]) => [k, v[0]])].map(([v, t]) => h("option", { value: v, selected: state.status === v }, t)));
  select.addEventListener("change", () => { state.status = select.value; state.page = 1; load(); });
  view.replaceChildren(
    ...[sourceSwitch()].filter(Boolean),
    h("p", { class: "note" }, `${SOURCE_NOTE[receiptSource()]}: אחרי כל תשלום נוצרת קבלה (PDF) ונשלחת ללקוח במייל אוטומטית, עם ניסיונות חוזרים. כאן רואים מה נשלח, ואפשר לשלוח שוב ללקוח או לכתובת אחרת. הדף מתעדכן לבד.`),
    h("div", { class: "toolbar" }, search, select), box);
  await load();
}

async function receiptDialog(id, reload, base) {
  const r = await api(`${base}/receipts/${id}`);
  const actions = [];
  if (can("admin")) {
    if (r.status === "failed") {
      actions.push(h("button", { class: "btn primary", onclick: async () => {
        try { await api(`${base}/receipts/${id}/retry`, { method: "POST", body: {} }); closeModal(); toast("נשלח לניסיון נוסף"); reload(); } catch (e) { toast(e.message); }
      } }, "נסה שוב"));
    } else if (r.status !== "pending") {
      actions.push(h("button", { class: "btn primary", onclick: async () => {
        try { await api(`${base}/receipts/${id}/send`, { method: "POST", body: {} }); closeModal(); toast("הקבלה נשלחת ללקוח"); reload(); } catch (e) { toast(e.message); }
      } }, "שלח שוב ללקוח"));
      const other = h("input", { type: "email", dir: "ltr", placeholder: "accountant@example.com", "aria-label": "כתובת אחרת" });
      actions.push(h("form", { class: "toolbar", onsubmit: async (ev) => {
        ev.preventDefault();
        try { await api(`${base}/receipts/${id}/send`, { method: "POST", body: { to: other.value } }); closeModal(); toast(`הקבלה נשלחת אל ${other.value}`); reload(); }
        catch (e) { toast(e.status === 409 ? "הכתובת לא תקינה" : e.message); }
      } }, other, h("button", { class: "btn ghost", type: "submit" }, "שלח לכתובת אחרת")));
    }
    if (r.status !== "pending") actions.push(h("a", { class: "btn ghost", href: `${base}/receipts/${id}/pdf`, download: "" }, "⬇ PDF"));
  }
  modal(
    h("h2", {}, `${r.kind === "refund" ? "החזר" : "קבלה"} ${r.number}`),
    h("dl", {},
      h("dt", {}, "לקוח"), h("dd", { dir: "ltr" }, r.email),
      h("dt", {}, "סכום"), h("dd", {}, money(r.amountMinor, r.currency)),
      h("dt", {}, "סטטוס"), h("dd", {}, statusPillOf(RECEIPT_STATUS, r.status)),
      h("dt", {}, "הונפקה"), h("dd", {}, dateTime(r.issuedAt)),
      r.lastError ? [h("dt", {}, "שגיאה"), h("dd", { class: "muted", dir: "ltr" }, r.lastError)] : null),
    h("h2", {}, "מיילים שנשלחו"),
    r.emails.length
      ? h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["מתי", "אל", "למה", "סטטוס"].map((t) => h("th", {}, t)))),
          h("tbody", {}, r.emails.map((e) => h("tr", {},
            h("td", {}, dateTime(e.updatedAt)), h("td", { dir: "ltr", class: "ltr" }, e.to), h("td", {}, TRIGGER[e.trigger] || e.trigger),
            h("td", { title: e.error || "" }, statusPillOf(EMAIL_STATUS, e.status)))))))
      : h("p", { class: "muted" }, "עוד לא נשלח מייל."),
    h("div", { class: "actions" }, h("button", { class: "btn ghost", onclick: closeModal }, "סגור"), ...actions));
}

async function sendingView(view) {
  const base = sourceBase();
  const s = await api(`${base}/receipt-settings`);
  const editable = can("admin");
  const draft = {}; // only what was changed is sent
  let lang = "he";
  const set = (key, value) => { draft[key] = value; dirty(); };

  const toggle = (key, title, hint) => {
    const box = h("input", { type: "checkbox", checked: s[key], disabled: !editable });
    box.addEventListener("change", () => set(key, box.checked));
    return h("label", { class: "switch" }, box, h("div", {}, h("div", { class: "switch-title" }, title), h("div", { class: "muted" }, hint)));
  };

  const copy = h("input", { type: "email", dir: "ltr", value: s.copyTo || "", placeholder: "archive@your-business.co.il", disabled: !editable });
  copy.addEventListener("input", () => set("copyTo", copy.value));

  const subject = h("input", { type: "text", maxlength: String(s.limits.subject), disabled: !editable });
  const intro = h("textarea", { maxlength: String(s.limits.intro), disabled: !editable });
  const key = (base) => `${base}${lang === "he" ? "He" : "En"}`;
  const value = (base) => (key(base) in draft ? draft[key(base)] : s[key(base)]) || "";
  const fill = () => {
    subject.value = value("subject");
    intro.value = value("intro");
    subject.dir = intro.dir = lang === "he" ? "rtl" : "ltr";
    subject.placeholder = lang === "he" ? "ברירת מחדל: הקבלה שלך מ-Kabala – {{month}}" : "Default: Your Kabala receipt – {{month}}";
    intro.placeholder = lang === "he" ? "ברירת מחדל: תודה שבחרת ב-Kabala Pro! מצורפת הקבלה על התשלום." : "Default: Thank you for choosing Kabala Pro! Your receipt is attached.";
  };
  subject.addEventListener("input", () => set(key("subject"), subject.value));
  intro.addEventListener("input", () => set(key("intro"), intro.value));
  let lastFocus = intro;
  subject.addEventListener("focus", () => (lastFocus = subject));
  intro.addEventListener("focus", () => (lastFocus = intro));
  const chips = h("div", { class: "chips" }, Object.entries(s.variables).map(([name, label]) => h("button", {
    class: "chip", type: "button", title: label, disabled: !editable,
    onclick: () => {
      const el = lastFocus, tag = `{{${name}}}`, at = el.selectionStart ?? el.value.length;
      el.value = el.value.slice(0, at) + tag + el.value.slice(el.selectionEnd ?? at);
      el.focus(); el.setSelectionRange(at + tag.length, at + tag.length);
      el.dispatchEvent(new Event("input"));
    },
  }, `${label} {{${name}}}`)));
  const langSelect = h("select", { "aria-label": "שפת המייל" }, h("option", { value: "he" }, "עברית"), h("option", { value: "en" }, "English"));
  langSelect.addEventListener("change", () => { lang = langSelect.value; fill(); refreshPreview(); });

  const pv = { subject: h("div", { class: "subject" }), intro: h("div", { class: "intro" }), error: h("div", { class: "field-error", role: "alert" }) };
  let pvTimer;
  const refreshPreview = () => {
    clearTimeout(pvTimer);
    pvTimer = setTimeout(async () => {
      try {
        const p = await api(`${base}/receipt-settings/preview`, { method: "POST", body: { ...draft, locale: lang } });
        pv.subject.textContent = p.subject; pv.intro.textContent = p.intro; pv.error.textContent = "";
      } catch (e) { pv.error.textContent = e.message; }
    }, 300);
  };

  const save = h("button", { class: "btn primary", disabled: true, onclick: async () => {
    try {
      Object.assign(s, await api(`${base}/receipt-settings`, { method: "POST", body: draft }));
      for (const k of Object.keys(draft)) delete draft[k];
      dirty(); toast("ההגדרות נשמרו. הן חלות מהקבלה הבאה."); meta.textContent = updatedLine();
    } catch (e) { toast(e.message); pv.error.textContent = e.message; }
  } }, "שמור");
  const test = h("button", { class: "btn ghost", disabled: !editable, onclick: async () => {
    try { const r = await api(`${base}/receipt-settings/test`, { method: "POST", body: draft }); toast(`מייל בדיקה נשלח אל ${r.to}`); }
    catch (e) { toast(e.status === 409 && e.message === "no_receipts" ? "אין עדיין קבלה לשלוח כבדיקה" : e.message); }
  } }, "שלח לי מייל בדיקה");
  const reset = h("button", { class: "btn link", disabled: !editable, onclick: () => { set(key("subject"), ""); set(key("intro"), ""); fill(); } }, "חזור לנוסח המובנה");
  function dirty() { save.disabled = !editable || !Object.keys(draft).length; refreshPreview(); }
  const updatedLine = () => (s.updatedBy ? `עודכן לאחרונה ${dateTime(s.updatedAt)} על ידי ${s.updatedBy}` : "עוד לא שונו הגדרות: פועל לפי ברירת המחדל.");
  const meta = h("p", { class: "muted" }, updatedLine());

  fill();
  view.replaceChildren(
    ...[sourceSwitch()].filter(Boolean),
    h("p", { class: "note" }, `כאן קובעים איך קבלות נשלחות ללקוחות: ${SOURCE_NOTE[receiptSource()]}. לכל מקור הגדרות משלו. שינוי חל מהקבלה הבאה; קבלות שכבר נשלחו לא משתנות.`),
    h("div", { class: "settings-grid" },
      h("div", { class: "card", }, h("h2", {}, "שליחה"),
        h("div", { class: "field" },
          toggle("autoSend", "שליחה אוטומטית ללקוח", "הקבלה נשלחת במייל מיד אחרי התשלום. כבוי: הקבלה נוצרת ומחכה לשליחה ידנית בדף \"קבלות ללקוחות\"."),
          toggle("autoRetry", "ניסיון חוזר אוטומטי", `קבלה שנכשלה (למשל שרת המייל לא זמין) תישלח שוב עד ${s.retry.rounds} פעמים, שעה בין ניסיון לניסיון.`))),
      h("div", { class: "card" }, h("h2", {}, "עותק לכל קבלה"),
        h("div", { class: "field" }, h("label", {}, "כתובת לעותק נסתר (Bcc)"), copy,
          h("div", { class: "muted" }, "למשל רואה החשבון או תיבת הארכיון של העסק. הלקוח לא רואה את הכתובת הזו. ריק: ללא עותק.")))),
    h("div", { class: "card" },
      h("div", { class: "toolbar" }, h("h2", {}, "נוסח המייל"), h("div", { class: "spacer" }), langSelect),
      h("div", { class: "settings-grid" },
        h("div", { class: "field" },
          h("label", {}, "נושא"), subject,
          h("label", {}, "שורת פתיחה"), intro,
          h("div", { class: "muted" }, "לחיצה על משתנה מכניסה אותו לשדה. רק המשתנים האלה מותרים:"), chips, reset),
        h("div", { class: "field" }, h("label", {}, "תצוגה מקדימה (לפי הקבלה האחרונה)"), h("div", { class: "preview" }, pv.subject, pv.intro), pv.error,
          h("div", { class: "muted" }, "פרטי הקבלה, ה-PDF והקישור להורדה מתווספים למייל אוטומטית.")))),
    h("div", { class: "toolbar" }, save, test, h("div", { class: "spacer" }), meta),
    ...(editable ? [] : [h("p", { class: "note" }, "כצופה אפשר לראות את ההגדרות, אבל לא לשנות אותן.")]));
  refreshPreview();
}

// ---- Gmail: the account every email is sent from ------------------------------------------------

async function gmailView(view) {
  const d = await api("/admin/api/gmail");
  const busy = (btn, fn) => async () => { btn.disabled = true; try { await fn(); } catch (e) { toast(e.message); } finally { btn.disabled = false; } };
  const connect = h("button", { class: "btn primary" }, d.connected ? "חבר חשבון אחר" : "חבר Gmail");
  connect.onclick = busy(connect, async () => { location.href = (await api("/admin/api/gmail/connect", { method: "POST" })).url; });
  const disconnect = h("button", { class: "btn ghost" }, "נתק");
  disconnect.onclick = busy(disconnect, async () => {
    if (!confirm(`לנתק את ${d.email}? עד שיחובר חשבון אחר לא יישלחו מיילים: לא קודי כניסה ולא קבלות.`)) return;
    await api("/admin/api/gmail/disconnect", { method: "POST" });
    toast("Gmail נותק");
    route();
  });
  const test = h("button", { class: "btn" }, "שלח לי מייל בדיקה");
  test.onclick = busy(test, async () => { const r = await api("/admin/api/gmail/test", { method: "POST" }); toast(`נשלח אל ${r.to}`); route(); });
  const copy = h("button", { class: "btn sm ghost", onclick: async () => { await navigator.clipboard?.writeText(d.redirectUri); toast("הועתק"); } }, "העתק");
  const row = (label, value) => h("div", { class: "kv" }, h("span", { class: "muted" }, label), value);
  const source = d.source === "env" ? "משתנה סביבה (GMAIL_REFRESH_TOKEN)" : "חובר מהפאנל";
  view.replaceChildren(
    h("p", { class: "note" }, "כל המיילים של השרת נשלחים דרך Gmail API מהחשבון שמחובר כאן: קודי כניסה, קבלות ללקוחות עם ה-PDF, ומיילי התראה. ההרשאה היא לשליחה בלבד: השרת לא יכול לקרוא את תיבת הדואר."),
    h("div", { class: "cards" },
      h("div", { class: "card" },
        h("h3", {}, "החשבון השולח"),
        d.connected
          ? h("div", {}, h("div", { class: "value ltr", dir: "ltr" }, d.email || "(הכתובת מ-MAIL_FROM)"), h("span", { class: "pill active" }, "מחובר"))
          : h("span", { class: "pill canceled" }, "לא מחובר: לא נשלחים מיילים"),
        d.connected ? row("מקור", source) : null,
        d.connectedAt ? row("חובר", `${dateTime(d.connectedAt)} · ${d.connectedBy}`) : null,
        d.lastSentAt ? row("שליחה אחרונה", dateTime(new Date(d.lastSentAt * 1000).toISOString())) : null,
        d.lastError ? h("div", { class: "error" }, `שגיאה אחרונה: ${d.lastError}`) : null),
      h("div", { class: "card" },
        h("h3", {}, "הגדרה ב-Google Cloud"),
        d.clientConfigured
          ? h("p", { class: "muted" }, "לקוח OAuth מוגדר בשרת. בלקוח (מסוג Web application) צריך להופיע ה-Redirect URI הזה:")
          : h("div", { class: "error" }, "חסרים בשרת GMAIL_CLIENT_ID ו-GMAIL_CLIENT_SECRET. צור לקוח OAuth מסוג Web application, הוסף לו את ה-Redirect URI הזה, הגדר את שני המשתנים והפעל את השרת מחדש."),
        h("div", { class: "toolbar" }, h("code", { dir: "ltr", class: "ltr" }, d.redirectUri), copy),
        h("p", { class: "muted" }, "וגם: להפעיל את Gmail API בפרויקט, ולפרסם את מסך ההסכמה (In production), אחרת Google מבטל את החיבור אחרי 7 ימים."))),
    can("owner")
      ? h("div", { class: "toolbar" }, d.clientConfigured ? connect : null, d.connected && d.source === "panel" ? disconnect : null,
        d.connected && can("admin") ? test : null)
      : h("div", { class: "toolbar" }, d.connected && can("admin") ? test : null,
        h("p", { class: "note" }, "רק בעלים יכולים לחבר או לנתק את Gmail.")));
}

// ---- boot --------------------------------------------------------------------------------------

api("/admin/api/me", { allow401: true }).then((m) => { me = m; start(); }).catch(showLogin);
