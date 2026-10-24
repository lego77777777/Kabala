/**
 * A JobQueue that runs each job immediately, retrying in-process with no delay. For tests and quick
 * local scripts: same retry semantics as pg-boss (attempt count, last-attempt flag), no waiting.
 */
import type { JobInfo, JobName, JobQueue } from "./queue.js";

export class InlineQueue implements JobQueue {
  readonly sent: { name: JobName; data: object }[] = [];
  private readonly handlers = new Map<JobName, (data: Record<string, unknown>, info: JobInfo) => Promise<void>>();
  /** set false to only record jobs (to run them later with drain()) */
  autoRun = true;
  private backlog: { name: JobName; data: object }[] = [];

  constructor(private readonly attempts = 5) {}

  async send(name: JobName, data: object): Promise<void> {
    this.sent.push({ name, data });
    if (this.autoRun) await this.run(name, data);
    else this.backlog.push({ name, data });
  }

  async work(name: JobName, handler: (data: Record<string, unknown>, info: JobInfo) => Promise<void>): Promise<void> {
    this.handlers.set(name, handler);
  }

  async schedule(): Promise<void> {}

  async stop(): Promise<void> {}

  /** runs jobs recorded while autoRun was false */
  async drain(): Promise<void> {
    while (this.backlog.length) {
      const job = this.backlog.shift()!;
      await this.run(job.name, job.data);
    }
  }

  private async run(name: JobName, data: object): Promise<void> {
    const handler = this.handlers.get(name);
    if (!handler) return;
    for (let attempt = 1; attempt <= this.attempts; attempt++) {
      try {
        await handler(data as Record<string, unknown>, { attempt, isLastAttempt: attempt === this.attempts });
        return;
      } catch {
        // retried, like pg-boss; the handler already recorded the error
      }
    }
  }
}
