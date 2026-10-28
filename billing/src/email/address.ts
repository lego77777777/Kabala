/**
 * Email address check for addresses people type (the admin panel's "send receipt to…", the accountant copy on
 * the account page), with isEmail from validator.js (src/vendor/validator).
 *
 * Stricter than validator's defaults on purpose:
 * - no display names ("Dana <d@x.com>") and no IP-address domains: a plain mailbox only;
 * - no `,` `;` `<` `>` `"` or line breaks: receipts store their recipients comma-joined
 *   (email_logs.to_addresses), so one of those inside an address could smuggle in another recipient or a
 *   mail header;
 * - ASCII local part only: what Gmail, Outlook and Israeli providers actually accept, and no look-alike letters.
 */
import isEmail from "../vendor/validator/isEmail.js";

export function isMailbox(input: string): boolean {
  const s = input.trim();
  if (!s || /[\r\n]/.test(s)) return false;
  return isEmail(s, {
    allow_display_name: false,
    allow_ip_domain: false,
    require_tld: true,
    allow_utf8_local_part: false,
    blacklisted_chars: ',;<>"',
  });
}

export const normalizeMailbox = (input: string) => input.trim().toLowerCase();
