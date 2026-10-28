# werkzeug.security 3.1.3 in Kabala

Source: https://github.com/pallets/werkzeug (BSD-3-Clause, see `LICENSE`), the password hashing module of Werkzeug, the library under Flask. Copied from `src/werkzeug/security.py` at tag 3.1.3.
The first commit that added this folder is the original; changes since are marked `# Kabala:`.

It hashes admin panel passwords with scrypt (memory-hard, from Python's `hashlib`): `scrypt:32768:8:1$<salt>$<hash>`, a random 16-character salt per password, and a constant-time comparison.

| Change | Why |
|---|---|
| `safe_join` (and its `os` / `posixpath` imports) removed | Not used: only password hashing is needed |
| `check_password_hash` accepts only `scrypt` with bounded parameters (n 2^14 to 2^17, r ≤ 16, p ≤ 4), and returns `False` for anything else instead of raising | A damaged hash, a weak method (`pbkdf2` with 1 iteration) or a huge scrypt cost in the database is "no match", never an error page or a memory blowup |
| `__init__.py` exports the two functions | Imported as `kabala_server.vendor.werkzeug_security` |

Used through `kabala_server/passwords.py`.
