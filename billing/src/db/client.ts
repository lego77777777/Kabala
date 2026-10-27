/**
 * One Drizzle database for both worlds:
 *   postgres://…        → a real PostgreSQL server (production)
 *   pglite:<dir>        → PGlite, PostgreSQL compiled to WASM, stored in a folder (local development)
 *   pglite:memory       → PGlite in memory (tests)
 * Same schema, same migrations.
 */
import { fileURLToPath } from "node:url";
import path from "node:path";
import { mkdirSync } from "node:fs";
import { PGlite } from "@electric-sql/pglite";
import { drizzle as drizzlePglite } from "drizzle-orm/pglite";
import { migrate as migratePglite } from "drizzle-orm/pglite/migrator";
import { drizzle as drizzlePostgres } from "drizzle-orm/postgres-js";
import { migrate as migratePostgres } from "drizzle-orm/postgres-js/migrator";
import postgres from "postgres";
import * as schema from "./schema.js";

export const MIGRATIONS = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../drizzle");

export type Db = ReturnType<typeof drizzlePglite<typeof schema>>;

/** How the job queue (pg-boss) reaches the same database. */
export type QueueConnection = { connectionString: string } | { db: { executeSql(text: string, values?: unknown[]): Promise<{ rows: unknown[] }> } };

export interface Database {
  db: Db;
  queue: QueueConnection;
  migrate(): Promise<void>;
  close(): Promise<void>;
}

/**
 * TLS for a real PostgreSQL server: required in production unless the URL itself says otherwise
 * (`?sslmode=disable` for a database on the same private host), so emails, PayPal ids and receipts never
 * cross a network in clear.
 */
export function postgresSsl(url: string, production: boolean): "require" | false | undefined {
  const mode = /[?&]sslmode=([a-z-]+)/i.exec(url)?.[1]?.toLowerCase();
  if (mode === "disable") return false;
  if (mode) return undefined; // the URL's own sslmode (require, verify-full…) is used as given
  return production ? "require" : undefined;
}

export function openDatabase(url: string, production = false): Database {
  if (url.startsWith("pglite:")) {
    const where = url.slice("pglite:".length);
    if (where !== "memory") mkdirSync(where, { recursive: true });
    const client = where === "memory" ? new PGlite() : new PGlite(where);
    const db = drizzlePglite(client, { schema });
    return {
      db,
      queue: {
        db: {
          // pg-boss sends multi-statement scripts without parameters; PGlite runs those with exec(),
          // and pg-boss needs the rows of every statement, not only the last one.
          async executeSql(text, values) {
            if (values?.length) return { rows: (await client.query(text, values)).rows };
            return { rows: (await client.exec(text)).flatMap((r) => r.rows) };
          },
        },
      },
      migrate: () => migratePglite(db, { migrationsFolder: MIGRATIONS }),
      close: () => client.close(),
    };
  }
  const ssl = postgresSsl(url, production);
  const client = postgres(url, { max: 10, ...(ssl === undefined ? {} : { ssl }) });
  const db = drizzlePostgres(client, { schema }) as unknown as Db;
  return {
    db,
    // pg-boss reads sslmode from the URL: give it the same TLS requirement as the main pool
    queue: { connectionString: ssl === "require" ? `${url}${url.includes("?") ? "&" : "?"}sslmode=require` : url },
    migrate: () => migratePostgres(db as never, { migrationsFolder: MIGRATIONS }),
    close: () => client.end(),
  };
}
