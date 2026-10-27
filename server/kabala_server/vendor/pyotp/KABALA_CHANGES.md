# pyotp 2.10.0 in Kabala

Source: https://github.com/pyauth/pyotp (MIT, see `LICENSE`), copied from the PyPI wheel. **Used unchanged**: the RFC 4226 code generation, the secure random secrets and the constant-time comparison are exactly what email sign-in needs.

The Kabala-specific parts live in `kabala_server/email_login.py`, outside this folder:
- each sign-in challenge gets a fresh `random_base32()` secret, and the code is `HOTP(secret).at(counter)`;
- a resend moves the counter, so only the latest code works;
- codes typed as `123 456`, `123-456` or with full-width digits are accepted;
- tries, expiry and sends per address are enforced in the database (`db.py`).
