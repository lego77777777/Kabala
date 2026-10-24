/**
 * The desktop app proves who the user is with a Google ID token (it already signs in to Google
 * for Gmail with the `openid email` scopes). We check Google's signature, issuer, audience and expiry.
 */
import { createRemoteJWKSet, jwtVerify } from "jose";

export type GoogleVerifier = (idToken: string) => Promise<{ email: string }>;

export function googleVerifier(clientId: string): GoogleVerifier {
  const jwks = createRemoteJWKSet(new URL("https://www.googleapis.com/oauth2/v3/certs"));
  return async (idToken) => {
    const { payload } = await jwtVerify(idToken, jwks, {
      issuer: ["https://accounts.google.com", "accounts.google.com"],
      audience: clientId,
    });
    if (typeof payload.email !== "string" || payload.email_verified !== true) throw new Error("email not verified by Google");
    return { email: payload.email };
  };
}
