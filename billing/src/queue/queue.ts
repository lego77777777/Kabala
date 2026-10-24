/**
 * Background jobs on pg-boss (MIT): the queue lives in the same PostgreSQL as everything else, so no
 * Redis to run. Every job retries automatically with exponential backoff; `onFinalFailure` runs once
 * when the last attempt fails (the pipeline marks the receipt failed and alerts the admin).
 */
import { PgBoss } from "pg-boss";
import type { QueueConnection } from "../db/client.js";

export const JOBS = {
  generate: "receipt-generate",
  email: "receipt-email",
  sweep: "receipt-sweep",
} as const;
export type JobName = (typeof JOBS)[keyof typeof JOBS];

export interface RetryPolicy {
  /** total attempts, including the first (spec: 5) */
  attempts: number;
  /** delay before the first retry; doubles each time (backoff) */
  delaySeconds: number;
}

export interface JobInfo {
  attempt: number;
  isLastAttempt: boolean;
}

export interface JobQueue {
  send(name: JobName, data: object): Promise<void>;
  work(name: JobName, handler: (data: Record<string, unknown>, info: JobInfo) => Promise<void>): Promise<void>;
  /** runs `name` on a cron schedule (UTC) */
  schedule(name: JobName, cron: string): Promise<void>;
  stop(): Promise<void>;
}

export async function startQueue(
  connection: QueueConnection,
  retry: RetryPolicy,
  opts: { pollingIntervalSeconds?: number; onError?: (e: Error) => void; schedules?: boolean } = {},
): Promise<JobQueue> {
  const boss = new PgBoss({ ...connection, schema: "pgboss", schedule: opts.schedules ?? true, supervise: true });
  boss.on("error", (e: Error) => opts.onError?.(e));
  await boss.start();
  const retryLimit = Math.max(0, retry.attempts - 1);
  for (const name of Object.values(JOBS)) {
    await boss.createQueue(name, { retryLimit, retryDelay: retry.delaySeconds, retryBackoff: true, expireInSeconds: 300 });
  }
  const polling = opts.pollingIntervalSeconds ?? 2;

  return {
    async send(name, data) {
      await boss.send(name, data);
    },
    async work(name, handler) {
      await boss.work(name, { pollingIntervalSeconds: polling, includeMetadata: true }, async (jobs) => {
        for (const job of jobs) {
          const retryCount = Number((job as { retryCount?: number }).retryCount ?? 0);
          await handler(job.data as Record<string, unknown>, { attempt: retryCount + 1, isLastAttempt: retryCount >= retryLimit });
        }
      });
    },
    async schedule(name, cron) {
      await boss.schedule(name, cron);
    },
    async stop() {
      await boss.stop({ graceful: true, timeout: 10_000 });
    },
  };
}
