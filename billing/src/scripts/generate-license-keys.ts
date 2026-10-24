/**
 * npm run keys:generate — prints a new Ed25519 key pair for license tokens.
 * Private key → server .env only. Public key → server .env AND bundled with the desktop app.
 */
import { TokenService } from "../services/tokens.js";

const keys = await TokenService.generate();
const oneLine = (pem: string) => pem.trim().replace(/\n/g, "\\n");
console.log("# add to billing/.env (keep the private key secret)");
console.log(`LICENSE_PRIVATE_KEY="${oneLine(keys.privateKeyPem)}"`);
console.log(`LICENSE_PUBLIC_KEY="${oneLine(keys.publicKeyPem)}"`);
console.log("\n# desktop app: save the public key as kabala-py/license_public_key.pem");
console.log(keys.publicKeyPem.trim());
