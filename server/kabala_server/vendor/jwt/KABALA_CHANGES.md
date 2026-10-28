# PyJWT 2.15.1 in Kabala

Source: https://github.com/jpadilla/pyjwt (MIT, see `LICENSE` and `AUTHORS.rst`), copied from the PyPI wheel.
The first commit that added this folder is the original; changes since are marked `# Kabala:`.

| Change | Why |
|---|---|
| `"none"` removed from the algorithm registry (`algorithms.py`) | An unsigned token can never be accepted, even by code that forgets `algorithms=` (the classic alg=none attack) |
| `jwks_client.py` and `jwk_set_cache.py` removed, and the `PyJWKClient` export dropped | That code downloads keys over the network; Kabala's keys are configured, never fetched, so it is attack surface with no use |

Used by `kabala_server/admin_billing.py`: the admin panel signs every request to the PayPal billing server with an Ed25519 key (`EdDSA`), and the billing server verifies it with `jose` and only the public key.
