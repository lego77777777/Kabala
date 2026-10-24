import { api, features, message, money, show } from "/common.js";

const $ = (id) => document.getElementById(id);
let pricing = null;
let interval = "monthly";

function render() {
  const pro = pricing.tiers.pro;
  const q = pro.prices[interval];
  $("free-name").textContent = pricing.tiers.free.name;
  $("free-price").textContent = money(0, pricing.currency);
  $("free-features").replaceChildren(...features(pricing.tiers.free.limits).map((t) => Object.assign(document.createElement("li"), { textContent: t })));
  $("pro-name").textContent = pro.name;
  $("pro-price").textContent = money(q.amount, q.currency);
  $("pro-per").textContent = interval === "monthly" ? "/ חודש" : "/ שנה";
  $("pro-note").textContent =
    interval === "yearly"
      ? `${money(q.monthlyEquivalentMinor / 100, q.currency)} לחודש · חוסכים ${money(q.savingsMinor / 100, q.currency)} בשנה`
      : `או ${money(pro.prices.yearly.amount, q.currency)} לשנה (חיסכון של ${pro.prices.yearly.savingsPercent}%)`;
  $("pro-features").replaceChildren(...features(pro.limits).map((t) => Object.assign(document.createElement("li"), { textContent: t })));
  $("save-badge").textContent = `חיסכון ${pro.prices.yearly.savingsPercent}%`;
  document.querySelectorAll(".toggle button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.interval === interval)));
}

document.querySelectorAll(".toggle button").forEach((b) =>
  b.addEventListener("click", () => {
    interval = b.dataset.interval;
    render();
  }),
);

try {
  pricing = await api("/api/pricing");
  render();
  $("plans").classList.remove("hidden");
} catch (e) {
  show($("error"), message(e));
} finally {
  $("loading").classList.add("hidden");
}
