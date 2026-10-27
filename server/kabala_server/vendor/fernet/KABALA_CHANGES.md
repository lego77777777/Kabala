# Fernet (pyca/cryptography 50.0.2) in Kabala

Source: https://github.com/pyca/cryptography, file `src/cryptography/fernet.py`, dual licensed Apache-2.0 OR BSD-3-Clause (`LICENSE*`).
Fernet is authenticated encryption: AES-128-CBC + HMAC-SHA256, a random IV per message and a timestamp. A token can't be read or changed without the key.
The first commit that added this folder is the original; changes since are marked `# Kabala:`.

| Change | Why |
|---|---|
| `_check_bytes` defined here instead of `cryptography.utils._check_bytes` | That is a private helper of the library and may change or disappear between releases |
| `__init__.py` re-exports `Fernet`, `MultiFernet`, `InvalidToken` | Package layout under `kabala_server.vendor` |

The AES/HMAC primitives still come from the `cryptography` package (they are compiled code and must not be re-implemented).
How Kabala uses it is in `kabala_server/crypto.py`: one key per purpose derived from `KABALA_SECRET` with HKDF, and `MultiFernet` so `KABALA_OLD_SECRETS` keeps old data readable after a key rotation.
