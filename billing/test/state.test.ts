import { describe, expect, it } from "vitest";
import { expireIfDue, isEntitled, transition, type SubState } from "../src/services/subscription-state.js";

const DAY = 86_400_000;
const t0 = new Date("2026-10-21T10:00:00Z");
const at = (days: number) => new Date(t0.getTime() + days * DAY);
const fresh: SubState = { status: "pending", currentPeriodEnd: null, graceUntil: null, cancelledAt: null };

describe("subscription state machine", () => {
  it("pending → active only by activation, and Pro follows", () => {
    expect(isEntitled(fresh, t0)).toBe(false);
    const s = transition(fresh, { type: "activated", inTrial: false, nextBillingTime: at(30) }, t0, 7);
    expect(s.status).toBe("active");
    expect(isEntitled(s, t0)).toBe(true);
  });

  it("a failed payment gives exactly 7 days of grace, not extended by more failures", () => {
    const active = transition(fresh, { type: "activated", inTrial: false, nextBillingTime: at(30) }, t0, 7);
    const pd = transition(active, { type: "payment_failed" }, at(30), 7);
    expect(pd.status).toBe("past_due");
    expect(pd.graceUntil).toEqual(at(37));
    const again = transition(pd, { type: "payment_failed" }, at(33), 7);
    expect(again.graceUntil).toEqual(at(37));
    expect(isEntitled(again, at(36))).toBe(true);
    expect(isEntitled(again, at(37))).toBe(false);
    expect(expireIfDue(again, at(37)).status).toBe("expired");
  });

  it("payment after a failure restores active and clears grace", () => {
    const pd: SubState = { status: "past_due", currentPeriodEnd: at(0), graceUntil: at(7), cancelledAt: null };
    const s = transition(pd, { type: "payment_completed", nextBillingTime: at(30) }, at(2), 7);
    expect(s).toMatchObject({ status: "active", graceUntil: null, currentPeriodEnd: at(30) });
  });

  it("cancel keeps Pro until the paid period ends", () => {
    const active: SubState = { status: "active", currentPeriodEnd: at(20), graceUntil: null, cancelledAt: null };
    const c = transition(active, { type: "cancelled" }, t0, 7);
    expect(c.status).toBe("cancelled");
    expect(isEntitled(c, at(19))).toBe(true);
    expect(isEntitled(c, at(20))).toBe(false);
    expect(expireIfDue(c, at(21)).status).toBe("expired");
  });

  it("late/out-of-order events never revive a finished subscription", () => {
    const expired: SubState = { status: "expired", currentPeriodEnd: null, graceUntil: null, cancelledAt: null };
    expect(transition(expired, { type: "activated", inTrial: false, nextBillingTime: at(30) }, t0, 7).status).toBe("expired");
    expect(transition(expired, { type: "payment_completed", nextBillingTime: at(30) }, t0, 7).status).toBe("expired");
    const cancelled: SubState = { status: "cancelled", currentPeriodEnd: at(5), graceUntil: null, cancelledAt: t0 };
    expect(transition(cancelled, { type: "payment_failed" }, t0, 7).status).toBe("cancelled");
  });

  it("full refund ends access", () => {
    const active: SubState = { status: "active", currentPeriodEnd: at(20), graceUntil: null, cancelledAt: null };
    const r = transition(active, { type: "refunded_in_full" }, t0, 7);
    expect(r.status).toBe("expired");
    expect(isEntitled(r, t0)).toBe(false);
  });
});
