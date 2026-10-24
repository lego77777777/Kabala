// Checkout page. The browser never decides what is charged: the PayPal plan comes from
// /api/checkout/start, and Pro is granted only after the server verifies the subscription with PayPal.
import { ApiError, api, linkToken, message, money, show } from "/common.js";

const $ = (id) => document.getElementById(id);
const token = linkToken();
let pricing;
let config;
let interval = "monthly";
let buttons = null;

function fail(e) {
  $("loading").classList.add("hidden");
  show($("error"), typeof e === "string" ? e : message(e));
}

function renderSummary() {
  const q = pricing.tiers.pro.prices[interval];
  $("amount").textContent = money(q.amount, q.currency);
  $("per").textContent = interval === "monthly" ? "· חודשי" : "· שנתי";
  $("note").textContent =
    interval === "yearly"
      ? `${money(q.monthlyEquivalentMinor / 100, q.currency)} לחודש · חוסכים ${money(q.savingsMinor / 100, q.currency)}`
      : `מתחדש כל חודש. אפשר לעבור לשנתי ולחסוך ${pricing.tiers.pro.prices.yearly.savingsPercent}%`;
  $("save-badge").textContent = `חיסכון ${pricing.tiers.pro.prices.yearly.savingsPercent}%`;
  document.querySelectorAll(".toggle button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.interval === interval)));
}

async function confirm(subscriptionId) {
  // PayPal may need a moment to activate; retry "not_active_yet" briefly.
  for (let attempt = 0; attempt < 8; attempt++) {
    try {
      return await api("/api/checkout/confirm", { method: "POST", token, body: { subscriptionId } });
    } catch (e) {
      if (!(e instanceof ApiError) || e.code !== "not_active_yet") throw e;
      show($("error"), message(e), "warn");
      await new Promise((r) => setTimeout(r, 2500));
    }
  }
  throw new ApiError("not_active_yet", 409);
}

async function renderButtons() {
  if (buttons) await buttons.close();
  const paypal = window.__paypal;
  buttons = paypal.Buttons({
    style: { layout: "vertical", shape: "rect", label: "subscribe", height: 48 },
    createSubscription: async (_data, actions) => {
      $("error").className = "hidden";
      const { planId, customId } = await api("/api/checkout/start", { method: "POST", token, body: { interval, currency: pricing.currency } });
      return actions.subscription.create({
        plan_id: planId,
        custom_id: customId,
        application_context: { brand_name: "Kabala", shipping_preference: "NO_SHIPPING", user_action: "SUBSCRIBE_NOW", locale: "he-IL" },
      });
    },
    onApprove: async (data) => {
      $("pay").classList.add("hidden");
      $("loading").classList.remove("hidden");
      try {
        const result = await confirm(data.subscriptionID);
        $("loading").classList.add("hidden");
        $("error").className = "hidden";
        $("back").href = result.deepLink;
        $("done").classList.remove("hidden");
        // open the app straight away; the button stays for browsers that ask first
        location.href = result.deepLink;
      } catch (e) {
        $("pay").classList.remove("hidden");
        fail(e);
      }
    },
    onCancel: () => show($("error"), message({ code: "cancelled_by_user" }), "warn"),
    onError: (err) => {
      const text = String(err?.message ?? err);
      fail(/INSTRUMENT_DECLINED|declined/i.test(text) ? { code: "payment_declined" } : err instanceof ApiError ? err : { code: "paypal_unavailable" });
    },
  });
  await buttons.render("#paypal-buttons");
}

document.querySelectorAll(".toggle button").forEach((b) =>
  b.addEventListener("click", () => {
    interval = b.dataset.interval;
    renderSummary();
  }),
);

if (!token) {
  fail({ code: "unauthorized" });
} else {
  try {
    [pricing, config] = await Promise.all([api("/api/pricing"), api("/api/client-config")]);
    if (!pricing.tiers.pro.prices.monthly.paypalPlanId) throw new ApiError("plan_not_synced", 503);
    if (!config.paypalClientId) throw new ApiError("paypal_unavailable", 503);
    $("mode-badge").textContent = config.mode === "sandbox" ? "Sandbox (בדיקות)" : "";
    $("mode-badge").classList.toggle("hidden", config.mode !== "sandbox");
    renderSummary();
    try {
      window.__paypal = await window.paypalLoadScript({
        clientId: config.paypalClientId,
        vault: true,
        intent: "subscription",
        currency: pricing.currency,
        locale: "he_IL",
        components: "buttons",
      });
    } catch {
      throw new ApiError("paypal_load", 0);
    }
    await renderButtons();
    $("loading").classList.add("hidden");
    $("pay").classList.remove("hidden");
  } catch (e) {
    fail(e);
  }
}
