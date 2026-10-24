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

export function openDatabase(url: string): Database {
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
  const client = postgres(url, { max: 10 });
  const db = drizzlePostgres(client, { schema }) as unknown as Db;
  return {
    db,
    queue: { connectionString: url },
    migrate: () => migratePostgres(db as never, { migrationsFolder: MIGRATIONS }),
    close: () => client.end(),
  };
}
