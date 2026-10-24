/**
 * Subscription state machine and entitlement rules. Pure functions: no DB, no PayPal.
 *
 *   pending ──ACTIVATED/verified──▶ active | trial
 *   active  ──PAYMENT.FAILED / SUSPENDED──▶ past_due (grace = 7 days from the first failure)
 *   past_due ──PAYMENT.SALE.COMPLETED──▶ active
 *   any     ──CANCELLED──▶ cancelled (Pro until the end of the paid period)
 *   any     ──EXPIRED──▶ expired
 *   past_due past grace / cancelled past period end ──reconcile──▶ expired
 */
import type { PayPalSubscriptionStatus } from "../paypal/gateway.js";

export type SubStatus = "pending" | "trial" | "active" | "past_due" | "cancelled" | "expired";

export interface SubState {
  status: SubStatus;
  currentPeriodEnd: Date | null;
  graceUntil: Date | null;
  cancelledAt: Date | null;
}

export type SubEvent =
  | { type: "activated"; inTrial: boolean; nextBillingTime: Date | null }
  | { type: "payment_completed"; nextBillingTime: Date | null }
  | { type: "payment_failed" }
  | { type: "suspended" }
  | { type: "cancelled" }
  | { type: "expired" }
  | { type: "refunded_in_full" };

const DAY = 86_400_000;

export function transition(state: SubState, event: SubEvent, now: Date, graceDays: number): SubState {
  const next: SubState = { ...state };
  switch (event.type) {
    case "activated":
      if (state.status === "cancelled" || state.status === "expired") return state; // late/out-of-order delivery
      next.status = event.inTrial ? "trial" : "active";
      next.currentPeriodEnd = event.nextBillingTime ?? state.currentPeriodEnd;
      next.graceUntil = null;
      return next;
    case "payment_completed":
      if (state.status === "expired") return state;
      if (state.status !== "cancelled") next.status = "active";
      next.currentPeriodEnd = event.nextBillingTime ?? state.currentPeriodEnd;
      next.graceUntil = null;
      return next;
    case "payment_failed":
    case "suspended":
      if (state.status === "cancelled" || state.status === "expired" || state.status === "pending") return state;
      next.status = "past_due";
      // the grace window starts at the FIRST failure and is never extended by later ones
      next.graceUntil = state.graceUntil ?? new Date(now.getTime() + graceDays * DAY);
      return next;
    case "cancelled":
      if (state.status === "expired") return state;
      next.status = "cancelled";
      next.cancelledAt = state.cancelledAt ?? now;
      // a past_due subscription that gets cancelled keeps only what's left of its grace
      if (state.status === "past_due" && state.graceUntil && (!state.currentPeriodEnd || state.graceUntil < state.currentPeriodEnd)) {
        next.currentPeriodEnd = state.graceUntil;
      }
      return next;
    case "expired":
    case "refunded_in_full":
      next.status = "expired";
      next.graceUntil = null;
      return next;
  }
}

/** Does this subscription grant Pro right now? */
export function isEntitled(state: SubState, now: Date): boolean {
  switch (state.status) {
    case "active":
    case "trial":
      return true;
    case "past_due":
      return state.graceUntil !== null && now < state.graceUntil;
    case "cancelled":
      return state.currentPeriodEnd !== null && now < state.currentPeriodEnd;
    default:
      return false;
  }
}

/** Time-based housekeeping (run by reconcile and before answering license requests). */
export function expireIfDue(state: SubState, now: Date): SubState {
  if (state.status === "past_due" && state.graceUntil && now >= state.graceUntil) return { ...state, status: "expired" };
  if (state.status === "cancelled" && (!state.currentPeriodEnd || now >= state.currentPeriodEnd)) return { ...state, status: "expired" };
  return state;
}

/** Maps PayPal's own status onto ours (used when verifying a checkout and by reconcile). */
export function fromPayPal(status: PayPalSubscriptionStatus, inTrial: boolean): SubEvent | null {
  switch (status) {
    case "ACTIVE":
      return { type: "activated", inTrial, nextBillingTime: null };
    case "SUSPENDED":
      return { type: "suspended" };
    case "CANCELLED":
      return { type: "cancelled" };
    case "EXPIRED":
      return { type: "expired" };
    default:
      return null; // APPROVAL_PENDING / APPROVED: nothing paid yet
  }
}
