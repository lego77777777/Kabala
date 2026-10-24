/**
 * Running receipt numbers with no gaps and no duplicates.
 *
 * One statement takes the number: the upsert locks the kind's row until the surrounding transaction
 * ends, so concurrent issuers wait their turn; if the transaction rolls back, the increment rolls back
 * with it and the number is handed out again. Call it only inside the transaction that inserts the
 * receipt, after every check that could still abort it.
 */
import { sql } from "drizzle-orm";
import type { Db } from "../db/client.js";

export type ReceiptKind = "receipt" | "refund";
type Tx = Parameters<Parameters<Db["transaction"]>[0]>[0];

export async function takeNumber(tx: Tx, kind: ReceiptKind): Promise<number> {
  const result = await tx.execute<{ number: number }>(sql`
    INSERT INTO receipt_sequences (kind, next_number) VALUES (${kind}, 2)
    ON CONFLICT (kind) DO UPDATE SET next_number = receipt_sequences.next_number + 1
    RETURNING next_number - 1 AS number`);
  const rows = (result as unknown as { rows?: { number: number }[] }).rows ?? (result as unknown as { number: number }[]);
  return Number(rows[0]!.number);
}

/** "2026-000123"-style label shown on documents; the stored number stays a plain integer. */
export function formatNumber(n: number): string {
  return String(n).padStart(6, "0");
}
