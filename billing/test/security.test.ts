import { describe, expect, it } from "vitest";
import { redactUrl } from "../src/app.js";
import { postgresSsl } from "../src/db/client.js";

describe("encryption in transit and secrets in logs", () => {
  it("requires TLS to PostgreSQL in production unless the URL says otherwise", () => {
    expect(postgresSsl("postgres://u:p@db.internal/kabala", true)).toBe("require");
    expect(postgresSsl("postgres://u:p@db.internal/kabala", false)).toBeUndefined();
    expect(postgresSsl("postgres://u:p@localhost/kabala?sslmode=disable", true)).toBe(false);
    expect(postgresSsl("postgres://u:p@db/kabala?sslmode=verify-full", true)).toBeUndefined();
  });

  it("never logs the signature of a receipt link", () => {
    const url = "/r/0b9e1c3a-1111-4222-8333-944445555666/1893456000/AbCdEfGhIjKlMnOpQrStUvWx.pdf";
    expect(redactUrl(url)).toBe("/r/0b9e1c3a-1111-4222-8333-944445555666/1893456000/[redacted]");
    expect(redactUrl("/api/pricing?currency=ILS")).toBe("/api/pricing?currency=ILS");
  });
});
